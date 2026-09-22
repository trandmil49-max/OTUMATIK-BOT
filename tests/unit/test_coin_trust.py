"""
Unit tests for engines/coin_trust.py (Module 11).

Run with:
    pytest tests/unit/test_coin_trust.py -v
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from config.schema import PlatformConfig
from core.models import Coin, CoinClassification, CoinProfile, CoinStatistics
from engines.coin_trust import CoinTrustEngine
from infrastructure.database.connection import Database
from infrastructure.database.repositories.coin_repository import (
    CoinProfileRepository,
    CoinRepository,
    CoinStatisticsRepository,
)
from infrastructure.database.schema import run_migrations


@pytest.fixture
def config() -> PlatformConfig:
    return PlatformConfig()  # min_trades_for_trust_score=10, max_trust_bonus=5.0, max_trust_penalty=10.0, default=50.0


@pytest.fixture
def database(tmp_path) -> Database:
    db = Database(db_path=str(tmp_path / "coin_trust_test.db"), config=PlatformConfig())
    run_migrations(db)
    return db


@pytest.fixture
def profile_repository(database: Database) -> CoinProfileRepository:
    return CoinProfileRepository(database=database)


@pytest.fixture
def statistics_repository(database: Database) -> CoinStatisticsRepository:
    return CoinStatisticsRepository(database=database)


@pytest.fixture
def engine(config, profile_repository, statistics_repository) -> CoinTrustEngine:
    return CoinTrustEngine(config=config, profile_repository=profile_repository, statistics_repository=statistics_repository)


def _seed_coin_and_profile(database: Database, symbol: str, **profile_overrides) -> None:
    CoinRepository(database=database).upsert(Coin(symbol=symbol, base_asset=symbol.replace("USDT", "")))
    defaults = dict(
        symbol=symbol, liquidity_score=80.0, volatility_score=20.0, trend_reliability_score=90.0,
        spread_quality_score=85.0, historical_stability_score=75.0,
    )
    defaults.update(profile_overrides)
    CoinProfileRepository(database=database).upsert(CoinProfile(**defaults))


def _seed_statistics(database: Database, symbol: str, **stats_overrides) -> None:
    defaults = dict(symbol=symbol, total_signals=20, win_rate_percent=70.0, current_streak=3)
    defaults.update(stats_overrides)
    stats = CoinStatistics(**defaults)
    # Direct INSERT ... ON CONFLICT (mirrors CoinStatisticsRepository's own
    # write path) so tests can seed arbitrary win_rate/streak combinations
    # directly, instead of only what record_trade_outcome()'s incremental
    # accumulation logic (already covered by Module 3's own tests) would produce.
    with database.transaction() as conn:
        conn.execute(
            """
            INSERT INTO coin_statistics (
                symbol, total_signals, winning_signals, losing_signals, tp1_count,
                sl_count, average_rr, average_confidence, average_duration_seconds,
                win_rate_percent, current_streak, longest_winning_streak, longest_losing_streak, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
                total_signals=excluded.total_signals, win_rate_percent=excluded.win_rate_percent,
                current_streak=excluded.current_streak, updated_at=excluded.updated_at
            """,
            (
                stats.symbol, stats.total_signals, stats.winning_signals, stats.losing_signals,
                stats.tp1_count, stats.sl_count,
                stats.average_rr, stats.average_confidence, stats.average_duration_seconds,
                stats.win_rate_percent, stats.current_streak, stats.longest_winning_streak,
                stats.longest_losing_streak, datetime.now(timezone.utc).isoformat(),
            ),
        )


# ─────────────────────────────────────────────────────────────────────────
# NEW / UNKNOWN COINS
# ─────────────────────────────────────────────────────────────────────────


def test_analyze_unknown_symbol_returns_default_score_and_new_listing(engine):
    result = engine.analyze("NEVERSEENUSDT")

    assert result.trust_score == pytest.approx(50.0)
    assert result.classification == CoinClassification.NEW_LISTING
    assert result.total_signals == 0
    assert result.components.data_confidence == pytest.approx(0.0)


def test_analyze_unknown_symbol_does_not_crash_without_a_profile_to_persist_to(engine, profile_repository):
    engine.analyze("NEVERSEENUSDT")
    assert profile_repository.get("NEVERSEENUSDT") is None  # nothing to persist to -- correctly a no-op


# ─────────────────────────────────────────────────────────────────────────
# FULL / PARTIAL DATA CONFIDENCE  (hand-computed)
# ─────────────────────────────────────────────────────────────────────────


def test_analyze_full_confidence_matches_hand_computed_score(database, engine):
    _seed_coin_and_profile(database, "BTCUSDT")
    _seed_statistics(database, "BTCUSDT", total_signals=20, win_rate_percent=70.0, current_streak=3)

    result = engine.analyze("BTCUSDT")

    # profile_component = mean(80, 100-20, 90, 85, 75) = mean(80,80,90,85,75) = 82.0
    # streak_bonus = min(5.0, 3*1.0) = 3.0 -> track_record = 70.0+3.0 = 73.0
    # raw = 82.0*0.5 + 73.0*0.5 = 77.5; data_confidence = min(1, 20/10) = 1.0
    # final = 50.0 + (77.5-50.0)*1.0 = 77.5
    assert result.trust_score == pytest.approx(77.5)
    assert result.classification == CoinClassification.HIGH_QUALITY  # 70 <= 77.5 < 85
    assert result.components.data_confidence == pytest.approx(1.0)


def test_analyze_partial_confidence_blends_toward_default(database, engine):
    _seed_coin_and_profile(database, "ETHUSDT")
    _seed_statistics(database, "ETHUSDT", total_signals=5, win_rate_percent=70.0, current_streak=3)

    result = engine.analyze("ETHUSDT")

    # Same raw_score (77.5) as the full-confidence case, but data_confidence = 5/10 = 0.5:
    # final = 50.0 + (77.5-50.0)*0.5 = 63.75
    assert result.trust_score == pytest.approx(63.75)
    assert result.classification == CoinClassification.MEDIUM_QUALITY  # 50 <= 63.75 < 70
    assert result.components.data_confidence == pytest.approx(0.5)


def test_analyze_with_profile_but_no_statistics_uses_default_track_record(database, engine):
    _seed_coin_and_profile(database, "SOLUSDT")
    # No statistics row inserted.
    result = engine.analyze("SOLUSDT")

    assert result.components.win_rate == pytest.approx(50.0)  # new_coin_default_trust_score
    assert result.total_signals == 0
    assert result.classification == CoinClassification.NEW_LISTING


# ─────────────────────────────────────────────────────────────────────────
# STREAK BONUS / PENALTY CAPS
# ─────────────────────────────────────────────────────────────────────────


def test_winning_streak_bonus_is_capped_by_config(database, engine):
    _seed_coin_and_profile(database, "BTCUSDT")
    _seed_statistics(database, "BTCUSDT", total_signals=20, win_rate_percent=70.0, current_streak=10)

    result = engine.analyze("BTCUSDT")

    assert result.components.streak_bonus == pytest.approx(5.0)  # capped at max_trust_bonus, not 10.0
    assert result.components.streak_penalty == pytest.approx(0.0)


def test_losing_streak_penalty_is_capped_by_config(database, engine):
    _seed_coin_and_profile(database, "BTCUSDT")
    _seed_statistics(database, "BTCUSDT", total_signals=20, win_rate_percent=40.0, current_streak=-20)

    result = engine.analyze("BTCUSDT")

    assert result.components.streak_penalty == pytest.approx(10.0)  # capped at max_trust_penalty, not 20.0
    assert result.components.streak_bonus == pytest.approx(0.0)


def test_track_record_component_is_clamped_to_valid_range(database, engine):
    _seed_coin_and_profile(database, "BTCUSDT")
    _seed_statistics(database, "BTCUSDT", total_signals=20, win_rate_percent=98.0, current_streak=10)

    result = engine.analyze("BTCUSDT")

    assert result.components.track_record_component <= 100.0  # 98 + 5 bonus would otherwise exceed 100


# ─────────────────────────────────────────────────────────────────────────
# CLASSIFICATION
# ─────────────────────────────────────────────────────────────────────────


def test_low_liquidity_overrides_an_otherwise_high_score(database, engine):
    _seed_coin_and_profile(
        database, "THINUSDT", liquidity_score=15.0, volatility_score=10.0,
        trend_reliability_score=95.0, spread_quality_score=95.0, historical_stability_score=95.0,
    )
    _seed_statistics(database, "THINUSDT", total_signals=20, win_rate_percent=95.0, current_streak=5)

    result = engine.analyze("THINUSDT")

    assert result.classification == CoinClassification.LOW_LIQUIDITY  # overrides the score band entirely


def test_low_score_falls_through_every_band_to_speculative(database, engine):
    _seed_coin_and_profile(
        database, "WEAKUSDT", liquidity_score=25.0, volatility_score=80.0,
        trend_reliability_score=20.0, spread_quality_score=20.0, historical_stability_score=20.0,
    )
    _seed_statistics(database, "WEAKUSDT", total_signals=20, win_rate_percent=20.0, current_streak=-5)

    result = engine.analyze("WEAKUSDT")

    assert result.trust_score < 30.0
    assert result.classification == CoinClassification.SPECULATIVE


# ─────────────────────────────────────────────────────────────────────────
# PERSISTENCE
# ─────────────────────────────────────────────────────────────────────────


def test_analyze_persists_trust_score_and_classification_to_the_profile(database, engine, profile_repository):
    _seed_coin_and_profile(database, "BTCUSDT")
    _seed_statistics(database, "BTCUSDT", total_signals=20, win_rate_percent=70.0, current_streak=3)

    result = engine.analyze("BTCUSDT")

    persisted = profile_repository.get("BTCUSDT")
    assert persisted.coin_trust_score == pytest.approx(result.trust_score)
    assert persisted.classification == result.classification


def test_get_current_trust_score_reads_without_recomputing(database, engine, profile_repository):
    _seed_coin_and_profile(database, "BTCUSDT")
    profile_repository.upsert(
        CoinProfile(symbol="BTCUSDT", liquidity_score=80.0, coin_trust_score=42.0)
    )

    assert engine.get_current_trust_score("BTCUSDT") == pytest.approx(42.0)
    assert engine.get_current_trust_score("NEVERSEENUSDT") is None
