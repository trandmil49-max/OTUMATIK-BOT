"""
engines/telegram_notifications.py

Telegram Notification Engine (Module 15) -- business layer only.

Owns every decision about *whether* and *how* something becomes a
Telegram message: message building, HTML formatting/escaping,
notification-level filtering, per-report enable/disable flags, and
routing a closed trade's final status to the right template. It never
performs HTTP itself -- all delivery is delegated to an injected
`infrastructure.telegram.client.TelegramClient` (SRS Part 20 Clean
Architecture: engines own business logic, infrastructure owns adapters).

Two independent gating mechanisms, deliberately kept separate:
    * Trade/signal/error events are gated by `notification_level`
      ("quiet" / "normal" / "verbose") via `_should_send()`, since none
      of them has its own dedicated on/off switch in `TelegramConfig`.
    * Scheduled reports (Daily/Weekly/Monthly/Health) are gated ONLY by
      their own `send_*_reports` boolean flags. Stacking
      `notification_level` on top of an already-explicit "send this
      report or not" flag would be redundant and confusing, so reports
      never consult `_should_send()`.
    * Critical/Fatal errors bypass both mechanisms and always send --
      `notification_level` controls how chatty routine trading updates
      are, never whether the operator hears about a real failure.

A failed *delivery* (a `TelegramError` that survived the client's own
retries) is logged and swallowed here -- returned to the caller as
`False` -- so a Telegram outage can never cascade into a scan-cycle or
position-monitor failure. A bug in *this module's own formatting code*
is not caught here and propagates normally, since that is a defect in
this layer, not an external-service failure.

Design system (premium redesign, per the platform owner's explicit
instruction -- see PROJECT_STATUS.md history for the "looks like debug
output" complaint this replaced): every template below shares the same
skeleton -- {header icon+title} blank-line {symbol/direction, when
applicable} blank-line {the message's own numeric content, grouped and
blank-line-separated by concern} blank-line {🕒 time} -- so a person
skimming Telegram recognizes the shape of a message before reading a
single number, regardless of which of the 14 notification types it is.
Emoji are used consistently for the same meaning everywhere (🎯=TP,
🛡=stop loss, 💰=entry, 📈/📉=long/short or profit/loss, 🕒=time,
etc.) rather than per-method ad hoc choices.

One-notification-per-event, not per-field: a trade that closes (via TP
hit, stop loss, expiry, or cancellation) gets exactly ONE Telegram
message (`notify_trade_closed()`) -- single-TP model, so a trade's only
lifecycle events are open (`notify_new_signal()`) and close
(`notify_trade_closed()`); there is no intermediate "partial hit" ping.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Callable, Optional, Protocol
from zoneinfo import ZoneInfo

from config.loader import get_config
from config.schema import PlatformConfig
from core.models import (
    BotHealthSnapshot,
    ConfidenceGrade,
    Report,
    ReportType,
    Signal,
    SignalDirection,
    Trade,
    TradeStatus,
)
from infrastructure.binance.models import AccountBalance
from infrastructure.database.repositories.market_repository import BtcStatisticsRepository
from infrastructure.database.repositories.signal_repository import SignalRepository
from infrastructure.telegram.client import TelegramClient
from system.exceptions import PlatformError, Severity, TelegramError
from system.logging_setup import get_logger

_logger = get_logger("telegram")

_LEVEL_ORDER: dict[str, int] = {"quiet": 0, "normal": 1, "verbose": 2}
_DEFAULT_LEVEL = "normal"

_ALERTABLE_ERROR_SEVERITIES: frozenset[Severity] = frozenset({Severity.CRITICAL, Severity.FATAL})
# WARNING/ERROR are "recoverable" (SRS Part 18) -- routed through
# notify_warning() (level-gated, so notification_level="quiet" can
# silence them) rather than notify_error() (which always bypasses the
# level, reserved for CRITICAL/FATAL). INFO stays silent -- too low-value
# for a push notification. See notify_warning()'s own docstring.
_WARNING_SEVERITIES: frozenset[Severity] = frozenset({Severity.WARNING, Severity.ERROR})

# 📈/📉 double as both "long/short" and "profit/loss" direction indicators
# throughout this module's templates -- one consistent meaning ("up-good"
# vs "down-bad"), never redefined per message type.
_DIRECTION_ICON: dict[SignalDirection, str] = {
    SignalDirection.LONG: "📈",
    SignalDirection.SHORT: "📉",
}

_CLOSED_STATUS_ICON: dict[TradeStatus, str] = {
    TradeStatus.TP1_HIT: "✅",
    TradeStatus.STOP_LOSS: "❌",
    TradeStatus.EXPIRED: "⌛",
    TradeStatus.CANCELLED: "🚫",
    TradeStatus.ERROR: "⚠️",
    TradeStatus.TRAILING_STOP_EXIT: "🔒",
}
_CLOSED_STATUS_LABEL_TR: dict[TradeStatus, str] = {
    TradeStatus.TP1_HIT: "TP ile Kapandı (Kâr)",
    TradeStatus.STOP_LOSS: "Stop Loss ile Kapandı (Zarar)",
    TradeStatus.EXPIRED: "Süresi Doldu",
    TradeStatus.CANCELLED: "İptal Edildi",
    TradeStatus.ERROR: "Hata ile Kapandı",
    TradeStatus.TRAILING_STOP_EXIT: "Kâr Kilitlendi (Erken Çıkış)",
}

_REPORT_TITLE_TR: dict[ReportType, str] = {
    ReportType.DAILY: "GÜNLÜK RAPOR",
    ReportType.WEEKLY: "HAFTALIK RAPOR",
    ReportType.MONTHLY: "AYLIK RAPOR",
}


def escape_html(text: str) -> str:
    """
    Escape the characters Telegram's HTML parse mode treats as markup
    (https://core.telegram.org/bots/api#html-style). '&' must be escaped
    first, then '<' and '>', or a double-escape would corrupt an already
    -escaped '&amp;'. Every piece of upstream data (symbols, error
    messages, Turkish analysis text) MUST pass through this before being
    interpolated into a template -- a stray '<' from exchange data could
    otherwise break message rendering or be misread as a tag.
    """
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _fmt_price(value: Optional[float]) -> str:
    """Fixed at up to 8 decimals with trailing zeros trimmed -- avoids float noise across wildly different price scales (BTC vs. a micro-cap)."""
    if value is None:
        return "—"
    text = f"{value:.8f}".rstrip("0").rstrip(".")
    return text if text else "0"


def _fmt_pct(value: Optional[float]) -> str:
    if value is None:
        return "—"
    return f"{value:.2f}%"


def _fmt_signed_pct(value: Optional[float]) -> str:
    """Same as `_fmt_pct()` but always shows a leading sign (+/-) -- PNL should never look ambiguous at a glance."""
    if value is None:
        return "—"
    return f"{value:+.2f}%"


def _pnl_icon(value: Optional[float]) -> str:
    """📈 for a non-negative PNL, 📉 for negative -- same up/down convention `_DIRECTION_ICON` uses."""
    if value is None:
        return "📊"
    return "📈" if value >= 0 else "📉"


def _fmt_score(value: Optional[float]) -> str:
    if value is None:
        return "—"
    return f"{value:.1f}"


class _BalanceSource(Protocol):
    """The one method `_current_balance_line()` needs -- satisfied by either exchange client, so this module never has to import a concrete client class just to type-hint this parameter."""

    async def get_account_balance(self) -> list[AccountBalance]: ...


class TelegramNotificationEngine:
    """
    Consumes a `TelegramClient` (dependency injection -- never
    constructs its own) and exposes one `notify_*` method per
    notification type required by the platform.

    Every `notify_*` method returns `True` if a message was actually
    sent, `False` if it was filtered out or delivery failed. Callers
    (engines/scanner_orchestrator.py, engines/position_monitor.py, the
    future Module 22 composition root) can `await` these without ever
    needing to know Telegram exists in detail -- exactly the seam
    `system/error_handler.py`'s `register_alert_callback()` was
    designed around.
    """

    def __init__(
        self,
        client: TelegramClient,
        config: Optional[PlatformConfig] = None,
        signal_repository: Optional[SignalRepository] = None,
        btc_repository: Optional[BtcStatisticsRepository] = None,
        balance_client: Optional[_BalanceSource] = None,
    ) -> None:
        self._client = client
        self._config = config or get_config()
        # Currently unused by any notify_*() method in this class --
        # kept as accepted constructor parameters (rather than removed)
        # since composition_root.py already wires them and a future
        # message may want them again; harmless to hold if nothing reads
        # them.
        self._signal_repository = signal_repository
        self._btc_repository = btc_repository
        # For the "💼 Bakiye" line only (see _current_balance_line()) --
        # the account that actually holds the trading balance, i.e. the
        # SAME already-open execution-exchange client TradeExecutionEngine
        # uses (composition_root.py wires BingXFuturesClient here, not
        # Binance -- see engines.trade_execution's module docstring for
        # why order execution and market-data live on different
        # exchanges), never a client this engine opens/closes itself.
        # `None` is a fully supported configuration (e.g. signal-only
        # PAPER mode with no trading credentials yet): the balance line
        # is simply omitted, exactly like the BTC Status line above.
        self._balance_client = balance_client

    # ─────────────────────────────────────────────────────────────────
    # SIGNALS & TRADE LIFECYCLE  (gated by notification_level)
    # ─────────────────────────────────────────────────────────────────

    async def _current_balance_line(self) -> Optional[str]:
        """
        "💼 Bakiye: $X.XX" line for `notify_new_signal()` /
        `notify_trade_closed()`'s `_format_closure_reference()`. Returns
        `None` -- silently omitted, never blocking or breaking the main
        notification -- if no `balance_client` was injected, if the
        account has no USDT balance row, or if the fetch itself fails
        for ANY reason (network blip, bad credentials, rate limit): a
        failed balance read must never cost the person the "trade
        opened"/"trade closed" message itself.
        """
        if self._balance_client is None:
            return None
        try:
            balances = await self._balance_client.get_account_balance()
        except Exception:
            _logger.warning("Failed to fetch account balance for a Telegram notification", exc_info=True)
            return None
        usdt_balance = next((b for b in balances if b.asset == "USDT"), None)
        if usdt_balance is None:
            return None
        return f"💼 Bakiye: ${usdt_balance.available_balance:,.2f}"

    async def notify_new_signal(self, signal: Signal) -> bool:
        """
        Short-form signal notification (platform owner's explicit
        request, replacing the earlier long-form "confirmation
        checklist" style): coin, direction, entry/stop/target, leverage,
        confidence, time -- one screen's worth, nothing to scroll past.
        The category-by-category checklist and BTC-macro confirmation
        lines this message used to carry are gone entirely (not just
        hidden) -- they read as clutter, not signal, once the platform
        moved to actually executing trades rather than asking a person
        to manually decide whether to take one.
        """
        if not self._should_send("normal"):
            return False
        direction_label = "LONG" if signal.direction == SignalDirection.LONG else "SHORT"
        direction_icon = "🟢" if signal.direction == SignalDirection.LONG else "🔴"
        lines = [
            f"🚨 <b>YENİ SİNYAL</b> {direction_icon} <b>{direction_label}</b>",
            "",
            f"🪙 Coin: <b>{escape_html(signal.symbol)}</b>",
            f"💰 Giriş: <code>{_fmt_price(signal.entry_price)}</code>",
            f"🛑 Stop: <code>{_fmt_price(signal.stop_loss)}</code>",
            f"🎯 TP: <code>{_fmt_price(signal.take_profit_1)}</code>",
            f"⚡ Kaldıraç: <b>{signal.leverage}x</b>",
            f"⭐ Güven: <b>{signal.confidence_score:.0f}/100</b>",
        ]
        balance_line = await self._current_balance_line()
        if balance_line is not None:
            lines.append(balance_line)
        lines.append("")
        lines.append(f"🕒 {self._fmt_time(signal.created_at)}")
        return await self._send("\n".join(lines))

    async def notify_trade_closed(self, trade: Trade) -> bool:
        """
        Final wrap-up for a trade that has reached a terminal
        `TradeStatus` (business validation: raises if the trade is not
        actually closed -- this method's contract is "the trade is
        done", not "some event happened"). Gated at the "quiet" level: it
        always sends unless notifications are effectively silenced.

        This is the ONLY notification `execution_modes/live.py` sends for
        a trade's terminal transition (TP1_HIT = full close,
        STOP_LOSS, or TRAILING_STOP_EXIT = early profit-lock close) --
        single-TP model, so there is no separate "hit"
        ping to also send; the status-specific icon/label already say
        which outcome closed the trade.

        TP1/stop-loss wording matches the platform owner's exact
        reference format (see `notify_new_signal()`'s docstring).
        EXPIRED/CANCELLED/ERROR have no reference screenshot to match,
        so they keep this module's own prior generic wording rather than
        guessing at a format that was never actually specified.
        """
        if not trade.is_closed:
            raise ValueError(
                f"notify_trade_closed() requires a closed trade; status={trade.status.value!r} is not terminal"
            )
        if not self._should_send("quiet"):
            return False

        if trade.status in (TradeStatus.TP1_HIT, TradeStatus.STOP_LOSS, TradeStatus.TRAILING_STOP_EXIT):
            return await self._send(await self._format_closure_reference(trade))

        icon = _CLOSED_STATUS_ICON.get(trade.status, "⚠️")
        label = _CLOSED_STATUS_LABEL_TR.get(trade.status, trade.status.value)
        pnl_icon = _pnl_icon(trade.realized_pnl_percent)
        lines = [
            f"{icon} <b>İŞLEM KAPANDI</b>",
            f"🎯 Sonuç: <b>{label}</b>",
            "",
            *self._format_trade_core(trade),
            "",
            f"💰 Giriş: <code>{_fmt_price(trade.entry_price)}</code>",
            f"🚪 Çıkış: <code>{_fmt_price(trade.exit_price)}</code>",
            f"{pnl_icon} PNL: <code>{_fmt_signed_pct(trade.realized_pnl_percent)}</code>",
        ]
        if trade.duration_seconds is not None:
            lines.append(f"⏱ Süre: <code>{self._format_duration(trade.duration_seconds)}</code>")
        lines.append("")
        lines.append(f"🕒 {self._fmt_time(trade.exit_time or datetime.now(timezone.utc))}")
        return await self._send("\n".join(lines))

    async def _format_closure_reference(self, trade: Trade) -> str:
        """TP1/real-SL branch of `notify_trade_closed()` -- see that method's docstring. `async` for the balance-line fetch."""
        direction_icon = "🟢" if trade.direction == SignalDirection.LONG else "🔴"
        raw_pct = trade.realized_pnl_percent
        leveraged_pct = raw_pct * trade.leverage if raw_pct is not None else None
        core = [
            f"🪙 Coin: <b>{escape_html(trade.symbol)}</b>",
            f"{direction_icon} Yön: <b>{trade.direction.value}</b>",
            f"💰 Giriş: <code>{_fmt_price(trade.entry_price)}</code>",
        ]

        if trade.status == TradeStatus.TP1_HIT:
            lines = [
                "🎯 <b>TP HIT</b>", "", *core,
                f"🎯 TP Fiyatı: <code>{_fmt_price(trade.exit_price)}</code>",
                f"📈 Kazanç: <b>{_fmt_signed_pct(raw_pct)}</b> (kaldıraçlı ~<b>{_fmt_signed_pct(leveraged_pct)}</b>, {trade.leverage}x)",
                "", "✅ Pozisyon tamamen kapandı.", "",
            ]
        elif trade.status == TradeStatus.TRAILING_STOP_EXIT:
            lines = [
                "🔒 <b>KÂR KİLİTLENDİ</b>", "", *core,
                f"🔒 Çıkış Fiyatı: <code>{_fmt_price(trade.exit_price)}</code>",
                f"📈 Kazanç: <b>{_fmt_signed_pct(raw_pct)}</b> (kaldıraçlı ~<b>{_fmt_signed_pct(leveraged_pct)}</b>, {trade.leverage}x)",
                "", "ℹ️ TP'ye ulaşmadan piyasa tersine döndü, kâr erken kilitlendi.", "",
            ]
        else:
            lines = [
                "🛑 <b>STOP LOSS HIT</b>", "", *core,
                f"🛑 Stop Fiyatı: <code>{_fmt_price(trade.exit_price)}</code>",
                f"📉 Kayıp: <b>{_fmt_signed_pct(raw_pct)}</b> (kaldıraçlı ~<b>{_fmt_signed_pct(leveraged_pct)}</b>, {trade.leverage}x)",
                "", "✅ Pozisyon kapandı.", "",
            ]
        balance_line = await self._current_balance_line()
        if balance_line is not None:
            lines.append(balance_line)
            lines.append("")
        lines.extend(self._format_position_timing(trade))
        return "\n".join(lines)

    async def notify_signal_expired(self, signal: Signal) -> bool:
        """
        A WAITING signal's entry window closed (`RiskConfig
        .signal_lifetime_hours`) without ever seeing fresh price data to
        activate into a trade -- see `engines/position_monitor.py`'s
        `_activate_waiting_signals()`. Takes a `Signal`, not a `Trade`:
        no `Trade` row is ever created for a signal that expires while
        still WAITING, so `notify_trade_closed()`'s signature does not
        fit here even though `TradeStatus.EXPIRED` already has an icon/
        label in `_CLOSED_STATUS_ICON`/`_CLOSED_STATUS_LABEL_TR` above
        (reused below for the same visual meaning). Gated at "normal",
        matching `notify_new_signal()`: whoever was told about the
        signal should be told it is no longer live, at the same
        chattiness level they were told about it in the first place.
        """
        if not self._should_send("normal"):
            return False
        icon = _CLOSED_STATUS_ICON[TradeStatus.EXPIRED]
        direction_icon = _DIRECTION_ICON.get(signal.direction, "➡️")
        lines = [
            f"{icon} <b>SİNYAL SÜRESİ DOLDU</b>",
            "",
            f"🪙 <b>{escape_html(signal.symbol)}</b>",
            f"{direction_icon} Yön: <b>{signal.direction.value}</b>",
            "",
            f"💰 Planlanan Giriş: <code>{_fmt_price(signal.entry_price)}</code>",
            "Fiyat giriş bölgesine ulaşmadı, sinyal artık geçersiz.",
            "",
            f"🕒 {self._fmt_time(datetime.now(timezone.utc))}",
        ]
        return await self._send("\n".join(lines))

    # ─────────────────────────────────────────────────────────────────
    # BOT LIFECYCLE  (gated at "quiet" -- same reasoning as
    # notify_trade_closed(): a one-time startup/shutdown event is
    # operationally important regardless of routine-trading-noise
    # preference, but it is still routed through _should_send() rather
    # than bypassing it outright the way CRITICAL/FATAL errors do, since
    # it is not itself a failure.)
    # ─────────────────────────────────────────────────────────────────

    async def notify_bot_started(
        self, *, version: str, run_mode: str, exchange_ok: bool, telegram_ok: bool, database_ok: bool,
        execution_exchange_ok: Optional[bool] = None,
    ) -> bool:
        if not self._should_send("quiet"):
            return False
        lines = [
            "🚀 <b>BOT BAŞLATILDI</b>",
            "",
            f"🏷 Versiyon: <code>{escape_html(version)}</code>",
            f"⚙️ Çalışma Modu: <code>{escape_html(run_mode)}</code>",
            "",
            f"{'✅' if exchange_ok else '❌'} Borsa Bağlantısı (Binance, analiz): <b>{'Aktif' if exchange_ok else 'Sorunlu'}</b>",
        ]
        # Only shown when autonomous trading is actually switched on --
        # in signal-only PAPER mode (or LIVE with trading_enabled still
        # False), the execution venue's connectivity is irrelevant noise,
        # since TradeExecutionEngine never calls it either way.
        if execution_exchange_ok is not None:
            lines.append(
                f"{'✅' if execution_exchange_ok else '❌'} Borsa Bağlantısı (BingX, işlem): "
                f"<b>{'Aktif' if execution_exchange_ok else 'Sorunlu'}</b>"
            )
        lines.extend([
            f"{'✅' if telegram_ok else '❌'} Telegram: <b>{'Aktif' if telegram_ok else 'Sorunlu'}</b>",
            f"{'✅' if database_ok else '❌'} Veritabanı: <b>{'Aktif' if database_ok else 'Sorunlu'}</b>",
            "",
            f"🕒 {self._fmt_time(datetime.now(timezone.utc))}",
        ])
        return await self._send("\n".join(lines))

    async def notify_bot_stopped(
        self, *, version: str, run_mode: str, uptime_seconds: Optional[float] = None
    ) -> bool:
        """Only sent for a graceful shutdown -- see `application/main.py`'s `_run_live_or_paper()`: a crash is reported through `notify_error()` instead, not this."""
        if not self._should_send("quiet"):
            return False
        lines = [
            "🛑 <b>BOT DURDURULDU</b>",
            "",
            f"🏷 Versiyon: <code>{escape_html(version)}</code>",
            f"⚙️ Çalışma Modu: <code>{escape_html(run_mode)}</code>",
        ]
        if uptime_seconds is not None:
            lines.append(f"⏱ Çalışma Süresi: <code>{self._format_duration(int(uptime_seconds))}</code>")
        lines.append("")
        lines.append(f"🕒 {self._fmt_time(datetime.now(timezone.utc))}")
        return await self._send("\n".join(lines))

    # ─────────────────────────────────────────────────────────────────
    # ERRORS  (severity-gated only -- never suppressed by notification_level)
    # ─────────────────────────────────────────────────────────────────

    async def notify_error(self, error: PlatformError) -> bool:
        """
        SRS Part 18: "Only Critical and Fatal should trigger urgent
        alerts." Deliberately bypasses `_should_send()` -- an operator
        who set `notification_level="quiet"` wanted fewer routine
        trading pings, not silence on a real platform failure.
        """
        if error.severity not in _ALERTABLE_ERROR_SEVERITIES:
            return False
        lines = [
            f"🚨 <b>KRİTİK HATA</b> ({escape_html(error.severity.value)})",
            "",
            f"<code>{escape_html(type(error).__name__)}</code>",
            escape_html(error.message),
        ]
        if error.context:
            context_text = ", ".join(f"{k}={v}" for k, v in error.context.items())
            lines.append(f"Bağlam: <code>{escape_html(context_text)}</code>")
        lines.append("")
        lines.append(f"🕒 {self._fmt_time(datetime.now(timezone.utc))}")
        return await self._send("\n".join(lines))

    async def notify_warning(self, error: PlatformError) -> bool:
        """
        SRS Part 18's WARNING/ERROR band -- "recoverable" failures that
        are worth a Telegram message but must never spam the channel the
        way an alert-on-every-retry would. Unlike `notify_error()`, this
        respects `notification_level`: a "quiet" operator does not get
        routine warnings, only the CRITICAL/FATAL alerts and final trade
        outcomes that always break through. See `as_alert_callback()` for
        how `system.error_handler` routes a given `PlatformError` to this
        method vs. `notify_error()` vs. nowhere (INFO).
        """
        if error.severity not in _WARNING_SEVERITIES:
            return False
        if not self._should_send("normal"):
            return False
        lines = [
            f"⚠️ <b>UYARI</b> ({escape_html(error.severity.value)})",
            "",
            f"<code>{escape_html(type(error).__name__)}</code>",
            escape_html(error.message),
        ]
        if error.context:
            context_text = ", ".join(f"{k}={v}" for k, v in error.context.items())
            lines.append(f"Bağlam: <code>{escape_html(context_text)}</code>")
        lines.append("")
        lines.append(f"🕒 {self._fmt_time(datetime.now(timezone.utc))}")
        return await self._send("\n".join(lines))

    def as_alert_callback(self) -> Callable[[PlatformError], None]:
        """
        Adapter for `system.error_handler.register_alert_callback()`,
        whose docstring names this engine as its intended subscriber but
        expects a *synchronous* `Callable[[PlatformError], None]` (the
        error handler is not async and does not await callbacks).

        Routes by severity rather than always calling `notify_error()`:
        CRITICAL/FATAL -> `notify_error()` (always sends); WARNING/ERROR
        -> `notify_warning()` (level-gated, so it can be quieted down);
        INFO -> dropped entirely, too low-value for a push notification.
        Either way the actual send is scheduled as a fire-and-forget task
        on the currently-running event loop rather than blocking the
        synchronous call site. Requires a running loop, true for this
        platform's only realistic call path (the async scan loop):

            register_alert_callback(notification_engine.as_alert_callback())
        """

        def _callback(error: PlatformError) -> None:
            if error.severity not in _ALERTABLE_ERROR_SEVERITIES and error.severity not in _WARNING_SEVERITIES:
                return
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                _logger.warning(
                    "as_alert_callback invoked with no running event loop; dropping alert for %r", error
                )
                return
            coro = self.notify_error(error) if error.severity in _ALERTABLE_ERROR_SEVERITIES else self.notify_warning(error)
            task = loop.create_task(coro)
            task.add_done_callback(self._log_alert_task_failure)

        return _callback

    @staticmethod
    def _log_alert_task_failure(task: "asyncio.Task[bool]") -> None:
        """
        Fire-and-forget tasks swallow exceptions unless something checks
        them -- surface a failure here instead of letting it disappear
        into asyncio's default "Task exception was never retrieved"
        warning.
        """
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            _logger.error("Unhandled exception in as_alert_callback's notify_error task: %s", exc, exc_info=exc)

    # ─────────────────────────────────────────────────────────────────
    # SCHEDULED REPORTS  (gated only by their own send_*_reports flags)
    # ─────────────────────────────────────────────────────────────────

    async def notify_daily_report(self, report: Report) -> bool:
        return await self._notify_report(report, ReportType.DAILY, self._config.telegram.send_daily_reports)

    async def notify_weekly_report(self, report: Report) -> bool:
        return await self._notify_report(report, ReportType.WEEKLY, self._config.telegram.send_weekly_reports)

    async def notify_monthly_report(self, report: Report) -> bool:
        return await self._notify_report(
            report, ReportType.MONTHLY, self._config.telegram.send_monthly_reports
        )

    async def notify_health_report(self, snapshot: BotHealthSnapshot) -> bool:
        if not self._config.telegram.send_health_reports:
            return False
        status_icon = {"HEALTHY": "💚", "WARNING": "🟡", "CRITICAL": "🔴"}.get(snapshot.status, "⚪")
        lines = [
            f"{status_icon} <b>SİSTEM SAĞLIK RAPORU</b>",
            f"Durum: <b>{escape_html(snapshot.status)}</b>",
            "",
            f"📡 Aktif İşlemler: <code>{snapshot.active_trades_count}</code>",
            f"🔍 Taranan Sembol: <code>{snapshot.symbols_scanned_count}</code>",
            f"🧯 Hata: <code>{snapshot.error_count}</code> · Retry: <code>{snapshot.retry_count}</code>",
        ]
        if snapshot.average_scan_duration_seconds is not None:
            lines.append(f"⏱ Ort. Tarama Süresi: <code>{snapshot.average_scan_duration_seconds:.2f}s</code>")
        if snapshot.average_api_response_ms is not None:
            lines.append(f"📶 Ort. API Yanıt: <code>{snapshot.average_api_response_ms:.0f}ms</code>")
        if snapshot.cpu_percent is not None or snapshot.ram_mb is not None:
            lines.append(
                f"🖥 CPU/RAM: <code>{_fmt_score(snapshot.cpu_percent)}% / {_fmt_score(snapshot.ram_mb)}MB</code>"
            )
        if snapshot.restart_count:
            lines.append(f"🔁 Yeniden Başlatma: <code>{snapshot.restart_count}</code>")
        lines.append("")
        lines.append(f"🕒 {self._fmt_time(snapshot.snapshot_time)}")
        return await self._send("\n".join(lines))

    async def _notify_report(self, report: Report, expected_type: ReportType, enabled: bool) -> bool:
        """
        Shared body for daily/weekly/monthly reports -- business
        validation ensures a report is never sent under the wrong
        heading (e.g. a WEEKLY `Report` accidentally passed to
        `notify_daily_report()`), then defers to `_format_report_body()`
        for the reusable template.
        """
        if report.report_type != expected_type:
            raise ValueError(
                f"notify_{expected_type.value.lower()}_report() requires report_type={expected_type!r}, "
                f"got {report.report_type!r}"
            )
        if not enabled:
            return False
        return await self._send(self._format_report_body(report))

    def _format_report_body(self, report: Report) -> str:
        """
        Platform owner's explicit reference format (sinyal_kanali_2's
        daily/weekly summary style): clean labeled stat lines only --
        NOT the earlier prose paragraph + recommendations + letter-grade
        layout. `report.turkish_analysis`/`turkish_recommendations`/
        `overall_grade` are still computed and stored on the `Report`
        row (other consumers may still want them), just no longer
        rendered into the Telegram message itself -- they read as
        clutter next to a short stat block, not signal.
        """
        title = _REPORT_TITLE_TR.get(report.report_type, report.report_type.value)
        if report.period_start.date() == report.period_end.date():
            period = report.period_start.strftime("%d.%m.%Y")
        else:
            period = f"{report.period_start.strftime('%d.%m.%Y')} – {report.period_end.strftime('%d.%m.%Y')}"
        lines = [f"📊 <b>{title}</b> — {period}", ""]
        lines.extend(self._format_report_stats(report.content))
        return "\n".join(lines)

    @staticmethod
    def _format_report_stats(content: dict) -> list[str]:
        """
        Surfaces `Report.content`'s already-computed statistics (see
        `ReportingEngine._build_content()`) as short labeled lines,
        matching the platform owner's exact reference style. Every field
        is optional -- a report built from zero signals has a `content`
        dict without most of these keys at all, so each line is only
        added when its value is actually present ("omit, don't
        fabricate").
        """
        lines: list[str] = []
        signals_generated = content.get("signals_generated")
        long_signals, short_signals = content.get("long_signals"), content.get("short_signals")
        if signals_generated is not None:
            breakdown = f" (LONG {long_signals} / SHORT {short_signals})" if long_signals is not None else ""
            lines.append(f"📤 Gönderilen sinyal: <code>{signals_generated}</code>{breakdown}")

        wins = content.get("wins")
        if wins is not None:
            lines.append(f"🎯 TP ile kapandı: <code>{wins}</code>")
        trailing_exits = content.get("trailing_stop_exits")
        if trailing_exits:  # 0 or None both mean "nothing to report" here
            lines.append(f"🔒 Kâr kilitlendi (erken çıkış): <code>{trailing_exits}</code>")
        losses = content.get("losses")
        if losses is not None:
            lines.append(f"🛑 SL (kaybetti): <code>{losses}</code>")
        still_open = content.get("still_open")
        if still_open is not None:
            lines.append(f"⏳ Hâlâ açık: <code>{still_open}</code>")

        win_rate = content.get("win_rate_percent")
        if win_rate is not None:
            lines.append(f"📈 Kazanma oranı: <code>{_fmt_pct(win_rate)}</code>")
        avg_confidence = content.get("average_confidence")
        if avg_confidence is not None:
            lines.append(f"⭐ Ortalama confidence: <code>{avg_confidence:.0f}</code>")

        best_symbol, best_pct = content.get("best_trade_symbol"), content.get("best_trade_pnl_percent")
        if best_symbol is not None and best_pct is not None:
            lines.append(f"🏅 En iyi işlem: <b>{escape_html(best_symbol)} {content.get('best_trade_direction', '')}</b> ({_fmt_signed_pct(best_pct)})")
        worst_symbol, worst_pct = content.get("worst_trade_symbol"), content.get("worst_trade_pnl_percent")
        if worst_symbol is not None and worst_pct is not None:
            lines.append(f"⚠️ En kötü işlem: <b>{escape_html(worst_symbol)} {content.get('worst_trade_direction', '')}</b> ({_fmt_signed_pct(worst_pct)})")

        if not lines:
            lines.append("Bu dönemde hiç sinyal üretilmedi.")
        return lines

    # ─────────────────────────────────────────────────────────────────
    # INTERNAL HELPERS
    # ─────────────────────────────────────────────────────────────────

    def _format_position_timing(self, trade: Trade) -> list[str]:
        """
        Açılış/Kapanış/Süre lines -- matches the platform owner's
        reference format exactly, including reusing "Kapanış" (closing)
        for TP1's non-terminal ping too: `sinyal_kanali_2`'s own
        `format_position_timing()` always stamps "closed" as *now*,
        regardless of whether the trade is actually closed yet. Kept
        unchanged here rather than "corrected", since an exact visual
        match was the explicit instruction -- not this module's own
        `_fmt_time()`/`_format_duration()` (different date format, no
        "(TR)" suffix, "s" not "sa"), a deliberate per-message divergence
        to match the reference precisely rather than the rest of this
        file's own house style.
        """
        opened_at = trade.entry_time or trade.created_at
        closed_at = datetime.now(timezone.utc)
        duration_seconds = max(0, int((closed_at - opened_at).total_seconds()))
        hours, remainder = divmod(duration_seconds, 3600)
        minutes, _ = divmod(remainder, 60)
        try:
            local_tz = ZoneInfo(self._config.general.timezone)
        except Exception:
            local_tz = timezone.utc
        return [
            f"Açılış: {opened_at.astimezone(local_tz).strftime('%Y-%m-%d %H:%M')} (TR)",
            f"Kapanış: {closed_at.astimezone(local_tz).strftime('%Y-%m-%d %H:%M')} (TR)",
            f"Süre: {hours} sa {minutes} dk",
        ]

    def _should_send(self, min_level: str) -> bool:
        """Defensive `.get()` fallback on both sides: an unrecognized configured or requested level never raises, it degrades to 'normal'."""
        configured = _LEVEL_ORDER.get(
            self._config.telegram.notification_level, _LEVEL_ORDER[_DEFAULT_LEVEL]
        )
        required = _LEVEL_ORDER.get(min_level, _LEVEL_ORDER[_DEFAULT_LEVEL])
        return configured >= required

    def _format_trade_core(self, trade: Trade) -> list[str]:
        """Shared symbol/direction/entry fragment (as ready-to-splice lines) reused across every trade-event template."""
        icon = _DIRECTION_ICON.get(trade.direction, "➡️")
        return [
            f"🪙 <b>{escape_html(trade.symbol)}</b>",
            f"{icon} Yön: <b>{trade.direction.value}</b>",
        ]

    def _fmt_time(self, moment: datetime) -> str:
        """Renders a (UTC-stored) timestamp in the configured timezone -- SRS General.timezone, "Europe/Istanbul" by default -- Turkish day.month.year convention."""
        try:
            local = moment.astimezone(ZoneInfo(self._config.general.timezone))
        except Exception:
            local = moment.astimezone(timezone.utc)
        return local.strftime("%d.%m.%Y %H:%M")

    @staticmethod
    def _format_duration(seconds: int) -> str:
        hours, remainder = divmod(max(seconds, 0), 3600)
        minutes, secs = divmod(remainder, 60)
        if hours:
            return f"{hours}s {minutes}dk"
        if minutes:
            return f"{minutes}dk {secs}sn"
        return f"{secs}sn"

    @staticmethod
    def _grade_tr(grade: ConfidenceGrade) -> str:
        labels = {
            ConfidenceGrade.REJECTED: "Reddedildi",
            ConfidenceGrade.STRONG: "Güçlü",
            ConfidenceGrade.VERY_STRONG: "Çok Güçlü",
            ConfidenceGrade.EXCELLENT: "Mükemmel",
            ConfidenceGrade.INSTITUTIONAL_GRADE: "Kurumsal Seviye",
        }
        return labels.get(grade, grade.value)

    async def _send(self, html: str) -> bool:
        """
        Every `notify_*` method funnels here. Only `TelegramError` (and
        its subclasses, already retried by the client) is caught -- a
        `KeyError`/`AttributeError` from a formatting bug above must
        propagate, since that is a defect in this module, not a
        delivery failure.
        """
        try:
            await self._client.send_html_message(html)
            return True
        except TelegramError as exc:
            _logger.warning("Failed to deliver Telegram notification: %s", exc)
            return False
