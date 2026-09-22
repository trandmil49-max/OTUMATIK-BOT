"""
Unit tests for engines/production.py (Module 20).

Health-check tests use fake repositories and fake Binance/Telegram
clients (no real network, no SQLite) to test `_determine_status()`'s
decision logic in isolation. Backup tests use real files under
`tmp_path`, since `backup_database()` is genuine file I/O with no
reasonable fake.

Run with:
    pytest tests/unit/test_production.py -v
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from config.schema import DatabaseConfig, PerformanceConfig, PlatformConfig
from core.models import BotHealthSnapshot, ErrorEvent
from engines.production import ProductionEngine, _BACKUP_TIMESTAMP_FORMAT


# ── fakes ──────────────────────────────────────────────────────────────


class _FakeBinanceClient:
    def __init__(self, healthy: bool = True):
        self.healthy = healthy

    async def check_health(self) -> bool:
        return self.healthy


class _FakeTelegramClient:
    def __init__(self, healthy: bool = True):
        self.healthy = healthy

    async def check_health(self) -> bool:
        return self.healthy


class _FakeBotHealthRepository:
    def __init__(self):
        self.saved: list[BotHealthSnapshot] = []

    def create(self, snapshot: BotHealthSnapshot) -> BotHealthSnapshot:
        snapshot.id = len(self.saved) + 1
        self.saved.append(snapshot)
        return snapshot


class _FakeErrorEventRepository:
    def __init__(self, unresolved: list[ErrorEvent] | None = None):
        self._unresolved = unresolved or []

    def get_unresolved(self) -> list[ErrorEvent]:
        return self._unresolved


class _FakeTradeRepository:
    def __init__(self, active: list | None = None):
        self._active = active or []

    def get_active_trades(self) -> list:
        return self._active


def _unresolved_error() -> ErrorEvent:
    return ErrorEvent(occurred_at=datetime.now(timezone.utc), severity="CRITICAL", category="test", message="boom")


def _engine(
    *,
    binance_healthy=True, telegram_healthy=True, unresolved_errors=None, active_trades=None,
    config: PlatformConfig | None = None,
) -> tuple[ProductionEngine, _FakeBotHealthRepository]:
    bot_health_repo = _FakeBotHealthRepository()
    engine = ProductionEngine(
        bot_health_repository=bot_health_repo,
        error_event_repository=_FakeErrorEventRepository(unresolved_errors),
        trade_repository=_FakeTradeRepository(active_trades),
        binance_client=_FakeBinanceClient(binance_healthy),
        telegram_client=_FakeTelegramClient(telegram_healthy),
        config=config or PlatformConfig(),
    )
    return engine, bot_health_repo


# ── health status determination ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_status_healthy_when_everything_ok():
    engine, _ = _engine(binance_healthy=True, telegram_healthy=True, unresolved_errors=[])
    snapshot = await engine.run_health_check()
    assert snapshot.status == "HEALTHY"


@pytest.mark.asyncio
async def test_status_critical_when_binance_down():
    engine, _ = _engine(binance_healthy=False, telegram_healthy=True)
    snapshot = await engine.run_health_check()
    assert snapshot.status == "CRITICAL"


@pytest.mark.asyncio
async def test_status_critical_when_unresolved_critical_errors_exist():
    engine, _ = _engine(binance_healthy=True, unresolved_errors=[_unresolved_error()])
    snapshot = await engine.run_health_check()
    assert snapshot.status == "CRITICAL"


@pytest.mark.asyncio
async def test_status_warning_when_only_telegram_down():
    engine, _ = _engine(binance_healthy=True, telegram_healthy=False, unresolved_errors=[])
    snapshot = await engine.run_health_check()
    assert snapshot.status == "WARNING"


@pytest.mark.asyncio
async def test_binance_down_takes_priority_over_telegram_down():
    engine, _ = _engine(binance_healthy=False, telegram_healthy=False)
    snapshot = await engine.run_health_check()
    assert snapshot.status == "CRITICAL"


@pytest.mark.asyncio
async def test_health_check_persists_the_snapshot():
    engine, repo = _engine()
    snapshot = await engine.run_health_check()
    assert snapshot.id is not None
    assert repo.saved == [snapshot]


@pytest.mark.asyncio
async def test_health_check_reports_active_trades_count():
    engine, _ = _engine(active_trades=[object(), object(), object()])
    snapshot = await engine.run_health_check()
    assert snapshot.active_trades_count == 3


@pytest.mark.asyncio
async def test_health_check_reports_unresolved_error_count():
    engine, _ = _engine(unresolved_errors=[_unresolved_error(), _unresolved_error()])
    snapshot = await engine.run_health_check()
    assert snapshot.error_count == 2


@pytest.mark.asyncio
async def test_health_check_cpu_and_ram_stay_none():
    """Deliberately deferred -- see engines/production.py's module docstring (needs psutil, not added unilaterally)."""
    engine, _ = _engine()
    snapshot = await engine.run_health_check()
    assert snapshot.cpu_percent is None
    assert snapshot.ram_mb is None


