"""
Unit tests for engines/position_monitor.py (Module 14).

Single-TP model: TP1_HIT is a full, final close. There is no TP2 and no
break-even stop move.

Run with:
    pytest tests/unit/test_position_monitor.py -v
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from config.schema import PlatformConfig
from core.models import Coin, ConfidenceGrade, Signal, SignalDirection, TradeStatus
from engines.position_monitor import PositionMonitorEngine
from infrastructure.database.connection import Database
from infrastructure.database.repositories.coin_repository import CoinRepository, CoinStatisticsRepository
from infrastructure.database.repositories.signal_repository import SignalRepository
from infrastructure.database.repositories.trade_repository import TradeRepository
from infrastructure.database.schema import run_migrations


def _create_waiting_signal(
    database: Database,
    symbol: str,
    direction: SignalDirection,
    entry: float = 100.0,
    sl: float = 95.0,
    tp1: float = 110.0,
    created_at: "datetime | None" = None,
) -> Signal:
    CoinRepository(database=database).upsert(Coin(symbol=symbol, base_asset=symbol.replace("USDT", "")))
    kwargs = dict(
        symbol=symbol, direction=direction, entry_price=entry, stop_loss=sl,
        take_profit_1=tp1, risk_reward_ratio=2.0,
        confidence_score=85.0, confidence_grade=ConfidenceGrade.STRONG,
    )
    if created_at is not None:
        kwargs["created_at"] = created_at
    return SignalRepository(database=database).create(Signal(**kwargs))


@pytest.fixture
def config() -> PlatformConfig:
    return PlatformConfig()


@pytest.fixture
def database(tmp_path) -> Database:
    db = Database(db_path=str(tmp_path / "position_monitor_test.db"), config=PlatformConfig())
    run_migrations(db)
    return db


@pytest.fixture
def signal_repository(database) -> SignalRepository:
    return SignalRepository(database=database)


@pytest.fixture
def trade_repository(database) -> TradeRepository:
    return TradeRepository(database=database)


@pytest.fixture
def stats_repository(database) -> CoinStatisticsRepository:
    return CoinStatisticsRepository(database=database)


@pytest.fixture
def engine(config, signal_repository, trade_repository, stats_repository) -> PositionMonitorEngine:
    return PositionMonitorEngine(
        config=config, signal_repository=signal_repository,
        trade_repository=trade_repository, coin_statistics_repository=stats_repository,
    )


# ─────────────────────────────────────────────────────────────────────────
# WAITING -> ACTIVE
# ─────────────────────────────────────────────────────────────────────────


def test_activates_waiting_signal_into_an_active_trade(engine, database, signal_repository, trade_repository):
    signal = _create_waiting_signal(database, "BTCUSDT", SignalDirection.LONG)

    result = engine.run_tick({"BTCUSDT": 100.0})

    assert len(result.activated_trade_ids) == 1
    assert signal_repository.get_by_id(signal.id).status == TradeStatus.ACTIVE
    active = trade_repository.get_active_trades()
    assert len(active) == 1
    assert active[0].symbol == "BTCUSDT"
    assert active[0].initial_stop_loss == pytest.approx(95.0)


def test_skips_waiting_signal_with_no_price_data_and_does_not_expire_it_early(engine, database, signal_repository):
    signal = _create_waiting_signal(database, "BTCUSDT", SignalDirection.LONG)

    result = engine.run_tick({})  # no price data for BTCUSDT at all

    assert result.activated_trade_ids == ()
    assert result.expired_signal_ids == ()
    assert signal_repository.get_by_id(signal.id).status == TradeStatus.WAITING


def test_does_not_reactivate_an_already_active_signal_on_a_later_tick(engine, database):
    _create_waiting_signal(database, "BTCUSDT", SignalDirection.LONG)
    engine.run_tick({"BTCUSDT": 100.0})

    second = engine.run_tick({"BTCUSDT": 100.0})

    assert second.activated_trade_ids == ()


# ─────────────────────────────────────────────────────────────────────────
# EXPIRATION
# ─────────────────────────────────────────────────────────────────────────


def test_expires_a_waiting_signal_past_its_lifetime_with_no_price_data(engine, database, signal_repository, config):
    old_time = datetime.now(timezone.utc) - timedelta(hours=config.risk.signal_lifetime_hours + 1)
    signal = _create_waiting_signal(database, "OLDUSDT", SignalDirection.LONG, created_at=old_time)

    result = engine.run_tick({})  # still no price data for OLDUSDT

    assert result.expired_signal_ids == (signal.id,)
    assert signal_repository.get_by_id(signal.id).status == TradeStatus.EXPIRED


def test_does_not_expire_a_young_waiting_signal(engine, database, signal_repository):
    signal = _create_waiting_signal(database, "BTCUSDT", SignalDirection.LONG)

    result = engine.run_tick({})

    assert result.expired_signal_ids == ()
    assert signal_repository.get_by_id(signal.id).status == TradeStatus.WAITING


# ─────────────────────────────────────────────────────────────────────────
# TP1 (LONG) -- full, final close in the single-TP model
# ─────────────────────────────────────────────────────────────────────────


def test_tp1_hit_closes_the_trade(engine, database, trade_repository, stats_repository):
    _create_waiting_signal(database, "BTCUSDT", SignalDirection.LONG, entry=100.0, sl=95.0, tp1=110.0)
    engine.run_tick({"BTCUSDT": 100.0})  # activate

    result = engine.run_tick({"BTCUSDT": 111.0})  # crosses TP1

    assert len(result.tp1_hit_trade_ids) == 1
    trade = trade_repository.get_by_id(result.tp1_hit_trade_ids[0])
    assert trade.tp1_hit_at is not None
    assert trade.status == TradeStatus.TP1_HIT
    assert trade.current_stop_loss == pytest.approx(95.0)  # never moved -- no break-even step anymore
    assert trade.initial_stop_loss == pytest.approx(95.0)
    assert trade.tp1_exit_price == pytest.approx(111.0)  # the observed price, not the 110.0 TP1 level itself
    assert trade.exit_price == pytest.approx(111.0)
    assert trade.realized_pnl_percent == pytest.approx(11.0)  # (111-100)/100 * 100
    assert trade not in trade_repository.get_active_trades()

    stats = stats_repository.get("BTCUSDT")
    assert stats.tp1_count == 1
    assert stats.winning_signals == 1


def test_closed_tp1_trade_is_never_reprocessed_on_a_later_tick(engine, database, trade_repository):
    _create_waiting_signal(database, "BTCUSDT", SignalDirection.LONG, entry=100.0, sl=95.0, tp1=110.0)
    engine.run_tick({"BTCUSDT": 100.0})
    first = engine.run_tick({"BTCUSDT": 111.0})
    assert len(first.tp1_hit_trade_ids) == 1

    second = engine.run_tick({"BTCUSDT": 111.0})

    assert second.tp1_hit_trade_ids == ()
    assert trade_repository.get_active_trades() == []


# ─────────────────────────────────────────────────────────────────────────
# STOP LOSS (LONG + SHORT)
# ─────────────────────────────────────────────────────────────────────────


def test_stop_loss_hit_long(engine, database, trade_repository, stats_repository):
    _create_waiting_signal(database, "BTCUSDT", SignalDirection.LONG, entry=100.0, sl=95.0, tp1=110.0)
    engine.run_tick({"BTCUSDT": 100.0})

    result = engine.run_tick({"BTCUSDT": 94.0})

    assert len(result.stop_loss_trade_ids) == 1
    trade = trade_repository.get_by_id(result.stop_loss_trade_ids[0])
    assert trade.status == TradeStatus.STOP_LOSS
    assert trade.realized_pnl_percent == pytest.approx(-6.0)
    stats = stats_repository.get("BTCUSDT")
    assert stats.losing_signals == 1
    assert stats.sl_count == 1


def test_stop_loss_triggers_at_the_exact_boundary_price(engine, database, trade_repository):
    _create_waiting_signal(database, "BTCUSDT", SignalDirection.LONG, entry=100.0, sl=95.0, tp1=110.0)
    engine.run_tick({"BTCUSDT": 100.0})

    result = engine.run_tick({"BTCUSDT": 95.0})  # exactly at the stop level

    assert len(result.stop_loss_trade_ids) == 1


# ─────────────────────────────────────────────────────────────────────────
# SHORT TRADES
# ─────────────────────────────────────────────────────────────────────────


def test_short_trade_tp1_hit_closes_the_trade(engine, database, trade_repository):
    _create_waiting_signal(database, "ETHUSDT", SignalDirection.SHORT, entry=100.0, sl=105.0, tp1=90.0)
    engine.run_tick({"ETHUSDT": 100.0})  # activate

    result = engine.run_tick({"ETHUSDT": 89.0})  # price down -> profit for a short

    assert len(result.tp1_hit_trade_ids) == 1
    trade = trade_repository.get_by_id(result.tp1_hit_trade_ids[0])
    assert trade.status == TradeStatus.TP1_HIT
    assert trade.realized_pnl_percent == pytest.approx(11.0)  # (100-89)/100*100


def test_short_trade_stop_loss_when_price_rises(engine, database, trade_repository):
    _create_waiting_signal(database, "ETHUSDT", SignalDirection.SHORT, entry=100.0, sl=105.0, tp1=90.0)
    engine.run_tick({"ETHUSDT": 100.0})

    result = engine.run_tick({"ETHUSDT": 106.0})  # price up -> stop loss for a short

    assert len(result.stop_loss_trade_ids) == 1
    trade = trade_repository.get_by_id(result.stop_loss_trade_ids[0])
    assert trade.status == TradeStatus.STOP_LOSS
    assert trade.realized_pnl_percent == pytest.approx(-6.0)


# ─────────────────────────────────────────────────────────────────────────
# MULTI-SYMBOL / MULTI-CYCLE EDGE CASES
# ─────────────────────────────────────────────────────────────────────────


def test_multiple_independent_trades_advance_independently_in_one_tick(engine, database, trade_repository):
    _create_waiting_signal(database, "BTCUSDT", SignalDirection.LONG, entry=100.0, sl=95.0, tp1=110.0)
    _create_waiting_signal(database, "ETHUSDT", SignalDirection.SHORT, entry=100.0, sl=105.0, tp1=90.0)
    engine.run_tick({"BTCUSDT": 100.0, "ETHUSDT": 100.0})

    result = engine.run_tick({"BTCUSDT": 94.0, "ETHUSDT": 106.0})  # both hit their own stop loss

    assert len(result.stop_loss_trade_ids) == 2
    statuses = {t.symbol: t.status for t in [trade_repository.get_by_id(i) for i in result.stop_loss_trade_ids]}
    assert statuses == {"BTCUSDT": TradeStatus.STOP_LOSS, "ETHUSDT": TradeStatus.STOP_LOSS}


def test_a_symbol_missing_from_current_prices_is_left_untouched(engine, database, trade_repository):
    _create_waiting_signal(database, "BTCUSDT", SignalDirection.LONG, entry=100.0, sl=95.0, tp1=110.0)
    engine.run_tick({"BTCUSDT": 100.0})

    result = engine.run_tick({})  # no price data this tick

    assert result.stop_loss_trade_ids == () and result.tp1_hit_trade_ids == ()
    assert len(trade_repository.get_active_trades()) == 1


def test_closed_trade_is_never_reprocessed_on_a_later_tick(engine, database, trade_repository):
    _create_waiting_signal(database, "BTCUSDT", SignalDirection.LONG, entry=100.0, sl=95.0, tp1=110.0)
    engine.run_tick({"BTCUSDT": 100.0})
    first = engine.run_tick({"BTCUSDT": 94.0})
    assert len(first.stop_loss_trade_ids) == 1

    second = engine.run_tick({"BTCUSDT": 94.0})  # same losing price again

    assert second.stop_loss_trade_ids == ()
    assert trade_repository.get_active_trades() == []


# ─────────────────────────────────────────────────────────────────────────
# TRAILING STOP (profit-lock early exit) -- entry=100, sl=97 gives
# risk_distance=3, original_atr=3/1.5=2, trailing_distance=1.0*2=2 with
# the default config (atr_stop_loss_multiplier=1.5, trailing_stop_atr_multiple=1.0).
# ─────────────────────────────────────────────────────────────────────────


def test_trailing_stop_never_triggers_before_the_trade_has_been_in_profit(engine, database, trade_repository):
    _create_waiting_signal(database, "BTCUSDT", SignalDirection.LONG, entry=100.0, sl=97.0, tp1=120.0)
    engine.run_tick({"BTCUSDT": 100.0})  # activate

    result = engine.run_tick({"BTCUSDT": 99.0})  # still below entry -- never been in profit

    assert result.trailing_stop_exit_trade_ids == ()
    active = trade_repository.get_active_trades()
    assert len(active) == 1
    assert active[0].best_price_since_entry is None


def test_trailing_stop_tracks_the_best_price_while_in_profit_without_closing(engine, database, trade_repository):
    _create_waiting_signal(database, "BTCUSDT", SignalDirection.LONG, entry=100.0, sl=97.0, tp1=120.0)
    engine.run_tick({"BTCUSDT": 100.0})

    result = engine.run_tick({"BTCUSDT": 110.0})  # moves into profit, retracement from itself is 0

    assert result.trailing_stop_exit_trade_ids == ()
    active = trade_repository.get_active_trades()
    assert active[0].best_price_since_entry == pytest.approx(110.0)


def test_trailing_stop_closes_a_long_trade_after_the_configured_retracement(engine, database, trade_repository, stats_repository):
    _create_waiting_signal(database, "BTCUSDT", SignalDirection.LONG, entry=100.0, sl=97.0, tp1=120.0)
    engine.run_tick({"BTCUSDT": 100.0})
    engine.run_tick({"BTCUSDT": 110.0})  # best price becomes 110

    result = engine.run_tick({"BTCUSDT": 107.9})  # retraced 2.1 from 110 -- >= trailing_distance (2.0)

    assert len(result.trailing_stop_exit_trade_ids) == 1
    trade = trade_repository.get_by_id(result.trailing_stop_exit_trade_ids[0])
    assert trade.status == TradeStatus.TRAILING_STOP_EXIT
    assert trade.exit_price == pytest.approx(107.9)
    assert trade.realized_pnl_percent == pytest.approx(7.9)  # still a net gain
    assert trade not in trade_repository.get_active_trades()

    stats = stats_repository.get("BTCUSDT")
    assert stats.winning_signals == 1  # positive PnL -> counted as a win


def test_trailing_stop_does_not_close_a_long_trade_below_the_configured_retracement(engine, database, trade_repository):
    _create_waiting_signal(database, "BTCUSDT", SignalDirection.LONG, entry=100.0, sl=97.0, tp1=120.0)
    engine.run_tick({"BTCUSDT": 100.0})
    engine.run_tick({"BTCUSDT": 110.0})

    result = engine.run_tick({"BTCUSDT": 108.5})  # retraced only 1.5 -- below the 2.0 trailing distance

    assert result.trailing_stop_exit_trade_ids == ()
    assert len(trade_repository.get_active_trades()) == 1


def test_trailing_stop_closes_a_short_trade_after_the_configured_retracement(engine, database, trade_repository):
    _create_waiting_signal(database, "ETHUSDT", SignalDirection.SHORT, entry=100.0, sl=103.0, tp1=80.0)
    engine.run_tick({"ETHUSDT": 100.0})
    engine.run_tick({"ETHUSDT": 90.0})  # best price becomes 90 (favorable for a short)

    result = engine.run_tick({"ETHUSDT": 92.1})  # retraced 2.1 from 90 -- >= trailing_distance (2.0)

    assert len(result.trailing_stop_exit_trade_ids) == 1
    trade = trade_repository.get_by_id(result.trailing_stop_exit_trade_ids[0])
    assert trade.status == TradeStatus.TRAILING_STOP_EXIT
    assert trade.realized_pnl_percent == pytest.approx(7.9)


def test_stop_loss_takes_priority_over_a_simultaneous_trailing_stop_signal(engine, database, trade_repository):
    """If a single tick's price would satisfy both the hard stop-loss AND a trailing retracement, the hard stop wins -- checked first."""
    _create_waiting_signal(database, "BTCUSDT", SignalDirection.LONG, entry=100.0, sl=97.0, tp1=120.0)
    engine.run_tick({"BTCUSDT": 100.0})
    engine.run_tick({"BTCUSDT": 110.0})  # best price becomes 110

    result = engine.run_tick({"BTCUSDT": 96.0})  # below the hard stop AND far past the trailing distance

    assert result.trailing_stop_exit_trade_ids == ()
    assert len(result.stop_loss_trade_ids) == 1


