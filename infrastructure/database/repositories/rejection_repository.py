"""
infrastructure/database/repositories/rejection_repository.py

Repositories for `rejections` and `missed_opportunities` (SRS Part 14
SIGNAL REJECTION DATABASE + MISSED OPPORTUNITY ENGINE).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Optional

from core.models import MissedOpportunity, Rejection, RejectionReason, SignalDirection
from infrastructure.database.repositories.base_repository import BaseRepository


class RejectionRepository(BaseRepository):
    """Every rejected signal is stored here -- SRS Part 14: 'Nothing should be discarded without explanation.'"""

    def create(self, rejection: Rejection) -> Rejection:
        with self.database.transaction() as conn:
            cursor = conn.execute(
                """
                INSERT INTO rejections (
                    symbol, direction, confidence_score, risk_score, bitcoin_score,
                    coin_trust_score, market_health_score, smart_money_score, primary_reason, secondary_reason,
                    rejected_filters, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rejection.symbol,
                    rejection.direction.value if rejection.direction else None,
                    rejection.confidence_score,
                    rejection.risk_score,
                    rejection.bitcoin_score,
                    rejection.coin_trust_score,
                    rejection.market_health_score,
                    rejection.smart_money_score,
                    rejection.primary_reason.value,
                    rejection.secondary_reason.value if rejection.secondary_reason else None,
                    self.to_json(rejection.rejected_filters),
                    self.to_iso(rejection.created_at),
                ),
            )
            rejection.id = cursor.lastrowid
        return rejection

    def get_by_id(self, rejection_id: int) -> Optional[Rejection]:
        with self.database.read_connection() as conn:
            row = conn.execute("SELECT * FROM rejections WHERE id = ?", (rejection_id,)).fetchone()
        return self._row_to_rejection(row) if row else None

    def count_by_reason_since(self, since_iso: str) -> dict[str, int]:
        """Feeds the daily report's 'top rejection reasons' breakdown (SRS Part 15)."""
        with self.database.read_connection() as conn:
            rows = conn.execute(
                """
                SELECT primary_reason, COUNT(*) AS c FROM rejections
                WHERE created_at >= ? GROUP BY primary_reason ORDER BY c DESC
                """,
                (since_iso,),
            ).fetchall()
        return {row["primary_reason"]: row["c"] for row in rows}

    def get_rejections_between(self, period_start: datetime, period_end: datetime) -> list[Rejection]:
        """All rejections created within [period_start, period_end) -- feeds the Analytics Engine (Module 19)."""
        with self.database.read_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM rejections WHERE created_at >= ? AND created_at < ? ORDER BY created_at",
                (self.to_iso(period_start), self.to_iso(period_end)),
            ).fetchall()
        return [self._row_to_rejection(row) for row in rows]

    def _row_to_rejection(self, row: sqlite3.Row) -> Rejection:
        return Rejection(
            id=row["id"],
            symbol=row["symbol"],
            direction=SignalDirection(row["direction"]) if row["direction"] else None,
            confidence_score=row["confidence_score"],
            risk_score=row["risk_score"],
            bitcoin_score=row["bitcoin_score"],
            coin_trust_score=row["coin_trust_score"],
            market_health_score=row["market_health_score"],
            smart_money_score=row["smart_money_score"],
            primary_reason=RejectionReason(row["primary_reason"]),
            secondary_reason=RejectionReason(row["secondary_reason"]) if row["secondary_reason"] else None,
            rejected_filters=self.from_json(row["rejected_filters"], default=[]),
            created_at=self.from_iso(row["created_at"]),
        )


class MissedOpportunityRepository(BaseRepository):
    """
    SRS Part 14: "Track [missed opportunities] silently. Do NOT change
    strategy. Use it for analysis only." This repository only ever
    stores and reads; nothing in Module 3 feeds these rows back into
    live decision-making.
    """

    def create(self, missed: MissedOpportunity) -> MissedOpportunity:
        with self.database.transaction() as conn:
            cursor = conn.execute(
                """
                INSERT INTO missed_opportunities (
                    rejection_id, symbol, rejected_at, reject_reason, price_at_rejection,
                    subsequent_max_move_percent, evaluation_window_hours,
                    confidence_score_at_rejection, market_condition, bitcoin_condition, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    missed.rejection_id,
                    missed.symbol,
                    self.to_iso(missed.rejected_at),
                    missed.reject_reason.value,
                    missed.price_at_rejection,
                    missed.subsequent_max_move_percent,
                    missed.evaluation_window_hours,
                    missed.confidence_score_at_rejection,
                    missed.market_condition,
                    missed.bitcoin_condition,
                    self.to_iso(missed.created_at),
                ),
            )
            missed.id = cursor.lastrowid
        return missed

    def get_by_symbol(self, symbol: str) -> list[MissedOpportunity]:
        with self.database.read_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM missed_opportunities WHERE symbol = ? ORDER BY rejected_at DESC",
                (symbol,),
            ).fetchall()
        return [self._row_to_missed(row) for row in rows]

    def get_between(self, period_start: datetime, period_end: datetime) -> list[MissedOpportunity]:
        """All missed-opportunity records whose rejected_at falls within [period_start, period_end) -- feeds the Analytics Engine (Module 19)."""
        with self.database.read_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM missed_opportunities WHERE rejected_at >= ? AND rejected_at < ? "
                "ORDER BY rejected_at",
                (self.to_iso(period_start), self.to_iso(period_end)),
            ).fetchall()
        return [self._row_to_missed(row) for row in rows]

    def _row_to_missed(self, row: sqlite3.Row) -> MissedOpportunity:
        return MissedOpportunity(
            id=row["id"],
            rejection_id=row["rejection_id"],
            symbol=row["symbol"],
            rejected_at=self.from_iso(row["rejected_at"]),
            reject_reason=RejectionReason(row["reject_reason"]),
            price_at_rejection=row["price_at_rejection"],
            subsequent_max_move_percent=row["subsequent_max_move_percent"],
            evaluation_window_hours=row["evaluation_window_hours"],
            confidence_score_at_rejection=row["confidence_score_at_rejection"],
            market_condition=row["market_condition"],
            bitcoin_condition=row["bitcoin_condition"],
            created_at=self.from_iso(row["created_at"]),
        )