@pytest.mark.asyncio
async def test_health_check_database_size_present_when_file_exists(tmp_path):
    db_path = tmp_path / "platform.db"
    db_path.write_bytes(b"x" * 2_097_152)  # exactly 2 MB
    config = PlatformConfig(database=DatabaseConfig(path=str(db_path)))
    engine, _ = _engine(config=config)

    snapshot = await engine.run_health_check()

    assert snapshot.database_size_mb == pytest.approx(2.0)


@pytest.mark.asyncio
async def test_health_check_database_size_none_when_file_missing(tmp_path):
    config = PlatformConfig(database=DatabaseConfig(path=str(tmp_path / "does_not_exist.db")))
    engine, _ = _engine(config=config)

    snapshot = await engine.run_health_check()

    assert snapshot.database_size_mb is None


def test_watchdog_seeded_from_max_scan_duration_seconds():
    config = PlatformConfig(performance=PerformanceConfig(max_scan_duration_seconds=123))
    engine, _ = _engine(config=config)
    assert engine.watchdog._stale_after_seconds == 123


# ── database backup ──────────────────────────────────────────────────────


def _make_engine_with_db(tmp_path, *, retention_days=None) -> tuple[ProductionEngine, Path]:
    db_path = tmp_path / "platform.db"
    db_path.write_bytes(b"fake sqlite content")
    config = PlatformConfig(database=DatabaseConfig(path=str(db_path), retention_policy_days=retention_days))
    engine, _ = _engine(config=config)
    return engine, db_path


def test_backup_database_creates_timestamped_copy(tmp_path):
    engine, db_path = _make_engine_with_db(tmp_path)
    destination = tmp_path / "backups"

    backup_path = engine.backup_database(destination_dir=destination)

    assert backup_path.exists()
    assert backup_path.read_bytes() == db_path.read_bytes()
    assert backup_path.parent == destination
    assert backup_path.name.startswith("platform_backup_")
    assert backup_path.suffix == ".db"


def test_backup_database_raises_when_source_missing(tmp_path):
    config = PlatformConfig(database=DatabaseConfig(path=str(tmp_path / "missing.db")))
    engine, _ = _engine(config=config)

    with pytest.raises(FileNotFoundError):
        engine.backup_database(destination_dir=tmp_path / "backups")


def test_backup_database_creates_destination_dir_if_missing(tmp_path):
    engine, _ = _make_engine_with_db(tmp_path)
    destination = tmp_path / "nested" / "backups"

    backup_path = engine.backup_database(destination_dir=destination)

    assert backup_path.exists()


def test_backup_retention_none_never_deletes_old_backups(tmp_path):
    engine, _ = _make_engine_with_db(tmp_path, retention_days=None)
    destination = tmp_path / "backups"
    destination.mkdir()

    old_name = f"platform_backup_{(datetime.now(timezone.utc) - timedelta(days=9999)).strftime(_BACKUP_TIMESTAMP_FORMAT)}.db"
    (destination / old_name).write_bytes(b"ancient backup")

    engine.backup_database(destination_dir=destination)

    assert (destination / old_name).exists()


def test_backup_retention_prunes_backups_older_than_policy(tmp_path):
    engine, _ = _make_engine_with_db(tmp_path, retention_days=30)
    destination = tmp_path / "backups"
    destination.mkdir()

    old_name = f"platform_backup_{(datetime.now(timezone.utc) - timedelta(days=45)).strftime(_BACKUP_TIMESTAMP_FORMAT)}.db"
    (destination / old_name).write_bytes(b"old backup")

    engine.backup_database(destination_dir=destination)

    assert not (destination / old_name).exists()


def test_backup_retention_keeps_backups_within_policy(tmp_path):
    engine, _ = _make_engine_with_db(tmp_path, retention_days=30)
    destination = tmp_path / "backups"
    destination.mkdir()

    recent_name = f"platform_backup_{(datetime.now(timezone.utc) - timedelta(days=5)).strftime(_BACKUP_TIMESTAMP_FORMAT)}.db"
    (destination / recent_name).write_bytes(b"recent backup")

    engine.backup_database(destination_dir=destination)

    assert (destination / recent_name).exists()


def test_backup_retention_never_deletes_unrecognized_files(tmp_path):
    """Only files matching this engine's own naming pattern are ever removed -- see _prune_old_backups' docstring."""
    engine, _ = _make_engine_with_db(tmp_path, retention_days=30)
    destination = tmp_path / "backups"
    destination.mkdir()

    mystery_file = destination / "platform_backup_not-a-real-timestamp.db"
    mystery_file.write_bytes(b"unknown origin")

    engine.backup_database(destination_dir=destination)

    assert mystery_file.exists()
