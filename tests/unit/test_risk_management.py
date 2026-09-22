"""
Unit tests for engines/risk_management.py's chase-prevention guard
(`RiskConfig.max_recent_move_atr_multiple` / `RejectionReason.OVEREXTENDED_MOVE`).

Not a full retrofit of every RiskManagementEngine behavior (SL/TP/RR/
leverage math is already exercised indirectly via test_confidence.py's
`_risk()` fixture) -- scoped to the one new behavior added for the
autonomous-trading pivot.

Run with:
    pytest tests/unit/test_risk_management.py -v
"""

from __future__ import annotations

import pytest

from config.schema import PlatformConfig, RiskConfig
from core.models import RejectionReason, SignalDirection
from engines.risk_management import RiskManagementEngine


class _FakeTradeRepository:
    """No active trades, ever -- portfolio-limit checks always pass, isolating the guard under test."""

    def get_active_trades(self):
        return []


@pytest.fixture
def engine() -> RiskManagementEngine:
    config = PlatformConfig(risk=RiskConfig(max_recent_move_atr_multiple=3.0))
    return RiskManagementEngine(config=config, trade_repository=_FakeTradeRepository())


# entry=100, atr=1.0 -> atr_pct_of_price = 1.0%, max_allowed_pct = 3.0 * 1.0% = 3.0%


def test_long_signal_rejected_when_price_already_pumped_past_the_atr_multiple(engine):
    result = engine.assess(
        "BTCUSDT", SignalDirection.LONG, entry_price=100.0, atr=1.0, recent_move_pct=5.0,
    )

    assert result.approved is False
    assert result.rejection_reason == RejectionReason.OVEREXTENDED_MOVE


def test_long_signal_approved_when_pump_is_within_the_atr_multiple(engine):
    result = engine.assess(
        "BTCUSDT", SignalDirection.LONG, entry_price=100.0, atr=1.0, recent_move_pct=2.0,
    )

    assert result.approved is True


def test_long_signal_never_rejected_for_a_same_size_opposite_direction_move(engine):
    """A LONG signal after a big recent DROP is a reversal setup, not a chase -- never penalized, however large."""
    result = engine.assess(
        "BTCUSDT", SignalDirection.LONG, entry_price=100.0, atr=1.0, recent_move_pct=-50.0,
    )

    assert result.approved is True


def test_short_signal_rejected_when_price_already_dumped_past_the_atr_multiple(engine):
    result = engine.assess(
        "BTCUSDT", SignalDirection.SHORT, entry_price=100.0, atr=1.0, recent_move_pct=-5.0,
    )

    assert result.approved is False
    assert result.rejection_reason == RejectionReason.OVEREXTENDED_MOVE


def test_short_signal_never_rejected_for_a_same_size_opposite_direction_move(engine):
    """A SHORT signal after a big recent PUMP is a reversal setup, not a chase."""
    result = engine.assess(
        "BTCUSDT", SignalDirection.SHORT, entry_price=100.0, atr=1.0, recent_move_pct=50.0,
    )

    assert result.approved is True


def test_guard_is_skipped_entirely_when_recent_move_pct_is_none(engine):
    """No candle history yet (or too little) -- must not reject on missing data."""
    result = engine.assess(
        "BTCUSDT", SignalDirection.LONG, entry_price=100.0, atr=1.0, recent_move_pct=None,
    )

    assert result.approved is True


def test_guard_scales_with_the_current_atr_not_a_fixed_percent(engine):
    """Same 5% move: rejected against a tight ATR, approved against a wide one -- the cap is ATR-relative, not fixed."""
    tight_atr_result = engine.assess(
        "BTCUSDT", SignalDirection.LONG, entry_price=100.0, atr=1.0, recent_move_pct=5.0,
    )
    wide_atr_result = engine.assess(
        "BTCUSDT", SignalDirection.LONG, entry_price=100.0, atr=3.0, recent_move_pct=5.0,
    )

    assert tight_atr_result.approved is False
    assert wide_atr_result.approved is True


def test_guard_respects_the_configured_multiple():
    config = PlatformConfig(risk=RiskConfig(max_recent_move_atr_multiple=10.0))
    lenient_engine = RiskManagementEngine(config=config, trade_repository=_FakeTradeRepository())

    result = lenient_engine.assess(
        "BTCUSDT", SignalDirection.LONG, entry_price=100.0, atr=1.0, recent_move_pct=5.0,
    )

    assert result.approved is True  # 5% is within a 10x ATR (10%) allowance
