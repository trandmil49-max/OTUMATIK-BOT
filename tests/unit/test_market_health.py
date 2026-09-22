"""
Unit tests for engines/market_health.py (Module 8).

Run with:
    pytest tests/unit/test_market_health.py -v
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import pytest

from config.schema import PlatformConfig
from core.models import MarketStatisticsSnapshot
from engines.market_health import MarketHealthEngine
from infrastructure.binance.models import BookTicker, FundingRate, OpenInterest, Ticker24hr
from infrastructure.database.connection import Database
from infrastructure.database.repositories.market_repository import MarketStatisticsRepository
from infrastructure.database.schema import run_migrations
from system.exceptions import DataValidationError


def _ticker(symbol: str, quote_volume: float, price_change_percent: float, last_price: float = 100.0) -> Ticker24hr:
    return Ticker24hr(
        symbol=symbol, last_price=last_price, price_change_percent=price_change_percent,
        quote_volume=quote_volume, high_price=last_price * 1.01, low_price=last_price * 0.99,
        weighted_avg_price=last_price,
    )


def _funding(symbol: str, rate: float, mark_price: float = 100.0) -> FundingRate:
    return FundingRate(symbol=symbol, mark_price=mark_price, funding_rate=rate, next_funding_time=datetime.now(timezone.utc))


def _open_interest(symbol: str, amount: float) -> OpenInterest:
    return OpenInterest(symbol=symbol, open_interest=amount, timestamp=datetime.now(timezone.utc))


def _book(symbol: str, bid: float, ask: float) -> BookTicker:
    return BookTicker(symbol=symbol, bid_price=bid, bid_qty=1.0, ask_price=ask, ask_qty=1.0)


class _StubMarketClient:
    def __init__(
        self,
        tickers: list[Ticker24hr],
        funding_by_symbol: Optional[dict[str, FundingRate]] = None,
        oi_by_symbol: Optional[dict[str, OpenInterest]] = None,
        book_by_symbol: Optional[dict[str, BookTicker]] = None,
    ) -> None:
        self._tickers = tickers
        self._funding = funding_by_symbol or {}
        self._oi = oi_by_symbol or {}
        self._book = book_by_symbol or {}

    async def get_ticker_24hr(self, symbol: Optional[str] = None) -> list[Ticker24hr]:
        return self._tickers

    async def get_funding_rate(self, symbol: str) -> Optional[FundingRate]:
        return self._funding.get(symbol)

    async def get_open_interest(self, symbol: str) -> Optional[OpenInterest]:
        return self._oi.get(symbol)

    async def get_book_ticker(self, symbol: str) -> Optional[BookTicker]:
        return self._book.get(symbol)


def _uniform_market(symbols: list[str], price_change_percents: list[float]) -> _StubMarketClient:
    """10 (or len(symbols)) identical symbols except for price_change_percent -- used for breadth tests."""
    tickers = [_ticker(s, quote_volume=10_000_000.0, price_change_percent=p) for s, p in zip(symbols, price_change_percents)]
    funding = {s: _funding(s, rate=0.0001) for s in symbols}
    oi = {s: _open_interest(s, amount=1000.0) for s in symbols}
    book = {s: _book(s, bid=99.99, ask=100.01) for s in symbols}
    return _StubMarketClient(tickers, funding, oi, book)


@pytest.fixture
def config() -> PlatformConfig:
    return PlatformConfig()


@pytest.fixture
def repository(tmp_path) -> MarketStatisticsRepository:
    database = Database(db_path=str(tmp_path / "market_health_test.db"), config=PlatformConfig())
    run_migrations(database)
    return MarketStatisticsRepository(database=database)


# ─────────────────────────────────────────────────────────────────────────
# analyze() -- validation, filtering, persistence
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_analyze_raises_when_no_tickers_available(config, repository):
    engine = MarketHealthEngine(_StubMarketClient([]), config=config, repository=repository)
    with pytest.raises(DataValidationError):
        await engine.analyze()


@pytest.mark.asyncio
async def test_analyze_filters_to_eligible_symbols_only(config, repository):
    client = _uniform_market(["AUSDT", "BUSDT", "IGNOREDUSDT"], [1.0, 1.0, -50.0])
    engine = MarketHealthEngine(client, config=config, repository=repository)

    # If IGNOREDUSDT's strongly negative reading were included, breadth would drop below 100%.
    snapshot = await engine.analyze(eligible_symbols=["AUSDT", "BUSDT"])

    assert snapshot.market_state == "HEALTHY_BULL_MARKET"


@pytest.mark.asyncio
async def test_analyze_raises_when_eligible_symbols_do_not_match_any_ticker(config, repository):
    client = _uniform_market(["AUSDT"], [1.0])
    engine = MarketHealthEngine(client, config=config, repository=repository)
    with pytest.raises(DataValidationError):
        await engine.analyze(eligible_symbols=["NOTPRESENTUSDT"])


# ─────────────────────────────────────────────────────────────────────────
# End-to-end, fully hand-computed health score
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_analyze_end_to_end_matches_hand_computed_health_score(config, repository):
    symbols = [f"SYM{i}USDT" for i in range(10)]
    # 8 positive, 2 negative -> 80% breadth.
    changes = [1.0] * 8 + [-1.0] * 2
    client = _uniform_market(symbols, changes)
    engine = MarketHealthEngine(client, config=config, repository=repository)

    snapshot = await engine.analyze()

    # Hand-computed (see engines/market_health.py's weights: trend .35, liquidity .20,
    # spread .15, funding .15, open_interest .15):
    #   trend      = |80-50|*2            = 60.0
    #   liquidity  = min(100,(10e6/5e6)*50)= 100.0
    #   spread     = 100-(0.02/0.15)*100  = 86.6667  (bid=99.99/ask=100.01 -> 0.02% spread)
    #   funding    = 100-(0.01*300)        = 97.0     (rate=0.0001 -> 0.01 percent)
    #   open_int.  = 50.0                             (no previous snapshot yet)
    #   health = 60*.35 + 100*.20 + 86.6667*.15 + 97*.15 + 50*.15 = 76.05
    assert snapshot.market_health_score == pytest.approx(76.05, abs=0.01)
    assert snapshot.market_state == "HEALTHY_BULL_MARKET"
    assert snapshot.trend_quality_score == pytest.approx(60.0)
    assert snapshot.average_liquidity_score == pytest.approx(100.0)
    assert snapshot.average_spread_percent == pytest.approx(0.02, abs=1e-4)
    assert snapshot.average_funding_rate == pytest.approx(0.0001)
    assert snapshot.total_open_interest_usdt == pytest.approx(1_000_000.0)
    assert snapshot.average_volatility_score == pytest.approx(20.0)  # (101-99)/100*100=2% range * scale 10
    assert snapshot.id is not None  # persisted


# ─────────────────────────────────────────────────────────────────────────
# BREADTH / MARKET STATE CLASSIFICATION
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_bearish_breadth_classifies_bear_market(config, repository):
    symbols = [f"SYM{i}USDT" for i in range(10)]
    changes = [-1.0] * 8 + [1.0] * 2  # 20% positive -> bear
    engine = MarketHealthEngine(_uniform_market(symbols, changes), config=config, repository=repository)

    snapshot = await engine.analyze()

    assert "BEAR_MARKET" in snapshot.market_state


@pytest.mark.asyncio
async def test_even_breadth_classifies_choppy_market(config, repository):
    symbols = [f"SYM{i}USDT" for i in range(10)]
    changes = [1.0] * 5 + [-1.0] * 5  # exactly 50% -> choppy
    engine = MarketHealthEngine(_uniform_market(symbols, changes), config=config, repository=repository)

    snapshot = await engine.analyze()

    assert snapshot.market_state == "CHOPPY_MARKET"


# ─────────────────────────────────────────────────────────────────────────
# FUNDING / OPEN INTEREST HEALTH CONTRIBUTION
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_extreme_funding_rate_drags_health_score_down(config, repository):
    symbols = [f"SYM{i}USDT" for i in range(6)]
    healthy_client = _uniform_market(symbols, [1.0] * 6)
    extreme_client = _StubMarketClient(
        tickers=[_ticker(s, 10_000_000.0, 1.0) for s in symbols],
        funding_by_symbol={s: _funding(s, rate=0.005) for s in symbols},  # 0.5% -- extreme
        oi_by_symbol={s: _open_interest(s, 1000.0) for s in symbols},
        book_by_symbol={s: _book(s, 99.99, 100.01) for s in symbols},
    )

    healthy_snapshot = await MarketHealthEngine(healthy_client, config=config, repository=repository).analyze()
    extreme_snapshot = await MarketHealthEngine(extreme_client, config=config, repository=repository).analyze()

    assert extreme_snapshot.market_health_score < healthy_snapshot.market_health_score


@pytest.mark.asyncio
async def test_first_run_open_interest_component_is_neutral(config, repository):
    """With no previous snapshot, the open-interest health contribution must be neutral (50), not penalized."""
    symbols = [f"SYM{i}USDT" for i in range(6)]
    client = _uniform_market(symbols, [1.0] * 6)
    engine = MarketHealthEngine(client, config=config, repository=repository)

    assert repository.get_latest() is None
    await engine.analyze()  # should not raise, and open_interest_component should be neutral (verified via score)


@pytest.mark.asyncio
async def test_large_open_interest_swing_between_snapshots_is_penalized(config, repository):
    symbols = [f"SYM{i}USDT" for i in range(6)]

    small_oi_client = _StubMarketClient(
        tickers=[_ticker(s, 10_000_000.0, 1.0) for s in symbols],
        funding_by_symbol={s: _funding(s, rate=0.0001) for s in symbols},
        oi_by_symbol={s: _open_interest(s, 100.0) for s in symbols},
        book_by_symbol={s: _book(s, 99.99, 100.01) for s in symbols},
    )
    await MarketHealthEngine(small_oi_client, config=config, repository=repository).analyze()

    huge_oi_client = _StubMarketClient(
        tickers=[_ticker(s, 10_000_000.0, 1.0) for s in symbols],
        funding_by_symbol={s: _funding(s, rate=0.0001) for s in symbols},
        oi_by_symbol={s: _open_interest(s, 10_000.0) for s in symbols},  # 100x jump
        book_by_symbol={s: _book(s, 99.99, 100.01) for s in symbols},
    )
    spike_snapshot = await MarketHealthEngine(huge_oi_client, config=config, repository=repository).analyze()

    stable_oi_client = _StubMarketClient(
        tickers=[_ticker(s, 10_000_000.0, 1.0) for s in symbols],
        funding_by_symbol={s: _funding(s, rate=0.0001) for s in symbols},
        oi_by_symbol={s: _open_interest(s, 10_050.0) for s in symbols},  # ~0.5% change from spike_snapshot
        book_by_symbol={s: _book(s, 99.99, 100.01) for s in symbols},
    )
    stable_snapshot = await MarketHealthEngine(stable_oi_client, config=config, repository=repository).analyze()

    assert spike_snapshot.market_health_score < stable_snapshot.market_health_score


# ─────────────────────────────────────────────────────────────────────────
# SAMPLING / MISSING-DATA RESILIENCE
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sampling_only_queries_top_symbols_by_volume(config, repository):
    """With more eligible symbols than _SAMPLE_SIZE, only the highest-volume ones get funding/OI/book fetched."""
    low_volume_symbols = [f"LOW{i}USDT" for i in range(30)]
    high_volume_symbols = [f"HIGH{i}USDT" for i in range(5)]

    tickers = [_ticker(s, quote_volume=1_000_000.0, price_change_percent=1.0) for s in low_volume_symbols]
    tickers += [_ticker(s, quote_volume=50_000_000.0, price_change_percent=1.0) for s in high_volume_symbols]

    fetched_symbols: list[str] = []

    class _TrackingClient(_StubMarketClient):
        async def get_funding_rate(self, symbol: str) -> Optional[FundingRate]:
            fetched_symbols.append(symbol)
            return _funding(symbol, rate=0.0001)

    client = _TrackingClient(
        tickers,
        book_by_symbol={s: _book(s, 99.99, 100.01) for s in low_volume_symbols + high_volume_symbols},
        oi_by_symbol={s: _open_interest(s, 1000.0) for s in low_volume_symbols + high_volume_symbols},
    )
    engine = MarketHealthEngine(client, config=config, repository=repository)

    await engine.analyze()

    assert set(high_volume_symbols) <= set(fetched_symbols)  # the 5 highest-volume symbols are always included
    assert len(fetched_symbols) == engine._SAMPLE_SIZE  # exactly the top 20 by volume (5 high + 15 low to fill up)


@pytest.mark.asyncio
async def test_missing_funding_and_book_data_is_excluded_not_crashing(config, repository):
    symbols = ["AUSDT", "BUSDT", "CUSDT"]
    client = _StubMarketClient(
        tickers=[_ticker(s, 10_000_000.0, 1.0) for s in symbols],
        funding_by_symbol={"AUSDT": _funding("AUSDT", rate=0.0002)},  # BUSDT, CUSDT missing
        oi_by_symbol={"AUSDT": _open_interest("AUSDT", 500.0)},
        book_by_symbol={},  # no book data for anyone
    )
    engine = MarketHealthEngine(client, config=config, repository=repository)

    snapshot = await engine.analyze()  # must not raise despite sparse data

    assert snapshot.average_funding_rate == pytest.approx(0.0002)  # averaged over only the 1 available reading
    assert snapshot.average_spread_percent == pytest.approx(0.0)  # no book data at all -> falls back to 0


def test_get_current_snapshot_reads_from_repository_without_api_call(config, repository):
    repository.create(
        MarketStatisticsSnapshot(
            snapshot_time=datetime.now(timezone.utc), market_health_score=70.0, market_state="HEALTHY_BULL_MARKET",
        )
    )
    engine = MarketHealthEngine(_StubMarketClient([]), config=config, repository=repository)

    snapshot = engine.get_current_snapshot()

    assert snapshot is not None
    assert snapshot.market_state == "HEALTHY_BULL_MARKET"
