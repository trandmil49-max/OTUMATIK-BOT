"""
Unit tests for Module 3 (infrastructure/database/ + core/models.py).

Every test builds its own `Database` against a `tmp_path` SQLite file
(never the process-wide singleton), so tests never share state or leak
file handles into each other.

Run with:
    pytest tests/unit/test_database.py -v
"""

from datetime import datetime, timedelta, timezone

import pytest

from config.schema import PlatformConfig
from core.models import (
    BotHealthSnapshot,
    BtcStatisticsSnapshot,
    Coin,
    CoinClassification,
    CoinProfile,
    ConfidenceGrade,
    ConfigSnapshot,
    ErrorEvent,
    FilterPerformance,
    MarketStatisticsSnapshot,
    MissedOpportunity,
    Rejection,
    RejectionReason,
    Report,
    ReportType,
    Signal,
    SignalDirection,
    SignalScoreComponent,
    Trade,
    TradeStatus,
)
from infrastructure.database.connection import Database
from infrastructure.database.repositories import (
    BotHealthRepository,
    BtcStatisticsRepository,
    CoinProfileRepository,
    CoinRepository,
    CoinStatisticsRepository,
    ConfigSnapshotRepository,
    ErrorEventRepository,
    FilterPerformanceRepository,
    MarketStatisticsRepository,
    MissedOpportunityRepository,
    RejectionRepository,
    ReportRepository,
    SignalRepository,
    TradeRepository,
)
from infrastructure.database.schema import get_schema_version, run_migrations

EXPECTED_TABLES = {
    "schema_migrations",
    "coins",
    "coin_profiles",
    "coin_statistics",
    "signals",
    "signal_score_breakdown",
    "trades",
    "rejections",
    "missed_opportunities",
    "btc_statistics",
    "market_statistics",
    "reports",
    "filter_performance",
    "bot_health",
    "error_events",
    "config_snapshots",
}


@pytest.fixture
def db(tmp_path) -> Database:
    """A fresh, fully migrated Database backed by a tmp_path SQLite file."""
    database = Database(db_path=str(tmp_path / "test_platform.db"), config=PlatformConfig())
    run_migrations(database)
    return database


@pytest.fixture
def seeded_coin(db) -> str:
    """Every FK-referencing table needs a `coins` row to point at first."""
    symbol = "BTCUSDT"
    CoinRepository(database=db).upsert(Coin(symbol=symbol, base_asset="BTC"))
    return symbol


def _make_signal(symbol: str, **overrides) -> Signal:
    defaults = dict(
        symbol=symbol,
        direction=SignalDirection.LONG,
        entry_price=50000.0,
        stop_loss=49000.0,
        take_profit_1=51000.0,
        risk_reward_ratio=2.5,
        confidence_score=87.5,
        confidence_grade=ConfidenceGrade.EXCELLENT,
    )
    defaults.update(overrides)
    return Signal(**defaults)


# ─────────────────────────────────────────────────────────────────────────
# MIGRATIONS
# ─────────────────────────────────────────────────────────────────────────


def test_run_migrations_creates_all_expected_tables(db):
    with db.read_connection() as conn:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    table_names = {row["name"] for row in rows}
    assert EXPECTED_TABLES <= table_names


def test_run_migrations_is_idempotent(db):
    first_run_applied = run_migrations(db)  # already applied by the `db` fixture
    assert first_run_applied == []
    assert get_schema_version(db) == 7


def test_get_schema_version_reflects_applied_migrations(db):
    assert get_schema_version(db) == 7


def test_trades_table_has_nullable_tp1_exit_price_column(db, seeded_coin):
    """Migration version=2: needed to report TP1's own partial profit % -- see MIGRATIONS' description."""
    signal_repo = SignalRepository(database=db)
    trade_repo = TradeRepository(database=db)
    signal = signal_repo.create(_make_signal(seeded_coin))
    trade = trade_repo.create(
        Trade(
            signal_id=signal.id,
            symbol=seeded_coin,
            direction=SignalDirection.LONG,
            entry_price=50000.0,
            entry_time=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
            initial_stop_loss=49000.0,
            current_stop_loss=49000.0,
            take_profit_1=51000.0,
            confidence_score=87.5,
        )
    )

    assert trade_repo.get_by_id(trade.id).tp1_exit_price is None  # defaults to NULL

    with db.transaction() as conn:
        conn.execute("UPDATE trades SET tp1_exit_price = ? WHERE id = ?", (50750.0, trade.id))
    assert trade_repo.get_by_id(trade.id).tp1_exit_price == 50750.0


