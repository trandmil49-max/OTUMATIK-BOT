"""
infrastructure/database/connection.py

SQLite connection management (SRS Part 12 DATABASE ENGINE: "Use SQLite.
The database should be lightweight, stable and optimized.").

Design (from PROJECT_STATUS.md's "Planned design", now implemented):
  * ONE connection PER CALL -- not a shared/pooled connection. This
    platform's write volume (signals/trades/reports, not tick-level
    data) does not need pooling; a fresh, short-lived connection per
    unit of work is simpler to reason about and matches the SRS's own
    stated Priority 1 ("Code Stability" -- Part 1 MAIN GOALS), preferring
    simple-and-safe over maximally performant.
  * WAL mode when `config.database.enable_wal_mode` (default True) --
    lets read-only queries (future reports/dashboards) run without
    blocking on an in-progress write.
  * Explicit BEGIN/COMMIT/ROLLBACK via `isolation_level=None`. Python's
    sqlite3 module's *implicit* transaction handling is a well-documented
    source of surprising commit points; driving transactions explicitly
    means a failure inside `Database.transaction()` is guaranteed to roll
    back cleanly instead of leaving a partial write (SRS Part 18:
    "Prevent ... Partial transactions. Always commit safely.").
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from config.loader import get_config
from config.schema import PlatformConfig
from system.exceptions import DatabaseError, DatabaseLockError
from system.logging_setup import get_logger

_logger = get_logger("database")


class Database:
    """
    Thin wrapper around a single SQLite database file.

    Holds no persistent connection of its own. `connect()` opens a fresh,
    fully configured `sqlite3.Connection`; callers use it directly or
    (preferably) via `transaction()` / `read_connection()` so the
    connection is always closed deterministically.
    """

    def __init__(
        self,
        db_path: Optional[str] = None,
        config: Optional[PlatformConfig] = None,
    ) -> None:
        self._config = config or get_config()
        self.db_path = Path(db_path or self._config.database.path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        """
        Open a new connection with foreign keys enabled, WAL mode applied
        if configured, `sqlite3.Row` for dict-like column access, and
        explicit transaction control (`isolation_level=None`).

        Raises:
            DatabaseError: the underlying `sqlite3.connect()` call failed
                (e.g. an unwritable path).
        """
        try:
            conn = sqlite3.connect(
                self.db_path,
                timeout=30.0,  # wait up to 30s for a lock before raising OperationalError
                isolation_level=None,  # explicit BEGIN/COMMIT/ROLLBACK -- see module docstring
                check_same_thread=False,
            )
        except sqlite3.Error as exc:
            raise DatabaseError(
                f"Failed to open SQLite connection at {self.db_path}",
                cause=exc,
                context={"db_path": str(self.db_path)},
            ) from exc

        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        if self._config.database.enable_wal_mode:
            conn.execute("PRAGMA journal_mode = WAL")
        return conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """
        Open a connection, `BEGIN` a transaction, yield the connection,
        then `COMMIT` on success or `ROLLBACK` on any exception -- always
        closing the connection afterward. This is the entry point every
        repository write goes through.

        Note: never call `conn.executescript(...)` on the yielded
        connection. `sqlite3.Connection.executescript()` implicitly
        commits whatever transaction is currently open before running
        (verified empirically while building this module), which would
        silently end the transaction this context manager just started.
        Use individual `conn.execute(statement)` calls instead (this is
        exactly why `infrastructure/database/schema.py`'s migrations are
        lists of individual statements rather than one script string).

        Uses `BEGIN IMMEDIATE` rather than a plain deferred `BEGIN`: this
        acquires SQLite's write lock up front instead of on the first
        write statement, which correctly serializes concurrent
        read-modify-write sequences (e.g. `CoinStatisticsRepository`'s
        streak counters) instead of letting two transactions both read
        stale data before either commits.

        Raises:
            DatabaseLockError: SQLite reported the database as locked
                even after the connection's own busy timeout elapsed.
            DatabaseError: any other `sqlite3.Error` during the transaction.
        """
        conn = self.connect()
        began = False
        try:
            conn.execute("BEGIN IMMEDIATE")
            began = True
            yield conn
            conn.execute("COMMIT")
        except sqlite3.OperationalError as exc:
            if began:
                conn.execute("ROLLBACK")
            if "locked" in str(exc).lower():
                raise DatabaseLockError(
                    "Database is locked",
                    cause=exc,
                    context={"db_path": str(self.db_path)},
                ) from exc
            raise DatabaseError(
                "Database operational error during transaction",
                cause=exc,
                context={"db_path": str(self.db_path)},
            ) from exc
        except sqlite3.Error as exc:
            if began:
                conn.execute("ROLLBACK")
            raise DatabaseError(
                "Database error during transaction",
                cause=exc,
                context={"db_path": str(self.db_path)},
            ) from exc
        except Exception:
            # A non-sqlite3 exception raised by caller code inside the
            # `with` block (e.g. a validation error) -- roll back and
            # re-raise it UNCHANGED; it is not a database-layer failure.
            if began:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    @contextmanager
    def read_connection(self) -> Iterator[sqlite3.Connection]:
        """
        Open a connection for read-only queries (no explicit BEGIN/COMMIT
        needed -- a single SELECT does not require one). Always closes
        the connection afterward. Repository read methods should use this
        instead of `connect()` directly.
        """
        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()


_database_singleton: Optional[Database] = None


def get_database(force_reload: bool = False) -> Database:
    """
    Return the process-wide `Database` singleton, mirroring
    `config.loader.get_config()`'s pattern. Repositories should call this
    rather than constructing their own `Database` instance, so the whole
    process agrees on one db path / one config.
    """
    global _database_singleton
    if _database_singleton is None or force_reload:
        _database_singleton = Database()
    return _database_singleton


def reset_database_singleton_for_tests() -> None:
    """Test-only helper: forces the next `get_database()` call to build a fresh instance."""
    global _database_singleton
    _database_singleton = None
