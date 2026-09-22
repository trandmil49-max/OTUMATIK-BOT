"""
infrastructure/database/repositories/system_repository.py

Repositories for `bot_health`, `error_events`, and `config_snapshots`
(SRS Part 18 PRODUCTION ENGINE + Part 19 VERSION CONTROL).
"""

from __future__ import annotations

import sqlite3
from typing import Optional

from core.models import BotHealthSnapshot, ConfigSnapshot, ErrorEvent
from infrastructure.database.repositories.base_repository import BaseRepository


class BotHealthRepository(BaseRepository):
    """Point-in-time health snapshots (SRS Part 18 HEALTH CHECK)."""

    def create(self, snapshot: BotHealthSnapshot) -> BotHealthSnapshot:
        with self.database.transaction() as conn:
            cursor = conn.execute(
                """
                INSERT INTO bot_health (
                    snapshot_time, status, cpu_percent, ram_mb, average_scan_duration_seconds,
                    average_api_response_ms, database_size_mb, restart_count, error_count,
                    retry_count, active_trades_count, symbols_scanned_count, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self.to_iso(snapshot.snapshot_time),
                    snapshot.status,
                    snapshot.cpu_percent,
                    snapshot.ram_mb,
                    snapshot.average_scan_duration_seconds,
                    snapshot.average_api_response_ms,
                    snapshot.database_size_mb,
                    snapshot.restart_count,
                    snapshot.error_count,
                    snapshot.retry_count,
                    snapshot.active_trades_count,
                    snapshot.symbols_scanned_count,
                    self.to_iso(snapshot.created_at),
                ),
            )
            snapshot.id = cursor.lastrowid
        return snapshot

    def get_latest(self) -> Optional[BotHealthSnapshot]:
        with self.database.read_connection() as conn:
            row = conn.execute(
                "SELECT * FROM bot_health ORDER BY snapshot_time DESC LIMIT 1"
            ).fetchone()
        return self._row_to_snapshot(row) if row else None

    def _row_to_snapshot(self, row: sqlite3.Row) -> BotHealthSnapshot:
        return BotHealthSnapshot(
            id=row["id"],
            snapshot_time=self.from_iso(row["snapshot_time"]),
            status=row["status"],
            cpu_percent=row["cpu_percent"],
            ram_mb=row["ram_mb"],
            average_scan_duration_seconds=row["average_scan_duration_seconds"],
            average_api_response_ms=row["average_api_response_ms"],
            database_size_mb=row["database_size_mb"],
            restart_count=row["restart_count"],
            error_count=row["error_count"],
            retry_count=row["retry_count"],
            active_trades_count=row["active_trades_count"],
            symbols_scanned_count=row["symbols_scanned_count"],
            created_at=self.from_iso(row["created_at"]),
        )


class ErrorEventRepository(BaseRepository):
    """
    Persists CRITICAL/FATAL errors only -- see `core.models.ErrorEvent`'s
    docstring for why INFO/WARNING/ERROR stay in Module 2's file logs
    instead of also being written here.
    """

    def create(self, event: ErrorEvent) -> ErrorEvent:
        with self.database.transaction() as conn:
            cursor = conn.execute(
                """
                INSERT INTO error_events (occurred_at, severity, category, message, context, resolved, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self.to_iso(event.occurred_at),
                    event.severity,
                    event.category,
                    event.message,
                    self.to_json(event.context),
                    int(event.resolved),
                    self.to_iso(event.created_at),
                ),
            )
            event.id = cursor.lastrowid
        return event

    def get_unresolved(self) -> list[ErrorEvent]:
        with self.database.read_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM error_events WHERE resolved = 0 ORDER BY occurred_at DESC"
            ).fetchall()
        return [self._row_to_event(row) for row in rows]

    def mark_resolved(self, event_id: int) -> None:
        with self.database.transaction() as conn:
            conn.execute("UPDATE error_events SET resolved = 1 WHERE id = ?", (event_id,))

    def _row_to_event(self, row: sqlite3.Row) -> ErrorEvent:
        return ErrorEvent(
            id=row["id"],
            occurred_at=self.from_iso(row["occurred_at"]),
            severity=row["severity"],
            category=row["category"],
            message=row["message"],
            context=self.from_json(row["context"], default={}),
            resolved=bool(row["resolved"]),
            created_at=self.from_iso(row["created_at"]),
        )


class ConfigSnapshotRepository(BaseRepository):
    """Audit trail of configuration at points in time (SRS Part 19 VERSION CONTROL)."""

    def create(self, snapshot: ConfigSnapshot) -> ConfigSnapshot:
        with self.database.transaction() as conn:
            cursor = conn.execute(
                """
                INSERT INTO config_snapshots
                    (captured_at, strategy_profile, schema_version, config_json, reason, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    self.to_iso(snapshot.captured_at),
                    snapshot.strategy_profile,
                    snapshot.schema_version,
                    self.to_json(snapshot.config_json),
                    snapshot.reason,
                    self.to_iso(snapshot.created_at),
                ),
            )
            snapshot.id = cursor.lastrowid
        return snapshot

    def get_latest(self) -> Optional[ConfigSnapshot]:
        with self.database.read_connection() as conn:
            row = conn.execute(
                "SELECT * FROM config_snapshots ORDER BY captured_at DESC LIMIT 1"
            ).fetchone()
        return self._row_to_snapshot(row) if row else None

    def _row_to_snapshot(self, row: sqlite3.Row) -> ConfigSnapshot:
        return ConfigSnapshot(
            id=row["id"],
            captured_at=self.from_iso(row["captured_at"]),
            strategy_profile=row["strategy_profile"],
            schema_version=row["schema_version"],
            config_json=self.from_json(row["config_json"], default={}),
            reason=row["reason"],
            created_at=self.from_iso(row["created_at"]),
        )
