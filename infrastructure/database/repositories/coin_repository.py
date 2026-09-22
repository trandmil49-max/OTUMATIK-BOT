"""
infrastructure/database/repositories/coin_repository.py

Repositories for `coins`, `coin_profiles`, and `coin_statistics`
(SRS Part 7 COIN DISCOVERY / COIN PROFILE / COIN TRUST SYSTEM + Part 12
COIN STATISTICS).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Literal, Optional

from core.models import Coin, CoinClassification, CoinProfile, CoinStatistics
from infrastructure.database.repositories.base_repository import BaseRepository

TradeOutcome = Literal["WIN", "LOSS"]


class CoinRepository(BaseRepository):
    """CRUD for the `coins` table: the universe of symbols the platform knows about."""

    def upsert(self, coin: Coin) -> Coin:
        """Insert a new symbol, or refresh `status`/`last_seen_at` if it already exists."""
        with self.database.transaction() as conn:
            conn.execute(
                """
                INSERT INTO coins (symbol, base_asset, quote_asset, status, is_active, first_seen_at, last_seen_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                    status = excluded.status,
                    is_active = excluded.is_active,
                    last_seen_at = excluded.last_seen_at
                """,
                (
                    coin.symbol,
                    coin.base_asset,
                    coin.quote_asset,
                    coin.status,
                    int(coin.is_active),
                    self.to_iso(coin.first_seen_at),
                    self.to_iso(coin.last_seen_at),
                ),
            )
        return coin

    def get(self, symbol: str) -> Optional[Coin]:
        with self.database.read_connection() as conn:
            row = conn.execute("SELECT * FROM coins WHERE symbol = ?", (symbol,)).fetchone()
        return self._row_to_coin(row) if row else None

    def list_active(self) -> list[Coin]:
        with self.database.read_connection() as conn:
            rows = conn.execute("SELECT * FROM coins WHERE is_active = 1 ORDER BY symbol").fetchall()
        return [self._row_to_coin(row) for row in rows]

    def _row_to_coin(self, row: sqlite3.Row) -> Coin:
        return Coin(
            symbol=row["symbol"],
            base_asset=row["base_asset"],
            quote_asset=row["quote_asset"],
            status=row["status"],
            is_active=bool(row["is_active"]),
            first_seen_at=self.from_iso(row["first_seen_at"]),
            last_seen_at=self.from_iso(row["last_seen_at"]),
        )


class CoinProfileRepository(BaseRepository):
    """CRUD for the `coin_profiles` table (SRS Part 7 COIN PROFILE SYSTEM)."""

    def upsert(self, profile: CoinProfile) -> CoinProfile:
        with self.database.transaction() as conn:
            conn.execute(
                """
                INSERT INTO coin_profiles (
                    symbol, liquidity_score, volatility_score, trend_reliability_score,
                    spread_quality_score, historical_stability_score, average_daily_volume_usdt,
                    average_atr_percent, average_trend_length_candles, average_pullback_size_percent,
                    average_fake_breakout_frequency, average_success_rate_percent, classification,
                    coin_trust_score, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                    liquidity_score = excluded.liquidity_score,
                    volatility_score = excluded.volatility_score,
                    trend_reliability_score = excluded.trend_reliability_score,
                    spread_quality_score = excluded.spread_quality_score,
                    historical_stability_score = excluded.historical_stability_score,
                    average_daily_volume_usdt = excluded.average_daily_volume_usdt,
                    average_atr_percent = excluded.average_atr_percent,
                    average_trend_length_candles = excluded.average_trend_length_candles,
                    average_pullback_size_percent = excluded.average_pullback_size_percent,
                    average_fake_breakout_frequency = excluded.average_fake_breakout_frequency,
                    average_success_rate_percent = excluded.average_success_rate_percent,
                    classification = excluded.classification,
                    coin_trust_score = excluded.coin_trust_score,
                    updated_at = excluded.updated_at
                """,
                (
                    profile.symbol,
                    profile.liquidity_score,
                    profile.volatility_score,
                    profile.trend_reliability_score,
                    profile.spread_quality_score,
                    profile.historical_stability_score,
                    profile.average_daily_volume_usdt,
                    profile.average_atr_percent,
                    profile.average_trend_length_candles,
                    profile.average_pullback_size_percent,
                    profile.average_fake_breakout_frequency,
                    profile.average_success_rate_percent,
                    profile.classification.value,
                    profile.coin_trust_score,
                    self.to_iso(profile.updated_at),
                ),
            )
        return profile

    def get(self, symbol: str) -> Optional[CoinProfile]:
        with self.database.read_connection() as conn:
            row = conn.execute("SELECT * FROM coin_profiles WHERE symbol = ?", (symbol,)).fetchone()
        return self._row_to_profile(row) if row else None

    def _row_to_profile(self, row: sqlite3.Row) -> CoinProfile:
        return CoinProfile(
            symbol=row["symbol"],
            liquidity_score=row["liquidity_score"],
            volatility_score=row["volatility_score"],
            trend_reliability_score=row["trend_reliability_score"],
            spread_quality_score=row["spread_quality_score"],
            historical_stability_score=row["historical_stability_score"],
            average_daily_volume_usdt=row["average_daily_volume_usdt"],
            average_atr_percent=row["average_atr_percent"],
            average_trend_length_candles=row["average_trend_length_candles"],
            average_pullback_size_percent=row["average_pullback_size_percent"],
            average_fake_breakout_frequency=row["average_fake_breakout_frequency"],
            average_success_rate_percent=row["average_success_rate_percent"],
            classification=CoinClassification(row["classification"]),
            coin_trust_score=row["coin_trust_score"],
            updated_at=self.from_iso(row["updated_at"]),
        )


class CoinStatisticsRepository(BaseRepository):
    """
    CRUD and streak accounting for the `coin_statistics` table (SRS
    Part 12 COIN STATISTICS: win rate, streaks, TP/SL counts).
    """

    def get(self, symbol: str) -> Optional[CoinStatistics]:
        with self.database.read_connection() as conn:
            row = conn.execute("SELECT * FROM coin_statistics WHERE symbol = ?", (symbol,)).fetchone()
        return self._row_to_stats(row) if row else None

    def record_trade_outcome(
        self,
        symbol: str,
        outcome: TradeOutcome,
        *,
        rr: Optional[float] = None,
        confidence_score: Optional[float] = None,
        duration_seconds: Optional[float] = None,
        hit_tp1: bool = False,
        hit_sl: bool = False,
    ) -> CoinStatistics:
        """
        Update every rolling statistic for `symbol` after one closed
        trade, atomically (read-modify-write inside a single `BEGIN
        IMMEDIATE` transaction -- see `Database.transaction()` -- so two
        concurrent closes for the same symbol cannot race each other).

        Streak semantics (SRS Part 12: "Longest Winning Streak, Longest
        Losing Streak"):
          * WIN  -> current_streak becomes +1 if it was <= 0, else current+1.
          * LOSS -> current_streak becomes -1 if it was >= 0, else current-1.
          `longest_winning_streak` / `longest_losing_streak` only ever
          increase, never decrease.

        Running averages (`average_rr`, `average_confidence`,
        `average_duration_seconds`) use the incremental-mean formula
        `new_avg = old_avg + (value - old_avg) / new_count`, numerically
        equivalent to recomputing the mean from every historical value
        without needing to store them.
        """
        with self.database.transaction() as conn:
            stats = self._get_or_create(conn, symbol)

            stats.total_signals += 1
            if outcome == "WIN":
                stats.winning_signals += 1
                stats.current_streak = 1 if stats.current_streak <= 0 else stats.current_streak + 1
                stats.longest_winning_streak = max(stats.longest_winning_streak, stats.current_streak)
            else:  # LOSS
                stats.losing_signals += 1
                stats.current_streak = -1 if stats.current_streak >= 0 else stats.current_streak - 1
                stats.longest_losing_streak = max(stats.longest_losing_streak, -stats.current_streak)

            if hit_tp1:
                stats.tp1_count += 1
            if hit_sl:
                stats.sl_count += 1

            decided_trades = stats.winning_signals + stats.losing_signals
            if decided_trades > 0:
                stats.win_rate_percent = (stats.winning_signals / decided_trades) * 100.0

            if rr is not None:
                stats.average_rr += (rr - stats.average_rr) / stats.total_signals
            if confidence_score is not None:
                stats.average_confidence += (
                    confidence_score - stats.average_confidence
                ) / stats.total_signals
            if duration_seconds is not None:
                stats.average_duration_seconds += (
                    duration_seconds - stats.average_duration_seconds
                ) / stats.total_signals

            stats.updated_at = _utc_now()

            conn.execute(
                """
                INSERT INTO coin_statistics (
                    symbol, total_signals, winning_signals, losing_signals, tp1_count,
                    sl_count, average_rr, average_confidence, average_duration_seconds,
                    win_rate_percent, current_streak, longest_winning_streak, longest_losing_streak, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                    total_signals = excluded.total_signals,
                    winning_signals = excluded.winning_signals,
                    losing_signals = excluded.losing_signals,
                    tp1_count = excluded.tp1_count,
                    sl_count = excluded.sl_count,
                    average_rr = excluded.average_rr,
                    average_confidence = excluded.average_confidence,
                    average_duration_seconds = excluded.average_duration_seconds,
                    win_rate_percent = excluded.win_rate_percent,
                    current_streak = excluded.current_streak,
                    longest_winning_streak = excluded.longest_winning_streak,
                    longest_losing_streak = excluded.longest_losing_streak,
                    updated_at = excluded.updated_at
                """,
                (
                    stats.symbol,
                    stats.total_signals,
                    stats.winning_signals,
                    stats.losing_signals,
                    stats.tp1_count,
                    stats.sl_count,
                    stats.average_rr,
                    stats.average_confidence,
                    stats.average_duration_seconds,
                    stats.win_rate_percent,
                    stats.current_streak,
                    stats.longest_winning_streak,
                    stats.longest_losing_streak,
                    self.to_iso(stats.updated_at),
                ),
            )

        return stats

    def _get_or_create(self, conn: sqlite3.Connection, symbol: str) -> CoinStatistics:
        row = conn.execute("SELECT * FROM coin_statistics WHERE symbol = ?", (symbol,)).fetchone()
        return self._row_to_stats(row) if row is not None else CoinStatistics(symbol=symbol)

    def _row_to_stats(self, row: sqlite3.Row) -> CoinStatistics:
        return CoinStatistics(
            symbol=row["symbol"],
            total_signals=row["total_signals"],
            winning_signals=row["winning_signals"],
            losing_signals=row["losing_signals"],
            tp1_count=row["tp1_count"],
            sl_count=row["sl_count"],
            average_rr=row["average_rr"],
            average_confidence=row["average_confidence"],
            average_duration_seconds=row["average_duration_seconds"],
            win_rate_percent=row["win_rate_percent"],
            current_streak=row["current_streak"],
            longest_winning_streak=row["longest_winning_streak"],
            longest_losing_streak=row["longest_losing_streak"],
            updated_at=self.from_iso(row["updated_at"]),
        )


def _utc_now() -> datetime:
    from datetime import timezone

    return datetime.now(timezone.utc)
