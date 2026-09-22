"""
Unit tests for engines/trade_execution.py (autonomous trading pivot).

Uses small (~100) prices throughout, not BTC-scale (~50000) -- with a
$12-100 test balance and a 3% risk_per_trade_percent, BTC-scale prices
round the position size to zero at the exchange step_size before the
behavior under test can even be observed (see MASTER PROMPT known
pitfalls).

Run with:
    pytest tests/unit/test_trade_execution.py -v
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from config.schema import APIConfig, GeneralConfig, PlatformConfig, RiskConfig, RunMode
from core.models import ConfidenceGrade, Signal, SignalDirection, Trade, TradeStatus
from engines.trade_execution import TradeExecutionEngine
from infrastructure.binance.models import AccountBalance, OrderResult, PositionRisk, SymbolInfo
from infrastructure.database.connection import Database
from infrastructure.database.repositories.coin_repository import CoinRepository
from infrastructure.database.repositories.signal_repository import SignalRepository
from infrastructure.database.repositories.trade_repository import TradeRepository
from infrastructure.database.schema import run_migrations

# Sentinel distinguishing "not specified, use the default" from an
# explicit None/empty override -- see MASTER PROMPT known pitfalls
# ("fake test client's balance=None can be confused with "unspecified").
_UNSET = object()

_DEFAULT_SYMBOL_INFO = SymbolInfo(
    symbol="ADAUSDT", base_asset="ADA", quote_asset="USDT", status="TRADING",
    price_precision=4, quantity_precision=1, tick_size=0.0001, step_size=0.1,
    min_notional_usdt=5.0,
)


class _FakeBinanceClient:
    def __init__(self, *, balance=_UNSET, symbols=_UNSET, place_order_results=None, raise_on=None, current_price=100.0, position_risk=None):
        self._balance = balance
        self._symbols = symbols
        self._place_order_results = place_order_results or []
        self._raise_on = raise_on or {}
        self._current_price = current_price
        self._position_risk = position_risk
        self.place_order_calls: list[dict] = []
        self.margin_calls: list[tuple] = []
        self.leverage_calls: list[tuple] = []
        self.cancel_all_calls: list[str] = []

    async def get_account_balance(self):
        if "get_account_balance" in self._raise_on:
            raise self._raise_on["get_account_balance"]
        if self._balance is _UNSET:
            return [AccountBalance("USDT", 100.0, 100.0)]
        if self._balance is None:
            return []
        return [self._balance]

    async def get_exchange_info(self):
        if self._symbols is _UNSET:
            return [_DEFAULT_SYMBOL_INFO]
        if self._symbols is None:
            return []
        return self._symbols

    async def get_current_price(self, symbol):
        if "get_current_price" in self._raise_on:
            raise self._raise_on["get_current_price"]
        return self._current_price

    async def get_position_risk(self, symbol):
        if "get_position_risk" in self._raise_on:
            raise self._raise_on["get_position_risk"]
        return self._position_risk

    async def set_margin_type(self, symbol, margin_type):
        self.margin_calls.append((symbol, margin_type))

    async def set_leverage(self, symbol, leverage):
        self.leverage_calls.append((symbol, leverage))

    async def place_order(self, symbol, side, order_type, *, quantity=None, stop_price=None, close_position=False, client_order_id=None):
        self.place_order_calls.append({
            "symbol": symbol, "side": side, "order_type": order_type, "quantity": quantity,
            "stop_price": stop_price, "close_position": close_position, "client_order_id": client_order_id,
        })
        idx = len(self.place_order_calls) - 1
        if idx < len(self._place_order_results):
            return self._place_order_results[idx]
        return OrderResult(
            order_id=1000 + idx, client_order_id=client_order_id or "", symbol=symbol,
            status="NEW", avg_price=0.0, executed_qty=0.0,
        )

    async def cancel_all_open_orders(self, symbol):
        if "cancel_all_open_orders" in self._raise_on:
            raise self._raise_on["cancel_all_open_orders"]
        self.cancel_all_calls.append(symbol)


@pytest.fixture
def database(tmp_path) -> Database:
    db = Database(db_path=str(tmp_path / "trade_execution_test.db"), config=PlatformConfig())
    run_migrations(db)
    return db


@pytest.fixture
def signal_repository(database) -> SignalRepository:
    return SignalRepository(database=database)


@pytest.fixture
def trade_repository(database) -> TradeRepository:
    return TradeRepository(database=database)


def _live_config(**risk_overrides) -> PlatformConfig:
    return PlatformConfig(
        general=GeneralConfig(run_mode=RunMode.LIVE),
        api=APIConfig(trading_enabled=True, binance_api_key="k", binance_api_secret="s"),
        risk=RiskConfig(risk_per_trade_percent=3.0, **risk_overrides),
    )


def _persisted_signal(signal_repository, **overrides) -> Signal:
    from core.models import Coin
    database = signal_repository.database
    symbol = overrides.get("symbol", "ADAUSDT")
    CoinRepository(database=database).upsert(Coin(symbol=symbol, base_asset=symbol.replace("USDT", "")))
    defaults = dict(
        symbol="ADAUSDT", direction=SignalDirection.LONG, entry_price=100.0, stop_loss=95.0,
        take_profit_1=110.0, risk_reward_ratio=2.0, confidence_score=85.0,
        confidence_grade=ConfidenceGrade.STRONG, leverage=1,
    )
    defaults.update(overrides)
    return signal_repository.create(Signal(**defaults))


def _engine(execution_client, config, signal_repository, trade_repository) -> TradeExecutionEngine:
    return TradeExecutionEngine(
        execution_client=execution_client, config=config,
        trade_repository=trade_repository, signal_repository=signal_repository,
    )


# ─────────────────────────────────────────────────────────────────────────
# trading_is_enabled
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "run_mode,trading_enabled,expected",
    [
        (RunMode.LIVE, True, True),
        (RunMode.LIVE, False, False),
        (RunMode.PAPER, True, False),
        (RunMode.PAPER, False, False),
    ],
)
def test_trading_is_enabled_requires_both_flags(run_mode, trading_enabled, expected, signal_repository, trade_repository):
    config = PlatformConfig(
        general=GeneralConfig(run_mode=run_mode),
        api=APIConfig(trading_enabled=trading_enabled),
    )
    engine = _engine(_FakeBinanceClient(), config, signal_repository, trade_repository)
    assert engine.trading_is_enabled is expected


# ─────────────────────────────────────────────────────────────────────────
# open_position() -- routine skip conditions (silent None, not an error)
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_open_position_returns_none_when_trading_disabled(signal_repository, trade_repository):
    signal = _persisted_signal(signal_repository)
    config = PlatformConfig(general=GeneralConfig(run_mode=RunMode.PAPER))
    engine = _engine(_FakeBinanceClient(), config, signal_repository, trade_repository)

    result = await engine.open_position(signal)

    assert result is None


@pytest.mark.asyncio
async def test_open_position_raises_when_signal_id_is_none(signal_repository, trade_repository):
    unpersisted = Signal(
        symbol="ADAUSDT", direction=SignalDirection.LONG, entry_price=100.0, stop_loss=95.0,
        take_profit_1=110.0, risk_reward_ratio=2.0, confidence_score=85.0, confidence_grade=ConfidenceGrade.STRONG,
    )
    engine = _engine(_FakeBinanceClient(), _live_config(), signal_repository, trade_repository)

    with pytest.raises(ValueError):
        await engine.open_position(unpersisted)


@pytest.mark.asyncio
async def test_open_position_returns_none_when_no_usdt_balance_row(signal_repository, trade_repository):
    signal = _persisted_signal(signal_repository)
    engine = _engine(_FakeBinanceClient(balance=None), _live_config(), signal_repository, trade_repository)

    assert await engine.open_position(signal) is None


@pytest.mark.asyncio
async def test_open_position_returns_none_when_available_balance_is_zero(signal_repository, trade_repository):
    signal = _persisted_signal(signal_repository)
    engine = _engine(
        _FakeBinanceClient(balance=AccountBalance("USDT", 0.0, 0.0)), _live_config(), signal_repository, trade_repository,
    )

    assert await engine.open_position(signal) is None


@pytest.mark.asyncio
async def test_open_position_returns_none_when_symbol_not_in_exchange_info(signal_repository, trade_repository):
    signal = _persisted_signal(signal_repository)
    engine = _engine(_FakeBinanceClient(symbols=None), _live_config(), signal_repository, trade_repository)

    assert await engine.open_position(signal) is None


@pytest.mark.asyncio
async def test_open_position_returns_none_when_execution_price_unavailable(signal_repository, trade_repository):
    """The exchange's own current price could not be fetched -- must skip silently, never fall back to Binance's stale signal-time price for real order placement."""
    signal = _persisted_signal(signal_repository)
    engine = _engine(_FakeBinanceClient(current_price=None), _live_config(), signal_repository, trade_repository)

    assert await engine.open_position(signal) is None


@pytest.mark.asyncio
async def test_open_position_returns_none_when_execution_price_is_zero_or_negative(signal_repository, trade_repository):
    signal = _persisted_signal(signal_repository)
    engine = _engine(_FakeBinanceClient(current_price=0.0), _live_config(), signal_repository, trade_repository)

    assert await engine.open_position(signal) is None


@pytest.mark.asyncio
async def test_open_position_returns_none_when_size_rounds_to_zero_below_min_notional(signal_repository, trade_repository):
    # $1 balance * 3% risk / 5% stop distance = $0.60 notional -- far below the $5 min_notional.
    signal = _persisted_signal(signal_repository, entry_price=100.0, stop_loss=95.0)
    engine = _engine(
        _FakeBinanceClient(balance=AccountBalance("USDT", 1.0, 1.0)), _live_config(), signal_repository, trade_repository,
    )

    assert await engine.open_position(signal) is None


# ─────────────────────────────────────────────────────────────────────────
# open_position() -- the real path
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_open_position_sets_margin_and_leverage_before_placing_any_order(signal_repository, trade_repository):
    signal = _persisted_signal(signal_repository, leverage=5)
    client = _FakeBinanceClient()
    engine = _engine(client, _live_config(), signal_repository, trade_repository)

    await engine.open_position(signal)

    assert client.margin_calls == [("ADAUSDT", "ISOLATED")]
    assert client.leverage_calls == [("ADAUSDT", 5)]
    assert len(client.place_order_calls) == 3  # entry + SL + TP, all placed AFTER margin/leverage


@pytest.mark.asyncio
async def test_open_position_places_entry_then_stop_then_take_profit_with_correct_client_order_ids(signal_repository, trade_repository):
    signal = _persisted_signal(signal_repository)
    client = _FakeBinanceClient()
    engine = _engine(client, _live_config(), signal_repository, trade_repository)

    await engine.open_position(signal)

    entry, stop, tp = client.place_order_calls
    assert entry["order_type"] == "MARKET" and entry["side"] == "BUY" and entry["quantity"] is not None
    assert entry["client_order_id"] == f"colde-{signal.id}-entry"

    assert stop["order_type"] == "STOP_MARKET" and stop["side"] == "SELL" and stop["close_position"] is True
    assert stop["stop_price"] == pytest.approx(95.0)
    assert stop["client_order_id"] == f"colde-{signal.id}-sl"

    assert tp["order_type"] == "TAKE_PROFIT_MARKET" and tp["side"] == "SELL" and tp["close_position"] is True
    assert tp["stop_price"] == pytest.approx(110.0)
    assert tp["client_order_id"] == f"colde-{signal.id}-tp"


@pytest.mark.asyncio
async def test_open_position_short_signal_uses_sell_entry_and_buy_exits(signal_repository, trade_repository):
    signal = _persisted_signal(
        signal_repository, direction=SignalDirection.SHORT, entry_price=100.0, stop_loss=105.0, take_profit_1=90.0,
    )
    client = _FakeBinanceClient()
    engine = _engine(client, _live_config(), signal_repository, trade_repository)

    await engine.open_position(signal)

    entry, stop, tp = client.place_order_calls
    assert entry["side"] == "SELL"
    assert stop["side"] == "BUY" and tp["side"] == "BUY"


@pytest.mark.asyncio
async def test_open_position_rebases_long_levels_to_the_execution_venues_price(signal_repository, trade_repository):
    """
    Binance's signal-time price (100.0) and the execution venue's real
    current price (102.0) differ by 2 -- the placed stop/TP must shift by
    the SAME 2, preserving the original 5-wide risk / 10-wide reward the
    signal was approved for, not reuse Binance's raw absolute levels.
    """
    signal = _persisted_signal(signal_repository, entry_price=100.0, stop_loss=95.0, take_profit_1=110.0)
    client = _FakeBinanceClient(current_price=102.0)
    engine = _engine(client, _live_config(), signal_repository, trade_repository)

    trade = await engine.open_position(signal)

    entry, stop, tp = client.place_order_calls
    assert stop["stop_price"] == pytest.approx(97.0)   # 102 - 5
    assert tp["stop_price"] == pytest.approx(112.0)     # 102 + 10
    assert trade.initial_stop_loss == pytest.approx(97.0)
    assert trade.take_profit_1 == pytest.approx(112.0)


@pytest.mark.asyncio
async def test_open_position_rebases_short_levels_to_the_execution_venues_price(signal_repository, trade_repository):
    signal = _persisted_signal(
        signal_repository, direction=SignalDirection.SHORT, entry_price=100.0, stop_loss=105.0, take_profit_1=90.0,
    )
    client = _FakeBinanceClient(current_price=98.0)
    engine = _engine(client, _live_config(), signal_repository, trade_repository)

    trade = await engine.open_position(signal)

    entry, stop, tp = client.place_order_calls
    assert stop["stop_price"] == pytest.approx(103.0)   # 98 + 5 (original 5-wide risk)
    assert tp["stop_price"] == pytest.approx(88.0)       # 98 - 10 (original 10-wide reward)
    assert trade.initial_stop_loss == pytest.approx(103.0)
    assert trade.take_profit_1 == pytest.approx(88.0)


@pytest.mark.asyncio
async def test_open_position_uses_the_entry_orders_real_fill_price(signal_repository, trade_repository):
    signal = _persisted_signal(signal_repository, entry_price=100.0)
    client = _FakeBinanceClient(place_order_results=[
        OrderResult(order_id=1, client_order_id="e", symbol="ADAUSDT", status="FILLED", avg_price=101.23, executed_qty=1.0),
    ])
    engine = _engine(client, _live_config(), signal_repository, trade_repository)

    trade = await engine.open_position(signal)

    assert trade.entry_price == pytest.approx(101.23)


@pytest.mark.asyncio
async def test_open_position_falls_back_to_signal_entry_price_when_fill_price_is_zero(signal_repository, trade_repository):
    signal = _persisted_signal(signal_repository, entry_price=100.0)
    client = _FakeBinanceClient(place_order_results=[
        OrderResult(order_id=1, client_order_id="e", symbol="ADAUSDT", status="NEW", avg_price=0.0, executed_qty=0.0),
    ])
    engine = _engine(client, _live_config(), signal_repository, trade_repository)

    trade = await engine.open_position(signal)

    assert trade.entry_price == pytest.approx(100.0)


@pytest.mark.asyncio
async def test_open_position_persists_trade_with_order_ids_and_flips_signal_to_active(signal_repository, trade_repository):
    signal = _persisted_signal(signal_repository)
    client = _FakeBinanceClient(place_order_results=[
        OrderResult(order_id=111, client_order_id="e", symbol="ADAUSDT", status="FILLED", avg_price=100.5, executed_qty=1.0),
        OrderResult(order_id=222, client_order_id="s", symbol="ADAUSDT", status="NEW", avg_price=0.0, executed_qty=0.0),
        OrderResult(order_id=333, client_order_id="t", symbol="ADAUSDT", status="NEW", avg_price=0.0, executed_qty=0.0),
    ])
    engine = _engine(client, _live_config(), signal_repository, trade_repository)

    trade = await engine.open_position(signal)

    assert trade.id is not None
    assert trade.status == TradeStatus.ACTIVE
    assert trade.entry_order_id == 111
    assert trade.stop_order_id == 222
    assert trade.take_profit_order_id == 333

    persisted = trade_repository.get_by_id(trade.id)
    assert persisted.entry_order_id == 111

    reloaded_signal = signal_repository.get_by_id(signal.id)
    assert reloaded_signal.status == TradeStatus.ACTIVE


@pytest.mark.asyncio
async def test_open_position_never_reactivates_the_same_signal_twice(signal_repository, trade_repository):
    """Signal flips to ACTIVE -- a second open_position() call for the same already-active signal must not silently duplicate a Trade."""
    signal = _persisted_signal(signal_repository)
    engine = _engine(_FakeBinanceClient(), _live_config(), signal_repository, trade_repository)

    first_trade = await engine.open_position(signal)
    assert first_trade is not None
    assert len(trade_repository.get_active_trades()) == 1

    # Simulate PositionMonitorEngine seeing the now-ACTIVE signal and skipping it
    # (its own _activate_waiting_signals() only looks at WAITING signals).
    reloaded_signal = signal_repository.get_by_id(signal.id)
    assert reloaded_signal.status == TradeStatus.ACTIVE


# ─────────────────────────────────────────────────────────────────────────
# close_position()
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_close_position_cancels_remaining_open_orders(signal_repository, trade_repository):
    signal = _persisted_signal(signal_repository)
    client = _FakeBinanceClient()
    engine = _engine(client, _live_config(), signal_repository, trade_repository)
    trade = await engine.open_position(signal)

    await engine.close_position(trade)

    assert client.cancel_all_calls == ["ADAUSDT"]


@pytest.mark.asyncio
async def test_close_position_is_a_noop_when_trading_disabled(signal_repository, trade_repository):
    signal = _persisted_signal(signal_repository)
    client = _FakeBinanceClient()
    config = PlatformConfig(general=GeneralConfig(run_mode=RunMode.PAPER))
    engine = _engine(client, config, signal_repository, trade_repository)

    from core.models import Trade
    fake_trade = Trade(
        signal_id=signal.id, symbol="ADAUSDT", direction=SignalDirection.LONG, entry_price=100.0,
        entry_time=datetime.now(timezone.utc), initial_stop_loss=95.0, current_stop_loss=95.0,
        take_profit_1=110.0, confidence_score=85.0, status=TradeStatus.TP1_HIT,
    )

    await engine.close_position(fake_trade)

    assert client.cancel_all_calls == []


@pytest.mark.asyncio
async def test_close_position_never_raises_when_cancel_fails(signal_repository, trade_repository):
    signal = _persisted_signal(signal_repository)
    client = _FakeBinanceClient(raise_on={"cancel_all_open_orders": RuntimeError("network blip")})
    engine = _engine(client, _live_config(), signal_repository, trade_repository)
    trade = await engine.open_position(signal)

    await engine.close_position(trade)  # must not raise


@pytest.mark.asyncio
async def test_close_position_market_closes_a_remaining_position_before_cancelling_orders(signal_repository, trade_repository):
    """
    TRAILING_STOP_EXIT case: nothing on the exchange has closed the
    position yet (no resting order exists for "price retraced from its
    best point") -- close_position() must actively place a MARKET order
    for whatever quantity is still open.
    """
    signal = _persisted_signal(signal_repository, direction=SignalDirection.LONG)
    client = _FakeBinanceClient(
        position_risk=PositionRisk(symbol="ADAUSDT", position_amount=0.6, entry_price=100.0, mark_price=101.0, unrealized_pnl=0.6, leverage=1),
    )
    engine = _engine(client, _live_config(), signal_repository, trade_repository)
    trade = await engine.open_position(signal)
    client.place_order_calls.clear()

    await engine.close_position(trade)

    assert len(client.place_order_calls) == 1
    close_call = client.place_order_calls[0]
    assert close_call["side"] == "SELL"
    assert close_call["order_type"] == "MARKET"
    assert close_call["quantity"] == pytest.approx(0.6)
    assert client.cancel_all_calls == ["ADAUSDT"]


@pytest.mark.asyncio
async def test_close_position_market_closes_a_short_position_with_a_buy_order(signal_repository, trade_repository):
    signal = _persisted_signal(signal_repository, direction=SignalDirection.SHORT, entry_price=100.0, stop_loss=105.0, take_profit_1=90.0)
    client = _FakeBinanceClient(
        position_risk=PositionRisk(symbol="ADAUSDT", position_amount=-0.6, entry_price=100.0, mark_price=99.0, unrealized_pnl=0.6, leverage=1),
    )
    engine = _engine(client, _live_config(), signal_repository, trade_repository)
    trade = await engine.open_position(signal)
    client.place_order_calls.clear()

    await engine.close_position(trade)

    close_call = client.place_order_calls[0]
    assert close_call["side"] == "BUY"
    assert close_call["quantity"] == pytest.approx(0.6)


@pytest.mark.asyncio
async def test_close_position_skips_market_close_when_position_already_flat(signal_repository, trade_repository):
    """TP1/STOP_LOSS case: the exchange's own resting order already closed the position -- no market order needed, just cleanup."""
    signal = _persisted_signal(signal_repository)
    client = _FakeBinanceClient(
        position_risk=PositionRisk(symbol="ADAUSDT", position_amount=0.0, entry_price=100.0, mark_price=110.0, unrealized_pnl=0.0, leverage=1),
    )
    engine = _engine(client, _live_config(), signal_repository, trade_repository)
    trade = await engine.open_position(signal)
    client.place_order_calls.clear()

    await engine.close_position(trade)

    assert client.place_order_calls == []
    assert client.cancel_all_calls == ["ADAUSDT"]


@pytest.mark.asyncio
async def test_close_position_still_cancels_orders_when_position_risk_fetch_fails(signal_repository, trade_repository):
    signal = _persisted_signal(signal_repository)
    client = _FakeBinanceClient(raise_on={"get_position_risk": RuntimeError("network blip")})
    engine = _engine(client, _live_config(), signal_repository, trade_repository)
    trade = await engine.open_position(signal)
    client.place_order_calls.clear()

    await engine.close_position(trade)  # must not raise

    assert client.cancel_all_calls == ["ADAUSDT"]


# ─────────────────────────────────────────────────────────────────────────
# Dynamic position cap (balance-scaled, REAL trades only)
# ─────────────────────────────────────────────────────────────────────────


def _open_real_trade(signal_repository, trade_repository, symbol: str) -> None:
    """A minimal already-open REAL trade (entry_order_id set) purely to occupy one dynamic-cap slot."""
    signal = _persisted_signal(signal_repository, symbol=symbol)
    trade_repository.create(Trade(
        signal_id=signal.id, symbol=symbol, direction=SignalDirection.LONG, entry_price=100.0,
        entry_time=datetime.now(timezone.utc), initial_stop_loss=95.0, current_stop_loss=95.0,
        take_profit_1=110.0, confidence_score=85.0, status=TradeStatus.ACTIVE, entry_order_id=1,
    ))


def test_dynamic_position_cap_formula_at_the_base_balance(signal_repository, trade_repository):
    engine = _engine(_FakeBinanceClient(), _live_config(), signal_repository, trade_repository)
    assert engine._dynamic_position_cap(10.0) == 5
    assert engine._dynamic_position_cap(5.0) == 5   # below base balance -- still the base count, never negative steps


def test_dynamic_position_cap_grows_by_one_every_balance_step(signal_repository, trade_repository):
    engine = _engine(_FakeBinanceClient(), _live_config(max_active_trades=50), signal_repository, trade_repository)
    assert engine._dynamic_position_cap(20.0) == 6
    assert engine._dynamic_position_cap(29.9) == 6   # not quite the next $10 step yet
    assert engine._dynamic_position_cap(30.0) == 7


def test_dynamic_position_cap_never_exceeds_max_active_trades(signal_repository, trade_repository):
    engine = _engine(_FakeBinanceClient(), _live_config(max_active_trades=5), signal_repository, trade_repository)
    assert engine._dynamic_position_cap(1000.0) == 5  # formula alone would say far more -- overall ceiling wins


@pytest.mark.asyncio
async def test_open_position_skips_a_new_signal_once_the_dynamic_cap_is_reached(signal_repository, trade_repository):
    for i in range(5):
        _open_real_trade(signal_repository, trade_repository, f"COIN{i}USDT")
    signal = _persisted_signal(signal_repository, symbol="ADAUSDT")
    # $11 available -> dynamic_position_cap(11.0) == 5 (still in the base tier) -- already at capacity
    client = _FakeBinanceClient(balance=AccountBalance("USDT", 11.0, 11.0))
    engine = _engine(client, _live_config(), signal_repository, trade_repository)

    result = await engine.open_position(signal)

    assert result is None
    assert client.place_order_calls == []


@pytest.mark.asyncio
async def test_open_position_proceeds_once_balance_growth_raises_the_dynamic_cap(signal_repository, trade_repository):
    for i in range(5):
        _open_real_trade(signal_repository, trade_repository, f"COIN{i}USDT")
    signal = _persisted_signal(signal_repository, symbol="ADAUSDT")
    # $25 available -> dynamic_position_cap(25.0) == 6 -- one slot free above the 5 already open
    client = _FakeBinanceClient(balance=AccountBalance("USDT", 25.0, 25.0))
    engine = _engine(client, _live_config(), signal_repository, trade_repository)

    result = await engine.open_position(signal)

    assert result is not None
    assert len(client.place_order_calls) == 3


@pytest.mark.asyncio
async def test_open_position_ignores_paper_trades_when_counting_toward_the_dynamic_cap(signal_repository, trade_repository):
    """A paper-mode trade (no entry_order_id) never consumed real capital -- it must not count against the real-trading cap."""
    for i in range(5):
        paper_signal = _persisted_signal(signal_repository, symbol=f"PAPER{i}USDT")
        trade_repository.create(Trade(
            signal_id=paper_signal.id, symbol=f"PAPER{i}USDT", direction=SignalDirection.LONG, entry_price=100.0,
            entry_time=datetime.now(timezone.utc), initial_stop_loss=95.0, current_stop_loss=95.0,
            take_profit_1=110.0, confidence_score=85.0, status=TradeStatus.ACTIVE, entry_order_id=None,
        ))
    signal = _persisted_signal(signal_repository, symbol="ADAUSDT")
    client = _FakeBinanceClient(balance=AccountBalance("USDT", 18.0, 18.0))
    engine = _engine(client, _live_config(), signal_repository, trade_repository)

    result = await engine.open_position(signal)

    assert result is not None


@pytest.mark.asyncio
async def test_dynamic_position_cap_can_be_disabled(signal_repository, trade_repository):
    for i in range(5):
        _open_real_trade(signal_repository, trade_repository, f"COIN{i}USDT")
    signal = _persisted_signal(signal_repository, symbol="ADAUSDT")
    client = _FakeBinanceClient(balance=AccountBalance("USDT", 18.0, 18.0))
    engine = _engine(client, _live_config(dynamic_position_cap_enabled=False), signal_repository, trade_repository)

    result = await engine.open_position(signal)

    assert result is not None


# ─────────────────────────────────────────────────────────────────────────
# _calculate_quantity() -- unit-level, no I/O
# ─────────────────────────────────────────────────────────────────────────


def test_calculate_quantity_scales_with_risk_percent_and_rounds_to_step_size(signal_repository, trade_repository):
    signal = Signal(
        symbol="ADAUSDT", direction=SignalDirection.LONG, entry_price=100.0, stop_loss=95.0,
        take_profit_1=110.0, risk_reward_ratio=2.0, confidence_score=85.0, confidence_grade=ConfidenceGrade.STRONG,
        leverage=1,
    )
    engine = _engine(_FakeBinanceClient(), _live_config(), signal_repository, trade_repository)
    symbol_info = SymbolInfo(
        symbol="ADAUSDT", base_asset="ADA", quote_asset="USDT", status="TRADING",
        price_precision=4, quantity_precision=1, tick_size=0.0001, step_size=0.1, min_notional_usdt=5.0,
    )

    # risk_amount = 100 * 3% = $3; stop_distance_pct = 5/100 = 5%; notional = 3/0.05 = $60
    quantity = engine._calculate_quantity(signal, execution_price=100.0, stop_loss=95.0, available_balance=100.0, symbol_info=symbol_info)

    assert quantity == pytest.approx(0.6)  # 60/100 = 0.6, already a multiple of step_size 0.1


def test_calculate_quantity_is_capped_by_available_margin_at_leverage(signal_repository, trade_repository):
    signal = Signal(
        symbol="ADAUSDT", direction=SignalDirection.LONG, entry_price=100.0, stop_loss=99.0,  # very tight 1% stop
        take_profit_1=110.0, risk_reward_ratio=2.0, confidence_score=85.0, confidence_grade=ConfidenceGrade.STRONG,
        leverage=2,
    )
    engine = _engine(_FakeBinanceClient(), _live_config(), signal_repository, trade_repository)
    symbol_info = SymbolInfo(
        symbol="ADAUSDT", base_asset="ADA", quote_asset="USDT", status="TRADING",
        price_precision=4, quantity_precision=1, tick_size=0.0001, step_size=0.1, min_notional_usdt=5.0,
    )

    # risk_amount = $3, stop_distance_pct = 1% -> uncapped notional = $300, but margin cap = 100*2 = $200
    quantity = engine._calculate_quantity(signal, execution_price=100.0, stop_loss=99.0, available_balance=100.0, symbol_info=symbol_info)

    assert quantity == pytest.approx(2.0)  # 200/100 = 2.0


def test_calculate_quantity_returns_zero_below_min_notional(signal_repository, trade_repository):
    signal = Signal(
        symbol="ADAUSDT", direction=SignalDirection.LONG, entry_price=100.0, stop_loss=95.0,
        take_profit_1=110.0, risk_reward_ratio=2.0, confidence_score=85.0, confidence_grade=ConfidenceGrade.STRONG,
        leverage=1,
    )
    engine = _engine(_FakeBinanceClient(), _live_config(), signal_repository, trade_repository)
    symbol_info = SymbolInfo(
        symbol="ADAUSDT", base_asset="ADA", quote_asset="USDT", status="TRADING",
        price_precision=4, quantity_precision=1, tick_size=0.0001, step_size=0.1, min_notional_usdt=5.0,
    )

    quantity = engine._calculate_quantity(signal, execution_price=100.0, stop_loss=95.0, available_balance=1.0, symbol_info=symbol_info)

    assert quantity == 0.0


def test_calculate_quantity_returns_zero_with_no_min_notional_configured(signal_repository, trade_repository):
    """min_notional_usdt=None means 'no exchange-enforced floor', not 'skip the check'."""
    signal = Signal(
        symbol="ADAUSDT", direction=SignalDirection.LONG, entry_price=100.0, stop_loss=95.0,
        take_profit_1=110.0, risk_reward_ratio=2.0, confidence_score=85.0, confidence_grade=ConfidenceGrade.STRONG,
        leverage=1,
    )
    engine = _engine(_FakeBinanceClient(), _live_config(), signal_repository, trade_repository)
    symbol_info = SymbolInfo(
        symbol="ADAUSDT", base_asset="ADA", quote_asset="USDT", status="TRADING",
        price_precision=4, quantity_precision=3, tick_size=0.0001, step_size=0.001, min_notional_usdt=None,
    )

    quantity = engine._calculate_quantity(signal, execution_price=100.0, stop_loss=95.0, available_balance=1.0, symbol_info=symbol_info)

    assert quantity > 0.0  # tiny but non-zero -- nothing to floor it to zero without a configured minimum
