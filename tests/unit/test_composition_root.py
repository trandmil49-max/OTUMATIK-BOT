"""
Unit tests for application/composition_root.py (Module 22).

The highest-value tests here verify every repository shares the SAME
`Database` instance -- exactly the bug class Module 21 caught (a
convenience default silently pointing a nested engine's repository at a
different database than everything else). A smoke test that only checks
"it didn't crash" would not have caught that bug; checking shared
identity would.

Run with:
    pytest tests/unit/test_composition_root.py -v
"""

import os

import pytest

from application.composition_root import build_platform
from config.schema import DatabaseConfig, GeneralConfig, PlatformConfig
from engines.bitcoin_intelligence import BitcoinIntelligenceEngine
from engines.production import ProductionEngine
from engines.scanner_orchestrator import ScannerOrchestrator
from engines.telegram_notifications import TelegramNotificationEngine
from engines.trade_execution import TradeExecutionEngine
from infrastructure.binance.client import BinanceFuturesClient
from infrastructure.bingx.client import BingXFuturesClient
from infrastructure.macro.client import MacroDataClient
from infrastructure.telegram.client import TelegramClient
from system import error_handler
from system.exceptions import ConfigurationError, PlatformError, Severity


@pytest.fixture(autouse=True)
def _reset_alert_callbacks():
    """
    Every test in this file calls build_platform(), which registers
    alert callbacks into system.error_handler's module-level (process-
    global) registry. Without this, callbacks accumulate silently across
    every test in this file for the rest of the pytest session -- a
    pre-existing gap that happened not to matter while no test here
    actually triggered handle_error(), but does matter for the
    error-event-persistence tests below.
    """
    error_handler.clear_alert_callbacks_for_tests()
    yield
    error_handler.clear_alert_callbacks_for_tests()


def _config(tmp_path, **general_overrides) -> PlatformConfig:
    return PlatformConfig(
        database=DatabaseConfig(path=str(tmp_path / "test_platform.db")),
        general=GeneralConfig(**general_overrides),
    )


def test_build_platform_creates_the_database_file_at_the_configured_path(tmp_path):
    config = _config(tmp_path)
    build_platform(config)
    assert os.path.exists(config.database.path)


def test_build_platform_runs_migrations(tmp_path):
    config = _config(tmp_path)
    components = build_platform(config)

    with components.database.read_connection() as conn:
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
    assert "trades" in tables
    assert "signals" in tables
    assert "coins" in tables


def test_build_platform_every_repository_shares_the_same_database_instance(tmp_path):
    """Regression coverage for the exact bug class Module 21 caught: a repository silently pointed at a different database than the rest of the platform."""
    components = build_platform(_config(tmp_path))

    repositories = [
        components.coin_repository, components.signal_repository, components.trade_repository,
        components.rejection_repository, components.missed_opportunity_repository,
        components.report_repository, components.coin_statistics_repository,
        components.bot_health_repository, components.error_event_repository,
        components.btc_statistics_repository,
    ]
    for repo in repositories:
        assert repo.database is components.database, f"{type(repo).__name__} does not share the platform's database"


def test_build_platform_scanner_shares_the_explicitly_built_bitcoin_intelligence_engine(tmp_path):
    """The explicitly-constructed BitcoinIntelligenceEngine passed to ScannerOrchestrator must be the SAME instance, not a second one built by ScannerOrchestrator's own fallback (which would default macro_client to None, leaving dominance/DXY permanently unfetched)."""
    components = build_platform(_config(tmp_path))
    assert components.scanner._bitcoin_engine is components.bitcoin_intelligence


def test_build_platform_bitcoin_intelligence_shares_macro_client_and_btc_repository(tmp_path):
    components = build_platform(_config(tmp_path))
    assert components.bitcoin_intelligence._macro_client is components.macro_client
    assert components.bitcoin_intelligence._repository is components.btc_statistics_repository


def test_build_platform_notifications_shares_signal_and_btc_repositories(tmp_path):
    """Wired so notify_new_signal()'s confirmation checklist / "BTC Status" line have real data to read, instead of silently omitting that section for lack of a signal_repository/btc_repository."""
    components = build_platform(_config(tmp_path))
    assert components.notifications._signal_repository is components.signal_repository
    assert components.notifications._btc_repository is components.btc_statistics_repository


