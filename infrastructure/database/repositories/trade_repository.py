"""
infrastructure/database/repositories/trade_repository.py

Repository for the `trades` table (SRS Part 10 POSITION MONITOR / TRADE
MANAGEMENT ENGINE + Part 12 TRADE STORAGE).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Optional

from core.models import SignalDirection, Trade, TradeStatus
from infrastructure.database.repositories.base_repository import BaseRepository

_ACTIVE_STATUSES: tuple[str, ...] = (
    TradeStatus.WAITING.value,
    TradeStatus.ACTIVE.value,
)


class TradeRepository(BaseRepository):
    """CRUD and lifecycle transitions for trades opened from an accepted signal."""

    def create(self, trade: Trade) -> Trade:
        with self.database.transaction() as conn:
            cursor = conn.execute(
                """
                INSERT INTO trades (
                    signal_id, symbol, direction, entry_price, entry_time,
                    initial_stop_loss, current_stop_loss, take_profit_1,
                    status, confidence_score, coin_trust_score, risk_score, bitcoin_score,
                    leverage, entry_order_id, stop_order_id, take_profit_order_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trade.signal_id,
                    trade.symbol,
                    trade.direction.value,
                    trade.entry_price,
                    self.to_iso(trade.entry_time),
                    trade.initial_stop_loss,
                    trade.current_stop_loss,
                    trade.take_profit_1,
                    trade.status.value,
                    trade.confidence_score,
                    trade.coin_trust_score,
                    trade.risk_score,
                    trade.bitcoin_score,
                    trade.leverage,
                    trade.entry_order_id,
                    trade.stop_order_id,
                    trade.take_profit_order_id,
                    self.to_iso(trade.created_at),
                ),
            )
            trade.id = cursor.lastrowid
        return trade

    def get_by_id(self, trade_id: int) -> Optional[Trade]:
        with self.database.read_connection() as conn:
            row = conn.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone()
        return self._row_to_trade(row) if row else None

    def get_active_trades(self) -> list[Trade]:
        """SRS Part 10: the Position Monitor's 30-second loop reads exactly this set."""
        placeholders = ",".join("?" for _ in _ACTIVE_STATUSES)
        with self.database.read_connection() as conn:
            rows = conn.execute(
                f"SELECT * FROM trades WHERE status IN ({placeholders}) ORDER BY entry_time",
                _ACTIVE_STATUSES,
            ).fetchall()
        return [self._row_to_trade(row) for row in rows]

    def get_closed_trades_between(self, period_start: datetime, period_end: datetime) -> list[Trade]:
        """All trades whose exit_time falls within [period_start, period_end) -- feeds the Reporting Engine (Module 18)."""
        with self.database.read_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM trades WHERE exit_time IS NOT NULL AND exit_time >= ? AND exit_time < ? "
                "ORDER BY exit_time",
                (self.to_iso(period_start), self.to_iso(period_end)),
            ).fetchall()
        return [self._row_to_trade(row) for row in rows]

    def move_stop_loss(self, trade_id: int, new_stop_loss: float) -> None:
        """Trailing-stop update. Never touches `initial_stop_loss`."""
        with self.database.transaction() as conn:
            conn.execute(
                "UPDATE trades SET current_stop_loss = ? WHERE id = ?",
                (new_stop_loss, trade_id),
            )

    def update_best_price(self, trade_id: int, best_price: float) -> None:
        """Records the best (most favorable) price seen since entry -- see Trade.best_price_since_entry's docstring."""
        with self.database.transaction() as conn:
            conn.execute(
                "UPDATE trades SET best_price_since_entry = ? WHERE id = ?",
                (best_price, trade_id),
            )

    def record_tp1_hit(self, trade_id: int, hit_at: datetime, exit_price: float) -> None:
        with self.database.transaction() as conn:
            conn.execute(
                "UPDATE trades SET status = ?, tp1_hit_at = ?, tp1_exit_price = ? WHERE id = ?",
                (TradeStatus.TP1_HIT.value, self.to_iso(hit_at), exit_price, trade_id),
            )

    def close_trade(
        self,
        trade_id: int,
        *,
        status: TradeStatus,
        exit_price: float,
        exit_time: datetime,
        realized_pnl_percent: float,
        exit_reason: str,
        max_favorable_excursion_percent: Optional[float] = None,
        max_adverse_excursion_percent: Optional[float] = None,
    ) -> None:
        """
        Finalize a trade. `duration_seconds` is computed here in Python
        from `entry_time`/`exit_time` (not in SQL) so the arithmetic is
        unit-testable without a live database and lives in one place.
        """
        trade = self.get_by_id(trade_id)
        if trade is None:
            raise ValueError(f"Cannot close trade {trade_id}: not found")

        duration_seconds = int((exit_time - trade.entry_time).total_seconds())

        with self.database.transaction() as conn:
            conn.execute(
                """
                UPDATE trades
                SET status = ?, exit_price = ?, exit_time = ?, realized_pnl_percent = ?,
                    exit_reason = ?, duration_seconds = ?,
                    max_favorable_excursion_percent = COALESCE(?, max_favorable_excursion_percent),
                    max_adverse_excursion_percent = COALESCE(?, max_adverse_excursion_percent)
                WHERE id = ?
                """,
                (
                    status.value,
                    exit_price,
                    self.to_iso(exit_time),
                    realized_pnl_percent,
                    exit_reason,
                    duration_seconds,
                    max_favorable_excursion_percent,
                    max_adverse_excursion_percent,
                    trade_id,
                ),
            )

    def _row_to_trade(self, row: sqlite3.Row) -> Trade:
        return Trade(
            id=row["id"],
            signal_id=row["signal_id"],
            symbol=row["symbol"],
            direction=SignalDirection(row["direction"]),
            entry_price=row["entry_price"],
            entry_time=self.from_iso(row["entry_time"]),
            initial_stop_loss=row["initial_stop_loss"],
            current_stop_loss=row["current_stop_loss"],
            take_profit_1=row["take_profit_1"],
            status=TradeStatus(row["status"]),
            confidence_score=row["confidence_score"],
            coin_trust_score=row["coin_trust_score"],
            risk_score=row["risk_score"],
            bitcoin_score=row["bitcoin_score"],
            leverage=row["leverage"],
            exit_price=row["exit_price"],
            exit_time=self.from_iso(row["exit_time"]),
            realized_pnl_percent=row["realized_pnl_percent"],
            max_favorable_excursion_percent=row["max_favorable_excursion_percent"],
            max_adverse_excursion_percent=row["max_adverse_excursion_percent"],
            exit_reason=row["exit_reason"],
            duration_seconds=row["duration_seconds"],
            tp1_hit_at=self.from_iso(row["tp1_hit_at"]),
            tp1_exit_price=row["tp1_exit_price"],
            entry_order_id=row["entry_order_id"],
            stop_order_id=row["stop_order_id"],
            take_profit_order_id=row["take_profit_order_id"],
            best_price_since_entry=row["best_price_since_entry"],
            created_at=self.from_iso(row["created_at"]),
        )
