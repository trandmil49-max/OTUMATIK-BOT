"""
Unit tests for execution_modes/backtest.py (Module 21).

`replay_signal()` tests use a real, migrated SQLite database (matching
tests/unit/test_database.py's `db`/`seeded_coin` pattern) because it
exercises genuine integration behaviour with the unchanged
`PositionMonitorEngine` -- a fake would just be re-asserting whatever the
fake was told to return, proving nothing about the real reuse this
module's entire point is to demonstrate.

`fetch_historical_candles()` tests mock HTTP via `aioresponses`, matching
tests/unit/test_binance_client.py's convention, so pagination is tested
against the real client rather than a hand-rolled substitute.

Run with:
    pytest tests/unit/test_backtest.py -v
"""

import re
from datetime import datetime, timezone

import pytest
from aioresponses import aioresponses

from config.schema import PlatformConfig
from core.models import Coin, ConfidenceGrade, Signal, SignalDirection, TradeStatus
from engines.position_monitor import PositionMonitorEngine
from execution_modes.backtest import BacktestResult, BacktestRunner, HistoricalMarketDataSource
from infrastructure.binance.client import BinanceFuturesClient
from infrastructure.binance.models import Candle
from infrastructure.database.connection import Database
from infrastructure.database.repositories import (
    CoinRepository,
    CoinStatisticsRepository,
    SignalRepository,
    TradeRepository,
)
from infrastructure.database.schema import run_migrations

BASE = "https://fapi.binance.com"


def _url_pattern(path: str) -> re.Pattern:
    return re.compile(rf"^{re.escape(BASE + path)}.*$")


def _raw_row(open_time_ms: int, close_time_ms: int, *, open_: float, high: float, low: float, close: float) -> list:
    """Binance's raw kline row format, for mocking HTTP responses."""
    return [
        open_time_ms, f"{open_:.2f}", f"{high:.2f}", f"{low:.2f}", f"{close:.2f}",
        "100.0", close_time_ms, "1000.0", 10, "50.0", "500.0", "0",
    ]


def _candle(open_time_ms: int, close_time_ms: int, *, close: float, high: float, low: float) -> Candle:
    """A real Candle object built directly, for tests that don't go through HTTP parsing."""
    return Candle(
        open_time=datetime.fromtimestamp(open_time_ms / 1000, tz=timezone.utc),
        open=close, high=high, low=low, close=close, volume=100.0,
        close_time=datetime.fromtimestamp(close_time_ms / 1000, tz=timezone.utc),
        quote_volume=1000.0, num_trades=10, taker_buy_base_volume=50.0, taker_buy_quote_volume=500.0,
    )


# ── fixtures (mirrors tests/unit/test_database.py) ──────────────────────


@pytest.fixture
def db(tmp_path) -> Database:
    database = Database(db_path=str(tmp_path / "backtest_test.db"), config=PlatformConfig())
    run_migrations(database)
    return database


@pytest.fixture
def seeded_coin(db) -> str:
    CoinRepository(database=db).upsert(Coin(symbol="BTCUSDT", base_asset="BTC"))
    return "BTCUSDT"


def _waiting_signal(symbol: str, **overrides) -> Signal:
    defaults = dict(
        symbol=symbol, direction=SignalDirection.LONG, entry_price=100.0, stop_loss=90.0,
        take_profit_1=110.0, risk_reward_ratio=2.0, confidence_score=80.0,
        confidence_grade=ConfidenceGrade.STRONG,
    )
    defaults.update(overrides)
    return Signal(**defaults)


def _runner(db) -> tuple[BacktestRunner, TradeRepository, SignalRepository]:
    trade_repo = TradeRepository(database=db)
    signal_repo = SignalRepository(database=db)
    coin_statistics_repo = CoinStatisticsRepository(database=db)
    position_monitor = PositionMonitorEngine(
        signal_repository=signal_repo, trade_repository=trade_repo,
        coin_statistics_repository=coin_statistics_repo,
    )
    client = BinanceFuturesClient(config=PlatformConfig())
    runner = BacktestRunner(
        client=client, position_monitor=position_monitor,
        signal_repository=signal_repo, trade_repository=trade_repo,
    )
    return runner, trade_repo, signal_repo