# ─────────────────────────────────────────────────────────────────────────
# CONNECTION / TRANSACTION SEMANTICS
# ─────────────────────────────────────────────────────────────────────────


def test_transaction_commits_on_success(db, seeded_coin):
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO coins (symbol, base_asset, first_seen_at, last_seen_at) VALUES (?, ?, ?, ?)",
            ("ETHUSDT", "ETH", "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
        )
    with db.read_connection() as conn:
        row = conn.execute("SELECT * FROM coins WHERE symbol = 'ETHUSDT'").fetchone()
    assert row is not None


def test_transaction_rolls_back_on_application_exception(db, seeded_coin):
    class BoomError(Exception):
        pass

    with pytest.raises(BoomError):
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO coins (symbol, base_asset, first_seen_at, last_seen_at) VALUES (?, ?, ?, ?)",
                ("SOLUSDT", "SOL", "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
            )
            raise BoomError("something went wrong after the write")

    with db.read_connection() as conn:
        row = conn.execute("SELECT * FROM coins WHERE symbol = 'SOLUSDT'").fetchone()
    assert row is None  # rolled back, not partially committed


def test_foreign_key_violation_raises_database_error(db):
    from system.exceptions import DatabaseError

    repo = SignalRepository(database=db)
    orphan_signal = _make_signal(symbol="NEVERUPSERTEDUSDT")

    with pytest.raises(DatabaseError):
        repo.create(orphan_signal)


# ─────────────────────────────────────────────────────────────────────────
# COIN / COIN PROFILE
# ─────────────────────────────────────────────────────────────────────────


def test_coin_repository_upsert_and_get(db):
    repo = CoinRepository(database=db)
    repo.upsert(Coin(symbol="BTCUSDT", base_asset="BTC"))

    fetched = repo.get("BTCUSDT")
    assert fetched is not None
    assert fetched.quote_asset == "USDT"
    assert fetched.is_active is True
    assert fetched.first_seen_at.tzinfo is not None  # never a naive datetime


def test_coin_repository_upsert_updates_existing_row_without_duplicating(db):
    repo = CoinRepository(database=db)
    repo.upsert(Coin(symbol="BTCUSDT", base_asset="BTC", status="TRADING"))
    repo.upsert(Coin(symbol="BTCUSDT", base_asset="BTC", status="BREAK"))

    assert repo.get("BTCUSDT").status == "BREAK"
    assert len(repo.list_active()) == 1


def test_coin_profile_repository_roundtrip(db, seeded_coin):
    repo = CoinProfileRepository(database=db)
    repo.upsert(
        CoinProfile(
            symbol=seeded_coin,
            liquidity_score=88.0,
            classification=CoinClassification.ULTRA_HIGH_QUALITY,
            coin_trust_score=91.5,
        )
    )

    fetched = repo.get(seeded_coin)
    assert fetched.classification == CoinClassification.ULTRA_HIGH_QUALITY
    assert fetched.coin_trust_score == 91.5


# ─────────────────────────────────────────────────────────────────────────
# COIN STATISTICS -- STREAK ACCUMULATION (explicitly flagged in PROJECT_STATUS.md)
# ─────────────────────────────────────────────────────────────────────────


