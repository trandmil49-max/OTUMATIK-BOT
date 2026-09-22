"""
engines/bitcoin_intelligence.py

Bitcoin Intelligence Engine (SRS Part 8): analyzes BTCUSDT to produce the
single Bitcoin trend/health/volatility picture every altcoin signal is
checked against ("Bitcoin dominates market sentiment; if Bitcoin is
unhealthy, reduce or suppress altcoin signals"). In practice this
suppression is gradual, not a hard veto: `score_for_direction()`'s output
becomes one of six inputs `ConfidenceEngine.assess()` blends into
`confidence_score`, so a poor Bitcoin alignment lowers a candidate's
overall score and can cause it to fail `ConfidenceConfig
.minimum_confidence` like any other weak input would. When that happens
it surfaces as the generic `RejectionReason.LOW_CONFIDENCE`, not a
dedicated `BITCOIN_CONFLICT` reason -- no engine in this codebase
currently assigns that enum member (it exists in `core.models
.RejectionReason` for a more granular future breakdown of *why*
confidence was low, same as `WEAK_MOMENTUM`/`WEAK_COIN_TRUST`/etc., which
are equally unassigned today).

Two distinct outputs, deliberately kept separate:
    * `analyze()` produces the OBJECTIVE, direction-agnostic snapshot
      (trend classification, health_score, volatility_score) persisted
      via `BtcStatisticsRepository` -- this is "what is Bitcoin doing
      right now", independent of any candidate trade.
    * `score_for_direction()` produces a DIRECTION-AWARE 0-100 score for
      one candidate LONG/SHORT signal on some OTHER symbol -- this is
      what actually populates `Signal.bitcoin_score`, since "is this
      trade aligned with Bitcoin" is inherently a function of the
      proposed direction, not a single absolute number.

Weighting note: `config.bitcoin.dominance_weight` (SRS Part 19) sat unwired
for a long time -- true BTC market-cap dominance needs a total-crypto-
market-cap data source this platform's own Binance-only market data can't
provide. It is wired now, via `infrastructure.macro.MacroDataClient`
(CoinGecko `/global` for BTC/USDT dominance, Yahoo Finance for DXY --
ported from sinyal_kanali_2's already-validated MacroClient at the
platform owner's explicit request). It does NOT feed `health_score` /
`_blend_health_score` below, which stays exactly what it was (BTC's own
trend + volatility quality) -- dominance measures capital ROTATION
between BTC, alts, and stables, a different question from "is BTC's own
price action healthy", and conflating the two would make health_score
mean something new and untested. Instead it's a soft nudge inside
`score_for_direction()`, the method that already asks "how should THIS
candidate trade on some OTHER symbol be judged given Bitcoin's state" --
exactly the question dominance rotation answers. `dominance_weight`,
`usdt_dominance_weight`, and `dxy_weight` are the point-scale weights for
that nudge; see `config.schema.BitcoinConfig`'s own docstring for the
asymmetric-vs-symmetric distinction ported from sinyal_kanali_2.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, Protocol

from config.loader import get_config
from config.schema import PlatformConfig
from core.models import BtcStatisticsSnapshot, SignalDirection
from engines.indicators import adx, atr, closes, ema
from infrastructure.binance.models import Candle, FundingRate, OpenInterest
from infrastructure.database.repositories.market_repository import BtcStatisticsRepository
from infrastructure.macro.models import DominanceSnapshot, DxySnapshot
from system.exceptions import DataValidationError
from system.logging_setup import get_logger

_logger = get_logger("trading")


class BitcoinTrend(str, Enum):
    """Classification levels this engine assigns to `BtcStatisticsSnapshot.trend`."""

    STRONG_BULLISH = "STRONG_BULLISH"
    BULLISH = "BULLISH"
    NEUTRAL = "NEUTRAL"
    BEARISH = "BEARISH"
    STRONG_BEARISH = "STRONG_BEARISH"


# How strongly each trend classification favors a LONG (SHORT alignment is
# `100 - this value`, kept symmetric by construction rather than a second table).
_TREND_LONG_ALIGNMENT: dict[BitcoinTrend, float] = {
    BitcoinTrend.STRONG_BULLISH: 95.0,
    BitcoinTrend.BULLISH: 75.0,
    BitcoinTrend.NEUTRAL: 50.0,
    BitcoinTrend.BEARISH: 25.0,
    BitcoinTrend.STRONG_BEARISH: 5.0,
}


class _SupportsBitcoinMarketData(Protocol):
    """Structural subset of `BinanceFuturesClient` this engine needs -- keeps tests decoupled from aiohttp."""

    async def get_klines(self, symbol: str, interval: str, limit: int = 200) -> list[Candle]: ...
    async def get_funding_rate(self, symbol: str) -> Optional[FundingRate]: ...
    async def get_open_interest(self, symbol: str) -> Optional[OpenInterest]: ...


class _SupportsMacroData(Protocol):
    """Structural subset of `MacroDataClient` this engine needs -- same decoupling as above."""

    async def get_dominance(self) -> Optional[DominanceSnapshot]: ...
    async def get_dxy_snapshot(self) -> Optional[DxySnapshot]: ...


class BitcoinIntelligenceEngine:
    """Computes and persists Bitcoin's trend/health/volatility state, and scores candidate directions against it."""

    _SYMBOL = "BTCUSDT"
    _TREND_INTERVAL = "1h"
    _EMA_FAST_PERIOD = 20
    _EMA_SLOW_PERIOD = 50
    _ADX_PERIOD = 14
    _ATR_PERIOD = 14
    # Wilder's own published convention: ADX >= 25 signals a trending (vs. choppy) market.
    _ADX_STRONG_TREND_THRESHOLD = 25.0
    # Heuristic scale mapping ATR-as-percent-of-price to a 0-100 volatility
    # penalty: BTC's 1h ATR rarely exceeds ~2-3% even in fast markets, so a
    # 5% reading should already be treated as maximally unhealthy (score 0).
    _VOLATILITY_PENALTY_SCALE = 20.0
    _MIN_CANDLES_REQUIRED = _EMA_SLOW_PERIOD + 2  # +2: need "current" and "previous" classification

    def __init__(
        self,
        client: _SupportsBitcoinMarketData,
        config: Optional[PlatformConfig] = None,
        repository: Optional[BtcStatisticsRepository] = None,
        macro_client: Optional[_SupportsMacroData] = None,
    ) -> None:
        self._client = client
        self._config = config or get_config()
        self._repository = repository or BtcStatisticsRepository()
        # No default instance constructed here (unlike `repository` above):
        # a real MacroDataClient is an aiohttp session that must be entered
        # via `async with` by whoever owns its lifecycle, the same as
        # `client` above -- there's no live scheduler yet to own that (see
        # PROJECT_STATUS.md, Module 22/main.py). `None` here means
        # analyze() simply skips the macro fetch and leaves the snapshot's
        # dominance/DXY fields at None, same graceful-degradation shape as
        # a fetch that fails -- not an error, and score_for_direction()
        # already treats missing dominance/DXY data as "Unknown" (zero
        # adjustment), so nothing downstream breaks either way.
        self._macro_client = macro_client

    async def analyze(self) -> BtcStatisticsSnapshot:
        """
        Fetch fresh BTCUSDT candles/funding/open-interest, compute trend +
        health + volatility, persist the snapshot, and return it.

        Raises:
            DataValidationError: fewer than `_MIN_CANDLES_REQUIRED` candles
                were returned -- analysis must never proceed on partial
                history and silently produce a misleading classification.
        """
        candles = await self._client.get_klines(
            self._SYMBOL, self._TREND_INTERVAL, limit=self._config.scanner.min_history_candles
        )
        if len(candles) < self._MIN_CANDLES_REQUIRED:
            raise DataValidationError(
                "Insufficient BTCUSDT candle history for Bitcoin Intelligence analysis",
                context={"candles_returned": len(candles), "required": self._MIN_CANDLES_REQUIRED},
            )

        funding = await self._client.get_funding_rate(self._SYMBOL)
        open_interest = await self._client.get_open_interest(self._SYMBOL)

        # Fetched before the new snapshot is created below, for two
        # independent reasons: _determine_confirmed_trend's whipsaw guard
        # reads it in its own flipped-trend branch, AND dominance trend
        # classification needs the PREVIOUS scan's raw dominance % to
        # compare against (CoinGecko's /global is a snapshot, not a
        # series -- see infrastructure/macro/models.py's DominanceSnapshot
        # docstring). One read serves both.
        previous_snapshot = self._repository.get_latest()

        dominance: Optional[DominanceSnapshot] = None
        dxy: Optional[DxySnapshot] = None
        if self._macro_client is not None:
            dominance = await self._macro_client.get_dominance()
            dxy = await self._macro_client.get_dxy_snapshot()

        btc_dominance_trend = self._dominance_trend(
            previous_snapshot.btc_dominance_pct if previous_snapshot else None,
            dominance.btc_pct if dominance else None,
            dead_zone=self._config.bitcoin.dominance_dead_zone_pct,
        )
        usdt_dominance_trend = self._dominance_trend(
            previous_snapshot.usdt_dominance_pct if previous_snapshot else None,
            dominance.usdt_pct if dominance else None,
            dead_zone=self._config.bitcoin.dominance_dead_zone_pct,
        )
        dxy_trend_value = self._classify_dxy(dxy.price, dxy.sma20, dxy.sma50) if dxy is not None else "Unknown"

        close_values = closes(candles)
        ema_fast_series = ema(close_values, self._EMA_FAST_PERIOD)
        ema_slow_series = ema(close_values, self._EMA_SLOW_PERIOD)
        adx_result = adx(candles, self._ADX_PERIOD)
        atr_series = atr(candles, self._ATR_PERIOD)

        latest_price = close_values[-1]
        confirmed_trend = self._determine_confirmed_trend(
            close_values, ema_fast_series, ema_slow_series, adx_result.adx, previous_snapshot
        )

        latest_adx = adx_result.adx[-1]
        trend_component = 0.0 if math.isnan(latest_adx) else min(100.0, max(0.0, (latest_adx / 50.0) * 100.0))

        latest_atr = atr_series[-1]
        latest_atr_percent = 0.0 if (math.isnan(latest_atr) or latest_price <= 0) else (latest_atr / latest_price) * 100.0
        volatility_component = max(0.0, 100.0 - latest_atr_percent * self._VOLATILITY_PENALTY_SCALE)

        health_score = self._blend_health_score(trend_component, volatility_component)

        snapshot = BtcStatisticsSnapshot(
            snapshot_time=datetime.now(timezone.utc),
            trend=confirmed_trend.value,
            health_score=round(health_score, 2),
            volatility_score=round(volatility_component, 2),
            price=latest_price,
            funding_rate=funding.funding_rate if funding is not None else None,
            open_interest_usdt=(open_interest.open_interest * latest_price) if open_interest is not None else None,
            btc_dominance_pct=dominance.btc_pct if dominance is not None else None,
            usdt_dominance_pct=dominance.usdt_pct if dominance is not None else None,
            btc_dominance_trend=btc_dominance_trend,
            usdt_dominance_trend=usdt_dominance_trend,
            dxy_trend=dxy_trend_value,
        )
        self._repository.create(snapshot)

        if health_score < self._config.bitcoin.health_threshold:
            _logger.warning(
                "Bitcoin health score %.1f is below the configured threshold %.1f (trend=%s)",
                health_score, self._config.bitcoin.health_threshold, confirmed_trend.value,
            )

        return snapshot

    def score_for_direction(
        self,
        direction: SignalDirection,
        snapshot: Optional[BtcStatisticsSnapshot] = None,
        symbol: Optional[str] = None,
    ) -> float:
        """
        Direction-aware 0-100 score for a candidate LONG/SHORT signal on
        some OTHER symbol, given Bitcoin's current (or provided) state.
        This is the value that belongs in `Signal.bitcoin_score`.

        Blends the trend/direction alignment toward neutral (50) as
        `health_score` drops -- an unhealthy, choppy Bitcoin should not be
        trusted as a confident directional signal in either direction.
        On top of that blend, applies a soft DXY/BTC-dominance/USDT-
        dominance macro nudge (see `_macro_adjustment`) -- pass `symbol`
        so BTC's own candidate signals correctly skip the BTC-dominance
        leg of that nudge (dominance reads rotation BETWEEN BTC and alts,
        which says nothing about BTC's own direction).

        Returns 50.0 (neutral -- never penalizing) if no snapshot exists
        yet, e.g. before the very first `analyze()` call.
        """
        snapshot = snapshot if snapshot is not None else self._repository.get_latest()
        if snapshot is None:
            return 50.0

        trend = BitcoinTrend(snapshot.trend)
        long_alignment = _TREND_LONG_ALIGNMENT[trend]
        base_score = long_alignment if direction == SignalDirection.LONG else (100.0 - long_alignment)

        health_weight = max(0.0, min(1.0, snapshot.health_score / 100.0))
        score = 50.0 + (base_score - 50.0) * health_weight
        score += self._macro_adjustment(direction, symbol, snapshot)
        return max(0.0, min(100.0, score))

    def _macro_adjustment(
        self, direction: SignalDirection, symbol: Optional[str], snapshot: BtcStatisticsSnapshot
    ) -> float:
        """
        Signed point delta from DXY/BTC-dominance/USDT-dominance, ported
        unchanged (weights included) from sinyal_kanali_2's validated
        scoring. `Unknown` trend (data never fetched, or this snapshot
        predates a macro_client being wired in) contributes 0 either way
        -- same "missing data never blocks, just goes quiet" posture as
        Smart Money (engines/smart_money.py).
        """
        bullish = direction == SignalDirection.LONG
        adjustment = 0.0

        # DXY moves OPPOSITE to risk assets: a falling dollar (Bearish DXY)
        # supports LONG, a rising dollar (Bullish DXY) supports SHORT.
        # Symmetric -- conflicting DXY actively penalizes, unlike the two
        # dominance legs below.
        dxy_weight = self._config.bitcoin.dxy_weight
        if snapshot.dxy_trend == ("Bearish" if bullish else "Bullish"):
            adjustment += dxy_weight
        elif snapshot.dxy_trend == ("Bullish" if bullish else "Bearish"):
            adjustment -= dxy_weight

        # BTC dominance: RISING means capital rotating INTO BTC and OUT of
        # altcoins -- bearish for an altcoin LONG, supportive of an altcoin
        # SHORT. Asymmetric (reward-only, matching sinyal_kanali_2): a
        # conflicting reading does not subtract. Excluded entirely for BTC's
        # own candidate signals -- dominance reads rotation between BTC and
        # alts, not BTC's own direction.
        if symbol != "BTCUSDT":
            btc_dom_weight = self._config.bitcoin.dominance_weight
            if bullish and snapshot.btc_dominance_trend == "Falling":
                adjustment += btc_dom_weight
            elif not bullish and snapshot.btc_dominance_trend == "Rising":
                adjustment += btc_dom_weight

        # USDT dominance: RISING means capital moving INTO stablecoins --
        # broad risk-off, bearish for crypto generally (any symbol, BTC
        # included -- unlike BTC dominance above, this is never excluded).
        # Also asymmetric/reward-only.
        usdt_dom_weight = self._config.bitcoin.usdt_dominance_weight
        if bullish and snapshot.usdt_dominance_trend == "Falling":
            adjustment += usdt_dom_weight
        elif not bullish and snapshot.usdt_dominance_trend == "Rising":
            adjustment += usdt_dom_weight

        return adjustment

    @staticmethod
    def classify_status(health_score: float, threshold: float) -> str:
        """
        "Healthy" / "Unhealthy" -- same threshold-comparison pattern as
        `MarketHealthEngine._classify_market_state`, for the reference
        Telegram format's "BTC Status: Healthy" line (Module 11). Static
        and public (unlike `_classify_trend` etc.) because
        `TelegramNotificationEngine` needs to call this without owning a
        whole `BitcoinIntelligenceEngine` instance -- it depends on
        `BtcStatisticsRepository` directly, the same way
        `ReportingEngine` depends on repositories rather than other
        engines.
        """
        return "Healthy" if health_score >= threshold else "Unhealthy"

    @staticmethod
    def confirmed_macro_reasons(
        direction: SignalDirection, snapshot: BtcStatisticsSnapshot, symbol: Optional[str] = None
    ) -> list[str]:
        """
        User-facing checklist lines (Telegram Module 11) for whichever of
        BTC's own trend / DXY / BTC dominance / USDT dominance genuinely
        confirmed this direction -- the exact same conditions
        `_macro_adjustment` scores by, duplicated here deliberately rather
        than having `_macro_adjustment` return them: keeping display
        logic fully separate from the tested scoring path means this
        method can never silently change what actually got scored, and
        vice versa. Deliberately does NOT report anything for the other
        6 confidence categories (Trend/Structure/Risk/Coin Trust/Market
        Health/Smart Money candidate-side signals) -- those aren't this
        engine's data and belong in the notification layer's own
        category-level pass over `get_score_breakdown()` instead, so a
        line here is never a partial, misleadingly-narrower duplicate of
        one already coming from there.
        """
        bullish = direction == SignalDirection.LONG
        trend = BitcoinTrend(snapshot.trend)
        reasons = []

        long_alignment = _TREND_LONG_ALIGNMENT[trend]
        if (bullish and long_alignment > 50.0) or (not bullish and long_alignment < 50.0):
            reasons.append("BTC Trend Confirmed")

        if snapshot.dxy_trend == ("Bearish" if bullish else "Bullish"):
            reasons.append("DXY Confirmed")

        if symbol != "BTCUSDT":
            if (bullish and snapshot.btc_dominance_trend == "Falling") or (
                not bullish and snapshot.btc_dominance_trend == "Rising"
            ):
                reasons.append("BTC Dominance Confirmed")

        if (bullish and snapshot.usdt_dominance_trend == "Falling") or (
            not bullish and snapshot.usdt_dominance_trend == "Rising"
        ):
            reasons.append("USDT Dominance Confirmed")

        return reasons

    def get_current_snapshot(self) -> Optional[BtcStatisticsSnapshot]:
        """Read the most recent persisted snapshot without any API call."""
        return self._repository.get_latest()

    def _determine_confirmed_trend(
        self,
        close_values: list[float],
        ema_fast_series: list[float],
        ema_slow_series: list[float],
        adx_series: list[float],
        previous_snapshot: Optional[BtcStatisticsSnapshot],
    ) -> BitcoinTrend:
        raw_trend_now = self._classify_trend(
            price=close_values[-1], ema_fast=ema_fast_series[-1], ema_slow=ema_slow_series[-1], adx_value=adx_series[-1]
        )
        if not self._config.bitcoin.confirmation_required:
            return raw_trend_now

        raw_trend_previous = self._classify_trend(
            price=close_values[-2], ema_fast=ema_fast_series[-2], ema_slow=ema_slow_series[-2], adx_value=adx_series[-2]
        )
        if raw_trend_now == raw_trend_previous:
            return raw_trend_now

        # Raw classification just flipped -- avoid whipsawing on a single
        # candle by holding the previously CONFIRMED trend until the new
        # reading is itself confirmed on a subsequent call.
        if previous_snapshot is None:
            return raw_trend_now
        return BitcoinTrend(previous_snapshot.trend)

    def _blend_health_score(self, trend_component: float, volatility_component: float) -> float:
        trend_weight = self._config.bitcoin.trend_weight
        volatility_weight = self._config.bitcoin.volatility_weight
        total_weight = trend_weight + volatility_weight  # dominance_weight excluded -- see module docstring
        if total_weight <= 0:
            return 50.0
        return trend_component * (trend_weight / total_weight) + volatility_component * (volatility_weight / total_weight)

    @staticmethod
    def _classify_trend(price: float, ema_fast: float, ema_slow: float, adx_value: float) -> BitcoinTrend:
        if math.isnan(ema_fast) or math.isnan(ema_slow):
            return BitcoinTrend.NEUTRAL
        is_strong = (not math.isnan(adx_value)) and adx_value >= BitcoinIntelligenceEngine._ADX_STRONG_TREND_THRESHOLD

        if price > ema_fast > ema_slow:
            return BitcoinTrend.STRONG_BULLISH if is_strong else BitcoinTrend.BULLISH
        if price < ema_fast < ema_slow:
            return BitcoinTrend.STRONG_BEARISH if is_strong else BitcoinTrend.BEARISH
        return BitcoinTrend.NEUTRAL

    @staticmethod
    def _dominance_trend(previous_pct: Optional[float], current_pct: Optional[float], dead_zone: float) -> str:
        """
        "Rising" / "Falling" / "Flat" / "Unknown", ported unchanged from
        sinyal_kanali_2's dominance trend comparison. CoinGecko's /global
        is a snapshot, not a series, so "trend" only exists by comparing
        this scan's reading against the previous scan's -- `Unknown` when
        either is unavailable (first run ever, or this scan's/last scan's
        fetch failed). `dead_zone` (percentage points) absorbs normal
        noise between consecutive scans so a +/-0.02pp wobble isn't
        reported as a trend flip.
        """
        if previous_pct is None or current_pct is None:
            return "Unknown"
        delta = current_pct - previous_pct
        if delta > dead_zone:
            return "Rising"
        if delta < -dead_zone:
            return "Falling"
        return "Flat"

    @staticmethod
    def _classify_dxy(price: float, sma20: float, sma50: float) -> str:
        """
        "Bullish" / "Bearish" / "Mixed", ported unchanged from
        sinyal_kanali_2's dxy_trend: price stacked above both MAs in order
        is Bullish, stacked below both in order is Bearish, anything else
        (crossed, tangled) is Mixed -- deliberately not "Unknown" here,
        since unlike dominance this needs no previous scan, only a single
        DxySnapshot; `Unknown` is reserved for when the snapshot itself
        couldn't be fetched at all (handled by the caller in analyze()).
        """
        if price < sma20 < sma50:
            return "Bearish"
        if price > sma20 > sma50:
            return "Bullish"
        return "Mixed"
