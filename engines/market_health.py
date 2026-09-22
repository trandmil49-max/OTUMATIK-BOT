"""
engines/market_health.py

Global Market Health Engine (SRS Part 8 GLOBAL MARKET HEALTH -- distinct
from Bitcoin Intelligence: this looks at breadth, liquidity, funding, and
open interest ACROSS THE WHOLE ELIGIBLE MARKET, not just BTCUSDT).

Sampling note: breadth, liquidity, and volatility are computed from ONE
bulk `get_ticker_24hr()` call (weight 40, covers every symbol) -- cheap
and comprehensive. Funding rate, open interest, and spread have no bulk
endpoint on this platform's Binance client, so they are sampled from the
TOP `_SAMPLE_SIZE` symbols by 24h quote volume (already the most liquid,
most representative slice of the market) rather than surveying every
eligible symbol individually, which would cost several hundred units of
request weight for metrics that are each only one of five inputs into a
single aggregate score. This is a deliberate, documented cost/accuracy
tradeoff -- see `_sample_funding_oi_spread()`.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Protocol

from config.loader import get_config
from config.schema import PlatformConfig
from core.models import MarketStatisticsSnapshot
from infrastructure.binance.models import BookTicker, FundingRate, OpenInterest, Ticker24hr
from infrastructure.database.repositories.market_repository import MarketStatisticsRepository
from system.exceptions import DataValidationError
from system.logging_setup import get_logger

_logger = get_logger("trading")


class _SupportsMarketData(Protocol):
    """Structural subset of `BinanceFuturesClient` this engine needs -- keeps tests decoupled from aiohttp."""

    async def get_ticker_24hr(self, symbol: Optional[str] = None) -> list[Ticker24hr]: ...
    async def get_funding_rate(self, symbol: str) -> Optional[FundingRate]: ...
    async def get_open_interest(self, symbol: str) -> Optional[OpenInterest]: ...
    async def get_book_ticker(self, symbol: str) -> Optional[BookTicker]: ...


@dataclass(frozen=True)
class _SampledMetrics:
    """Averaged funding/open-interest/spread across the sampled top-volume symbol set."""

    average_funding_rate: float
    total_open_interest_usdt: float
    average_spread_percent: float
    symbols_sampled: int


@dataclass(frozen=True)
class _HealthComponents:
    """The five normalized (0-100) sub-scores that blend into `market_health_score`, kept for transparency/logging."""

    health_score: float
    trend_component: float
    liquidity_component: float
    spread_component: float
    funding_component: float
    open_interest_component: float


class MarketHealthEngine:
    """Aggregates market-wide breadth, liquidity, funding, open interest, and spread into one health snapshot."""

    _SAMPLE_SIZE = 20
    # Breadth band treated as "no clear direction" -- neither bulls nor
    # bears control a meaningful majority of the eligible symbol universe.
    _BREADTH_CHOPPY_LOWER = 40.0
    _BREADTH_CHOPPY_UPPER = 60.0
    # Normalizes raw 24h (high-low)/close percent into a 0-100 magnitude:
    # a 10% daily range (already a very volatile day for most futures pairs
    # in aggregate) saturates the score at 100.
    _VOLATILITY_SCORE_SCALE = 10.0
    # abs(funding_rate) expressed as a percent (0.0001 decimal -> 0.01
    # percent) times this scale; ~0.33% average funding fully saturates
    # the penalty -- funding rates that extreme indicate one-sided,
    # over-leveraged positioning market-wide.
    _FUNDING_RATE_HEALTH_SCALE = 300.0
    # Percent change in total open interest since the previous snapshot;
    # a 50% swing in aggregate open interest between checks fully
    # saturates the penalty (rapid, market-wide leverage buildup/unwind).
    _OPEN_INTEREST_CHANGE_HEALTH_SCALE = 2.0

    def __init__(
        self,
        client: _SupportsMarketData,
        config: Optional[PlatformConfig] = None,
        repository: Optional[MarketStatisticsRepository] = None,
    ) -> None:
        self._client = client
        self._config = config or get_config()
        self._repository = repository or MarketStatisticsRepository()

    async def analyze(self, eligible_symbols: Optional[list[str]] = None) -> MarketStatisticsSnapshot:
        """
        Compute and persist one market-wide health snapshot.

        Args:
            eligible_symbols: restrict breadth/liquidity/trend statistics
                to this symbol set -- typically
                `SymbolDiscoveryEngine.get_eligible_symbols()`. If None,
                every symbol the bulk ticker call returns is used.

        Raises:
            DataValidationError: the bulk ticker call (after filtering to
                `eligible_symbols`, if given) returned no usable rows --
                there is nothing to aggregate.
        """
        all_tickers = await self._client.get_ticker_24hr()
        if eligible_symbols is not None:
            eligible_set = set(eligible_symbols)
            tickers = [t for t in all_tickers if t.symbol in eligible_set]
        else:
            tickers = all_tickers

        if not tickers:
            raise DataValidationError(
                "No ticker data available to compute market health",
                context={"eligible_symbols_requested": len(eligible_symbols) if eligible_symbols else 0},
            )

        pct_positive = self._breadth_percent_positive(tickers)
        average_volatility_percent = self._average_volatility_percent(tickers)
        average_liquidity = sum(t.quote_volume for t in tickers) / len(tickers)

        sampled = await self._sample_funding_oi_spread(tickers)
        previous_snapshot = self._repository.get_latest()

        components = self._compute_health_score(
            pct_positive=pct_positive,
            average_liquidity=average_liquidity,
            average_spread_percent=sampled.average_spread_percent,
            average_funding_rate=sampled.average_funding_rate,
            total_open_interest_usdt=sampled.total_open_interest_usdt,
            previous_snapshot=previous_snapshot,
        )
        market_state = self._classify_market_state(
            pct_positive, components.health_score, self._config.market_health.health_threshold
        )
        volatility_score = min(100.0, average_volatility_percent * self._VOLATILITY_SCORE_SCALE)

        snapshot = MarketStatisticsSnapshot(
            snapshot_time=datetime.now(timezone.utc),
            market_health_score=round(components.health_score, 2),
            market_state=market_state,
            average_liquidity_score=round(components.liquidity_component, 2),
            average_volatility_score=round(volatility_score, 2),
            trend_quality_score=round(components.trend_component, 2),
            average_spread_percent=round(sampled.average_spread_percent, 4),
            average_funding_rate=round(sampled.average_funding_rate, 6),
            total_open_interest_usdt=round(sampled.total_open_interest_usdt, 2),
        )
        self._repository.create(snapshot)

        if components.health_score < self._config.market_health.health_threshold:
            _logger.warning(
                "Market health score %.1f is below the configured threshold %.1f (state=%s)",
                components.health_score, self._config.market_health.health_threshold, market_state,
            )

        return snapshot

    def get_current_snapshot(self) -> Optional[MarketStatisticsSnapshot]:
        """Read the most recent persisted snapshot without any API call."""
        return self._repository.get_latest()

    async def _sample_funding_oi_spread(self, tickers: list[Ticker24hr]) -> _SampledMetrics:
        """Fetch funding/open-interest/book-ticker for the top `_SAMPLE_SIZE` symbols by 24h quote volume."""
        top_symbols = [
            t.symbol for t in sorted(tickers, key=lambda t: t.quote_volume, reverse=True)[: self._SAMPLE_SIZE]
        ]
        semaphore = asyncio.Semaphore(self._config.scanner.max_concurrent_workers)

        async def fetch_one(symbol: str) -> tuple[Optional[FundingRate], Optional[OpenInterest], Optional[BookTicker]]:
            async with semaphore:
                return await asyncio.gather(
                    self._client.get_funding_rate(symbol),
                    self._client.get_open_interest(symbol),
                    self._client.get_book_ticker(symbol),
                )

        results = await asyncio.gather(*(fetch_one(symbol) for symbol in top_symbols))

        funding_rates: list[float] = []
        open_interest_usdt_values: list[float] = []
        spread_percents: list[float] = []
        for funding, open_interest, book in results:
            if funding is not None:
                funding_rates.append(funding.funding_rate)
                if open_interest is not None:
                    open_interest_usdt_values.append(open_interest.open_interest * funding.mark_price)
            if book is not None:
                spread_percents.append(book.spread_percent)

        return _SampledMetrics(
            average_funding_rate=(sum(funding_rates) / len(funding_rates)) if funding_rates else 0.0,
            total_open_interest_usdt=sum(open_interest_usdt_values),
            average_spread_percent=(sum(spread_percents) / len(spread_percents)) if spread_percents else 0.0,
            symbols_sampled=len(top_symbols),
        )

    def _compute_health_score(
        self,
        *,
        pct_positive: float,
        average_liquidity: float,
        average_spread_percent: float,
        average_funding_rate: float,
        total_open_interest_usdt: float,
        previous_snapshot: Optional[MarketStatisticsSnapshot],
    ) -> _HealthComponents:
        cfg = self._config.market_health
        scanner_cfg = self._config.scanner

        trend_component = abs(pct_positive - 50.0) * 2.0  # 0 at an even 50/50 split, 100 at unanimous direction

        liquidity_component = 100.0
        if scanner_cfg.min_24h_quote_volume_usdt > 0:
            liquidity_component = min(100.0, (average_liquidity / scanner_cfg.min_24h_quote_volume_usdt) * 50.0)

        spread_component = 100.0
        if scanner_cfg.max_spread_pct > 0:
            spread_component = max(0.0, 100.0 - (average_spread_percent / scanner_cfg.max_spread_pct) * 100.0)

        funding_rate_percent = abs(average_funding_rate) * 100.0
        funding_component = max(0.0, 100.0 - funding_rate_percent * self._FUNDING_RATE_HEALTH_SCALE)

        if previous_snapshot is not None and previous_snapshot.total_open_interest_usdt:
            oi_change_percent = (
                (total_open_interest_usdt - previous_snapshot.total_open_interest_usdt)
                / previous_snapshot.total_open_interest_usdt
                * 100.0
            )
            open_interest_component = max(
                0.0, 100.0 - abs(oi_change_percent) * self._OPEN_INTEREST_CHANGE_HEALTH_SCALE
            )
        else:
            open_interest_component = 50.0  # no prior snapshot yet to judge a rate of change against

        health_score = (
            trend_component * cfg.trend_weight
            + liquidity_component * cfg.liquidity_weight
            + spread_component * cfg.spread_weight
            + funding_component * cfg.funding_weight
            + open_interest_component * cfg.open_interest_weight
        )
        return _HealthComponents(
            health_score=health_score,
            trend_component=trend_component,
            liquidity_component=liquidity_component,
            spread_component=spread_component,
            funding_component=funding_component,
            open_interest_component=open_interest_component,
        )

    @staticmethod
    def _breadth_percent_positive(tickers: list[Ticker24hr]) -> float:
        """Percent of `tickers` with a positive 24h price change -- market-wide directional breadth."""
        positive_count = sum(1 for t in tickers if t.price_change_percent > 0)
        return (positive_count / len(tickers)) * 100.0

    @staticmethod
    def _average_volatility_percent(tickers: list[Ticker24hr]) -> float:
        """Mean of each symbol's 24h (high-low)/last_price percent -- a cheap, bulk-call volatility proxy."""
        ranges = [((t.high_price - t.low_price) / t.last_price) * 100.0 for t in tickers if t.last_price > 0]
        return (sum(ranges) / len(ranges)) if ranges else 0.0

    @classmethod
    def _classify_market_state(cls, pct_positive: float, health_score: float, health_threshold: float) -> str:
        """Combine directional breadth with the health score into one label, e.g. `HEALTHY_BULL_MARKET`."""
        if cls._BREADTH_CHOPPY_LOWER <= pct_positive <= cls._BREADTH_CHOPPY_UPPER:
            return "CHOPPY_MARKET"
        direction = "BULL" if pct_positive > cls._BREADTH_CHOPPY_UPPER else "BEAR"
        healthiness = "HEALTHY" if health_score >= health_threshold else "UNHEALTHY"
        return f"{healthiness}_{direction}_MARKET"