def test_coin_statistics_streak_accumulation_over_multiple_outcomes(db, seeded_coin):
    """
    Sequence: WIN, WIN, LOSS, WIN, WIN, WIN, LOSS, LOSS, LOSS, LOSS.

    Expected running peaks:
        after WIN, WIN            -> longest_winning_streak = 2
        after ... LOSS            -> longest_losing_streak  = 1
        after ... WIN, WIN, WIN   -> longest_winning_streak = 3 (new peak)
        after ... LOSS x4         -> longest_losing_streak  = 4 (new peak)
    Final state: current_streak = -4, longest_winning_streak = 3,
    longest_losing_streak = 4, win_rate_percent = 50.0.
    """
    repo = CoinStatisticsRepository(database=db)
    outcomes = ["WIN", "WIN", "LOSS", "WIN", "WIN", "WIN", "LOSS", "LOSS", "LOSS", "LOSS"]

    stats = None
    for outcome in outcomes:
        stats = repo.record_trade_outcome(seeded_coin, outcome, rr=2.0, confidence_score=80.0)

    assert stats.total_signals == 10
    assert stats.winning_signals == 5
    assert stats.losing_signals == 5
    assert stats.win_rate_percent == pytest.approx(50.0)
    assert stats.current_streak == -4
    assert stats.longest_winning_streak == 3
    assert stats.longest_losing_streak == 4

    # Persisted, not just returned in-memory:
    reloaded = repo.get(seeded_coin)
    assert reloaded.longest_winning_streak == 3
    assert reloaded.longest_losing_streak == 4
    assert reloaded.current_streak == -4


def test_coin_statistics_loss_streak_does_not_affect_longest_winning_streak(db, seeded_coin):
    repo = CoinStatisticsRepository(database=db)
    repo.record_trade_outcome(seeded_coin, "WIN")
    repo.record_trade_outcome(seeded_coin, "WIN")
    stats = repo.record_trade_outcome(seeded_coin, "LOSS")

    assert stats.current_streak == -1
    assert stats.longest_winning_streak == 2  # untouched by the loss


def test_coin_statistics_running_average_rr_is_correct(db, seeded_coin):
    repo = CoinStatisticsRepository(database=db)
    repo.record_trade_outcome(seeded_coin, "WIN", rr=1.0)
    repo.record_trade_outcome(seeded_coin, "WIN", rr=2.0)
    stats = repo.record_trade_outcome(seeded_coin, "LOSS", rr=3.0)

    assert stats.average_rr == pytest.approx((1.0 + 2.0 + 3.0) / 3)


# ─────────────────────────────────────────────────────────────────────────
# SIGNALS
# ─────────────────────────────────────────────────────────────────────────


def test_signal_repository_create_and_get_with_score_breakdown(db, seeded_coin):
    repo = SignalRepository(database=db)
    signal = _make_signal(seeded_coin)
    components = [
        SignalScoreComponent(signal_id=0, category="trend", points=18.0),
        SignalScoreComponent(signal_id=0, category="structure", points=15.0),
        SignalScoreComponent(signal_id=0, category="penalty", points=-3.0),
    ]

    created = repo.create(signal, components)
    assert created.id is not None

    fetched = repo.get_by_id(created.id)
    assert fetched.direction == SignalDirection.LONG
    assert fetched.confidence_grade == ConfidenceGrade.EXCELLENT
    assert fetched.created_at.tzinfo is not None

    breakdown = repo.get_score_breakdown(created.id)
    assert [c.category for c in breakdown] == ["trend", "structure", "penalty"]
    assert sum(c.points for c in breakdown) == 30.0


def test_signal_repository_find_by_symbol_and_status(db, seeded_coin):
    repo = SignalRepository(database=db)
    created = repo.create(_make_signal(seeded_coin))

    waiting = repo.find_by_symbol_and_status(seeded_coin, TradeStatus.WAITING)
    assert len(waiting) == 1

    repo.update_status(created.id, TradeStatus.ACTIVE)
    assert repo.find_by_symbol_and_status(seeded_coin, TradeStatus.WAITING) == []
    assert len(repo.find_by_symbol_and_status(seeded_coin, TradeStatus.ACTIVE)) == 1


def test_signal_repository_find_most_recent_by_symbol(db, seeded_coin):
    repo = SignalRepository(database=db)
    assert repo.find_most_recent_by_symbol(seeded_coin) is None

    older = repo.create(_make_signal(seeded_coin, created_at=datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)))
    newer = repo.create(_make_signal(seeded_coin, created_at=datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)))

    most_recent = repo.find_most_recent_by_symbol(seeded_coin)
    assert most_recent.id == newer.id
    assert most_recent.id != older.id