def test_take_profit_takes_priority_over_a_simultaneous_trailing_stop_signal(engine, database, trade_repository):
    _create_waiting_signal(database, "BTCUSDT", SignalDirection.LONG, entry=100.0, sl=97.0, tp1=105.0)
    engine.run_tick({"BTCUSDT": 100.0})

    result = engine.run_tick({"BTCUSDT": 106.0})  # crosses TP1 (105) on the very first profitable tick

    assert result.trailing_stop_exit_trade_ids == ()
    assert len(result.tp1_hit_trade_ids) == 1


def test_trailing_stop_disabled_never_closes_a_trade_early(database, signal_repository, trade_repository, stats_repository):
    from config.schema import RiskConfig

    config = PlatformConfig(risk=RiskConfig(trailing_stop_enabled=False))
    engine = PositionMonitorEngine(
        config=config, signal_repository=signal_repository,
        trade_repository=trade_repository, coin_statistics_repository=stats_repository,
    )
    _create_waiting_signal(database, "BTCUSDT", SignalDirection.LONG, entry=100.0, sl=97.0, tp1=120.0)
    engine.run_tick({"BTCUSDT": 100.0})
    engine.run_tick({"BTCUSDT": 110.0})

    result = engine.run_tick({"BTCUSDT": 107.9})  # would have triggered if enabled

    assert result.trailing_stop_exit_trade_ids == ()
    assert len(trade_repository.get_active_trades()) == 1