# ── replay_signal: outcomes ──────────────────────────────────────────────


def test_replay_signal_closes_via_take_profit_1(db, seeded_coin):
    runner, _, signal_repo = _runner(db)
    signal = signal_repo.create(_waiting_signal(seeded_coin))

    candles = [
        _candle(0, 60_000, close=100, high=102, low=99),          # activates
        _candle(60_000, 120_000, close=121, high=125, low=100),   # closes via TP1
    ]

    result = runner.replay_signal(signal, candles)

    assert result.trade is not None
    assert result.trade.status == TradeStatus.TP1_HIT
    assert result.trade.is_closed is True
    assert result.candles_processed == 2


def test_replay_signal_closes_via_stop_loss(db, seeded_coin):
    runner, _, signal_repo = _runner(db)
    signal = signal_repo.create(_waiting_signal(seeded_coin))

    candles = [
        _candle(0, 60_000, close=100, high=101, low=99),         # activates
        _candle(60_000, 120_000, close=88, high=100, low=85),    # closes via SL
    ]

    result = runner.replay_signal(signal, candles)

    assert result.trade.status == TradeStatus.STOP_LOSS
    assert result.trade.is_closed is True


def test_replay_signal_returns_open_trade_when_candles_run_out_first(db, seeded_coin):
    runner, _, signal_repo = _runner(db)
    signal = signal_repo.create(_waiting_signal(seeded_coin))

    candles = [_candle(0, 60_000, close=101, high=102, low=99)]  # only activates

    result = runner.replay_signal(signal, candles)

    assert result.trade is not None
    assert result.trade.is_closed is False
    assert result.candles_processed == 1


def test_replay_signal_sorts_candles_out_of_order_input(db, seeded_coin):
    """Passing candles in the wrong order must not change the outcome -- replay_signal sorts internally."""
    runner, _, signal_repo = _runner(db)
    signal = signal_repo.create(_waiting_signal(seeded_coin))

    in_order = [
        _candle(0, 60_000, close=100, high=102, low=99),
        _candle(60_000, 120_000, close=121, high=125, low=100),
    ]
    shuffled = [in_order[1], in_order[0]]

    result = runner.replay_signal(signal, shuffled)

    assert result.trade.status == TradeStatus.TP1_HIT


def test_replay_signal_requires_at_least_one_candle(db, seeded_coin):
    runner, _, signal_repo = _runner(db)
    signal = signal_repo.create(_waiting_signal(seeded_coin))

    with pytest.raises(ValueError):
        runner.replay_signal(signal, [])


def test_replay_signal_requires_waiting_status(db, seeded_coin):
    runner, _, signal_repo = _runner(db)
    signal = signal_repo.create(_waiting_signal(seeded_coin))
    signal_repo.update_status(signal.id, TradeStatus.ACTIVE)
    signal.status = TradeStatus.ACTIVE

    candle = _candle(0, 60_000, close=100, high=102, low=99)
    with pytest.raises(ValueError):
        runner.replay_signal(signal, [candle])


