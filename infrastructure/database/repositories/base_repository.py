"""
infrastructure/database/repositories/base_repository.py

Shared repository base class (SRS Part 20: Clean Architecture / DRY --
"Never duplicate logic").

Every concrete repository composes a `Database` and reuses this base's
small set of helpers so that datetime<->TEXT and list/dict<->JSON column
conversions are written exactly once instead of once per repository.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

from infrastructure.database.connection import Database, get_database


class BaseRepository:
    """Common helpers shared by every repository. Not meant to be instantiated directly."""

    def __init__(self, database: Optional[Database] = None) -> None:
        self.database = database or get_database()

    # ---- datetime <-> TEXT (SQLite has no native datetime type) -----------

    @staticmethod
    def to_iso(value: Optional[datetime]) -> Optional[str]:
        """Serialize a datetime to ISO-8601 TEXT for storage, assuming UTC if naive."""
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()

    @staticmethod
    def from_iso(value: Optional[str]) -> Optional[datetime]:
        """Parse an ISO-8601 TEXT column back into a tz-aware datetime (never a bare string)."""
        if value is None:
            return None
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed

    # ---- list/dict <-> JSON TEXT -------------------------------------------

    @staticmethod
    def to_json(value: Any) -> str:
        """Serialize a list/dict column to a JSON TEXT column."""
        return json.dumps(value)

    @staticmethod
    def from_json(value: Optional[str], default: Any = None) -> Any:
        """Parse a JSON TEXT column back into a list/dict, defaulting if NULL."""
        if value is None:
            return default
        return json.loads(value)
