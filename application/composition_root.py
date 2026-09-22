"""
application/composition_root.py

Module 22 composition root -- constructs every repository, infrastructure
adapter, and engine from one `PlatformConfig`, wiring each one exactly as
its own constructor was designed to be used. This is the one place in the
codebase allowed to know about every module at once; nothing here
contains business logic of its own -- it only calls constructors and
returns the result.

Explicit over implicit: every repository below is constructed with this
function's own single `Database` instance passed in by name, rather than
relying on each repository's zero-argument `get_database()` fallback.
That fallback isn't wrong in production (it resolves to the same
configured path via a lazily-constructed singleton), but Module 21's own
development caught a real bug from exactly this class of implicit
fallback (a caller-supplied database not reaching a nested engine's own
repository dependency, because that engine's convenience default quietly
built its own). Being fully explicit here, once, at the one place that
affects the entire running platform rather than a single call, removes
that whole class of mistake outright.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

from config.loader import get_config
from config.schema import PlatformConfig
from core.models import ErrorEvent
from engines.analytics import AnalyticsEngine
from engines.bitcoin_intelligence import BitcoinIntelligenceEngine
from engines.position_monitor import PositionMonitorEngine
from engines.production import ProductionEngine
from engines.reporting import ReportingEngine
from engines.scanner_orchestrator import ScannerOrchestrator
from engines.telegram_notifications import TelegramNotificationEngine
from engines.trade_execution import TradeExecutionEngine
from infrastructure.bingx.client import BingXFuturesClient
from infrastructure.binance.client import BinanceFuturesClient
from infrastructure.database.connection import Database
from infrastructure.database.repositories import (
    CoinRepository,
    CoinStatisticsRepository,
    MissedOpportunityRepository,
    RejectionRepository,
    ReportRepository,
    SignalRepository,
    TradeRepository,
)
from infrastructure.database.repositories.market_repository import BtcStatisticsRepository
from infrastructure.database.repositories.system_repository import BotHealthRepository, ErrorEventRepository
from infrastructure.database.schema import run_migrations
from infrastructure.macro.client import MacroDataClient
from infrastructure.telegram.client import TelegramClient
from system.error_handler import register_alert_callback
from system.exceptions import PlatformError, Severity
from system.logging_setup import configure_logging, get_logger

_logger = get_logger("system")

_PERSISTED_SEVERITIES: frozenset[Severity] = frozenset({Severity.CRITICAL, Severity.FATAL})


def _persist_critical_errors(repository: ErrorEventRepository) -> Callable[[PlatformError], None]:
    """
    Builds the second alert callback registered by `build_platform()`,
    alongside `TelegramNotificationEngine.as_alert_callback()`.

    Without this, `ErrorEventRepository` (SRS Part 12/18) was never
    written to by anything in the platform -- `ProductionEngine
    .run_health_check()`'s `unresolved_critical` count (which decides
    whether a health snapshot is CRITICAL) always read back zero rows,
    regardless of how many CRITICAL/FATAL errors had actually occurred.
    `system.error_handler.register_alert_callback()`'s fan-out now
    includes WARNING/ERROR too (for the Telegram callback's
    notify_warning() path -- see that function's docstring), so this
    callback filters itself down to CRITICAL/FATAL only, matching
    `ErrorEvent`'s own docstring ("Persists CRITICAL/FATAL errors only").

    `category` has no dedicated channel from `handle_error()` through to
    a registered callback (only the `PlatformError` instance itself is
    passed -- see error_handler.py's `AlertCallback` type and its
    documented severity-override limitation, the same underlying gap).
    Using the concrete exception's class name as the category is real,
    always-available information (never guessed/invented) rather than
    forcing a choice among the file-logging categories, which this
    callback has no way to know.
    """

    def _callback(error: PlatformError) -> None:
        if error.severity not in _PERSISTED_SEVERITIES:
            return
        repository.create(
            ErrorEvent(
                occurred_at=datetime.now(timezone.utc),
                severity=error.severity.value,
                category=type(error).__name__,
                message=error.message,
                context=dict(error.context),
            )
        )

    return _callback


@dataclass
class PlatformComponents:
    """
    Everything `application/main.py` (and tests) need, already wired.
    One object instead of a dozen loose parameters passed around --
    this is a plain data container, not a service locator: nothing
    reaches back into it to look things up dynamically, every consumer
    just takes the specific fields it needs from it once, at
    construction time.
    """

    config: PlatformConfig
    database: Database

    binance_client: BinanceFuturesClient
    telegram_client: TelegramClient

    coin_repository: CoinRepository
    signal_repository: SignalRepository
    trade_repository: TradeRepository
    rejection_repository: RejectionRepository
    missed_opportunity_repository: MissedOpportunityRepository
    report_repository: ReportRepository
    coin_statistics_repository: CoinStatisticsRepository
    bot_health_repository: BotHealthRepository
    error_event_repository: ErrorEventRepository
    btc_statistics_repository: BtcStatisticsRepository

    # Own aiohttp session, same as binance_client/telegram_client above --
    # constructed here, entered (async with) by application/main.py, not
    # by this function (see build_platform()'s own docstring on that
    # split). Unlike those two, going without it is a supported, graceful
    # degradation (see infrastructure/macro/client.py's module docstring
    # and BitcoinIntelligenceEngine.__init__), not a hard requirement --
    # exposed here mainly so main.py has something to open.
    macro_client: MacroDataClient

    position_monitor: PositionMonitorEngine
    scanner: ScannerOrchestrator
    bitcoin_intelligence: BitcoinIntelligenceEngine
    notifications: TelegramNotificationEngine
    reporting: ReportingEngine
    analytics: AnalyticsEngine
    production: ProductionEngine
    trade_execution: TradeExecutionEngine
    bingx_client: BingXFuturesClient


def build_platform(config: Optional[PlatformConfig] = None) -> PlatformComponents:
    """
    Constructs the full dependency graph and registers two alert
    callbacks (SRS: the seam `system/error_handler.py`'s
    `register_alert_callback()` was built for): the Telegram engine's
    `as_alert_callback()` (WARNING/ERROR/CRITICAL/FATAL -> Telegram
    messages) and `_persist_critical_errors()` (CRITICAL/FATAL only ->
    `error_events` rows, which `ProductionEngine.run_health_check()`
    reads back).

    Deliberately synchronous and side-effect-limited to the database
    (construction + migrations only): `BinanceFuturesClient` /
    `TelegramClient` / `MacroDataClient` are async context managers, and
    entering them -- opening the actual HTTP sessions -- is
    `application/main.py`'s job as part of SRS's startup sequence, not
    this function's. Building the dependency graph and starting it are
    kept as two separate steps.
    """
    config = config or get_config()
    configure_logging(config.logging)

    database = Database(db_path=config.database.path, config=config)
    run_migrations(database)

    coin_repository = CoinRepository(database=database)
    signal_repository = SignalRepository(database=database)
    trade_repository = TradeRepository(database=database)
    rejection_repository = RejectionRepository(database=database)
    missed_opportunity_repository = MissedOpportunityRepository(database=database)
    report_repository = ReportRepository(database=database)
    coin_statistics_repository = CoinStatisticsRepository(database=database)
    bot_health_repository = BotHealthRepository(database=database)
    error_event_repository = ErrorEventRepository(database=database)
    btc_statistics_repository = BtcStatisticsRepository(database=database)

    binance_client = BinanceFuturesClient(config=config)
    telegram_client = TelegramClient(config=config)
    macro_client = MacroDataClient(config=config)

    # Constructed explicitly (rather than left to ScannerOrchestrator's own
    # bitcoin_engine=None fallback) for the same explicit-over-implicit
    # reason documented in this module's own docstring: that fallback
    # would build its OWN BitcoinIntelligenceEngine with macro_client=None,
    # silently leaving BTC/USDT dominance and DXY (this platform's own
    # ported sinyal_kanali_2 macro signals) permanently unfetched in a
    # real deployment. `notifications` below also depends on
    # btc_statistics_repository, so it needs to exist before that anyway.
    bitcoin_intelligence = BitcoinIntelligenceEngine(
        client=binance_client, config=config, repository=btc_statistics_repository, macro_client=macro_client,
    )

    # Constructed explicitly and handed to ScannerOrchestrator (rather than
    # left to its own position_monitor=None fallback) for the same reason
    # documented in this module's docstring: its fallback wouldn't receive
    # coin_statistics_repository, silently pointing trade-closing updates
    # at the wrong database.
    position_monitor = PositionMonitorEngine(
        config=config,
        signal_repository=signal_repository,
        trade_repository=trade_repository,
        coin_statistics_repository=coin_statistics_repository,
    )
    scanner = ScannerOrchestrator(
        client=binance_client,
        config=config,
        coin_repository=coin_repository,
        position_monitor=position_monitor,
        bitcoin_engine=bitcoin_intelligence,
    )

    # EXCHANGE SPLIT: real order execution goes to BingX, not Binance --
    # BingX does not mandate an IP allowlist for Futures trading
    # permission on an API key, so it works from a host with no static
    # outbound IP. Every market-data engine (scanner, bitcoin_intelligence,
    # etc.) keeps reading from binance_client regardless -- see
    # infrastructure.bingx.client's and engines.trade_execution's module
    # docstrings for the full reasoning. Constructed here (before
    # notifications and trade_execution both need it) rather than owned
    # by either.
    bingx_client = BingXFuturesClient(config=config)

    notifications = TelegramNotificationEngine(
        client=telegram_client, config=config,
        signal_repository=signal_repository, btc_repository=btc_statistics_repository,
        balance_client=bingx_client,
    )
    reporting = ReportingEngine(
        trade_repository=trade_repository,
        signal_repository=signal_repository,
        report_repository=report_repository,
        config=config,
    )
    analytics = AnalyticsEngine(
        rejection_repository=rejection_repository,
        trade_repository=trade_repository,
        missed_opportunity_repository=missed_opportunity_repository,
    )
    production = ProductionEngine(
        bot_health_repository=bot_health_repository,
        error_event_repository=error_event_repository,
        trade_repository=trade_repository,
        binance_client=binance_client,
        telegram_client=telegram_client,
        config=config,
    )
    # Safe to construct unconditionally even outside RunMode.LIVE:
    # trading_is_enabled gates every real action inside it, so a
    # PAPER-mode deployment simply never calls open_position()/
    # close_position() on it (see execution_modes/live.py's
    # _dispatch_notifications()).
    trade_execution = TradeExecutionEngine(
        execution_client=bingx_client, config=config,
        trade_repository=trade_repository, signal_repository=signal_repository,
    )

    register_alert_callback(notifications.as_alert_callback())
    register_alert_callback(_persist_critical_errors(error_event_repository))
    _logger.info(
        "Composition root: platform wired (run_mode=%s, db=%s)",
        config.general.run_mode.value, config.database.path,
    )

    return PlatformComponents(
        config=config,
        database=database,
        binance_client=binance_client,
        telegram_client=telegram_client,
        coin_repository=coin_repository,
        signal_repository=signal_repository,
        trade_repository=trade_repository,
        rejection_repository=rejection_repository,
        missed_opportunity_repository=missed_opportunity_repository,
        report_repository=report_repository,
        coin_statistics_repository=coin_statistics_repository,
        bot_health_repository=bot_health_repository,
        error_event_repository=error_event_repository,
        btc_statistics_repository=btc_statistics_repository,
        macro_client=macro_client,
        position_monitor=position_monitor,
        scanner=scanner,
        bitcoin_intelligence=bitcoin_intelligence,
        notifications=notifications,
        reporting=reporting,
        analytics=analytics,
        production=production,
        trade_execution=trade_execution,
        bingx_client=bingx_client,
    )