def test_signal_repository_find_most_recent_by_symbol_ignores_status(db, seeded_coin):
    """
    Unlike find_by_symbol_and_status(), this must return a signal
    regardless of its current status -- it backs the cooldown check
    (RiskConfig.signal_cooldown_minutes), which cares about recency, not
    whether the signal is still WAITING/ACTIVE.
    """
    repo = SignalRepository(database=db)
    created = repo.create(_make_signal(seeded_coin))
    repo.update_status(created.id, TradeStatus.STOP_LOSS)

    most_recent = repo.find_most_recent_by_symbol(seeded_coin)
    assert most_recent is not None
    assert most_recent.id == created.id


def test_signal_repository_get_signals_between_filters_by_created_at(db, seeded_coin):
    repo = SignalRepository(database=db)

    before_window = repo.create(
        _make_signal(seeded_coin, created_at=datetime(2026, 5, 31, 23, 0, tzinfo=timezone.utc))
    )
    inside_window = repo.create(
        _make_signal(seeded_coin, created_at=datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc))
    )
    after_window = repo.create(
        _make_signal(seeded_coin, created_at=datetime(2026, 6, 2, 1, 0, tzinfo=timezone.utc))
    )

    period_start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    period_end = datetime(2026, 6, 2, tzinfo=timezone.utc)
    result_ids = {s.id for s in repo.get_signals_between(period_start, period_end)}

    assert result_ids == {inside_window.id}
    assert before_window.id not in result_ids
    assert after_window.id not in result_ids


def test_rejection_repository_get_rejections_between_filters_by_created_at(db, seeded_coin):
    repo = RejectionRepository(database=db)

    def _reject(created_at):
        return repo.create(
            Rejection(
                symbol=seeded_coin, primary_reason=RejectionReason.WEAK_TREND,
                rejected_filters=["trend_filter", "volume_filter"], created_at=created_at,
            )
        )

    before = _reject(datetime(2026, 5, 31, 23, 0, tzinfo=timezone.utc))
    inside = _reject(datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc))
    after = _reject(datetime(2026, 6, 2, 1, 0, tzinfo=timezone.utc))

    result_ids = {
        r.id for r in repo.get_rejections_between(
            datetime(2026, 6, 1, tzinfo=timezone.utc), datetime(2026, 6, 2, tzinfo=timezone.utc)
        )
    }
    assert result_ids == {inside.id}
    assert before.id not in result_ids
    assert after.id not in result_ids


def test_missed_opportunity_repository_get_between_filters_by_rejected_at(db, seeded_coin):
    rejection_repo = RejectionRepository(database=db)
    missed_repo = MissedOpportunityRepository(database=db)

    rejection = rejection_repo.create(
        Rejection(symbol=seeded_coin, primary_reason=RejectionReason.POOR_RISK_REWARD)
    )

    def _missed(rejected_at):
        return missed_repo.create(
            MissedOpportunity(
                symbol=seeded_coin, rejected_at=rejected_at, reject_reason=RejectionReason.POOR_RISK_REWARD,
                price_at_rejection=100.0, subsequent_max_move_percent=6.5, evaluation_window_hours=24,
                rejection_id=rejection.id,
            )
        )

    before = _missed(datetime(2026, 5, 31, 23, 0, tzinfo=timezone.utc))
    inside = _missed(datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc))
    after = _missed(datetime(2026, 6, 2, 1, 0, tzinfo=timezone.utc))

    result_ids = {
        m.id for m in missed_repo.get_between(
            datetime(2026, 6, 1, tzinfo=timezone.utc), datetime(2026, 6, 2, tzinfo=timezone.utc)
        )
    }
    assert result_ids == {inside.id}
    assert before.id not in result_ids
    assert after.id not in result_ids


# ─────────────────────────────────────────────────────────────────────────
# TRADES
# ─────────────────────────────────────────────────────────────────────────


