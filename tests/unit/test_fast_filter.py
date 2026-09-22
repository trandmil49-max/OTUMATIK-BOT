"""
Unit tests for engines/fast_filter.py (SRS Part 16 Stage 1).

Run with:
    pytest tests/unit/test_fast_filter.py -v
"""

from __future__ import annotations

from typing import Optional

import pytest

from config.schema import PlatformConfig
from core.models import RejectionReason
from engines.fast_filter import FastFilterEngine
from infrastructure.binance.models import BookTicker, Ticker24hr


def _ticker(symbol: str, quote_volume: float) -> Ticker24hr:
    return Ticker24hr(
        symbol=symbol, last_price=1.0, price_change_percent=0.0,
        quote_volume=quote_volume, high_price=1.0, low_price=1.0, weighted_avg_price=1.0,
    )


def _book(symbol: str, bid: float, ask: float) -> BookTicker:
    return BookTicker(symbol=symbol, bid_price=bid, bid_qty=1.0, ask_price=ask, ask_qty=1.0)


class _StubBinanceClient:
    """Controls exactly what each Stage 1 sub-filter sees, without any real HTTP (Module 4 owns that coverage)."""

    def __init__(
        self,
        tickers: list[Ticker24hr],
        books: Optional[dict[str, BookTicker]] = None,
        candle_counts: Optional[dict[str, int]] = None,
        default_candle_count: int = 200,
    ) -> None:
        self._tickers = tickers
        self._books = books or {}
        self._candle_counts = candle_counts or {}
        self._default_candle_count = default_candle_count

    async def get_ticker_24hr(self, symbol: Optional[str] = None) -> list[Ticker24hr]:
        return self._tickers

    async def get_book_ticker(self, symbol: str) -> Optional[BookTicker]:
        return self._books.get(symbol)

    async def get_klines(self, symbol: str, interval: str, limit: int = 200) -> list[object]:
        count = self._candle_counts.get(symbol, self._default_candle_count)
        return [object()] * count


@pytest.fixture
def config() -> PlatformConfig:
    return PlatformConfig()  # min_24h_quote_volume_usdt=5_000_000, max_spread_pct=0.15, min_history_candles=200


@pytest.mark.asyncio
async def test_run_survivor_passes_all_three_stages(config):
    client = _StubBinanceClient(
        tickers=[_ticker("BTCUSDT", quote_volume=10_000_000)],
        books={"BTCUSDT": _book("BTCUSDT", bid=49990, ask=50010)},  # ~0.04% spread
    )
    result = await FastFilterEngine(client, config=config).run(["BTCUSDT"])

    assert result.surviving_symbols == ("BTCUSDT",)
    assert result.rejections == ()
    assert result.total_evaluated == 1


@pytest.mark.asyncio
async def test_run_rejects_low_volume_symbol_before_any_spread_check(config):
    client = _StubBinanceClient(tickers=[_ticker("LOWVOLUSDT", quote_volume=1_000_000)])
    result = await FastFilterEngine(client, config=config).run(["LOWVOLUSDT"])

    assert result.surviving_symbols == ()
    assert len(result.rejections) == 1
    assert result.rejections[0].reason == RejectionReason.LOW_VOLUME


@pytest.mark.asyncio
async def test_run_rejects_wide_spread_symbol_that_passed_volume(config):
    client = _StubBinanceClient(
        tickers=[_ticker("WIDESPREADUSDT", quote_volume=10_000_000)],
        books={"WIDESPREADUSDT": _book("WIDESPREADUSDT", bid=49000, ask=51000)},  # ~4% spread
    )
    result = await FastFilterEngine(client, config=config).run(["WIDESPREADUSDT"])

    assert result.surviving_symbols == ()
    assert result.rejections[0].reason == RejectionReason.LARGE_SPREAD


@pytest.mark.asyncio
async def test_run_rejects_insufficient_history_symbol_that_passed_volume_and_spread(config):
    client = _StubBinanceClient(
        tickers=[_ticker("NEWUSDT", quote_volume=10_000_000)],
        books={"NEWUSDT": _book("NEWUSDT", bid=1.0, ask=1.001)},
        candle_counts={"NEWUSDT": 50},  # fewer than min_history_candles (200)
    )
    result = await FastFilterEngine(client, config=config).run(["NEWUSDT"])

    assert result.surviving_symbols == ()
    assert result.rejections[0].reason == RejectionReason.INVALID_DATA
    assert "50" in result.rejections[0].detail


@pytest.mark.asyncio
async def test_run_missing_ticker_data_is_rejected_as_invalid_data(config):
    client = _StubBinanceClient(tickers=[])  # requested symbol absent from the bulk ticker response
    result = await FastFilterEngine(client, config=config).run(["GHOSTUSDT"])

    assert result.rejections[0].reason == RejectionReason.INVALID_DATA
    assert result.rejections[0].symbol == "GHOSTUSDT"


@pytest.mark.asyncio
async def test_run_missing_book_ticker_is_rejected_as_invalid_data(config):
    client = _StubBinanceClient(tickers=[_ticker("NOBOOKUSDT", quote_volume=10_000_000)], books={})
    result = await FastFilterEngine(client, config=config).run(["NOBOOKUSDT"])

    assert result.rejections[0].reason == RejectionReason.INVALID_DATA


@pytest.mark.asyncio
async def test_run_evaluates_each_symbol_independently(config):
    client = _StubBinanceClient(
        tickers=[_ticker("GOODUSDT", quote_volume=10_000_000), _ticker("LOWVOLUSDT", quote_volume=100)],
        books={"GOODUSDT": _book("GOODUSDT", bid=99.99, ask=100.01)},
    )
    result = await FastFilterEngine(client, config=config).run(["GOODUSDT", "LOWVOLUSDT"])

    assert result.surviving_symbols == ("GOODUSDT",)
    assert result.total_evaluated == 2


@pytest.mark.asyncio
async def test_rejection_counts_by_reason_rollup(config):
    client = _StubBinanceClient(tickers=[_ticker("A", quote_volume=100), _ticker("B", quote_volume=200)])
    result = await FastFilterEngine(client, config=config).run(["A", "B"])

    assert result.rejection_counts_by_reason() == {RejectionReason.LOW_VOLUME.value: 2}
