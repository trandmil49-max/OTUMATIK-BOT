"""
engines/market_structure.py

Market Structure Engine (SRS Part 5): classifies price action into a
bullish/bearish structural bias, detects Break of Structure (BOS) and
Change of Character (CHoCH) events, flags liquidity sweeps, and locates
order blocks and Fair Value Gaps (FVG) -- the price-action inputs the
future Confidence Engine's "Structure" scoring category reads.

Pure, stateless analysis over a `list[Candle]`, mirroring
`engines.indicators`' design (no I/O, no config, fully unit-testable).
Builds directly on `engines.indicators.swing_points()` rather than
re-deriving swing highs/lows -- Module 6 already owns that calculation.

Definitions used here (documented since ICT-style terminology has no
single universally standardized definition):
    BOS (Break of Structure)   -- price closes beyond the most recent
        swing point IN THE DIRECTION of the already-established bias
        (trend continuation).
    CHoCH (Change of Character) -- price closes beyond the most recent
        swing point AGAINST the established bias (potential reversal).
    Order block -- the last opposing-color candle immediately before a
        candle whose close breaks past that candle's own high/low (a
        strong, immediate continuation move).
    Fair Value Gap -- a 3-candle imbalance where candle[i-1]'s high sits
        below candle[i+1]'s low (bullish gap) or candle[i-1]'s low sits
        above candle[i+1]'s high (bearish gap).
    Liquidity sweep -- a candle's wick pierces a prior swing level by no
        more than `sweep_tolerance_percent` and then CLOSES back on the
        other side of it (a brief stop-hunt, not a genuine breakout).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from engines.indicators import swing_points
from infrastructure.binance.models import Candle


class StructureBias(str, Enum):
    """Overall structural bias derived from the sequence of swing highs/lows."""

    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    UNCLEAR = "UNCLEAR"


class StructureEventType(str, Enum):
    BOS_BULLISH = "BOS_BULLISH"
    BOS_BEARISH = "BOS_BEARISH"
    CHOCH_BULLISH = "CHOCH_BULLISH"
    CHOCH_BEARISH = "CHOCH_BEARISH"


@dataclass(frozen=True)
class StructureEvent:
    """A single BOS or CHoCH detected at the most recent candle."""

    event_type: StructureEventType
    broken_level: float
    broken_index: int
    confirming_index: int


@dataclass(frozen=True)
class OrderBlock:
    """The last opposing-color candle before a strong same-direction continuation move."""

    direction: StructureBias
    candle_index: int
    high: float
    low: float


@dataclass(frozen=True)
class FairValueGap:
    """A 3-candle imbalance; `start_index` is the index of the first candle of the pattern."""

    direction: StructureBias
    gap_high: float
    gap_low: float
    start_index: int


@dataclass(frozen=True)
class LiquiditySweep:
    """A brief wick-only piercing of a prior swing level, followed by a close back on the other side."""

    direction: StructureBias
    swept_level: float
    candle_index: int


@dataclass(frozen=True)
class MarketStructureResult:
    """Everything `MarketStructureEngine.analyze()` produces for one candle series."""

    bias: StructureBias
    events: tuple[StructureEvent, ...]
    order_blocks: tuple[OrderBlock, ...]
    fair_value_gaps: tuple[FairValueGap, ...]
    liquidity_sweeps: tuple[LiquiditySweep, ...]
    last_swing_high: Optional[float]
    last_swing_low: Optional[float]

    @property
    def has_bos(self) -> bool:
        return any(e.event_type in (StructureEventType.BOS_BULLISH, StructureEventType.BOS_BEARISH) for e in self.events)

    @property
    def has_choch(self) -> bool:
        return any(
            e.event_type in (StructureEventType.CHOCH_BULLISH, StructureEventType.CHOCH_BEARISH) for e in self.events
        )


class MarketStructureEngine:
    """Analyzes one candle series into a `MarketStructureResult`. Stateless -- safe to share across symbols."""

    def __init__(self, swing_lookback: int = 2, sweep_tolerance_percent: float = 0.1) -> None:
        """
        Args:
            swing_lookback: passed straight through to `engines.indicators.swing_points()`.
            sweep_tolerance_percent: maximum wick overshoot (as a percent
                of the swept level) still counted as a liquidity sweep
                rather than a genuine breakout.
        """
        self._swing_lookback = swing_lookback
        self._sweep_tolerance_percent = sweep_tolerance_percent

    def analyze(self, candles: list[Candle]) -> MarketStructureResult:
        """Run the full structure analysis over `candles` (oldest-first, as `get_klines()` returns)."""
        if len(candles) < 2 * self._swing_lookback + 1:
            return MarketStructureResult(
                bias=StructureBias.UNCLEAR, events=(), order_blocks=(), fair_value_gaps=(),
                liquidity_sweeps=(), last_swing_high=None, last_swing_low=None,
            )

        swings = swing_points(candles, lookback=self._swing_lookback)
        swing_highs = [(i, candles[i].high) for i in swings.swing_high_indices]
        swing_lows = [(i, candles[i].low) for i in swings.swing_low_indices]

        bias = self._determine_bias(swing_highs, swing_lows)
        latest_index = len(candles) - 1
        event = self._detect_latest_event(candles[latest_index].close, swing_highs, swing_lows, bias, latest_index)

        return MarketStructureResult(
            bias=bias,
            events=(event,) if event is not None else (),
            order_blocks=tuple(self._detect_order_blocks(candles)),
            fair_value_gaps=tuple(self._detect_fair_value_gaps(candles)),
            liquidity_sweeps=tuple(self._detect_liquidity_sweeps(candles, swing_highs, swing_lows)),
            last_swing_high=swing_highs[-1][1] if swing_highs else None,
            last_swing_low=swing_lows[-1][1] if swing_lows else None,
        )

    @staticmethod
    def _determine_bias(
        swing_highs: list[tuple[int, float]], swing_lows: list[tuple[int, float]]
    ) -> StructureBias:
        if len(swing_highs) < 2 or len(swing_lows) < 2:
            return StructureBias.UNCLEAR
        higher_highs = swing_highs[-1][1] > swing_highs[-2][1]
        higher_lows = swing_lows[-1][1] > swing_lows[-2][1]
        lower_highs = swing_highs[-1][1] < swing_highs[-2][1]
        lower_lows = swing_lows[-1][1] < swing_lows[-2][1]
        if higher_highs and higher_lows:
            return StructureBias.BULLISH
        if lower_highs and lower_lows:
            return StructureBias.BEARISH
        return StructureBias.UNCLEAR

    @staticmethod
    def _detect_latest_event(
        latest_close: float,
        swing_highs: list[tuple[int, float]],
        swing_lows: list[tuple[int, float]],
        bias: StructureBias,
        latest_index: int,
    ) -> Optional[StructureEvent]:
        last_high = swing_highs[-1] if swing_highs else None
        last_low = swing_lows[-1] if swing_lows else None

        if last_high is not None and latest_index > last_high[0] and latest_close > last_high[1]:
            event_type = (
                StructureEventType.BOS_BULLISH if bias == StructureBias.BULLISH else StructureEventType.CHOCH_BULLISH
            )
            return StructureEvent(
                event_type=event_type, broken_level=last_high[1], broken_index=last_high[0], confirming_index=latest_index
            )

        if last_low is not None and latest_index > last_low[0] and latest_close < last_low[1]:
            event_type = (
                StructureEventType.BOS_BEARISH if bias == StructureBias.BEARISH else StructureEventType.CHOCH_BEARISH
            )
            return StructureEvent(
                event_type=event_type, broken_level=last_low[1], broken_index=last_low[0], confirming_index=latest_index
            )

        return None

    @staticmethod
    def _detect_order_blocks(candles: list[Candle]) -> list[OrderBlock]:
        order_blocks: list[OrderBlock] = []
        for i in range(len(candles) - 1):
            current, following = candles[i], candles[i + 1]
            current_is_down = current.close < current.open
            current_is_up = current.close > current.open
            following_is_up = following.close > following.open
            following_is_down = following.close < following.open

            if current_is_down and following_is_up and following.close > current.high:
                order_blocks.append(OrderBlock(direction=StructureBias.BULLISH, candle_index=i, high=current.high, low=current.low))
            if current_is_up and following_is_down and following.close < current.low:
                order_blocks.append(OrderBlock(direction=StructureBias.BEARISH, candle_index=i, high=current.high, low=current.low))
        return order_blocks

    @staticmethod
    def _detect_fair_value_gaps(candles: list[Candle]) -> list[FairValueGap]:
        gaps: list[FairValueGap] = []
        for i in range(1, len(candles) - 1):
            prev_candle, next_candle = candles[i - 1], candles[i + 1]
            if prev_candle.high < next_candle.low:
                gaps.append(FairValueGap(direction=StructureBias.BULLISH, gap_high=next_candle.low, gap_low=prev_candle.high, start_index=i - 1))
            elif prev_candle.low > next_candle.high:
                gaps.append(FairValueGap(direction=StructureBias.BEARISH, gap_high=prev_candle.low, gap_low=next_candle.high, start_index=i - 1))
        return gaps

    def _detect_liquidity_sweeps(
        self, candles: list[Candle], swing_highs: list[tuple[int, float]], swing_lows: list[tuple[int, float]]
    ) -> list[LiquiditySweep]:
        sweeps: list[LiquiditySweep] = []
        for swing_index, swing_price in swing_highs:
            for i in range(swing_index + 1, len(candles)):
                candle = candles[i]
                if candle.high > swing_price and candle.close < swing_price:
                    overshoot_percent = ((candle.high - swing_price) / swing_price) * 100.0
                    if overshoot_percent <= self._sweep_tolerance_percent:
                        sweeps.append(LiquiditySweep(direction=StructureBias.BEARISH, swept_level=swing_price, candle_index=i))
        for swing_index, swing_price in swing_lows:
            for i in range(swing_index + 1, len(candles)):
                candle = candles[i]
                if candle.low < swing_price and candle.close > swing_price:
                    overshoot_percent = ((swing_price - candle.low) / swing_price) * 100.0
                    if overshoot_percent <= self._sweep_tolerance_percent:
                        sweeps.append(LiquiditySweep(direction=StructureBias.BULLISH, swept_level=swing_price, candle_index=i))
        return sweeps
