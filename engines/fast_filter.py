"""
engines/fast_filter.py

Fast Filter Engine -- SRS Part 16 STAGE 1: ULTRA FAST FILTER ("Eliminate
70-90% of coins ... using only ticker/volume data ... Very Fast").

Applied in increasing order of per-symbol cost, so each stage only ever
runs against symbols that survived the previous, cheaper one (SRS Part 6:
"Cache repeated data. Do not query the same data twice."):

    1. 24h quote volume (`ScannerConfig.min_24h_quote_volume_usdt`) --
       ONE bulk `get_ticker_24hr()` call covers the entire market.
    2. Bid/ask spread (`ScannerConfig.max_spread_pct`) -- one
       `get_book_ticker()` call per volume-survivor only.
    3. History length (`ScannerConfig.min_history_candles`) -- one
       `get_klines()` call per volume+spread-survivor only.

Rejections are returned as lightweight, in-memory `FastFilterRejection`
records rather than written to Module 3's `rejections` table directly:
Stage 1 runs every `fast_scan_interval_seconds` against the *entire*
market (SRS Part 21 Rule 2: "scan the entire eligible market"), so most
symbols are rejected here on almost every cycle. Persisting one DB row
per symbol per cycle would flood `rejections` with low-value volume/spread
noise and contradicts Part 12's "the database should be lightweight."
The future Scanner Orchestrator (which calls this engine every cycle) is
responsible for aggregating `FastFilterResult.rejections` into rollup
counts if/when it chooses to persist a summary; Part 14's rich
per-signal `Rejection` record (with confidence/risk/coin-trust scores)
only makes sense for symbols that make it *past* Stage 1 and into actual
signal evaluation.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Optional

from config.loader import get_config
from config.schema import PlatformConfig
from core.models import RejectionReason
from infrastructure.binance.client import BinanceFuturesClient
from system.logging_setup import get_logger

_logger = get_logger("trading")

# Coarsest interval that still yields one candle per day -- cheap way to
# probe how much history a symbol has without pulling a large payload.
_HISTORY_PROBE_INTERVAL = "1d"


@dataclass(frozen=True)
class FastFilterRejection:
    """One symbol eliminated during Stage 1, with the specific reason and a human-readable detail."""

    symbol: str
    reason: RejectionReason
    detail: str


@dataclass(frozen=True)
class FastFilterResult:
    """Outcome of one Stage 1 pass across a candidate symbol universe."""

    surviving_symbols: tuple[str, ...]
    rejections: tuple[FastFilterRejection, ...]

    @property
    def total_evaluated(self) -> int:
        return len(self.surviving_symbols) + len(self.rejections)

    def rejection_counts_by_reason(self) -> dict[str, int]:
        """Rollup used by callers that persist a summary instead of one row per symbol."""
        counts: dict[str, int] = {}
        for rejection in self.rejections:
            counts[rejection.reason.value] = counts.get(rejection.reason.value, 0) + 1
        return counts


class FastFilterEngine:
    """Runs the three-stage cheap-to-expensive Stage 1 filter described in the module docstring."""

    def __init__(self, client: BinanceFuturesClient, config: Optional[PlatformConfig] = None) -> None:
        self._client = client
        self._config = config or get_config()

    async def run(self, symbols: list[str]) -> FastFilterResult:
        """Run the full Stage 1 pipeline against `symbols` (typically every active symbol from Module 5's discovery sync)."""
        volume_survivors, volume_rejections = await self._filter_by_volume(symbols)
        spread_survivors, spread_rejections = await self._filter_by_spread(volume_survivors)
        history_survivors, history_rejections = await self._filter_by_history(spread_survivors)

        all_rejections = (*volume_rejections, *spread_rejections, *history_rejections)
        _logger.info(
            "Fast filter: %d/%d symbols survived Stage 1 (volume -%d, spread -%d, history -%d)",
            len(history_survivors),
            len(symbols),
            len(volume_rejections),
            len(spread_rejections),
            len(history_rejections),
        )
        return FastFilterResult(surviving_symbols=tuple(history_survivors), rejections=all_rejections)

    async def _filter_by_volume(self, symbols: list[str]) -> tuple[list[str], list[FastFilterRejection]]:
        tickers = await self._client.get_ticker_24hr()  # one bulk call covers the whole market
        ticker_by_symbol = {t.symbol: t for t in tickers}
        min_volume = self._config.scanner.min_24h_quote_volume_usdt

        survivors: list[str] = []
        rejections: list[FastFilterRejection] = []
        for symbol in symbols:
            ticker = ticker_by_symbol.get(symbol)
            if ticker is None:
                rejections.append(
                    FastFilterRejection(symbol, RejectionReason.INVALID_DATA, "No 24hr ticker data returned")
                )
                continue
            if ticker.quote_volume < min_volume:
                rejections.append(
                    FastFilterRejection(
                        symbol,
                        RejectionReason.LOW_VOLUME,
                        f"24h quote volume {ticker.quote_volume:,.0f} < minimum {min_volume:,.0f} USDT",
                    )
                )
                continue
            survivors.append(symbol)
        return survivors, rejections

    async def _filter_by_spread(self, symbols: list[str]) -> tuple[list[str], list[FastFilterRejection]]:
        max_spread_pct = self._config.scanner.max_spread_pct
        semaphore = asyncio.Semaphore(self._config.scanner.max_concurrent_workers)

        async def check_one(symbol: str) -> tuple[str, Optional[FastFilterRejection]]:
            async with semaphore:
                book = await self._client.get_book_ticker(symbol)
            if book is None:
                return symbol, FastFilterRejection(
                    symbol, RejectionReason.INVALID_DATA, "No book ticker data returned"
                )
            if book.spread_percent > max_spread_pct:
                return symbol, FastFilterRejection(
                    symbol,
                    RejectionReason.LARGE_SPREAD,
                    f"Spread {book.spread_percent:.3f}% > maximum {max_spread_pct:.3f}%",
                )
            return symbol, None

        results = await asyncio.gather(*(check_one(s) for s in symbols))
        survivors = [symbol for symbol, rejection in results if rejection is None]
        rejections = [rejection for _, rejection in results if rejection is not None]
        return survivors, rejections

    async def _filter_by_history(self, symbols: list[str]) -> tuple[list[str], list[FastFilterRejection]]:
        min_candles = self._config.scanner.min_history_candles
        semaphore = asyncio.Semaphore(self._config.scanner.max_concurrent_workers)

        async def check_one(symbol: str) -> tuple[str, Optional[FastFilterRejection]]:
            async with semaphore:
                candles = await self._client.get_klines(symbol, _HISTORY_PROBE_INTERVAL, limit=min_candles)
            if len(candles) < min_candles:
                return symbol, FastFilterRejection(
                    symbol,
                    RejectionReason.INVALID_DATA,
                    f"Only {len(candles)} daily candles available, need {min_candles}",
                )
            return symbol, None

        results = await asyncio.gather(*(check_one(s) for s in symbols))
        survivors = [symbol for symbol, rejection in results if rejection is None]
        rejections = [rejection for _, rejection in results if rejection is not None]
        return survivors, rejections
