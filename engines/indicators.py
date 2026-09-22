"""
engines/indicators.py

Technical indicator calculations (SRS Part 5 MARKET STRUCTURE ENGINE +
Part 9 CONFIDENCE ENGINE inputs -- the Trend, Structure, and Volume
scoring categories all derive from these; SRS Part 4 RISK MANAGEMENT's
ATR-based TP/SL sizing depends on `atr()` directly).

Every function is a pure, stateless transform over a `list[Candle]` or
`list[float]` (oldest-first, exactly as `BinanceFuturesClient.get_klines()`
returns) -- no I/O, no config, no side effects. This is deliberate:
indicators are mathematical primitives shared by many later engines
(Bitcoin Intelligence, Market Health, Market Structure, Risk, Confidence),
so they must carry no dependency on `infrastructure/` beyond the `Candle`
type itself, and must be trivially unit-testable against hand-computed
values.

Every series-returning function returns exactly one value per input
element, using `float("nan")` for indices inside the indicator's warm-up
period rather than raising or truncating -- callers can always zip a
returned series back against the original candle list index-for-index.
Use `math.isnan(x)` to check for warm-up placeholders.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from infrastructure.binance.models import Candle

NAN = float("nan")


# ─────────────────────────────────────────────────────────────────────────
# EXTRACTION HELPERS
# ─────────────────────────────────────────────────────────────────────────


def closes(candles: list[Candle]) -> list[float]:
    return [c.close for c in candles]


def highs(candles: list[Candle]) -> list[float]:
    return [c.high for c in candles]


def lows(candles: list[Candle]) -> list[float]:
    return [c.low for c in candles]


def volumes(candles: list[Candle]) -> list[float]:
    return [c.volume for c in candles]


# ─────────────────────────────────────────────────────────────────────────
# MOVING AVERAGES
# ─────────────────────────────────────────────────────────────────────────


def sma(values: list[float], period: int) -> list[float]:
    """Simple moving average. `result[i]` is NaN for i < period - 1."""
    if period <= 0:
        raise ValueError("period must be positive")
    result = [NAN] * len(values)
    running_sum = 0.0
    for i, value in enumerate(values):
        running_sum += value
        if i >= period:
            running_sum -= values[i - period]
        if i >= period - 1:
            result[i] = running_sum / period
    return result


def ema(values: list[float], period: int) -> list[float]:
    """
    Exponential moving average, seeded with an SMA of the first `period`
    values -- an EMA needs *some* seed, and seeding with SMA is the
    convention virtually every charting platform (including Binance's
    own) uses, so downstream comparisons against what a person sees on
    a chart line up.
    """
    if period <= 0:
        raise ValueError("period must be positive")
    result = [NAN] * len(values)
    if len(values) < period:
        return result

    multiplier = 2.0 / (period + 1)
    result[period - 1] = sum(values[:period]) / period
    for i in range(period, len(values)):
        result[i] = (values[i] - result[i - 1]) * multiplier + result[i - 1]
    return result


# ─────────────────────────────────────────────────────────────────────────
# MOMENTUM
# ─────────────────────────────────────────────────────────────────────────


def rsi(close_values: list[float], period: int = 14) -> list[float]:
    """Wilder's RSI. `result[i]` is NaN for i <= period - 1 (needs `period` price changes to seed)."""
    n = len(close_values)
    result = [NAN] * n
    if n <= period:
        return result

    gains = [0.0] * n
    losses = [0.0] * n
    for i in range(1, n):
        delta = close_values[i] - close_values[i - 1]
        gains[i] = max(delta, 0.0)
        losses[i] = max(-delta, 0.0)

    avg_gain = sum(gains[1 : period + 1]) / period
    avg_loss = sum(losses[1 : period + 1]) / period
    result[period] = _rsi_from_averages(avg_gain, avg_loss)

    for i in range(period + 1, n):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        result[i] = _rsi_from_averages(avg_gain, avg_loss)

    return result


