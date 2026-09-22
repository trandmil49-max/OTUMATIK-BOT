"""
engines/production.py

Production Engine (Module 20) -- SRS Part 18: "Production Engine
(Watchdog, Auto-Recovery, Health Checks, Backups)".

SCOPE OF THIS PASS -- read before extending. Same discipline as Modules
18/19: implement only what's objectively derivable from existing,
documented sources; defer and clearly flag anything that would need an
invented formula, an undocumented policy, or a new dependency.

    IN SCOPE, fully implemented and tested:
        * `run_health_check()` -- combines real connectivity checks
          (`BinanceFuturesClient.check_health()`,
          `TelegramClient.check_health()`, both real network calls, not
          guesses) with objective counters (active trades via
          `TradeRepository`, unresolved CRITICAL/FATAL errors via
          `ErrorEventRepository`, database file size via `os.path`) into
          a persisted `BotHealthSnapshot`. Status (HEALTHY/WARNING/
          CRITICAL) is a transparent, documented rule over those
          signals -- not a claimed SRS formula, see `_determine_status`.
        * `backup_database()` -- copies the SQLite file at
          `DatabaseConfig.path`, prunes backups older than
          `DatabaseConfig.retention_policy_days` (None = never delete,
          per that field's own SRS-quoted validator in
          `config/schema.py`). Every parameter here is an existing,
          documented config value; nothing is invented.
        * `infrastructure/watchdog.py`'s `Watchdog` -- generic
          heartbeat-staleness DETECTION, exposed here via
          `self.watchdog`, seeded from
          `PerformanceConfig.max_scan_duration_seconds`.

    DELIBERATELY NOT IMPLEMENTED HERE -- flagged rather than guessed at:
        * CPU / RAM metrics (`BotHealthSnapshot.cpu_percent`/`.ram_mb`
          stay `None`). Reading real process resource usage needs
          `psutil` (or fragile manual `/proc` parsing), which is not in
          requirements.txt. Adding a dependency is not this module's
          call to make unilaterally -- same reasoning as Module 18's
          deferred CSV/Excel/PDF export.
        * `average_scan_duration_seconds`, `average_api_response_ms`,
          `symbols_scanned_count`, `retry_count`, `restart_count` stay
          at their dataclass defaults. Nothing in the codebase currently
          instruments these as running counters anywhere this engine
          can read from; populating them for real means adding timing/
          counting instrumentation inside `scanner_orchestrator.py` and
          friends, which is a change to already-completed modules this
          pass was told not to make.
        * Auto-recovery ACTION (automatically restarting or repairing
          something after a detected failure). There is no running
          process to restart yet -- Module 22's main loop does not
          exist -- and the recovery POLICY (retry immediately? back off?
          how many attempts before alerting a human instead?) is not
          documented anywhere. `Watchdog` provides the detection half;
          the action half is Module 22's decision once there is
          something concrete to act on.
        * `PerformanceConfig.max_cpu_percent` / `.max_ram_mb` are not
          used in `_determine_status()` for the same reason as the first
          bullet above: there is no cpu/ram reading to compare them
          against yet. They're the natural future input once that
          dependency question is resolved.

Clean Architecture: this engine owns the health-status decision and
backup-retention logic; it consumes repositories and the Binance/
Telegram clients via constructor injection and performs no networking
of its own beyond calling those clients' own `check_health()` methods.
"""

from __future__ import annotations

import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from config.loader import get_config
from config.schema import PlatformConfig
from core.models import BotHealthSnapshot
from infrastructure.binance.client import BinanceFuturesClient
from infrastructure.database.repositories.system_repository import BotHealthRepository, ErrorEventRepository
from infrastructure.database.repositories.trade_repository import TradeRepository
from infrastructure.telegram.client import TelegramClient
from infrastructure.watchdog import Watchdog
from system.logging_setup import get_logger

_logger = get_logger("system")

_BACKUP_TIMESTAMP_FORMAT = "%Y%m%dT%H%M%SZ"


