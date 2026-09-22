"""
Unit tests for engines/indicators.py (Module 6).

Test data is chosen so expected values can be verified by hand (constant
series, pure linear trends, classic textbook std-dev numbers) rather than
asserting against another implementation's output.

Run with:
    pytest tests/unit/test_indicators.py -v
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest

from engines.indicators import (
    adx,
    atr,
    bollinger_bands,
    ema,
    macd,
    recent_price_change_pct,
    rsi,
    sma,
    swing_points,
    true_range,
    volume_ratio,
)
from infrastructure.binance.models import Candle


def _candle(high: float, low: float, close: float, volume: float = 1.0) -> Candle:
    """Minimal Candle for indicator math -- open_time/close_time are irrelevant to every function under test."""
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return Candle(
        open_time=now, open=close, high=high, low=low, close=close, volume=volume,
        close_time=now + timedelta(minutes=1), quote_volume=volume * close,
        num_trades=1, taker_buy_base_volume=0.0, taker_buy_quote_volume=0.0,
    )


def _is_nan(x: float) -> bool:
    return isinstance(x, float) and math.isnan(x)


# ─────────────────────────────────────────────────────────────────────────
# SMA / EMA
# ─────────────────────────────────────────────────────────────────────────


def test_sma_warmup_is_nan_and_values_match_hand_computed_means():
    values = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
    result = sma(values, period=3)

    assert _is_nan(result[0]) and _is_nan(result[1])
    assert result[2] == pytest.approx(2.0)  # mean(1,2,3)
    assert result[3] == pytest.approx(3.0)  # mean(2,3,4)
    assert result[9] == pytest.approx(9.0)  # mean(8,9,10)


def test_sma_rejects_non_positive_period():
    with pytest.raises(ValueError):
        sma([1.0, 2.0], period=0)


def test_ema_warmup_is_nan_and_seeds_from_sma():
    values = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
    result = ema(values, period=3)

    assert _is_nan(result[0]) and _is_nan(result[1])
    assert result[2] == pytest.approx(2.0)  # seed = mean(1,2,3)
    # multiplier = 0.5; for this linear sequence EMA converges to value - 1 exactly
    for i in range(3, len(values)):
        assert result[i] == pytest.approx(values[i] - 1.0)


def test_ema_returns_all_nan_when_shorter_than_period():
    result = ema([1.0, 2.0], period=5)
    assert all(_is_nan(v) for v in result)


# ─────────────────────────────────────────────────────────────────────────
# RSI
# ─────────────────────────────────────────────────────────────────────────


def test_rsi_is_100_for_a_strictly_increasing_series():
    closes = [float(i) for i in range(1, 17)]  # 16 values, all deltas = +1
    result = rsi(closes, period=14)

    assert all(_is_nan(v) for v in result[:14])
    assert result[14] == pytest.approx(100.0)
    assert result[15] == pytest.approx(100.0)


def test_rsi_is_0_for_a_strictly_decreasing_series():
    closes = [float(i) for i in range(16, 0, -1)]  # all deltas = -1
    result = rsi(closes, period=14)

    assert result[14] == pytest.approx(0.0)
    assert result[15] == pytest.approx(0.0)


def test_rsi_is_bounded_between_0_and_100_for_mixed_data():
    closes = [10.0, 11.0, 9.5, 12.0, 8.0, 13.0, 7.0, 14.0, 6.0, 15.0, 5.0, 16.0, 4.0, 17.0, 3.0, 18.0]
    result = rsi(closes, period=14)
    for value in result:
        if not _is_nan(value):
            assert 0.0 <= value <= 100.0


# ─────────────────────────────────────────────────────────────────────────
# ATR / TRUE RANGE
# ─────────────────────────────────────────────────────────────────────────


def test_true_range_first_candle_uses_only_high_low():
    candles = [_candle(high=105, low=95, close=100)]
    result = true_range(candles)
    assert result[0] == pytest.approx(10.0)


def test_atr_is_constant_when_true_range_is_constant():
    # high-low = 10 for every candle, and close stays at the midpoint (100) so
    # neither gap term (|high-prev_close|, |low-prev_close|) ever exceeds 10.
    candles = [_candle(high=105, low=95, close=100) for _ in range(20)]
    result = atr(candles, period=14)

    assert all(_is_nan(v) for v in result[:13])
    for i in range(13, 20):
        assert result[i] == pytest.approx(10.0)


# ─────────────────────────────────────────────────────────────────────────
# ADX
# ─────────────────────────────────────────────────────────────────────────


def test_adx_sustained_uptrend_has_zero_minus_di_and_adx_near_100():
    # high/low both strictly increase every candle -> +DM constant positive, -DM constant 0.
    candles = [_candle(high=100 + 2 * i, low=99 + 2 * i, close=99.5 + 2 * i) for i in range(40)]
    result = adx(candles, period=14)

    warmup_end = 2 * 14 - 1
    assert all(_is_nan(v) for v in result.adx[:warmup_end])
    for i in range(14, 40):
        assert result.minus_di[i] == pytest.approx(0.0)
        assert result.plus_di[i] > 0.0
    for i in range(warmup_end, 40):
        assert result.adx[i] == pytest.approx(100.0, abs=1e-6)


def test_adx_empty_candles_returns_empty_result():
    result = adx([], period=14)
    assert result.plus_di == []
    assert result.minus_di == []
    assert result.adx == []


# ─────────────────────────────────────────────────────────────────────────
# MACD  (cross-checked against ema() directly, since ema() is independently verified above)
# ─────────────────────────────────────────────────────────────────────────


def test_macd_line_equals_fast_ema_minus_slow_ema():
    closes = [100.0 + i * 0.7 for i in range(60)]
    result = macd(closes, fast_period=12, slow_period=26, signal_period=9)

    fast = ema(closes, 12)
    slow = ema(closes, 26)
    for i in range(60):
        if math.isnan(fast[i]) or math.isnan(slow[i]):
            assert _is_nan(result.macd_line[i])
        else:
            assert result.macd_line[i] == pytest.approx(fast[i] - slow[i])


def test_macd_histogram_equals_macd_line_minus_signal_line_wherever_both_defined():
    closes = [100.0 + (i % 5) * 0.3 - i * 0.05 for i in range(60)]
    result = macd(closes)

    for i in range(60):
        if not (math.isnan(result.macd_line[i]) or math.isnan(result.signal_line[i])):
            assert result.histogram[i] == pytest.approx(result.macd_line[i] - result.signal_line[i])
        else:
            assert _is_nan(result.histogram[i])


def test_macd_handles_series_too_short_for_slow_period_without_crashing():
    closes = [100.0, 101.0, 102.0]
    result = macd(closes, fast_period=12, slow_period=26, signal_period=9)
    assert all(_is_nan(v) for v in result.macd_line)
    assert all(_is_nan(v) for v in result.signal_line)
    assert all(_is_nan(v) for v in result.histogram)


# ─────────────────────────────────────────────────────────────────────────
# BOLLINGER BANDS
# ─────────────────────────────────────────────────────────────────────────


def test_bollinger_bands_constant_series_has_zero_width():
    closes = [10.0] * 20
    result = bollinger_bands(closes, period=5, num_std=2.0)

    for i in range(4, 20):
        assert result.middle[i] == pytest.approx(10.0)
        assert result.upper[i] == pytest.approx(10.0)
        assert result.lower[i] == pytest.approx(10.0)


def test_bollinger_bands_matches_classic_textbook_stddev_example():
    # Classic population-std-dev worked example: mean=5.0, population std=2.0.
    closes = [2.0, 4.0, 4.0, 4.0, 5.0, 5.0, 7.0, 9.0]
    result = bollinger_bands(closes, period=8, num_std=2.0)

    assert result.middle[7] == pytest.approx(5.0)
    assert result.upper[7] == pytest.approx(9.0)  # 5.0 + 2*2.0
    assert result.lower[7] == pytest.approx(1.0)  # 5.0 - 2*2.0


# ─────────────────────────────────────────────────────────────────────────
# VOLUME RATIO
# ─────────────────────────────────────────────────────────────────────────


def test_volume_ratio_flags_above_average_volume():
    candles = [
        _candle(high=1, low=1, close=1, volume=10) for _ in range(5)
    ] + [_candle(high=1, low=1, close=1, volume=20)]
    result = volume_ratio(candles, period=5)

    assert result[4] == pytest.approx(1.0)  # avg of first 5 volumes (all 10) vs volume 10
    assert result[5] == pytest.approx(20.0 / 12.0)  # avg of volumes[1:6] = (10*4+20)/5 = 12.0


# ─────────────────────────────────────────────────────────────────────────
# SWING POINTS
# ─────────────────────────────────────────────────────────────────────────


def test_swing_points_detects_a_single_unambiguous_high_and_low():
    candles = [
        _candle(high=5, low=5, close=5),
        _candle(high=5, low=5, close=5),
        _candle(high=5, low=1, close=3),   # swing low
        _candle(high=10, low=5, close=7),  # swing high
        _candle(high=5, low=5, close=5),
        _candle(high=5, low=5, close=5),
        _candle(high=5, low=5, close=5),
    ]
    result = swing_points(candles, lookback=2)

    assert result.swing_high_indices == (3,)
    assert result.swing_low_indices == (2,)


def test_swing_points_returns_empty_when_series_too_short_for_lookback():
    candles = [_candle(high=1, low=1, close=1) for _ in range(3)]
    result = swing_points(candles, lookback=2)
    assert result.swing_high_indices == ()
    assert result.swing_low_indices == ()


# ─────────────────────────────────────────────────────────────────────────
# RECENT PRICE CHANGE % (chase-prevention guard input)
# ─────────────────────────────────────────────────────────────────────────


def test_recent_price_change_pct_computes_signed_percent_change():
    # 15 candles: close 100 at index 0 (14 candles ago from the last), then flat, then 110 at the end.
    candles = [_candle(high=100, low=100, close=100)] + [
        _candle(high=100, low=100, close=100) for _ in range(13)
    ] + [_candle(high=110, low=110, close=110)]

    result = recent_price_change_pct(candles, lookback_periods=14)

    assert result == pytest.approx(10.0)  # (110-100)/100 * 100


def test_recent_price_change_pct_is_negative_when_price_fell():
    candles = [_candle(high=100, low=100, close=100) for _ in range(15)]
    candles[0] = _candle(high=100, low=100, close=100)
    candles[-1] = _candle(high=90, low=90, close=90)

    result = recent_price_change_pct(candles, lookback_periods=14)

    assert result == pytest.approx(-10.0)


def test_recent_price_change_pct_returns_none_with_insufficient_candles():
    candles = [_candle(high=100, low=100, close=100) for _ in range(10)]
    assert recent_price_change_pct(candles, lookback_periods=14) is None


def test_recent_price_change_pct_returns_none_at_exactly_the_boundary():
    """Needs lookback_periods + 1 candles -- exactly lookback_periods is still insufficient."""
    candles = [_candle(high=100, low=100, close=100) for _ in range(14)]
    assert recent_price_change_pct(candles, lookback_periods=14) is None


def test_recent_price_change_pct_returns_none_when_reference_close_is_zero():
    candles = [_candle(high=0, low=0, close=0)] + [
        _candle(high=100, low=100, close=100) for _ in range(14)
    ]
    assert recent_price_change_pct(candles, lookback_periods=14) is None
