"""
engines/reporting.py

Reporting Engine (Module 18) -- SRS Part 15 PROFESSIONAL REPORTING ENGINE.

SCOPE OF THIS PASS -- read this before extending the module:

    IN SCOPE, fully implemented and tested:
        * Daily / Weekly / Monthly report generation: objective trade and
          signal statistics for a caller-supplied period (win rate, P&L,
          best/worst trade, signal count), persisted via `ReportRepository`.
        * Turkish `turkish_analysis` / `turkish_recommendations` text,
          templated directly from those computed numbers.
        * JSON export (`ReportConfig.export_json`, on by default).

    DELIBERATELY NOT IMPLEMENTED HERE -- flagged rather than guessed at:
        * `FilterPerformance` scoring (contribution/reliability/success-
          rate scores). That is SRS Part 14, a distinct, separately-
          documented formula ("Performance Score, Reliability Score,
          Contribution Score, Historical Accuracy") this codebase gives
          no concrete formula for anywhere Module 18 can see. Inventing
          one would be guessing at a number that looks like it means
          something. `FilterPerformanceRepository` already exists
          (infrastructure/database/repositories/report_repository.py)
          for whichever engine ends up owning that computation --
          plausibly the Fast Filter engine evaluating itself, not this
          one evaluating overall trading performance.
        * CSV / Excel / PDF export (`ReportConfig.export_csv/excel/pdf`).
          Excel and PDF need new dependencies (e.g. openpyxl, reportlab)
          not currently in requirements.txt; adding a dependency is a
          decision this module does not make unilaterally. CSV needs no
          new dependency (stdlib `csv`) and is a natural next addition.
        * `ReportType.QUARTERLY` exists on the enum but has no
          `TelegramConfig.send_quarterly_reports` flag and is not part
          of PROJECT_STATUS.md's Module 18 description -- no
          `generate_quarterly_report()` method here yet.
        * Calendar/timezone arithmetic ("what is 'today'", "what is
          'this week'"): callers supply `period_start`/`period_end`
          explicitly rather than this engine guessing at day/week
          boundaries or a timezone. That belongs to, and is handled by,
          `execution_modes/live.py`'s `LiveRunner` (its
          `_run_scheduled_tasks()` computes each period via
          `_is_due_daily/_weekly/_monthly()` and `_run_daily_report()` /
          `_run_weekly_report()` / `_run_monthly_report()`, which call
          `generate_daily_report()` / `generate_weekly_report()` /
          `generate_monthly_report()` here) -- correcting this
          docstring's earlier claim that no such caller existed yet.

Clean Architecture: this engine owns the business logic (statistics,
templated Turkish text, grading); it consumes `TradeRepository` /
`SignalRepository` / `ReportRepository` via constructor injection and
never touches SQL directly.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Optional

from config.loader import get_config
from config.schema import PlatformConfig
from core.models import Report, ReportType, Signal, Trade, TradeStatus
from infrastructure.database.repositories.report_repository import ReportRepository
from infrastructure.database.repositories.signal_repository import SignalRepository
from infrastructure.database.repositories.trade_repository import TradeRepository
from system.logging_setup import get_logger

_logger = get_logger("system")

# Simple, transparent, easily-adjusted heuristic thresholds -- NOT a
# documented SRS formula (none was available; see module docstring).
_GRADE_THRESHOLDS: tuple[tuple[float, str], ...] = ((70.0, "A"), (55.0, "B"), (40.0, "C"))
_DEFAULT_GRADE = "D"


class ReportingEngine:
    """
    Consumes repositories via dependency injection (never constructs its
    own `Database`). One `generate_*_report()` method per `ReportType`
    currently wired to a `TelegramConfig.send_*_reports` flag, sharing a
    single private computation path (`_generate`) to avoid duplicating
    the statistics/formatting logic three times.
    """

    def __init__(
        self,
        trade_repository: TradeRepository,
        signal_repository: SignalRepository,
        report_repository: ReportRepository,
        config: Optional[PlatformConfig] = None,
    ) -> None:
        self._trades = trade_repository
        self._signals = signal_repository
        self._reports = report_repository
        self._config = config or get_config()

    async def generate_daily_report(self, *, period_start: datetime, period_end: datetime) -> Report:
        return await self._generate(ReportType.DAILY, period_start, period_end)

    async def generate_weekly_report(self, *, period_start: datetime, period_end: datetime) -> Report:
        return await self._generate(ReportType.WEEKLY, period_start, period_end)

    async def generate_monthly_report(self, *, period_start: datetime, period_end: datetime) -> Report:
        return await self._generate(ReportType.MONTHLY, period_start, period_end)

    async def _generate(self, report_type: ReportType, period_start: datetime, period_end: datetime) -> Report:
        if period_end <= period_start:
            raise ValueError(f"period_end ({period_end}) must be after period_start ({period_start})")

        trades = self._trades.get_closed_trades_between(period_start, period_end)
        signals = self._signals.get_signals_between(period_start, period_end)
        earliest_signal_at = self._signals.get_earliest_signal_time()

        content = self._build_content(trades, signals)
        # Data-coverage caveat (added at the platform owner's explicit
        # request, after a real incident it described: hopping to a new
        # Railway account/database when a free-tier trial runs out starts
        # this database from empty, so a "weekly" report generated a few
        # days after that switch silently covers only those few days, not
        # the nominal 7 -- undercounting real activity as if it were a
        # quiet week. `None` when this database's earliest signal already
        # predates `period_start` (the normal case: nothing was lost).
        if earliest_signal_at is not None and earliest_signal_at > period_start:
            content["data_covers_from"] = earliest_signal_at.isoformat()
        else:
            content["data_covers_from"] = None

        report = Report(
            report_type=report_type,
            period_start=period_start,
            period_end=period_end,
            content=content,
            turkish_analysis=self._build_analysis(content),
            turkish_recommendations=self._build_recommendations(content),
            overall_grade=self._grade(content),
        )
        saved = self._reports.create(report)
        _logger.info(
            "Generated %s report for %s..%s: %d trades closed, %d signals",
            report_type.value, period_start.isoformat(), period_end.isoformat(),
            content["trades_closed"], content["signals_generated"],
        )
        return saved

    # ─────────────────────────────────────────────────────────────────
    # STATISTICS  (pure counting/averaging over verified Trade/Signal fields --
    # no invented scoring)
    # ─────────────────────────────────────────────────────────────────

    def _build_content(self, trades: list[Trade], signals: list[Signal]) -> dict[str, Any]:
        # TRAILING_STOP_EXIT (profit-lock early exit) is classified by its
        # ACTUAL realized PnL sign, not lumped in with either bucket
        # blindly -- see PositionMonitorEngine._close_trade()'s matching
        # classification, which this mirrors exactly so the report's
        # win/loss counts always agree with what coin_statistics recorded.
        wins = [
            t for t in trades
            if t.status == TradeStatus.TP1_HIT
            or (t.status == TradeStatus.TRAILING_STOP_EXIT and (t.realized_pnl_percent or 0) >= 0)
        ]
        losses = [
            t for t in trades
            if t.status == TradeStatus.STOP_LOSS
            or (t.status == TradeStatus.TRAILING_STOP_EXIT and (t.realized_pnl_percent or 0) < 0)
        ]
        other = [
            t for t in trades
            if t.status not in (TradeStatus.TP1_HIT, TradeStatus.STOP_LOSS, TradeStatus.TRAILING_STOP_EXIT)
        ]
        pnl_values = [t.realized_pnl_percent for t in trades if t.realized_pnl_percent is not None]

        decided = len(wins) + len(losses)  # excludes "other" (expired/cancelled/error) from the rate
        win_rate = round(len(wins) / decided * 100, 2) if decided else None

        return {
            "signals_generated": len(signals),
            "trades_closed": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "other_closed": len(other),
            "win_rate_percent": win_rate,
            "total_pnl_percent": round(sum(pnl_values), 2) if pnl_values else None,
            "average_pnl_percent": round(sum(pnl_values) / len(pnl_values), 2) if pnl_values else None,
            "best_trade_pnl_percent": round(max(pnl_values), 2) if pnl_values else None,
            "worst_trade_pnl_percent": round(min(pnl_values), 2) if pnl_values else None,
        }

    # ─────────────────────────────────────────────────────────────────
    # TURKISH TEXT  (templated directly from `content` -- see module
    # docstring: not an SRS-specified formula, a transparent description
    # of the numbers above)
    # ─────────────────────────────────────────────────────────────────

    def _build_analysis(self, content: dict[str, Any]) -> str:
        signals = content["signals_generated"]
        closed = content["trades_closed"]
        coverage_note = (
            f"⚠️ Not: bu dönemin verisi {content['data_covers_from'][:10]} tarihinden itibaren mevcut "
            f"(daha eski kayıt yok -- yeni bir veritabanı/dağıtımdan kaynaklanıyor olabilir), yani aşağıdaki "
            f"sayılar dönemin tamamını değil, o tarihten sonrasını kapsıyor. "
        ) if content.get("data_covers_from") else ""
        if closed == 0:
            return f"{coverage_note}Bu dönemde {signals} sinyal üretildi, ancak kapanan işlem olmadı."
        win_rate_clause = (
            f"Kazanma oranı %{content['win_rate_percent']:.2f}, "
            if content["win_rate_percent"] is not None
            else "Kazanma oranı hesaplanamadı (kazanç/zarar yok), "
        )
        return (
            f"{coverage_note}Bu dönemde {signals} sinyal üretildi, {closed} işlem kapandı "
            f"({content['wins']} kazanç, {content['losses']} zarar, {content['other_closed']} diğer). "
            f"{win_rate_clause}"
            f"toplam getiri %{content['total_pnl_percent']:.2f}, "
            f"ortalama getiri %{content['average_pnl_percent']:.2f}."
        )

    def _build_recommendations(self, content: dict[str, Any]) -> str:
        min_trades = self._config.reports.min_trades_for_recommendation
        closed = content["trades_closed"]
        if closed < min_trades:
            return (
                f"Anlamlı bir öneri sunmak için en az {min_trades} kapanmış işlem gerekiyor "
                f"(bu dönemde: {closed}). Bu dönem için öneri atlandı."
            )
        win_rate = content["win_rate_percent"] or 0.0
        if win_rate >= 60:
            return "Kazanma oranı güçlü seviyede; mevcut strateji parametreleri korunabilir."
        if win_rate >= 45:
            return "Kazanma oranı kabul edilebilir seviyede; risk ve güven eşikleri gözden geçirilebilir."
        return "Kazanma oranı düşük; giriş kriterleri ve risk yönetimi ayarlarının gözden geçirilmesi önerilir."

    def _grade(self, content: dict[str, Any]) -> Optional[str]:
        win_rate = content.get("win_rate_percent")
        if win_rate is None:
            return None
        for threshold, grade in _GRADE_THRESHOLDS:
            if win_rate >= threshold:
                return grade
        return _DEFAULT_GRADE

    # ─────────────────────────────────────────────────────────────────
    # EXPORT
    # ─────────────────────────────────────────────────────────────────

    def export_json(self, report: Report) -> str:
        """
        SRS Part 19 `export_json` (on by default). Returns the JSON text
        rather than writing a file -- this engine has no opinion on
        where exports belong on disk; the caller decides. `ensure_ascii=
        False` keeps Turkish characters (ı, ş, ğ, ç, ö, ü) as literal
        UTF-8 instead of escaped \\uXXXX sequences, since a human is the
        actual audience for an exported report.
        """
        payload = {
            "report_type": report.report_type.value,
            "period_start": report.period_start.isoformat(),
            "period_end": report.period_end.isoformat(),
            "generated_at": report.generated_at.isoformat(),
            "content": report.content,
            "turkish_analysis": report.turkish_analysis,
            "turkish_recommendations": report.turkish_recommendations,
            "overall_grade": report.overall_grade,
        }
        return json.dumps(payload, indent=2, ensure_ascii=False)
