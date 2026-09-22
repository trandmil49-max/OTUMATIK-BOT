"""
Unit tests for engines/analytics.py (Module 19).

Uses lightweight fake repositories (no SQLite) so the engine's own
aggregation logic is tested in isolation and fast. The underlying
repository methods this engine calls
(`RejectionRepository.get_rejections_between`,
`TradeRepository.get_closed_trades_between`,
`MissedOpportunityRepository.get_between`) are exercised against a real
database in tests/unit/test_database.py.

Run with:
    pytest tests/unit/test_analytics.py -v
"""

from datetime import datetime, timezone

import pytest

from core.models import MissedOpportunity, Rejection, RejectionReason, SignalDirection, Trade, TradeStatus
from engines.analytics import AnalyticsEngine

PERIOD_START = datetime(2026, 6, 1, tzinfo=timezone.utc)
PERIOD_END = datetime(2026, 6, 2, tzinfo=timezone.utc)


# ── fakes ──────────────────────────────────────────────────────────────


class _FakeRejectionRepository:
    def __init__(self, rejections: list[Rejection]):
        self._rejections = rejections

    def get_rejections_between(self, period_start, period_end):
        return [r for r in self._rejections if period_start <= r.created_at < period_end]


class _FakeTradeRepository:
    def __init__(self, trades: list[Trade]):
        self._trades = trades

    def get_closed_trades_between(self, period_start, period_end):
        return [t for t in self._trades if t.exit_time and period_start <= t.exit_time < period_end]


class _FakeMissedOpportunityRepository:
    def __init__(self, missed: list[MissedOpportunity]):
        self._missed = missed

    def get_between(self, period_start, period_end):
        return [m for m in self._missed if period_start <= m.rejected_at < period_end]


def _rejection(*, reason: RejectionReason, symbol="BTCUSDT", filters=None, created_at=None) -> Rejection:
    return Rejection(
        symbol=symbol,
        primary_reason=reason,
        rejected_filters=filters or [],
        created_at=created_at or PERIOD_START.replace(hour=6),
    )


def _trade(*, status: TradeStatus, exit_time=None) -> Trade:
    return Trade(
        signal_id=1, symbol="BTCUSDT", direction=SignalDirection.LONG, entry_price=100.0,
        entry_time=PERIOD_START, initial_stop_loss=95.0, current_stop_loss=95.0,
        take_profit_1=105.0, confidence_score=80.0,
        status=status, exit_price=100.0, exit_time=exit_time or PERIOD_START.replace(hour=12),
        realized_pnl_percent=1.0 if status == TradeStatus.TP1_HIT else -1.0,
    )


def _missed(*, reason: RejectionReason, move_percent: float, rejected_at=None) -> MissedOpportunity:
    return MissedOpportunity(
        symbol="BTCUSDT", rejected_at=rejected_at or PERIOD_START.replace(hour=6), reject_reason=reason,
        price_at_rejection=100.0, subsequent_max_move_percent=move_percent, evaluation_window_hours=24,
    )


def _engine(rejections=None, trades=None, missed=None) -> AnalyticsEngine:
    return AnalyticsEngine(
        rejection_repository=_FakeRejectionRepository(rejections or []),
        trade_repository=_FakeTradeRepository(trades or []),
        missed_opportunity_repository=_FakeMissedOpportunityRepository(missed or []),
    )


# ── rejection breakdown ──────────────────────────────────────────────────


def test_rejection_breakdown_counts_by_reason():
    rejections = [
        _rejection(reason=RejectionReason.WEAK_TREND),
        _rejection(reason=RejectionReason.WEAK_TREND),
        _rejection(reason=RejectionReason.LOW_LIQUIDITY),
    ]
    engine = _engine(rejections=rejections)

    result = engine.get_rejection_breakdown(PERIOD_START, PERIOD_END)

    assert result["total_rejections"] == 3
    assert result["by_reason"] == {"WEAK_TREND": 2, "LOW_LIQUIDITY": 1}


def test_rejection_breakdown_counts_by_symbol():
    rejections = [
        _rejection(reason=RejectionReason.WEAK_TREND, symbol="BTCUSDT"),
        _rejection(reason=RejectionReason.WEAK_TREND, symbol="ETHUSDT"),
        _rejection(reason=RejectionReason.WEAK_TREND, symbol="BTCUSDT"),
    ]
    engine = _engine(rejections=rejections)

    result = engine.get_rejection_breakdown(PERIOD_START, PERIOD_END)

    assert result["by_symbol"] == {"BTCUSDT": 2, "ETHUSDT": 1}


def test_rejection_breakdown_counts_by_individual_filter_name():
    rejections = [
        _rejection(reason=RejectionReason.WEAK_TREND, filters=["trend_filter", "volume_filter"]),
        _rejection(reason=RejectionReason.LOW_LIQUIDITY, filters=["volume_filter"]),
    ]
    engine = _engine(rejections=rejections)

    result = engine.get_rejection_breakdown(PERIOD_START, PERIOD_END)

    # a rejection can list multiple filters -- each is counted, not just the primary_reason
    assert result["by_filter"] == {"volume_filter": 2, "trend_filter": 1}