def test_trade_repository_full_lifecycle(db, seeded_coin):
    signal_repo = SignalRepository(database=db)
    trade_repo = TradeRepository(database=db)

    signal = signal_repo.create(_make_signal(seeded_coin))
    entry_time = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)

    trade = trade_repo.create(
        Trade(
            signal_id=signal.id,
            symbol=seeded_coin,
            direction=SignalDirection.LONG,
            entry_price=50000.0,
            entry_time=entry_time,
            initial_stop_loss=49000.0,
            current_stop_loss=49000.0,
            take_profit_1=51000.0,
            confidence_score=87.5,
        )
    )
    assert trade.id is not None
    assert trade in trade_repo.get_active_trades()

    tp1_time = entry_time + timedelta(minutes=30)
    trade_repo.record_tp1_hit(trade.id, tp1_time, 50750.0)
    tp1_trade = trade_repo.get_by_id(trade.id)
    assert tp1_trade.status == TradeStatus.TP1_HIT
    assert tp1_trade.tp1_exit_price == 50750.0
    assert tp1_trade.tp1_pnl_percent == pytest.approx(1.5)  # (50750-50000)/50000 * 100
    assert tp1_trade.current_stop_loss == 49000.0  # never moved -- no break-even step in the single-TP model

    exit_time = entry_time + timedelta(hours=2)
    trade_repo.close_trade(
        trade.id,
        status=TradeStatus.TP1_HIT,
        exit_price=50750.0,
        exit_time=exit_time,
        realized_pnl_percent=1.5,
        exit_reason="Take profit hit",
    )
    closed = trade_repo.get_by_id(trade.id)
    assert closed.is_closed is True
    assert closed.duration_seconds == 7200
    assert closed.tp1_hit_at == tp1_time
    assert closed not in trade_repo.get_active_trades()


def test_trade_repository_get_active_trades_excludes_closed(db, seeded_coin):
    signal_repo = SignalRepository(database=db)
    trade_repo = TradeRepository(database=db)
    entry_time = datetime(2026, 1, 1, tzinfo=timezone.utc)

    open_signal = signal_repo.create(_make_signal(seeded_coin))
    closed_signal = signal_repo.create(_make_signal(seeded_coin))

    open_trade = trade_repo.create(
        Trade(
            signal_id=open_signal.id, symbol=seeded_coin, direction=SignalDirection.LONG,
            entry_price=100.0, entry_time=entry_time, initial_stop_loss=95.0,
            current_stop_loss=95.0, take_profit_1=105.0, confidence_score=80.0,
        )
    )
    closed_trade = trade_repo.create(
        Trade(
            signal_id=closed_signal.id, symbol=seeded_coin, direction=SignalDirection.SHORT,
            entry_price=100.0, entry_time=entry_time, initial_stop_loss=105.0,
            current_stop_loss=105.0, take_profit_1=95.0, confidence_score=80.0,
        )
    )
    trade_repo.close_trade(
        closed_trade.id, status=TradeStatus.STOP_LOSS, exit_price=105.0,
        exit_time=entry_time + timedelta(hours=1), realized_pnl_percent=-5.0, exit_reason="SL hit",
    )

    active_ids = {t.id for t in trade_repo.get_active_trades()}
    assert active_ids == {open_trade.id}


