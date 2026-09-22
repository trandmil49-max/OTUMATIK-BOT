"""
infrastructure/database/repositories/market_repository.py

Repositories for `btc_statistics` and `market_statistics` (SRS Part 8
BITCOIN INTELLIGENCE / GLOBAL MARKET HEALTH + Part 12 storage).
"""

from __future__ import annotations

import sqlite3
from typing import Optional

from core.models import BtcStatisticsSnapshot, MarketStatisticsSnapshot
from infrastructure.database.repositories.base_repository import BaseRepository


class BtcStatisticsRepository(BaseRepository):
    """Append-only time series of Bitcoin health snapshots."""

    def create(self, snapshot: BtcStatisticsSnapshot) -> BtcStatisticsSnapshot:
        with self.database.transaction() as conn:
            cursor = conn.execute(
                """
                INSERT INTO btc_statistics (
                    snapshot_time, trend, health_score, volatility_score,
                    funding_rate, open_interest_usdt, price,
                    btc_dominance_pct, usdt_dominance_pct,
                    btc_dominance_trend, usdt_dominance_trend, dxy_trend,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self.to_iso(snapshot.snapshot_time),
                    snapshot.trend,
                    snapshot.health_score,
                    snapshot.volatility_score,
                    snapshot.funding_rate,
                    snapshot.open_interest_usdt,
                    snapshot.price,
                    snapshot.btc_dominance_pct,
                    snapshot.usdt_dominance_pct,
                    snapshot.btc_dominance_trend,
                    snapshot.usdt_dominance_trend,
                    snapshot.dxy_trend,
                    self.to_iso(snapshot.created_at),
                ),
            )
            snapshot.id = cursor.lastrowid
        return snapshot

    def get_latest(self) -> Optional[BtcStatisticsSnapshot]:
        with self.database.read_connection() as conn:
            row = conn.execute(
                "SELECT * FROM btc_statistics ORDER BY snapshot_time DESC LIMIT 1"
            ).fetchone()
        return self._row_to_snapshot(row) if row else None

    def get_since(self, since_iso: str) -> list[BtcStatisticsSnapshot]:
        with self.database.read_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM btc_statistics WHERE snapshot_time >= ? ORDER BY snapshot_time",
                (since_iso,),
            ).fetchall()
        return [self._row_to_snapshot(row) for row in rows]

    def _row_to_snapshot(self, row: sqlite3.Row) -> BtcStatisticsSnapshot:
        row_keys = row.keys()
        return BtcStatisticsSnapshot(
            id=row["id"],
            snapshot_time=self.from_iso(row["snapshot_time"]),
            trend=row["trend"],
            health_score=row["health_score"],
            volatility_score=row["volatility_score"],
            funding_rate=row["funding_rate"],
            open_interest_usdt=row["open_interest_usdt"],
            price=row["price"],
            # column-existence guard: a database still on migration 3 (this
            # process hasn't run migrations yet, or is reading mid-migration)
            # won't have these columns -- degrade to None rather than KeyError.
            btc_dominance_pct=row["btc_dominance_pct"] if "btc_dominance_pct" in row_keys else None,
            usdt_dominance_pct=row["usdt_dominance_pct"] if "usdt_dominance_pct" in row_keys else None,
            btc_dominance_trend=row["btc_dominance_trend"] if "btc_dominance_trend" in row_keys else None,
            usdt_dominance_trend=row["usdt_dominance_trend"] if "usdt_dominance_trend" in row_keys else None,
            dxy_trend=row["dxy_trend"] if "dxy_trend" in row_keys else None,
            created_at=self.from_iso(row["created_at"]),
        )


class MarketStatisticsRepository(BaseRepository):
    """Append-only time series of overall market-health snapshots (SRS Part 8 MARKET STATES)."""

    def create(self, snapshot: MarketStatisticsSnapshot) -> MarketStatisticsSnapshot:
        with self.database.transaction() as conn:
            cursor = conn.execute(
                """
                INSERT INTO market_statistics (
                    snapshot_time, market_health_score, average_liquidity_score,
                    average_volatility_score, trend_quality_score, average_spread_percent,
                    average_funding_rate, total_open_interest_usdt, market_state, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self.to_iso(snapshot.snapshot_time),
                    snapshot.market_health_score,
                    snapshot.average_liquidity_score,
                    snapshot.average_volatility_score,
                    snapshot.trend_quality_score,
                    snapshot.average_spread_percent,
                    snapshot.average_funding_rate,
                    snapshot.total_open_interest_usdt,
                    snapshot.market_state,
                    self.to_iso(snapshot.created_at),
                ),
            )
            snapshot.id = cursor.lastrowid
        return snapshot

    def get_latest(self) -> Optional[MarketStatisticsSnapshot]:
        with self.database.read_connection() as conn:
            row = conn.execute(
                "SELECT * FROM market_statistics ORDER BY snapshot_time DESC LIMIT 1"
            ).fetchone()
        return self._row_to_snapshot(row) if row else None

    def _row_to_snapshot(self, row: sqlite3.Row) -> MarketStatisticsSnapshot:
        return MarketStatisticsSnapshot(
            id=row["id"],
            snapshot_time=self.from_iso(row["snapshot_time"]),
            market_health_score=row["market_health_score"],
            average_liquidity_score=row["average_liquidity_score"],
            average_volatility_score=row["average_volatility_score"],
            trend_quality_score=row["trend_quality_score"],
            average_spread_percent=row["average_spread_percent"],
            average_funding_rate=row["average_funding_rate"],
            total_open_interest_usdt=row["total_open_interest_usdt"],
            market_state=row["market_state"],
            created_at=self.from_iso(row["created_at"]),
        )
