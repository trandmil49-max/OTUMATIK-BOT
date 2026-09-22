"""
infrastructure/database/repositories/signal_repository.py

Repository for the `signals` and `signal_score_breakdown` tables
(SRS Part 12 SIGNAL STORAGE + Part 9 EXPLAINABLE DECISION).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Optional

from core.models import ConfidenceGrade, Signal, SignalDirection, SignalScoreComponent, TradeStatus
from infrastructure.database.repositories.base_repository import BaseRepository


class SignalRepository(BaseRepository):
    """CRUD and queries for signals and their per-category score breakdown."""

    def create(
        self,
        signal: Signal,
        score_components: Optional[list[SignalScoreComponent]] = None,
    ) -> Signal:
        """
        Insert `signal` and, if provided, its `score_components` in ONE
        transaction -- a signal and its explainability breakdown must
        never exist independently of each other (SRS Part 9: "Every
        signal should store internally why it was accepted").

        Returns `signal` with `.id` populated from the new row.
        """
        with self.database.transaction() as conn:
            cursor = conn.execute(
                """
                INSERT INTO signals (
                    symbol, direction, entry_price, stop_loss, take_profit_1,
                    risk_reward_ratio, leverage, confidence_score,
                    confidence_grade, coin_trust_score, risk_score, bitcoin_score,
                    market_score, status, trade_result, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    signal.symbol,
                    signal.direction.value,
                    signal.entry_price,
                    signal.stop_loss,
                    signal.take_profit_1,
                    signal.risk_reward_ratio,
                    signal.leverage,
                    signal.confidence_score,
                    signal.confidence_grade.value,
                    signal.coin_trust_score,
                    signal.risk_score,
                    signal.bitcoin_score,
                    signal.market_score,
                    signal.status.value,
                    signal.trade_result,
                    self.to_iso(signal.created_at),
                ),
            )
            signal.id = cursor.lastrowid

            for component in score_components or []:
                component.signal_id = signal.id
                conn.execute(
                    """
                    INSERT INTO signal_score_breakdown (signal_id, category, points, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        component.signal_id,
                        component.category,
                        component.points,
                        self.to_iso(component.created_at),
                    ),
                )

        return signal

    def get_by_id(self, signal_id: int) -> Optional[Signal]:
        with self.database.read_connection() as conn:
            row = conn.execute("SELECT * FROM signals WHERE id = ?", (signal_id,)).fetchone()
        return self._row_to_signal(row) if row else None

    def get_score_breakdown(self, signal_id: int) -> list[SignalScoreComponent]:
        with self.database.read_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM signal_score_breakdown WHERE signal_id = ? ORDER BY id",
                (signal_id,),
            ).fetchall()
        return [
            SignalScoreComponent(
                id=row["id"],
                signal_id=row["signal_id"],
                category=row["category"],
                points=row["points"],
                created_at=self.from_iso(row["created_at"]),
            )
            for row in rows
        ]

    def update_status(
        self,
        signal_id: int,
        status: TradeStatus,
        trade_result: Optional[str] = None,
    ) -> None:
        """Used by the future Position Monitor (Module 16) as a trade progresses."""
        with self.database.transaction() as conn:
            conn.execute(
                "UPDATE signals SET status = ?, trade_result = COALESCE(?, trade_result) WHERE id = ?",
                (status.value, trade_result, signal_id),
            )

    def find_by_symbol_and_status(self, symbol: str, status: TradeStatus) -> list[Signal]:
        """Backs the duplicate-signal guard from the existing bot's history (three-layer guard, layer 2)."""
        with self.database.read_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM signals WHERE symbol = ? AND status = ? ORDER BY created_at DESC",
                (symbol, status.value),
            ).fetchall()
        return [self._row_to_signal(row) for row in rows]

    def find_most_recent_by_symbol(self, symbol: str) -> Optional[Signal]:
        """Most recent signal for `symbol` regardless of status -- backs the signal-cooldown check (RiskConfig.signal_cooldown_minutes)."""
        with self.database.read_connection() as conn:
            row = conn.execute(
                "SELECT * FROM signals WHERE symbol = ? ORDER BY created_at DESC LIMIT 1",
                (symbol,),
            ).fetchone()
        return self._row_to_signal(row) if row is not None else None

    def find_by_status(self, status: TradeStatus) -> list[Signal]:
        """
        Every signal in `status`, across all symbols -- backs the Position
        Monitor Engine's per-tick scan of every WAITING signal (Module 14),
        which needs the whole set rather than one symbol at a time.
        """
        with self.database.read_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM signals WHERE status = ? ORDER BY created_at", (status.value,)
            ).fetchall()
        return [self._row_to_signal(row) for row in rows]

    def count_since(self, since_iso: str) -> int:
        """Total signals created since a given ISO timestamp -- feeds daily/weekly reports."""
        with self.database.read_connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM signals WHERE created_at >= ?", (since_iso,)
            ).fetchone()
        return row["c"]

    def get_signals_between(self, period_start: datetime, period_end: datetime) -> list[Signal]:
        """All signals created within [period_start, period_end) -- feeds the Reporting Engine (Module 18)."""
        with self.database.read_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM signals WHERE created_at >= ? AND created_at < ? ORDER BY created_at",
                (self.to_iso(period_start), self.to_iso(period_end)),
            ).fetchall()
        return [self._row_to_signal(row) for row in rows]

    def get_earliest_signal_time(self) -> Optional[datetime]:
        """
        The `created_at` of this database's very first signal ever (not
        scoped to any period), or `None` if no signal has ever been
        persisted here. Feeds the Reporting Engine's data-coverage
        caveat (Module 18, added at the platform owner's explicit
        request): a report whose nominal `period_start` predates this
        timestamp is not reporting on a quiet week -- it is reporting on
        a database that did not exist yet for part of that window (e.g.
        a fresh Railway deployment/account), and the report should say so
        rather than silently presenting a partial window as if it were
        the whole one.
        """
        with self.database.read_connection() as conn:
            row = conn.execute("SELECT MIN(created_at) AS earliest FROM signals").fetchone()
        if row is None or row["earliest"] is None:
            return None
        return self.from_iso(row["earliest"])

    def _row_to_signal(self, row: sqlite3.Row) -> Signal:
        return Signal(
            id=row["id"],
            symbol=row["symbol"],
            direction=SignalDirection(row["direction"]),
            entry_price=row["entry_price"],
            stop_loss=row["stop_loss"],
            take_profit_1=row["take_profit_1"],
            risk_reward_ratio=row["risk_reward_ratio"],
            leverage=row["leverage"],
            confidence_score=row["confidence_score"],
            confidence_grade=ConfidenceGrade(row["confidence_grade"]),
            coin_trust_score=row["coin_trust_score"],
            risk_score=row["risk_score"],
            bitcoin_score=row["bitcoin_score"],
            market_score=row["market_score"],
            status=TradeStatus(row["status"]),
            trade_result=row["trade_result"],
            created_at=self.from_iso(row["created_at"]),
        )