def test_trade_repository_get_closed_trades_between_filters_by_exit_time(db, seeded_coin):
    signal_repo = SignalRepository(database=db)
    trade_repo = TradeRepository(database=db)
    entry_time = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def _open_and_close(exit_time, *, status, pnl):
        signal = signal_repo.create(_make_signal(seeded_coin))
        trade = trade_repo.create(
            Trade(
                signal_id=signal.id, symbol=seeded_coin, direction=SignalDirection.LONG,
                entry_price=100.0, entry_time=entry_time, initial_stop_loss=95.0,
                current_stop_loss=95.0, take_profit_1=105.0, confidence_score=80.0,
            )
        )
        trade_repo.close_trade(
            trade.id, status=status, exit_price=100.0, exit_time=exit_time,
            realized_pnl_percent=pnl, exit_reason="test",
        )
        return trade.id

    before_window = _open_and_close(datetime(2026, 5, 31, 23, 0, tzinfo=timezone.utc), status=TradeStatus.TP1_HIT, pnl=5.0)
    inside_window = _open_and_close(datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc), status=TradeStatus.STOP_LOSS, pnl=-3.0)
    after_window = _open_and_close(datetime(2026, 6, 2, 1, 0, tzinfo=timezone.utc), status=TradeStatus.TP1_HIT, pnl=4.0)

    still_open_signal = signal_repo.create(_make_signal(seeded_coin))
    trade_repo.create(
        Trade(
            signal_id=still_open_signal.id, symbol=seeded_coin, direction=SignalDirection.LONG,
            entry_price=100.0, entry_time=datetime(2026, 6, 1, 6, 0, tzinfo=timezone.utc),
            initial_stop_loss=95.0, current_stop_loss=95.0, take_profit_1=105.0,
            confidence_score=80.0,
        )
    )  # never closed -- exit_time is NULL, must never appear in a period query

    period_start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    period_end = datetime(2026, 6, 2, tzinfo=timezone.utc)
    result_ids = {t.id for t in trade_repo.get_closed_trades_between(period_start, period_end)}

    assert result_ids == {inside_window}
    assert before_window not in result_ids
    assert after_window not in result_ids


# ─────────────────────────────────────────────────────────────────────────
# REJECTIONS / MISSED OPPORTUNITIES
# ─────────────────────────────────────────────────────────────────────────


def test_rejection_repository_roundtrip_including_json_filters(db):
    repo = RejectionRepository(database=db)
    rejection = repo.create(
        Rejection(
            symbol="DOGEUSDT",
            direction=SignalDirection.LONG,
            primary_reason=RejectionReason.WEAK_MARKET_STRUCTURE,
            secondary_reason=RejectionReason.LOW_CONFIDENCE,
            rejected_filters=["structure_filter", "confidence_filter"],
            confidence_score=42.0,
        )
    )

    fetched = repo.get_by_id(rejection.id)
    assert fetched.primary_reason == RejectionReason.WEAK_MARKET_STRUCTURE
    assert fetched.rejected_filters == ["structure_filter", "confidence_filter"]


def test_rejection_repository_count_by_reason_since(db):
    repo = RejectionRepository(database=db)
    repo.create(Rejection(symbol="A", primary_reason=RejectionReason.LOW_LIQUIDITY))
    repo.create(Rejection(symbol="B", primary_reason=RejectionReason.LOW_LIQUIDITY))
    repo.create(Rejection(symbol="C", primary_reason=RejectionReason.BITCOIN_CONFLICT))

    counts = repo.count_by_reason_since("2000-01-01T00:00:00+00:00")
    assert counts[RejectionReason.LOW_LIQUIDITY.value] == 2
    assert counts[RejectionReason.BITCOIN_CONFLICT.value] == 1


def test_missed_opportunity_repository_roundtrip(db):
    rejection = RejectionRepository(database=db).create(
        Rejection(symbol="PEPEUSDT", primary_reason=RejectionReason.LOW_CONFIDENCE)
    )
    repo = MissedOpportunityRepository(database=db)
    repo.create(
        MissedOpportunity(
            rejection_id=rejection.id,
            symbol="PEPEUSDT",
            rejected_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
            reject_reason=RejectionReason.LOW_CONFIDENCE,
            price_at_rejection=0.00001234,
            subsequent_max_move_percent=27.5,
            evaluation_window_hours=24,
        )
    )

    results = repo.get_by_symbol("PEPEUSDT")
    assert len(results) == 1
    assert results[0].subsequent_max_move_percent == 27.5


# ─────────────────────────────────────────────────────────────────────────
# MARKET / BITCOIN STATISTICS
# ─────────────────────────────────────────────────────────────────────────