def test_replay_signal_matches_by_signal_id_not_activation_order(db, seeded_coin):
    """
    Regression test for a real bug caught during self-review: with two
    WAITING signals for the same symbol, replay_signal must track the
    trade for the signal it was actually given, not just the first
    trade_id activated that tick. `other_signal` uses deliberately wide
    levels so it never closes during this test's candles, keeping the
    assertions unambiguous.
    """
    runner, _, signal_repo = _runner(db)

    other_signal = signal_repo.create(
        _waiting_signal(seeded_coin, entry_price=50.0, stop_loss=1.0, take_profit_1=999.0)
    )
    target_signal = signal_repo.create(
        _waiting_signal(seeded_coin, entry_price=100.0, stop_loss=90.0, take_profit_1=110.0)
    )

    candles = [
        _candle(0, 60_000, close=100, high=102, low=99),
        _candle(60_000, 120_000, close=121, high=125, low=100),
    ]

    result = runner.replay_signal(target_signal, candles)

    assert result.trade.signal_id == target_signal.id
    assert result.trade.signal_id != other_signal.id
    assert result.trade.entry_price == pytest.approx(100.0)


# ── fetch_historical_candles: pagination ─────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_historical_candles_pages_across_multiple_requests(db):
    client = BinanceFuturesClient(config=PlatformConfig())
    runner = BacktestRunner(client=client, trade_repository=TradeRepository(database=db))

    page_1 = [
        _raw_row(0, 59_999, open_=100, high=101, low=99, close=100),
        _raw_row(60_000, 119_999, open_=100, high=101, low=99, close=100),
    ]
    # page_2 re-returns the last candle of page_1 (boundary overlap) then one new candle
    page_2 = [
        _raw_row(60_000, 119_999, open_=100, high=101, low=99, close=100),
        _raw_row(120_000, 179_999, open_=100, high=101, low=99, close=100),
    ]

    with aioresponses() as mocked:
        mocked.get(_url_pattern("/fapi/v1/klines"), payload=page_1)
        mocked.get(_url_pattern("/fapi/v1/klines"), payload=page_2)
        mocked.get(_url_pattern("/fapi/v1/klines"), payload=[])
        async with client:
            candles = await runner.fetch_historical_candles(
                "BTCUSDT", "1m",
                start_time=datetime.fromtimestamp(0, tz=timezone.utc),
                end_time=datetime.fromtimestamp(200, tz=timezone.utc),
            )

    open_times_ms = sorted(int(c.open_time.timestamp() * 1000) for c in candles)
    assert open_times_ms == [0, 60_000, 120_000]  # boundary duplicate removed, no gap


@pytest.mark.asyncio
async def test_fetch_historical_candles_returns_empty_list_when_no_data(db):
    client = BinanceFuturesClient(config=PlatformConfig())
    runner = BacktestRunner(client=client, trade_repository=TradeRepository(database=db))

    with aioresponses() as mocked:
        mocked.get(_url_pattern("/fapi/v1/klines"), payload=[])
        async with client:
            candles = await runner.fetch_historical_candles(
                "BTCUSDT", "1m",
                start_time=datetime.fromtimestamp(0, tz=timezone.utc),
                end_time=datetime.fromtimestamp(3600, tz=timezone.utc),
            )

    assert candles == []


# ── structural sanity (BacktestResult / HistoricalMarketDataSource) ─────


def test_backtest_result_holds_all_three_fields():
    signal = Signal(
        symbol="BTCUSDT", direction=SignalDirection.LONG, entry_price=100.0, stop_loss=90.0,
        take_profit_1=110.0, risk_reward_ratio=2.0, confidence_score=80.0,
        confidence_grade=ConfidenceGrade.STRONG, id=1,
    )
    result = BacktestResult(signal=signal, trade=None, candles_processed=5)
    assert result.signal is signal
    assert result.trade is None
    assert result.candles_processed == 5


def test_binance_client_satisfies_historical_market_data_source_shape():
    """Sanity check that HistoricalMarketDataSource's four named methods still exist on the real client (catches future drift if one is ever renamed)."""
    for method_name in ("get_ticker_24hr", "get_book_ticker", "get_funding_rate", "get_open_interest"):
        assert hasattr(BinanceFuturesClient, method_name), f"BinanceFuturesClient is missing {method_name}"
    assert HistoricalMarketDataSource.__name__ == "HistoricalMarketDataSource"