def test_build_platform_position_monitor_shares_repositories_with_scanner(tmp_path):
    """The explicitly-constructed PositionMonitorEngine passed to ScannerOrchestrator must be the SAME instance, not a second one built by ScannerOrchestrator's own fallback."""
    components = build_platform(_config(tmp_path))
    assert components.scanner._position_monitor is components.position_monitor


def test_build_platform_returns_correctly_typed_engines(tmp_path):
    components = build_platform(_config(tmp_path))

    assert isinstance(components.scanner, ScannerOrchestrator)
    assert isinstance(components.notifications, TelegramNotificationEngine)
    assert isinstance(components.production, ProductionEngine)
    assert isinstance(components.binance_client, BinanceFuturesClient)
    assert isinstance(components.telegram_client, TelegramClient)
    assert isinstance(components.bitcoin_intelligence, BitcoinIntelligenceEngine)
    assert isinstance(components.macro_client, MacroDataClient)
    assert isinstance(components.trade_execution, TradeExecutionEngine)
    assert isinstance(components.bingx_client, BingXFuturesClient)


def test_build_platform_reporting_and_analytics_share_trade_repository(tmp_path):
    components = build_platform(_config(tmp_path))
    assert components.reporting._trades is components.trade_repository
    assert components.analytics._trades is components.trade_repository


def test_build_platform_production_engine_shares_clients(tmp_path):
    components = build_platform(_config(tmp_path))
    assert components.production._binance is components.binance_client
    assert components.production._telegram is components.telegram_client


def test_build_platform_trade_execution_shares_bingx_client_and_repositories(tmp_path):
    """TradeExecutionEngine must share the SAME bingx_client the rest of the platform uses -- never a second one it opens itself."""
    components = build_platform(_config(tmp_path))
    assert components.trade_execution._client is components.bingx_client
    assert components.trade_execution._trade_repository is components.trade_repository
    assert components.trade_execution._signal_repository is components.signal_repository


def test_build_platform_notifications_shares_the_same_bingx_client_used_for_execution(tmp_path):
    """The "💼 Bakiye" balance line must read the SAME account TradeExecutionEngine trades on (BingX), not Binance's."""
    components = build_platform(_config(tmp_path))
    assert components.notifications._balance_client is components.bingx_client
    assert components.notifications._balance_client is components.trade_execution._client


def test_build_platform_registers_an_alert_callback_without_error(tmp_path):
    """register_alert_callback() is called during wiring -- build_platform() itself succeeding is the observable proof it didn't raise; deeper introspection of the callback registry is system/error_handler.py's own test surface, not this module's."""
    build_platform(_config(tmp_path))  # must not raise


def test_build_platform_persists_critical_errors_to_error_events(tmp_path):
    """
    Regression coverage: before _persist_critical_errors() existed,
    nothing anywhere ever wrote to ErrorEventRepository, so
    ProductionEngine.run_health_check()'s unresolved_critical count was
    permanently zero no matter how many CRITICAL/FATAL errors occurred.
    """
    components = build_platform(_config(tmp_path))

    error_handler.handle_error(ConfigurationError("STRATEGY_PROFILE missing"))  # default_severity == FATAL

    unresolved = components.error_event_repository.get_unresolved()
    assert len(unresolved) == 1
    assert unresolved[0].severity == Severity.FATAL.value
    assert unresolved[0].category == "ConfigurationError"
    assert unresolved[0].message == "STRATEGY_PROFILE missing"


def test_build_platform_does_not_persist_non_critical_errors_to_error_events(tmp_path):
    """WARNING/ERROR now reach the Telegram callback (notify_warning()) but must NOT also create an error_events row -- that table is CRITICAL/FATAL only, per ErrorEvent's own docstring."""
    components = build_platform(_config(tmp_path))

    error_handler.handle_error(PlatformError("recoverable hiccup", severity=Severity.WARNING))

    assert components.error_event_repository.get_unresolved() == []


def test_build_platform_uses_provided_config_run_mode(tmp_path):
    components = build_platform(_config(tmp_path, run_mode="backtest"))
    assert components.config.general.run_mode.value == "backtest"


def test_build_platform_defaults_to_get_config_when_none_provided(tmp_path, monkeypatch):
    """Calling build_platform() with no argument falls back to get_config() -- verified without touching the real default database path by monkeypatching get_config itself."""
    fake_config = _config(tmp_path)
    monkeypatch.setattr("application.composition_root.get_config", lambda: fake_config)

    components = build_platform()

    assert components.config is fake_config