def test_btc_statistics_repository_get_latest_returns_most_recent(db):
    repo = BtcStatisticsRepository(database=db)
    repo.create(
        BtcStatisticsSnapshot(
            snapshot_time=datetime(2026, 1, 1, tzinfo=timezone.utc),
            trend="BULLISH", health_score=70.0, volatility_score=30.0, price=95000.0,
        )
    )
    repo.create(
        BtcStatisticsSnapshot(
            snapshot_time=datetime(2026, 1, 2, tzinfo=timezone.utc),
            trend="STRONG_BULLISH", health_score=82.0, volatility_score=25.0, price=97500.0,
        )
    )

    latest = repo.get_latest()
    assert latest.trend == "STRONG_BULLISH"
    assert latest.price == 97500.0


def test_market_statistics_repository_get_latest(db):
    repo = MarketStatisticsRepository(database=db)
    repo.create(
        MarketStatisticsSnapshot(
            snapshot_time=datetime(2026, 1, 1, tzinfo=timezone.utc),
            market_health_score=65.0,
            market_state="HEALTHY_BULL_MARKET",
        )
    )
    latest = repo.get_latest()
    assert latest.market_state == "HEALTHY_BULL_MARKET"


# ─────────────────────────────────────────────────────────────────────────
# REPORTS / FILTER PERFORMANCE
# ─────────────────────────────────────────────────────────────────────────


def test_report_repository_roundtrip_with_json_content(db):
    repo = ReportRepository(database=db)
    period_start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    period_end = period_start + timedelta(days=1)

    repo.create(
        Report(
            report_type=ReportType.DAILY,
            period_start=period_start,
            period_end=period_end,
            content={"total_signals": 12, "win_rate_percent": 66.7},
            turkish_analysis="Bugün piyasa sağlıklıydı.",
            overall_grade="A",
        )
    )

    fetched = repo.get_latest(ReportType.DAILY)
    assert fetched.content["total_signals"] == 12
    assert fetched.overall_grade == "A"


def test_filter_performance_repository_history_ordered_by_period(db):
    repo = FilterPerformanceRepository(database=db)
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    repo.create(
        FilterPerformance(
            filter_name="liquidity_filter", period_start=base, period_end=base + timedelta(days=1),
            trades_rejected=10, saved_losses_count=3,
        )
    )
    repo.create(
        FilterPerformance(
            filter_name="liquidity_filter", period_start=base + timedelta(days=1),
            period_end=base + timedelta(days=2), trades_rejected=8, saved_losses_count=5,
        )
    )

    history = repo.get_history("liquidity_filter")
    assert len(history) == 2
    assert history[0].period_start < history[1].period_start


# ─────────────────────────────────────────────────────────────────────────
# SYSTEM: BOT HEALTH / ERROR EVENTS / CONFIG SNAPSHOTS
# ─────────────────────────────────────────────────────────────────────────


def test_bot_health_repository_get_latest(db):
    repo = BotHealthRepository(database=db)
    repo.create(BotHealthSnapshot(snapshot_time=datetime(2026, 1, 1, tzinfo=timezone.utc), status="HEALTHY"))
    repo.create(BotHealthSnapshot(snapshot_time=datetime(2026, 1, 2, tzinfo=timezone.utc), status="WARNING"))

    assert repo.get_latest().status == "WARNING"


def test_error_event_repository_create_and_resolve(db):
    repo = ErrorEventRepository(database=db)
    event = repo.create(
        ErrorEvent(
            occurred_at=datetime.now(timezone.utc),
            severity="CRITICAL",
            category="api",
            message="Binance API unreachable for 5 consecutive minutes",
            context={"endpoint": "/fapi/v1/klines"},
        )
    )

    assert len(repo.get_unresolved()) == 1
    repo.mark_resolved(event.id)
    assert repo.get_unresolved() == []


def test_config_snapshot_repository_roundtrip(db):
    repo = ConfigSnapshotRepository(database=db)
    repo.create(
        ConfigSnapshot(
            captured_at=datetime.now(timezone.utc),
            strategy_profile="balanced",
            schema_version="1.0.0",
            config_json={"risk": {"min_risk_reward_ratio": 1.5}},
            reason="startup",
        )
    )

    latest = repo.get_latest()
    assert latest.strategy_profile == "balanced"
    assert latest.config_json["risk"]["min_risk_reward_ratio"] == 1.5