def test_rejection_breakdown_excludes_rejections_outside_period():
    rejections = [
        _rejection(reason=RejectionReason.WEAK_TREND, created_at=datetime(2026, 5, 1, tzinfo=timezone.utc)),
        _rejection(reason=RejectionReason.WEAK_TREND, created_at=PERIOD_START.replace(hour=6)),
    ]
    engine = _engine(rejections=rejections)

    result = engine.get_rejection_breakdown(PERIOD_START, PERIOD_END)

    assert result["total_rejections"] == 1


def test_rejection_breakdown_handles_zero_rejections():
    engine = _engine(rejections=[])
    result = engine.get_rejection_breakdown(PERIOD_START, PERIOD_END)
    assert result == {"total_rejections": 0, "by_reason": {}, "by_symbol": {}, "by_filter": {}}


# ── signal accuracy (false positive rate) ────────────────────────────────


def test_signal_accuracy_computes_false_positive_rate():
    trades = [
        _trade(status=TradeStatus.TP1_HIT),
        _trade(status=TradeStatus.TP1_HIT),
        _trade(status=TradeStatus.STOP_LOSS),
    ]
    engine = _engine(trades=trades)

    result = engine.get_signal_accuracy(PERIOD_START, PERIOD_END)

    assert result["trades_evaluated"] == 3
    assert result["true_positives"] == 2
    assert result["false_positives"] == 1
    assert result["false_positive_rate_percent"] == pytest.approx(33.33, abs=0.01)


def test_signal_accuracy_handles_zero_trades():
    engine = _engine(trades=[])
    result = engine.get_signal_accuracy(PERIOD_START, PERIOD_END)
    assert result["trades_evaluated"] == 0
    assert result["false_positive_rate_percent"] is None


def test_signal_accuracy_excludes_non_terminal_win_loss_statuses():
    trades = [_trade(status=TradeStatus.EXPIRED), _trade(status=TradeStatus.CANCELLED)]
    engine = _engine(trades=trades)

    result = engine.get_signal_accuracy(PERIOD_START, PERIOD_END)

    assert result["trades_evaluated"] == 2
    assert result["true_positives"] == 0
    assert result["false_positives"] == 0
    assert result["false_positive_rate_percent"] == 0.0


# ── missed opportunity summary ──────────────────────────────────────────


def test_missed_opportunity_summary_aggregates_moves():
    missed = [
        _missed(reason=RejectionReason.POOR_RISK_REWARD, move_percent=4.0),
        _missed(reason=RejectionReason.POOR_RISK_REWARD, move_percent=8.0),
        _missed(reason=RejectionReason.LOW_CONFIDENCE, move_percent=2.0),
    ]
    engine = _engine(missed=missed)

    result = engine.get_missed_opportunity_summary(PERIOD_START, PERIOD_END)

    assert result["total_missed_opportunities"] == 3
    assert result["average_subsequent_move_percent"] == pytest.approx(4.67, abs=0.01)
    assert result["max_subsequent_move_percent"] == pytest.approx(8.0)
    assert result["min_subsequent_move_percent"] == pytest.approx(2.0)
    assert result["by_reject_reason"] == {"POOR_RISK_REWARD": 2, "LOW_CONFIDENCE": 1}


def test_missed_opportunity_summary_handles_zero_rows_gracefully():
    """Nothing currently populates missed_opportunities (see engines/analytics.py's module docstring) -- must not crash on empty data."""
    engine = _engine(missed=[])

    result = engine.get_missed_opportunity_summary(PERIOD_START, PERIOD_END)

    assert result == {
        "total_missed_opportunities": 0,
        "average_subsequent_move_percent": None,
        "max_subsequent_move_percent": None,
        "min_subsequent_move_percent": None,
        "by_reject_reason": {},
    }


def test_missed_opportunity_summary_excludes_rows_outside_period():
    missed = [
        _missed(reason=RejectionReason.LOW_CONFIDENCE, move_percent=5.0,
                rejected_at=datetime(2026, 5, 1, tzinfo=timezone.utc)),
        _missed(reason=RejectionReason.LOW_CONFIDENCE, move_percent=3.0, rejected_at=PERIOD_START.replace(hour=6)),
    ]
    engine = _engine(missed=missed)

    result = engine.get_missed_opportunity_summary(PERIOD_START, PERIOD_END)

    assert result["total_missed_opportunities"] == 1
    assert result["average_subsequent_move_percent"] == pytest.approx(3.0)


# ── period validation (shared across all three methods) ─────────────────


@pytest.mark.parametrize(
    "method_name",
    ["get_rejection_breakdown", "get_signal_accuracy", "get_missed_opportunity_summary"],
)
def test_every_method_rejects_period_end_not_after_period_start(method_name):
    engine = _engine()
    method = getattr(engine, method_name)
    with pytest.raises(ValueError):
        method(PERIOD_END, PERIOD_START)
