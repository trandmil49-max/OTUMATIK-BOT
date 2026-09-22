"""
engines/analytics.py

Analytics Engine (Module 19) -- SRS Part 14, PROJECT_STATUS.md: "Analytics
& Recommendation Engine (filter performance, false pos/neg, missed
opportunity)".

SCOPE OF THIS PASS -- read this before extending the module. Per explicit
instruction: implement only what is objectively derivable from existing,
verified models/repositories; never invent a scoring formula or a
"recommendation" rule that isn't documented anywhere this module can see.

    IN SCOPE, fully implemented and tested (pure counting/aggregation,
    zero invented thresholds or weights):
        * `get_rejection_breakdown()` -- rejection counts by reason, by
          symbol, and by individual filter name (from each `Rejection`'s
          `rejected_filters` list) for a caller-supplied period.
        * `get_signal_accuracy()` -- "false positive rate": the fraction
          of closed trades that hit STOP_LOSS. This is the same
          objective loss-rate arithmetic as `engines/reporting.py`'s win
          rate, just complementary and framed for signal-quality
          analysis rather than trading performance. Computed
          independently from `TradeRepository` (this engine does not
          import `ReportingEngine` -- engines stay decoupled from each
          other, each depends on the repository layer directly).
        * `get_missed_opportunity_summary()` -- count and
          average/max/min `subsequent_max_move_percent` across whatever
          `MissedOpportunity` rows exist for the period, broken down by
          `reject_reason`. Handles zero rows gracefully, which is
          expected right now: see the next paragraph.

    DELIBERATELY NOT IMPLEMENTED HERE -- flagged rather than guessed at:
        * "False negative" classification (labeling a specific missed
          opportunity as one "the bot should have taken"). That needs a
          magnitude threshold ("moved favorably by more than X%") that
          is not documented anywhere -- picking one myself would be
          exactly the invented-threshold problem this pass was told to
          avoid. `get_missed_opportunity_summary()` exposes the raw
          `subsequent_max_move_percent` distribution instead and leaves
          the judgment call to a human reader.
        * `FilterPerformance.contribution_score` /
          `.reliability_score` / `.success_rate_percent` /
          `.saved_losses_count`. SRS Part 14 names these but gives no
          formula anywhere this module can see. `.trades_rejected` is
          the one field of that dataclass that IS pure counting; it is
          exposed via `get_rejection_breakdown()`'s per-filter counts
          rather than through a half-populated `FilterPerformance`
          record (that table's own docstring describes it as
          accumulating complete history, not partial rows).
        * Actually populating `MissedOpportunity` rows in the first
          place (tracking a rejected signal's price forward over its
          `evaluation_window_hours` and computing
          `subsequent_max_move_percent`). That is a data-GENERATION
          concern -- something like a "Missed Opportunity Monitor"
          alongside `engines/position_monitor.py`, not yet built -- not
          an analytics/aggregation concern. Nothing in the codebase
          currently creates these rows, so `get_missed_opportunity_summary()`
          will correctly report zero until that piece exists.
        * Free-form "recommendation" text connecting these statistics to
          specific tuning advice. Doing that without a documented rule
          would be inventing the exact kind of business logic this pass
          was told not to invent.

Clean Architecture: this engine owns aggregation logic only; it consumes
`RejectionRepository` / `TradeRepository` / `MissedOpportunityRepository`
via constructor injection and never touches SQL directly.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Any

from core.models import TradeStatus
from infrastructure.database.repositories.rejection_repository import (
    MissedOpportunityRepository,
    RejectionRepository,
)
from infrastructure.database.repositories.trade_repository import TradeRepository
from system.logging_setup import get_logger

_logger = get_logger("system")


class AnalyticsEngine:
    """
    Consumes repositories via dependency injection. Every method takes
    an explicit `period_start`/`period_end` (this engine does not decide
    what "today" or "this week" means -- see `engines/reporting.py`'s
    docstring for the same reasoning) and returns a plain dict of
    objective counts/statistics rather than a scored verdict.
    """

    def __init__(
        self,
        rejection_repository: RejectionRepository,
        trade_repository: TradeRepository,
        missed_opportunity_repository: MissedOpportunityRepository,
    ) -> None:
        self._rejections = rejection_repository
        self._trades = trade_repository
        self._missed = missed_opportunity_repository

    def get_rejection_breakdown(self, period_start: datetime, period_end: datetime) -> dict[str, Any]:
        self._validate_period(period_start, period_end)
        rejections = self._rejections.get_rejections_between(period_start, period_end)

        by_reason: Counter[str] = Counter(r.primary_reason.value for r in rejections)
        by_symbol: Counter[str] = Counter(r.symbol for r in rejections)
        by_filter: Counter[str] = Counter()
        for rejection in rejections:
            by_filter.update(rejection.rejected_filters)

        result = {
            "total_rejections": len(rejections),
            "by_reason": dict(by_reason.most_common()),
            "by_symbol": dict(by_symbol.most_common()),
            "by_filter": dict(by_filter.most_common()),
        }
        _logger.info(
            "Rejection breakdown %s..%s: %d rejections across %d filters",
            period_start.isoformat(), period_end.isoformat(), len(rejections), len(by_filter),
        )
        return result

    def get_signal_accuracy(self, period_start: datetime, period_end: datetime) -> dict[str, Any]:
        """
        "False positive rate": of the signals we acted on (became closed
        trades), what fraction turned out wrong (STOP_LOSS)? Pure
        arithmetic over `TradeStatus`, no invented weighting.
        """
        self._validate_period(period_start, period_end)
        trades = self._trades.get_closed_trades_between(period_start, period_end)

        losses = [
            t for t in trades
            if t.status == TradeStatus.STOP_LOSS
            or (t.status == TradeStatus.TRAILING_STOP_EXIT and (t.realized_pnl_percent or 0) < 0)
        ]
        wins = [
            t for t in trades
            if t.status == TradeStatus.TP1_HIT
            or (t.status == TradeStatus.TRAILING_STOP_EXIT and (t.realized_pnl_percent or 0) >= 0)
        ]

        false_positive_rate = round(len(losses) / len(trades) * 100, 2) if trades else None

        return {
            "trades_evaluated": len(trades),
            "false_positives": len(losses),
            "true_positives": len(wins),
            "false_positive_rate_percent": false_positive_rate,
        }

    def get_missed_opportunity_summary(self, period_start: datetime, period_end: datetime) -> dict[str, Any]:
        self._validate_period(period_start, period_end)
        missed = self._missed.get_between(period_start, period_end)

        if not missed:
            return {
                "total_missed_opportunities": 0,
                "average_subsequent_move_percent": None,
                "max_subsequent_move_percent": None,
                "min_subsequent_move_percent": None,
                "by_reject_reason": {},
            }

        moves = [m.subsequent_max_move_percent for m in missed]
        by_reason: Counter[str] = Counter(m.reject_reason.value for m in missed)

        return {
            "total_missed_opportunities": len(missed),
            "average_subsequent_move_percent": round(sum(moves) / len(moves), 2),
            "max_subsequent_move_percent": round(max(moves), 2),
            "min_subsequent_move_percent": round(min(moves), 2),
            "by_reject_reason": dict(by_reason.most_common()),
        }

    @staticmethod
    def _validate_period(period_start: datetime, period_end: datetime) -> None:
        if period_end <= period_start:
            raise ValueError(f"period_end ({period_end}) must be after period_start ({period_start})")
