"""
Unit tests for engines/market_structure.py (Module 9).

Bias/BOS/CHoCH/sweep detectors are tested directly against their pure
internal seams with hand-crafted swing tuples -- naturally growing real
candle data into an exact desired swing configuration is far less
reliable than constructing the configuration directly (the same approach
used for Module 7's confirmation logic). Order blocks and FVGs are tested
against small, precise 2-3 candle patterns. One full `analyze()`
integration test ties everything together over a realistic series.

Run with:
    pytest tests/unit/test_market_structure.py -v
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from engines.market_structure import (
    MarketStructureEngine,
    StructureBias,
    StructureEventType,
)
from infrastructure.binance.models import Candle


def _candle(open_: float, high: float, low: float, close: float) -> Candle:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return Candle(
        open_time=now, open=open_, high=high, low=low, close=close, volume=1.0,
        close_time=now + timedelta(minutes=1), quote_volume=close, num_trades=1,
        taker_buy_base_volume=0.0, taker_buy_quote_volume=0.0,
    )


def _level_candle(level: float) -> Candle:
    """A simple candle centered on `level`, used for bias/swing staircase test data."""
    return _candle(open_=level, high=level + 1, low=level - 1, close=level)


# Staircase-up levels: swing highs at indices 2,6,10,14 = [10,13,15,17] (higher highs);
# swing lows at indices 4,8,12 = [5,6,7] (higher lows) -> BULLISH bias, confirmed by
# hand-tracing engines.indicators.swing_points()' neighbor comparisons (lookback=2).
_STAIRCASE_UP_LEVELS = [5, 7, 10, 7, 5, 8, 13, 8, 6, 9, 15, 9, 7, 10, 17, 10, 8]


def _staircase_candles(final_level: float) -> list[Candle]:
    return [_level_candle(v) for v in _STAIRCASE_UP_LEVELS] + [_level_candle(final_level)]


@pytest.fixture
def engine() -> MarketStructureEngine:
    return MarketStructureEngine()


# ─────────────────────────────────────────────────────────────────────────
# BIAS DETERMINATION
# ─────────────────────────────────────────────────────────────────────────


def test_bias_bullish_on_higher_highs_and_higher_lows():
    bias = MarketStructureEngine._determine_bias(
        swing_highs=[(2, 100.0), (6, 110.0)], swing_lows=[(4, 90.0), (8, 95.0)]
    )
    assert bias == StructureBias.BULLISH


def test_bias_bearish_on_lower_highs_and_lower_lows():
    bias = MarketStructureEngine._determine_bias(
        swing_highs=[(2, 110.0), (6, 100.0)], swing_lows=[(4, 95.0), (8, 90.0)]
    )
    assert bias == StructureBias.BEARISH


def test_bias_unclear_on_mixed_signals():
    # Higher highs but LOWER lows -- expanding range, not a clean trend.
    bias = MarketStructureEngine._determine_bias(
        swing_highs=[(2, 100.0), (6, 110.0)], swing_lows=[(4, 95.0), (8, 90.0)]
    )
    assert bias == StructureBias.UNCLEAR


def test_bias_unclear_with_fewer_than_two_swings():
    assert MarketStructureEngine._determine_bias(swing_highs=[(2, 100.0)], swing_lows=[]) == StructureBias.UNCLEAR
    assert MarketStructureEngine._determine_bias(swing_highs=[], swing_lows=[]) == StructureBias.UNCLEAR


# ─────────────────────────────────────────────────────────────────────────
# BOS / CHoCH EVENT DETECTION
# ─────────────────────────────────────────────────────────────────────────


def test_bos_bullish_when_bias_bullish_and_price_breaks_above_last_swing_high():
    event = MarketStructureEngine._detect_latest_event(
        latest_close=120.0, swing_highs=[(6, 110.0)], swing_lows=[(4, 90.0)],
        bias=StructureBias.BULLISH, latest_index=10,
    )
    assert event is not None
    assert event.event_type == StructureEventType.BOS_BULLISH
    assert event.broken_level == 110.0


def test_choch_bullish_when_bias_bearish_and_price_breaks_above_last_swing_high():
    event = MarketStructureEngine._detect_latest_event(
        latest_close=120.0, swing_highs=[(6, 110.0)], swing_lows=[(4, 90.0)],
        bias=StructureBias.BEARISH, latest_index=10,
    )
    assert event is not None
    assert event.event_type == StructureEventType.CHOCH_BULLISH


def test_bos_bearish_when_bias_bearish_and_price_breaks_below_last_swing_low():
    event = MarketStructureEngine._detect_latest_event(
        latest_close=80.0, swing_highs=[(6, 110.0)], swing_lows=[(4, 90.0)],
        bias=StructureBias.BEARISH, latest_index=10,
    )
    assert event is not None
    assert event.event_type == StructureEventType.BOS_BEARISH
    assert event.broken_level == 90.0


def test_choch_bearish_when_bias_bullish_and_price_breaks_below_last_swing_low():
    event = MarketStructureEngine._detect_latest_event(
        latest_close=80.0, swing_highs=[(6, 110.0)], swing_lows=[(4, 90.0)],
        bias=StructureBias.BULLISH, latest_index=10,
    )
    assert event is not None
    assert event.event_type == StructureEventType.CHOCH_BEARISH


def test_no_event_when_price_stays_within_the_established_range():
    event = MarketStructureEngine._detect_latest_event(
        latest_close=100.0, swing_highs=[(6, 110.0)], swing_lows=[(4, 90.0)],
        bias=StructureBias.BULLISH, latest_index=10,
    )
    assert event is None


# ─────────────────────────────────────────────────────────────────────────
# ORDER BLOCKS
# ─────────────────────────────────────────────────────────────────────────


def test_detect_bullish_order_block():
    candles = [
        _candle(open_=110, high=112, low=98, close=100),   # down candle
        _candle(open_=101, high=130, low=100, close=125),  # up candle, close > prior high
    ]
    blocks = MarketStructureEngine._detect_order_blocks(candles)
    assert len(blocks) == 1
    assert blocks[0].direction == StructureBias.BULLISH
    assert blocks[0].candle_index == 0
    assert blocks[0].high == 112 and blocks[0].low == 98


def test_detect_bearish_order_block():
    candles = [
        _candle(open_=100, high=112, low=98, close=110),  # up candle
        _candle(open_=109, high=100, low=70, close=75),   # down candle, close < prior low
    ]
    blocks = MarketStructureEngine._detect_order_blocks(candles)
    assert len(blocks) == 1
    assert blocks[0].direction == StructureBias.BEARISH


def test_no_order_block_without_a_strong_continuation():
    candles = [
        _candle(open_=110, high=112, low=98, close=100),  # down candle
        _candle(open_=101, high=105, low=100, close=103),  # up candle, but doesn't break prior high
    ]
    assert MarketStructureEngine._detect_order_blocks(candles) == []


# ─────────────────────────────────────────────────────────────────────────
# FAIR VALUE GAPS
# ─────────────────────────────────────────────────────────────────────────


def test_detect_bullish_fair_value_gap():
    candles = [
        _candle(100, 100, 95, 98),
        _candle(98, 115, 97, 110),
        _candle(110, 120, 105, 115),  # low(105) > candle0.high(100)
    ]
    gaps = MarketStructureEngine._detect_fair_value_gaps(candles)
    assert len(gaps) == 1
    assert gaps[0].direction == StructureBias.BULLISH
    assert gaps[0].gap_low == 100 and gaps[0].gap_high == 105
    assert gaps[0].start_index == 0


def test_detect_bearish_fair_value_gap():
    candles = [
        _candle(100, 105, 95, 98),
        _candle(98, 100, 85, 90),
        _candle(90, 92, 80, 85),  # high(92) < candle0.low(95)
    ]
    gaps = MarketStructureEngine._detect_fair_value_gaps(candles)
    assert len(gaps) == 1
    assert gaps[0].direction == StructureBias.BEARISH


def test_no_fair_value_gap_when_ranges_overlap():
    candles = [
        _candle(100, 105, 95, 98),
        _candle(98, 100, 90, 95),
        _candle(95, 102, 93, 97),  # overlaps candle0's [95,105] range
    ]
    assert MarketStructureEngine._detect_fair_value_gaps(candles) == []


# ─────────────────────────────────────────────────────────────────────────
# LIQUIDITY SWEEPS
# ─────────────────────────────────────────────────────────────────────────


def test_liquidity_sweep_of_a_swing_high_within_tolerance(engine):
    candles = [_candle(9, 9, 8, 9), _candle(9, 10.005, 9.4, 9.5)]
    sweeps = engine._detect_liquidity_sweeps(candles, swing_highs=[(0, 10.0)], swing_lows=[])
    assert len(sweeps) == 1
    assert sweeps[0].direction == StructureBias.BEARISH
    assert sweeps[0].swept_level == 10.0
    assert sweeps[0].candle_index == 1


def test_liquidity_sweep_of_a_swing_low_within_tolerance(engine):
    candles = [_candle(11, 12, 11, 11), _candle(11, 11.4, 9.995, 10.5)]
    sweeps = engine._detect_liquidity_sweeps(candles, swing_highs=[], swing_lows=[(0, 10.0)])
    assert len(sweeps) == 1
    assert sweeps[0].direction == StructureBias.BULLISH
    assert sweeps[0].swept_level == 10.0


def test_no_sweep_when_overshoot_exceeds_tolerance():
    engine = MarketStructureEngine(sweep_tolerance_percent=0.1)
    candles = [_candle(9, 9, 8, 9), _candle(9, 10.5, 9.4, 9.5)]  # 5% overshoot >> 0.1% tolerance
    assert engine._detect_liquidity_sweeps(candles, swing_highs=[(0, 10.0)], swing_lows=[]) == []


def test_no_sweep_when_close_does_not_revert_past_the_level(engine):
    # High pierces the level, but the close stays beyond it too -- a genuine breakout, not a sweep.
    candles = [_candle(9, 9, 8, 9), _candle(9, 10.6, 9.9, 10.5)]
    assert engine._detect_liquidity_sweeps(candles, swing_highs=[(0, 10.0)], swing_lows=[]) == []


# ─────────────────────────────────────────────────────────────────────────
# FULL analyze() INTEGRATION
# ─────────────────────────────────────────────────────────────────────────


def test_analyze_returns_unclear_for_a_too_short_series(engine):
    candles = [_level_candle(v) for v in [10, 11, 12]]
    result = engine.analyze(candles)
    assert result.bias == StructureBias.UNCLEAR
    assert result.events == ()


def test_analyze_detects_bullish_structure_and_bos(engine):
    candles = _staircase_candles(final_level=20.0)  # closes above the last swing high (level 17 -> high 18)
    result = engine.analyze(candles)

    assert result.bias == StructureBias.BULLISH
    assert result.last_swing_high == 18.0  # candle.high = level(17) + 1
    assert result.last_swing_low == 6.0  # candle.low = level(7) - 1
    assert result.has_bos is True
    assert result.has_choch is False
    assert result.events[0].event_type == StructureEventType.BOS_BULLISH


def test_analyze_detects_choch_bearish_reversal_of_bullish_structure(engine):
    candles = _staircase_candles(final_level=3.0)  # closes below the last swing low (7)
    result = engine.analyze(candles)

    assert result.bias == StructureBias.BULLISH  # structure was bullish right up to this candle
    assert result.has_choch is True
    assert result.has_bos is False
    assert result.events[0].event_type == StructureEventType.CHOCH_BEARISH
    assert result.events[0].broken_level == 6.0  # candle.low = level(7) - 1


def test_analyze_no_event_when_final_candle_stays_within_range(engine):
    candles = _staircase_candles(final_level=12.0)  # between the last swing low (7) and high (17)
    result = engine.analyze(candles)
    assert result.events == ()