def _rsi_from_averages(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


@dataclass(frozen=True)
class MACDResult:
    """SRS Part 9 Trend/momentum scoring input."""

    macd_line: list[float]
    signal_line: list[float]
    histogram: list[float]


def macd(
    close_values: list[float],
    fast_period: int = 12,
    slow_period: int = 26,
    signal_period: int = 9,
) -> MACDResult:
    """Standard MACD: EMA(fast) - EMA(slow), that line's own EMA(signal_period), and their difference."""
    fast_ema = ema(close_values, fast_period)
    slow_ema = ema(close_values, slow_period)
    macd_line = [
        (f - s) if not (math.isnan(f) or math.isnan(s)) else NAN for f, s in zip(fast_ema, slow_ema)
    ]

    # ema() seeds itself from the first `period` values of whatever list it's
    # given, so it must never be handed a NaN-prefixed list -- re-align the
    # signal EMA over macd_line's NaN-free tail, then map results back.
    first_valid_index = next((i for i, v in enumerate(macd_line) if not math.isnan(v)), None)
    signal_line = [NAN] * len(close_values)
    histogram = [NAN] * len(close_values)
    if first_valid_index is not None:
        signal_tail = ema(macd_line[first_valid_index:], signal_period)
        for offset, value in enumerate(signal_tail):
            signal_line[first_valid_index + offset] = value
        for i in range(len(close_values)):
            if not (math.isnan(macd_line[i]) or math.isnan(signal_line[i])):
                histogram[i] = macd_line[i] - signal_line[i]

    return MACDResult(macd_line=macd_line, signal_line=signal_line, histogram=histogram)


# ─────────────────────────────────────────────────────────────────────────
# VOLATILITY
# ─────────────────────────────────────────────────────────────────────────


def true_range(candles: list[Candle]) -> list[float]:
    """True Range per candle. `result[0]` uses only high/low (no prior close exists yet)."""
    n = len(candles)
    result = [NAN] * n
    if n == 0:
        return result
    result[0] = candles[0].high - candles[0].low
    for i in range(1, n):
        prev_close = candles[i - 1].close
        result[i] = max(
            candles[i].high - candles[i].low,
            abs(candles[i].high - prev_close),
            abs(candles[i].low - prev_close),
        )
    return result


def atr(candles: list[Candle], period: int = 14) -> list[float]:
    """
    Wilder's Average True Range (SRS Part 4: ATR-based dynamic TP/SL/leverage
    sizing). `result[i]` is NaN for i < period - 1.
    """
    tr = true_range(candles)
    n = len(candles)
    result = [NAN] * n
    if n < period:
        return result

    result[period - 1] = sum(tr[:period]) / period
    for i in range(period, n):
        result[i] = (result[i - 1] * (period - 1) + tr[i]) / period
    return result


@dataclass(frozen=True)
class BollingerBands:
    """SRS Part 9 volatility-band scoring input."""

    upper: list[float]
    middle: list[float]
    lower: list[float]


def bollinger_bands(close_values: list[float], period: int = 20, num_std: float = 2.0) -> BollingerBands:
    """Middle band = SMA(period); upper/lower = middle +/- num_std * rolling population std-dev."""
    middle = sma(close_values, period)
    n = len(close_values)
    upper = [NAN] * n
    lower = [NAN] * n
    for i in range(period - 1, n):
        window = close_values[i - period + 1 : i + 1]
        mean = middle[i]
        variance = sum((v - mean) ** 2 for v in window) / period
        std_dev = math.sqrt(variance)
        upper[i] = mean + num_std * std_dev
        lower[i] = mean - num_std * std_dev
    return BollingerBands(upper=upper, middle=middle, lower=lower)


# ─────────────────────────────────────────────────────────────────────────
# TREND STRENGTH  (SRS: existing bot history includes "ADX filtering")
# ─────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ADXResult:
    """+DI/-DI/ADX share one index space with the input candle list."""

    plus_di: list[float]
    minus_di: list[float]
    adx: list[float]


def adx(candles: list[Candle], period: int = 14) -> ADXResult:
    """
    Wilder's Average Directional Index, +DI, and -DI.

    `adx[i]` is NaN until index `2 * period - 1`: DX itself isn't defined
    before index `period` (the smoothed TR/+DM/-DM warm-up), and ADX is
    then the plain average of the first `period` DX values -- the
    standard "needs roughly double the period" warm-up documented for
    this indicator on every major charting platform.
    """
    n = len(candles)
    if n == 0:
        return ADXResult(plus_di=[], minus_di=[], adx=[])

    tr = true_range(candles)
    plus_dm = [0.0] * n
    minus_dm = [0.0] * n
    for i in range(1, n):
        up_move = candles[i].high - candles[i - 1].high
        down_move = candles[i - 1].low - candles[i].low
        if up_move > down_move and up_move > 0:
            plus_dm[i] = up_move
        if down_move > up_move and down_move > 0:
            minus_dm[i] = down_move

    smoothed_tr = _wilder_sum_smooth(tr, period)
    smoothed_plus_dm = _wilder_sum_smooth(plus_dm, period)
    smoothed_minus_dm = _wilder_sum_smooth(minus_dm, period)

    plus_di = [NAN] * n
    minus_di = [NAN] * n
    dx = [NAN] * n
    for i in range(period, n):
        if math.isnan(smoothed_tr[i]) or smoothed_tr[i] == 0:
            continue
        plus_di[i] = 100.0 * smoothed_plus_dm[i] / smoothed_tr[i]
        minus_di[i] = 100.0 * smoothed_minus_dm[i] / smoothed_tr[i]
        di_sum = plus_di[i] + minus_di[i]
        dx[i] = 0.0 if di_sum == 0 else 100.0 * abs(plus_di[i] - minus_di[i]) / di_sum

    adx_values = [NAN] * n
    first_adx_index = period * 2 - 1
    if first_adx_index < n:
        window = dx[period : first_adx_index + 1]
        if all(not math.isnan(v) for v in window):
            adx_values[first_adx_index] = sum(window) / period
            for i in range(first_adx_index + 1, n):
                adx_values[i] = (adx_values[i - 1] * (period - 1) + dx[i]) / period

    return ADXResult(plus_di=plus_di, minus_di=minus_di, adx=adx_values)


def _wilder_sum_smooth(values: list[float], period: int) -> list[float]:
    """
    Wilder's *sum* smoothing (used for TR/+DM/-DM inside `adx()` --
    distinct from the *average* smoothing `atr()` uses directly): the
    first smoothed value (index `period`) is a plain sum over the first
    `period` values starting at index 1 (index 0 has no directional
    movement to measure); each later value is `prev - prev/period + current`.
    """
    n = len(values)
    result = [NAN] * n
    if n <= period:
        return result
    result[period] = sum(values[1 : period + 1])
    for i in range(period + 1, n):
        result[i] = result[i - 1] - (result[i - 1] / period) + values[i]
    return result


# ─────────────────────────────────────────────────────────────────────────
# VOLUME  (existing bot history: "pump/dump filters")
# ─────────────────────────────────────────────────────────────────────────


def volume_ratio(candles: list[Candle], period: int = 20) -> list[float]:
    """Current volume / trailing SMA(period) of volume. >1 means above-average volume."""
    vols = volumes(candles)
    average = sma(vols, period)
    return [
        (v / a) if (not math.isnan(a) and a > 0) else NAN for v, a in zip(vols, average)
    ]


# ─────────────────────────────────────────────────────────────────────────
# MARKET STRUCTURE INPUT  (SRS Part 5: swing points feed BOS/CHoCH detection)
# ─────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SwingPoints:
    """Indices into the original candle list -- input to the future Market Structure Engine's BOS/CHoCH detection."""

    swing_high_indices: tuple[int, ...]
    swing_low_indices: tuple[int, ...]


def swing_points(candles: list[Candle], lookback: int = 2) -> SwingPoints:
    """
    A candle at index i is a swing high if its high is strictly greater
    than every other candle's high within `lookback` positions on BOTH
    sides (symmetrically for swing lows). No index within `lookback` of
    either end of the list can be evaluated and is skipped.
    """
    n = len(candles)
    swing_highs: list[int] = []
    swing_lows: list[int] = []
    for i in range(lookback, n - lookback):
        neighbor_highs = [candles[j].high for j in range(i - lookback, i + lookback + 1) if j != i]
        neighbor_lows = [candles[j].low for j in range(i - lookback, i + lookback + 1) if j != i]
        if all(candles[i].high > h for h in neighbor_highs):
            swing_highs.append(i)
        if all(candles[i].low < low for low in neighbor_lows):
            swing_lows.append(i)
    return SwingPoints(swing_high_indices=tuple(swing_highs), swing_low_indices=tuple(swing_lows))


def recent_price_change_pct(candles: list[Candle], lookback_periods: int) -> Optional[float]:
    """
    Signed percent price change from `lookback_periods` candles ago to
    the most recent close (e.g. `lookback_periods=14` on 15m candles ==
    the same 3.5-hour window `atr()`'s default period covers). Positive
    means price rose over the window, negative means it fell -- sign is
    preserved deliberately so a caller (see
    `RiskManagementEngine.assess()`'s chase-prevention guard) can tell a
    chase (price already ran the SAME direction as a new signal) apart
    from a reversal (price ran the OPPOSITE direction), which this
    platform treats very differently.

    Pure function, no ATR/config knowledge of its own -- returns a plain
    percent change; converting that into "how many ATRs is this" is the
    caller's job (it already has the current ATR reading).

    Returns None if there are fewer than `lookback_periods + 1` candles
    (not enough history for the requested window) or if the reference
    close is exactly 0.0 (division would be meaningless).
    """
    if len(candles) <= lookback_periods:
        return None
    reference_close = candles[-(lookback_periods + 1)].close
    if reference_close == 0:
        return None
    latest_close = candles[-1].close
    return (latest_close - reference_close) / reference_close * 100.0
