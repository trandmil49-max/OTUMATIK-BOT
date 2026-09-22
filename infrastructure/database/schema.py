"""
infrastructure/database/schema.py

Versioned schema migrations (SRS Part 12 DATABASE ENGINE: "Database
should support future upgrades without breaking old data.").

Each `Migration` is a flat tuple of INDIVIDUAL SQL statements -- never one
multi-statement script (see `Database.transaction()`'s docstring for why
`executescript()` is never used here) -- applied inside one transaction
and tracked by version number in `schema_migrations`.

Table inventory (15 domain tables, SRS Part 12's suggested list mapped
1:1 onto `core/models.py`'s dataclasses):
    coins, coin_profiles, coin_statistics          <- SRS Part 7
    signals, signal_score_breakdown                <- SRS Part 4/9/11
    trades                                         <- SRS Part 10
    rejections, missed_opportunities               <- SRS Part 14
    btc_statistics, market_statistics               <- SRS Part 8
    reports, filter_performance                    <- SRS Part 14/15
    bot_health, error_events, config_snapshots      <- SRS Part 18/19
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import NamedTuple

from infrastructure.database.connection import Database
from system.logging_setup import get_logger

_logger = get_logger("database")


class Migration(NamedTuple):
    version: int
    description: str
    statements: tuple[str, ...]


_MIGRATION_001_STATEMENTS: tuple[str, ...] = (
    # ---- coins (SRS Part 7 COIN DISCOVERY) --------------------------------
    """
    CREATE TABLE IF NOT EXISTS coins (
        symbol TEXT PRIMARY KEY,
        base_asset TEXT NOT NULL,
        quote_asset TEXT NOT NULL DEFAULT 'USDT',
        status TEXT NOT NULL DEFAULT 'TRADING',
        is_active INTEGER NOT NULL DEFAULT 1,
        first_seen_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_coins_is_active ON coins(is_active)",
    # ---- coin_profiles (SRS Part 7 COIN PROFILE SYSTEM) -------------------
    """
    CREATE TABLE IF NOT EXISTS coin_profiles (
        symbol TEXT PRIMARY KEY REFERENCES coins(symbol) ON DELETE CASCADE,
        liquidity_score REAL NOT NULL DEFAULT 0,
        volatility_score REAL NOT NULL DEFAULT 0,
        trend_reliability_score REAL NOT NULL DEFAULT 0,
        spread_quality_score REAL NOT NULL DEFAULT 0,
        historical_stability_score REAL NOT NULL DEFAULT 0,
        average_daily_volume_usdt REAL NOT NULL DEFAULT 0,
        average_atr_percent REAL NOT NULL DEFAULT 0,
        average_trend_length_candles REAL NOT NULL DEFAULT 0,
        average_pullback_size_percent REAL NOT NULL DEFAULT 0,
        average_fake_breakout_frequency REAL NOT NULL DEFAULT 0,
        average_success_rate_percent REAL NOT NULL DEFAULT 0,
        classification TEXT NOT NULL DEFAULT 'NEW_LISTING',
        coin_trust_score REAL NOT NULL DEFAULT 50.0,
        updated_at TEXT NOT NULL
    )
    """,
    # ---- coin_statistics (SRS Part 12 COIN STATISTICS) --------------------
    """
    CREATE TABLE IF NOT EXISTS coin_statistics (
        symbol TEXT PRIMARY KEY REFERENCES coins(symbol) ON DELETE CASCADE,
        total_signals INTEGER NOT NULL DEFAULT 0,
        winning_signals INTEGER NOT NULL DEFAULT 0,
        losing_signals INTEGER NOT NULL DEFAULT 0,
        tp1_count INTEGER NOT NULL DEFAULT 0,
        tp2_count INTEGER NOT NULL DEFAULT 0,
        sl_count INTEGER NOT NULL DEFAULT 0,
        breakeven_count INTEGER NOT NULL DEFAULT 0,
        average_rr REAL NOT NULL DEFAULT 0,
        average_confidence REAL NOT NULL DEFAULT 0,
        average_duration_seconds REAL NOT NULL DEFAULT 0,
        win_rate_percent REAL NOT NULL DEFAULT 0,
        current_streak INTEGER NOT NULL DEFAULT 0,
        longest_winning_streak INTEGER NOT NULL DEFAULT 0,
        longest_losing_streak INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL
    )
    """,
    # ---- signals (SRS Part 4/9/11/12) --------------------------------------
    """
    CREATE TABLE IF NOT EXISTS signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT NOT NULL REFERENCES coins(symbol),
        direction TEXT NOT NULL,
        entry_price REAL NOT NULL,
        stop_loss REAL NOT NULL,
        take_profit_1 REAL NOT NULL,
        take_profit_2 REAL NOT NULL,
        risk_reward_ratio REAL NOT NULL,
        leverage INTEGER NOT NULL DEFAULT 1,
        confidence_score REAL NOT NULL,
        confidence_grade TEXT NOT NULL,
        coin_trust_score REAL,
        risk_score REAL,
        bitcoin_score REAL,
        market_score REAL,
        status TEXT NOT NULL DEFAULT 'WAITING',
        trade_result TEXT,
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_signals_symbol_status ON signals(symbol, status)",
    "CREATE INDEX IF NOT EXISTS idx_signals_created_at ON signals(created_at)",
    # ---- signal_score_breakdown (SRS Part 9 EXPLAINABLE DECISION) --------
    """
    CREATE TABLE IF NOT EXISTS signal_score_breakdown (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        signal_id INTEGER NOT NULL REFERENCES signals(id) ON DELETE CASCADE,
        category TEXT NOT NULL,
        points REAL NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_score_breakdown_signal_id ON signal_score_breakdown(signal_id)",
    # ---- trades (SRS Part 10 POSITION MONITOR) -----------------------------
    """
    CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        signal_id INTEGER NOT NULL REFERENCES signals(id),
        symbol TEXT NOT NULL REFERENCES coins(symbol),
        direction TEXT NOT NULL,
        entry_price REAL NOT NULL,
        entry_time TEXT NOT NULL,
        exit_price REAL,
        exit_time TEXT,
        initial_stop_loss REAL NOT NULL,
        current_stop_loss REAL NOT NULL,
        take_profit_1 REAL NOT NULL,
        take_profit_2 REAL NOT NULL,
        status TEXT NOT NULL DEFAULT 'ACTIVE',
        confidence_score REAL NOT NULL,
        coin_trust_score REAL,
        risk_score REAL,
        bitcoin_score REAL,
        leverage INTEGER NOT NULL DEFAULT 1,
        realized_pnl_percent REAL,
        max_favorable_excursion_percent REAL,
        max_adverse_excursion_percent REAL,
        exit_reason TEXT,
        duration_seconds INTEGER,
        tp1_hit_at TEXT,
        tp2_hit_at TEXT,
        break_even_at TEXT,
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_trades_symbol_status ON trades(symbol, status)",
    "CREATE INDEX IF NOT EXISTS idx_trades_entry_time ON trades(entry_time)",
    "CREATE INDEX IF NOT EXISTS idx_trades_signal_id ON trades(signal_id)",
    # ---- rejections (SRS Part 14 SIGNAL REJECTION DATABASE) ---------------
    """
    CREATE TABLE IF NOT EXISTS rejections (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT NOT NULL,
        direction TEXT,
        confidence_score REAL,
        risk_score REAL,
        bitcoin_score REAL,
        coin_trust_score REAL,
        market_health_score REAL,
        primary_reason TEXT NOT NULL,
        secondary_reason TEXT,
        rejected_filters TEXT,
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_rejections_created_at ON rejections(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_rejections_primary_reason ON rejections(primary_reason)",
    # ---- missed_opportunities (SRS Part 14 MISSED OPPORTUNITY ENGINE) -----
    """
    CREATE TABLE IF NOT EXISTS missed_opportunities (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        rejection_id INTEGER REFERENCES rejections(id) ON DELETE SET NULL,
        symbol TEXT NOT NULL,
        rejected_at TEXT NOT NULL,
        reject_reason TEXT NOT NULL,
        price_at_rejection REAL NOT NULL,
        subsequent_max_move_percent REAL NOT NULL,
        evaluation_window_hours INTEGER NOT NULL,
        confidence_score_at_rejection REAL,
        market_condition TEXT,
        bitcoin_condition TEXT,
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_missed_opportunities_symbol ON missed_opportunities(symbol)",
    # ---- btc_statistics (SRS Part 8 + Part 12) -----------------------------
    """
    CREATE TABLE IF NOT EXISTS btc_statistics (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        snapshot_time TEXT NOT NULL,
        trend TEXT NOT NULL,
        health_score REAL NOT NULL,
        volatility_score REAL NOT NULL,
        funding_rate REAL,
        open_interest_usdt REAL,
        price REAL NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_btc_statistics_snapshot_time ON btc_statistics(snapshot_time)",
    # ---- market_statistics (SRS Part 8 MARKET STATES + Part 12) -----------
    """
    CREATE TABLE IF NOT EXISTS market_statistics (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        snapshot_time TEXT NOT NULL,
        market_health_score REAL NOT NULL,
        average_liquidity_score REAL,
        average_volatility_score REAL,
        trend_quality_score REAL,
        average_spread_percent REAL,
        average_funding_rate REAL,
        total_open_interest_usdt REAL,
        market_state TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_market_statistics_snapshot_time ON market_statistics(snapshot_time)",
    # ---- reports (SRS Part 15 PROFESSIONAL REPORTING ENGINE) ---------------
    """
    CREATE TABLE IF NOT EXISTS reports (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        report_type TEXT NOT NULL,
        period_start TEXT NOT NULL,
        period_end TEXT NOT NULL,
        generated_at TEXT NOT NULL,
        content TEXT NOT NULL,
        turkish_analysis TEXT,
        turkish_recommendations TEXT,
        overall_grade TEXT,
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_reports_type_period ON reports(report_type, period_start)",
    # ---- filter_performance (SRS Part 14 FILTER PERFORMANCE) ---------------
    """
    CREATE TABLE IF NOT EXISTS filter_performance (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        filter_name TEXT NOT NULL,
        period_start TEXT NOT NULL,
        period_end TEXT NOT NULL,
        trades_rejected INTEGER NOT NULL DEFAULT 0,
        saved_losses_count INTEGER NOT NULL DEFAULT 0,
        contribution_score REAL,
        success_rate_percent REAL,
        reliability_score REAL,
        computed_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_filter_performance_name_period ON filter_performance(filter_name, period_start)",
    # ---- bot_health (SRS Part 18 HEALTH CHECK) -----------------------------
    """
    CREATE TABLE IF NOT EXISTS bot_health (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        snapshot_time TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'HEALTHY',
        cpu_percent REAL,
        ram_mb REAL,
        average_scan_duration_seconds REAL,
        average_api_response_ms REAL,
        database_size_mb REAL,
        restart_count INTEGER NOT NULL DEFAULT 0,
        error_count INTEGER NOT NULL DEFAULT 0,
        retry_count INTEGER NOT NULL DEFAULT 0,
        active_trades_count INTEGER NOT NULL DEFAULT 0,
        symbols_scanned_count INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_bot_health_snapshot_time ON bot_health(snapshot_time)",
    # ---- error_events (SRS Rule 11: NEVER HIDE ERRORS) --------------------
    """
    CREATE TABLE IF NOT EXISTS error_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        occurred_at TEXT NOT NULL,
        severity TEXT NOT NULL,
        category TEXT NOT NULL,
        message TEXT NOT NULL,
        context TEXT,
        resolved INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_error_events_occurred_at ON error_events(occurred_at)",
    "CREATE INDEX IF NOT EXISTS idx_error_events_severity ON error_events(severity)",
    # ---- config_snapshots (SRS Part 19 VERSION CONTROL) --------------------
    """
    CREATE TABLE IF NOT EXISTS config_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        captured_at TEXT NOT NULL,
        strategy_profile TEXT NOT NULL,
        schema_version TEXT NOT NULL,
        config_json TEXT NOT NULL,
        reason TEXT,
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_config_snapshots_captured_at ON config_snapshots(captured_at)",
)

MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        version=1,
        description="Initial schema: all 15 Module 3 domain tables + indexes",
        statements=_MIGRATION_001_STATEMENTS,
    ),
    Migration(
        version=2,
        description=(
            "Add trades.tp1_exit_price: the price recorded at the moment "
            "TP1 fires, needed to report TP1's own partial profit % "
            "(previously only tp1_hit_at was recorded, with no price -- "
            "notify_tp1_hit() had nothing to show a percentage from)"
        ),
        statements=("ALTER TABLE trades ADD COLUMN tp1_exit_price REAL",),
    ),
    Migration(
        version=3,
        description=(
            "Add rejections.smart_money_score (Module 23 Smart Money "
            "Engine, added at the platform owner's explicit request): "
            "the same context-score treatment bitcoin_score/coin_trust_score/"
            "market_health_score already got, so a rejection caused or "
            "influenced by weak smart-money alignment is analyzable after "
            "the fact instead of only visible in that cycle's log line"
        ),
        statements=("ALTER TABLE rejections ADD COLUMN smart_money_score REAL",),
    ),
    Migration(
        version=4,
        description=(
            "Add btc_statistics.{btc_dominance_pct,usdt_dominance_pct,"
            "btc_dominance_trend,usdt_dominance_trend,dxy_trend}: BTC/USDT "
            "dominance (CoinGecko) and DXY (Yahoo Finance), ported from "
            "sinyal_kanali_2's MacroClient at the platform owner's explicit "
            "request, activating BitcoinConfig.dominance_weight -- present "
            "in the schema since Module 8 but unwired for lack of a "
            "market-cap data source until now"
        ),
        statements=(
            "ALTER TABLE btc_statistics ADD COLUMN btc_dominance_pct REAL",
            "ALTER TABLE btc_statistics ADD COLUMN usdt_dominance_pct REAL",
            "ALTER TABLE btc_statistics ADD COLUMN btc_dominance_trend TEXT",
            "ALTER TABLE btc_statistics ADD COLUMN usdt_dominance_trend TEXT",
            "ALTER TABLE btc_statistics ADD COLUMN dxy_trend TEXT",
        ),
    ),
    Migration(
        version=5,
        description=(
            "Drop signals.take_profit_2 and trades.take_profit_2: platform "
            "pivoted to a single-TP model where TP1_HIT is a full close, "
            "not a partial -- TP2/BREAK_EVEN no longer exist as trade "
            "states (see core.models.TradeStatus). Base schema deliberately "
            "left unchanged (still creates the column on a fresh DB) so "
            "this migration always has something to drop, on old and new "
            "databases alike."
        ),
        statements=(
            "ALTER TABLE signals DROP COLUMN take_profit_2",
            "ALTER TABLE trades DROP COLUMN take_profit_2",
        ),
    ),
    Migration(
        version=6,
        description=(
            "Add trades.{entry_order_id,stop_order_id,take_profit_order_id}: "
            "the real Binance order IDs for a trade opened by "
            "TradeExecutionEngine (autonomous trading pivot), for "
            "reconciliation against the exchange later. NULL for every "
            "PAPER-mode trade -- no real order was ever placed for those."
        ),
        statements=(
            "ALTER TABLE trades ADD COLUMN entry_order_id INTEGER",
            "ALTER TABLE trades ADD COLUMN stop_order_id INTEGER",
            "ALTER TABLE trades ADD COLUMN take_profit_order_id INTEGER",
        ),
    ),
    Migration(
        version=7,
        description=(
            "Add trades.best_price_since_entry: the best (highest for "
            "LONG, lowest for SHORT) price observed once a trade has been "
            "in profit at least once, driving the profit-lock trailing "
            "exit (RiskConfig.trailing_stop_enabled / TradeStatus"
            ".TRAILING_STOP_EXIT). NULL until the trade first moves into "
            "profit."
        ),
        statements=("ALTER TABLE trades ADD COLUMN best_price_since_entry REAL",),
    ),
)


def run_migrations(db: Database) -> list[int]:
    """
    Apply every migration in MIGRATIONS newer than the highest version
    recorded in `schema_migrations`, each inside its own transaction.

    Idempotent and safe to call on every process startup (SRS Part 18:
    the platform must self-heal its schema on every boot rather than
    require a separate manual `migrate` step) -- running this against an
    already-current database is a no-op.

    Returns:
        Migration version numbers that were newly applied, ascending.
    """
    with db.transaction() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                description TEXT NOT NULL,
                applied_at TEXT NOT NULL
            )
            """
        )

    with db.read_connection() as conn:
        current_version = conn.execute(
            "SELECT COALESCE(MAX(version), 0) AS v FROM schema_migrations"
        ).fetchone()["v"]

    applied: list[int] = []
    for migration in sorted(MIGRATIONS, key=lambda m: m.version):
        if migration.version <= current_version:
            continue
        with db.transaction() as conn:
            for statement in migration.statements:
                conn.execute(statement)
            conn.execute(
                "INSERT INTO schema_migrations (version, description, applied_at) VALUES (?, ?, ?)",
                (migration.version, migration.description, datetime.now(timezone.utc).isoformat()),
            )
        applied.append(migration.version)
        _logger.info("Applied migration %d: %s", migration.version, migration.description)

    return applied


def get_schema_version(db: Database) -> int:
    """Return the highest applied migration version, or 0 if migrations have never run."""
    with db.read_connection() as conn:
        try:
            row = conn.execute(
                "SELECT COALESCE(MAX(version), 0) AS v FROM schema_migrations"
            ).fetchone()
        except sqlite3.OperationalError:
            return 0
    return row["v"] if row else 0