class ProductionEngine:
    """
    Consumes repositories and both external clients via dependency
    injection. Every dependency here serves a distinct, necessary
    purpose for "what is the platform's current operational state" --
    not accidental coupling: connectivity (Binance/Telegram), workload
    (active trades), and unresolved failures (error events) are each a
    genuinely separate signal a production health check needs.
    """

    def __init__(
        self,
        bot_health_repository: BotHealthRepository,
        error_event_repository: ErrorEventRepository,
        trade_repository: TradeRepository,
        binance_client: BinanceFuturesClient,
        telegram_client: TelegramClient,
        config: Optional[PlatformConfig] = None,
    ) -> None:
        self._bot_health = bot_health_repository
        self._errors = error_event_repository
        self._trades = trade_repository
        self._binance = binance_client
        self._telegram = telegram_client
        self._config = config or get_config()
        self.watchdog = Watchdog(stale_after_seconds=self._config.performance.max_scan_duration_seconds)

    # ─────────────────────────────────────────────────────────────────
    # HEALTH CHECK
    # ─────────────────────────────────────────────────────────────────

    async def run_health_check(self) -> BotHealthSnapshot:
        binance_ok = await self._binance.check_health()
        telegram_ok = await self._telegram.check_health()
        active_trades = len(self._trades.get_active_trades())
        unresolved_critical = len(self._errors.get_unresolved())
        database_size_mb = self._read_database_size_mb()

        status = self._determine_status(
            binance_ok=binance_ok, telegram_ok=telegram_ok, unresolved_critical=unresolved_critical,
        )

        snapshot = BotHealthSnapshot(
            snapshot_time=datetime.now(timezone.utc),
            status=status,
            database_size_mb=database_size_mb,
            error_count=unresolved_critical,
            active_trades_count=active_trades,
        )
        saved = self._bot_health.create(snapshot)
        _logger.info(
            "Health check: status=%s binance_ok=%s telegram_ok=%s active_trades=%d unresolved_critical=%d",
            status, binance_ok, telegram_ok, active_trades, unresolved_critical,
        )
        return saved

    def _determine_status(self, *, binance_ok: bool, telegram_ok: bool, unresolved_critical: int) -> str:
        """
        Transparent, documented interpretation -- not a claimed SRS
        formula (see module docstring). Binance connectivity is
        existential: no Binance means no scanning at all, the
        platform's entire purpose, so its failure is CRITICAL. An
        unresolved CRITICAL/FATAL error was already judged critical by
        whoever recorded it (system/error_handler.py's own
        ALERTABLE_SEVERITIES gate), so that judgment propagates here
        unchanged rather than being re-scored. A Telegram outage
        degrades notifications only, not the platform's core scanning
        function, so it's WARNING rather than CRITICAL.
        """
        if not binance_ok or unresolved_critical > 0:
            return "CRITICAL"
        if not telegram_ok:
            return "WARNING"
        return "HEALTHY"

    def _read_database_size_mb(self) -> Optional[float]:
        path = Path(self._config.database.path)
        if not path.exists():
            return None
        return round(path.stat().st_size / (1024 * 1024), 3)

    # ─────────────────────────────────────────────────────────────────
    # BACKUPS  (sync -- pure file I/O, no network/async work involved)
    # ─────────────────────────────────────────────────────────────────

    def backup_database(self, *, destination_dir: Path) -> Path:
        """
        Copies the SQLite file at `config.database.path` into
        `destination_dir` with a timestamped name, then prunes old
        backups per `config.database.retention_policy_days`. Raises
        `FileNotFoundError` if the source database doesn't exist yet
        rather than silently doing nothing.
        """
        source = Path(self._config.database.path)
        if not source.exists():
            raise FileNotFoundError(f"Database file not found at {source}")

        destination_dir = Path(destination_dir)
        destination_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now(timezone.utc).strftime(_BACKUP_TIMESTAMP_FORMAT)
        backup_path = destination_dir / f"{source.stem}_backup_{timestamp}{source.suffix}"
        shutil.copy2(source, backup_path)

        removed = self._prune_old_backups(destination_dir, source.stem, source.suffix)
        _logger.info(
            "Database backed up to %s (%d old backup(s) pruned)", backup_path, len(removed)
        )
        return backup_path

    def _prune_old_backups(self, destination_dir: Path, stem: str, suffix: str) -> list[Path]:
        """
        `retention_policy_days=None` means never delete (SRS: "Do not
        delete historical data" -- config/schema.py's own validator
        enforces >= 30 if set at all). A backup file whose name doesn't
        match the expected timestamp format is left alone -- this
        method only ever removes files it can positively identify as
        its own past backups, mirroring PROJECT_STATUS.md's own logged
        rule to never delete an unfamiliar file without being able to
        account for what it is.
        """
        retention_days = self._config.database.retention_policy_days
        if retention_days is None:
            return []

        cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
        prefix = f"{stem}_backup_"
        removed: list[Path] = []
        for path in destination_dir.glob(f"{prefix}*{suffix}"):
            timestamp_text = path.name[len(prefix): -len(suffix)] if suffix else path.name[len(prefix):]
            try:
                file_time = datetime.strptime(timestamp_text, _BACKUP_TIMESTAMP_FORMAT).replace(
                    tzinfo=timezone.utc
                )
            except ValueError:
                continue
            if file_time < cutoff:
                path.unlink()
                removed.append(path)
        return removed
