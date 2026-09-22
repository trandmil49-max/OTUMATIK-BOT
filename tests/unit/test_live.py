"""
Unit tests for execution_modes/live.py (Module 22).

Uses lightweight fakes for every engine LiveRunner touches (scanner,
production, notifications, reporting, trade_repository) so scheduling
and dispatch logic is tested in isolation and fast, with a real
PlatformConfig so ReportConfig/DatabaseConfig/GeneralConfig validation
and defaults are exercised for real rather than assumed. Fields of
PlatformComponents that LiveRunner never touches are left as None.

Run with:
    pytest tests/unit/test_live.py -v
"""

import asyncio
from datetime import date, datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from application.composition_root import PlatformComponents
from config.schema import DatabaseConfig, GeneralConfig, PlatformConfig, ReportConfig, ScannerConfig
from execution_modes.live import LiveRunner, _previous_month_range

UTC = timezone.utc


# ── fakes ──────────────────────────────────────────────────────────────


class _FakeWatchdog:
    def __init__(self):
        self.heartbeat_count = 0

    def record_heartbeat(self):
        self.heartbeat_count += 1


class _FakeProduction:
    def __init__(self, *, backup_raises: bool = False):
        self.watchdog = _FakeWatchdog()
        self.health_check_count = 0
        self.backup_calls: list = []
        self._backup_raises = backup_raises

    async def run_health_check(self):
        self.health_check_count += 1

    def backup_database(self, *, destination_dir):
        if self._backup_raises:
            raise FileNotFoundError("no db yet")
        self.backup_calls.append(destination_dir)
        return destination_dir / "fake_backup.db"


class _FakeNotifications:
    def __init__(self):
        self.new_signal_calls: list = []
        self.expired_calls: list = []
        self.tp1_calls: list = []
        self.tp2_calls: list = []
        self.sl_calls: list = []
        self.trade_closed_calls: list = []
        self.daily_report_calls: list = []
        self.weekly_report_calls: list = []
        self.monthly_report_calls: list = []

    async def notify_new_signal(self, signal):
        self.new_signal_calls.append(signal)

    async def notify_signal_expired(self, signal):
        self.expired_calls.append(signal)

    async def notify_trade_closed(self, trade):
        self.trade_closed_calls.append(trade)

    async def notify_daily_report(self, report):
        self.daily_report_calls.append(report)

    async def notify_weekly_report(self, report):
        self.weekly_report_calls.append(report)

    async def notify_monthly_report(self, report):
        self.monthly_report_calls.append(report)


class _FakeReporting:
    def __init__(self):
        self.daily_calls: list = []
        self.weekly_calls: list = []
        self.monthly_calls: list = []

    async def generate_daily_report(self, *, period_start, period_end):
        self.daily_calls.append((period_start, period_end))
        return "DAILY_REPORT"

    async def generate_weekly_report(self, *, period_start, period_end):
        self.weekly_calls.append((period_start, period_end))
        return "WEEKLY_REPORT"

    async def generate_monthly_report(self, *, period_start, period_end):
        self.monthly_calls.append((period_start, period_end))
        return "MONTHLY_REPORT"


class _FakeTradeRepository:
    def __init__(self, trades_by_id: dict):
        self._trades = trades_by_id

    def get_by_id(self, trade_id):
        return self._trades.get(trade_id)


class _FakeSignalRepository:
    def __init__(self, signals_by_id: dict):
        self._signals = signals_by_id

    def get_by_id(self, signal_id):
        return self._signals.get(signal_id)


class _FakeTradeExecution:
    def __init__(self, *, trading_is_enabled: bool = False, open_position_returns=None):
        self.trading_is_enabled = trading_is_enabled
        self._open_position_returns = open_position_returns
        self.open_position_calls: list = []
        self.close_position_calls: list = []

    async def open_position(self, signal):
        self.open_position_calls.append(signal)
        return self._open_position_returns

    async def close_position(self, trade):
        self.close_position_calls.append(trade)


class _FakeScanner:
    def __init__(self, result=None, *, raises: Exception = None):
        self._result = result or SimpleNamespace(signals_generated=[], monitor_result=None)
        self._raises = raises
        self.call_count = 0

    async def run_scan_cycle(self):
        self.call_count += 1
        if self._raises:
            raise self._raises
        return self._result


def _monitor_result(tp1=(), sl=(), expired=(), trailing=()):
    return SimpleNamespace(
        tp1_hit_trade_ids=tp1, stop_loss_trade_ids=sl, expired_signal_ids=expired,
        trailing_stop_exit_trade_ids=trailing,
    )


def _components(
    *, scanner=None, production=None, notifications=None, reporting=None, trades=None, signals=None,
    trade_execution=None,
    auto_restart=True, run_mode="paper", fast_scan_interval_seconds=30,
    daily_report_time="23:55", weekly_report_day="sunday", monthly_report_day=1,
    backup_interval_hours=24, timezone_name="UTC",
) -> PlatformComponents:
    config = PlatformConfig(
        general=GeneralConfig(auto_restart=auto_restart, run_mode=run_mode, timezone=timezone_name),
        scanner=ScannerConfig(fast_scan_interval_seconds=fast_scan_interval_seconds),
        reports=ReportConfig(
            daily_report_time=daily_report_time, weekly_report_day=weekly_report_day,
            monthly_report_day=monthly_report_day,
        ),
        database=DatabaseConfig(backup_interval_hours=backup_interval_hours),
    )
    return PlatformComponents(
        config=config, database=None, binance_client=None, telegram_client=None,
        coin_repository=None, signal_repository=_FakeSignalRepository(signals or {}),
        trade_repository=_FakeTradeRepository(trades or {}),
        rejection_repository=None, missed_opportunity_repository=None, report_repository=None,
        coin_statistics_repository=None, bot_health_repository=None, error_event_repository=None,
        btc_statistics_repository=None, macro_client=None,
        position_monitor=None,
        scanner=scanner or _FakeScanner(),
        bitcoin_intelligence=None,
        notifications=notifications or _FakeNotifications(),
        reporting=reporting or _FakeReporting(),
        analytics=None,
        production=production or _FakeProduction(),
        trade_execution=trade_execution or _FakeTradeExecution(),
        bingx_client=None,
    )


# ── run_one_cycle: notification dispatch ─────────────────────────────────


@pytest.mark.asyncio
async def test_run_one_cycle_records_watchdog_heartbeat_on_success():
    components = _components()
    runner = LiveRunner(components)

    await runner.run_one_cycle(now=datetime(2026, 6, 1, 10, 0, tzinfo=UTC))

    assert components.production.watchdog.heartbeat_count == 1


@pytest.mark.asyncio
async def test_run_one_cycle_pushes_the_scan_result_to_a_wired_command_poller():
    """Regression coverage for the engines/telegram_commands.py wiring: /scan has nothing to report unless this actually happens every cycle."""
    class _FakePoller:
        def __init__(self):
            self.recorded: list[tuple] = []

        def record_scan_summary(self, result, *, at=None):
            self.recorded.append((result, at))

    components = _components()
    poller = _FakePoller()
    runner = LiveRunner(components, command_poller=poller)
    now = datetime(2026, 6, 1, 10, 0, tzinfo=UTC)

    await runner.run_one_cycle(now=now)

    assert len(poller.recorded) == 1
    result, at = poller.recorded[0]
    assert result is components.scanner._result
    assert at == now


@pytest.mark.asyncio
async def test_run_one_cycle_without_a_wired_poller_does_not_error():
    """The default (no poller passed) must keep working exactly as before this feature existed."""
    components = _components()
    runner = LiveRunner(components)  # no command_poller=

    await runner.run_one_cycle(now=datetime(2026, 6, 1, 10, 0, tzinfo=UTC))  # must not raise


@pytest.mark.asyncio
async def test_run_one_cycle_dispatches_new_signal_notifications():
    fake_signal = object()
    scanner = _FakeScanner(SimpleNamespace(signals_generated=[fake_signal], monitor_result=None))
    components = _components(scanner=scanner)
    runner = LiveRunner(components)

    await runner.run_one_cycle(now=datetime(2026, 6, 1, 10, 0, tzinfo=UTC))

    assert components.notifications.new_signal_calls == [fake_signal]


# ── run_one_cycle: autonomous trading dispatch (trade_execution) ─────────


@pytest.mark.asyncio
async def test_run_one_cycle_paper_mode_never_calls_open_position():
    """trading_is_enabled=False (the _FakeTradeExecution default) -- notify immediately, exactly like before the pivot."""
    fake_signal = object()
    scanner = _FakeScanner(SimpleNamespace(signals_generated=[fake_signal], monitor_result=None))
    trade_execution = _FakeTradeExecution(trading_is_enabled=False)
    components = _components(scanner=scanner, trade_execution=trade_execution)
    runner = LiveRunner(components)

    await runner.run_one_cycle(now=datetime(2026, 6, 1, 10, 0, tzinfo=UTC))

    assert trade_execution.open_position_calls == []
    assert components.notifications.new_signal_calls == [fake_signal]


@pytest.mark.asyncio
async def test_run_one_cycle_live_trading_opens_a_real_position_and_then_notifies():
    fake_signal = object()
    fake_trade = object()
    scanner = _FakeScanner(SimpleNamespace(signals_generated=[fake_signal], monitor_result=None))
    trade_execution = _FakeTradeExecution(trading_is_enabled=True, open_position_returns=fake_trade)
    components = _components(scanner=scanner, trade_execution=trade_execution)
    runner = LiveRunner(components)

    await runner.run_one_cycle(now=datetime(2026, 6, 1, 10, 0, tzinfo=UTC))

    assert trade_execution.open_position_calls == [fake_signal]
    assert components.notifications.new_signal_calls == [fake_signal]


@pytest.mark.asyncio
async def test_run_one_cycle_live_trading_skips_notification_when_open_position_returns_none():
    """A silently-skipped signal (no balance, size rounds to zero, etc.) must never send a misleading "trade opened" message."""
    fake_signal = object()
    scanner = _FakeScanner(SimpleNamespace(signals_generated=[fake_signal], monitor_result=None))
    trade_execution = _FakeTradeExecution(trading_is_enabled=True, open_position_returns=None)
    components = _components(scanner=scanner, trade_execution=trade_execution)
    runner = LiveRunner(components)

    await runner.run_one_cycle(now=datetime(2026, 6, 1, 10, 0, tzinfo=UTC))

    assert trade_execution.open_position_calls == [fake_signal]
    assert components.notifications.new_signal_calls == []


@pytest.mark.asyncio
async def test_run_one_cycle_closes_the_real_position_after_notifying_tp1():
    fake_trade = object()
    scanner = _FakeScanner(SimpleNamespace(signals_generated=[], monitor_result=_monitor_result(tp1=(1,))))
    trade_execution = _FakeTradeExecution(trading_is_enabled=True)
    components = _components(scanner=scanner, trades={1: fake_trade}, trade_execution=trade_execution)
    runner = LiveRunner(components)

    await runner.run_one_cycle(now=datetime(2026, 6, 1, 10, 0, tzinfo=UTC))

    assert components.notifications.trade_closed_calls == [fake_trade]
    assert trade_execution.close_position_calls == [fake_trade]


@pytest.mark.asyncio
async def test_run_one_cycle_closes_the_real_position_after_notifying_stop_loss():
    fake_trade = object()
    scanner = _FakeScanner(SimpleNamespace(signals_generated=[], monitor_result=_monitor_result(sl=(2,))))
    trade_execution = _FakeTradeExecution(trading_is_enabled=True)
    components = _components(scanner=scanner, trades={2: fake_trade}, trade_execution=trade_execution)
    runner = LiveRunner(components)

    await runner.run_one_cycle(now=datetime(2026, 6, 1, 10, 0, tzinfo=UTC))

    assert components.notifications.trade_closed_calls == [fake_trade]
    assert trade_execution.close_position_calls == [fake_trade]


@pytest.mark.asyncio
async def test_run_one_cycle_closes_the_real_position_after_notifying_trailing_stop_exit():
    fake_trade = object()
    scanner = _FakeScanner(SimpleNamespace(signals_generated=[], monitor_result=_monitor_result(trailing=(3,))))
    trade_execution = _FakeTradeExecution(trading_is_enabled=True)
    components = _components(scanner=scanner, trades={3: fake_trade}, trade_execution=trade_execution)
    runner = LiveRunner(components)

    await runner.run_one_cycle(now=datetime(2026, 6, 1, 10, 0, tzinfo=UTC))

    assert components.notifications.trade_closed_calls == [fake_trade]
    assert trade_execution.close_position_calls == [fake_trade]


@pytest.mark.asyncio
async def test_run_one_cycle_never_calls_close_position_for_an_untracked_trade_id():
    scanner = _FakeScanner(SimpleNamespace(signals_generated=[], monitor_result=_monitor_result(tp1=(999,))))
    trade_execution = _FakeTradeExecution(trading_is_enabled=True)
    components = _components(scanner=scanner, trades={}, trade_execution=trade_execution)  # 999 not in trades
    runner = LiveRunner(components)

    await runner.run_one_cycle(now=datetime(2026, 6, 1, 10, 0, tzinfo=UTC))

    assert trade_execution.close_position_calls == []


@pytest.mark.asyncio
async def test_run_one_cycle_dispatches_expired_signal_notifications():
    """
    Regression test: PositionMonitorEngine already computed
    expired_signal_ids every tick, but _dispatch_notifications() never
    read the field at all, so a signal that timed out while WAITING
    produced zero Telegram notification.
    """
    fake_signal = object()
    scanner = _FakeScanner(SimpleNamespace(signals_generated=[], monitor_result=_monitor_result(expired=(3,))))
    components = _components(scanner=scanner, signals={3: fake_signal})
    runner = LiveRunner(components)

    await runner.run_one_cycle(now=datetime(2026, 6, 1, 10, 0, tzinfo=UTC))

    assert components.notifications.expired_calls == [fake_signal]


@pytest.mark.asyncio
async def test_run_one_cycle_skips_expired_notification_if_signal_missing():
    """Defensive: an id present in expired_signal_ids but absent from the repository (shouldn't happen, but must not crash the cycle) is silently skipped, same defensive pattern as the trade lookups below."""
    scanner = _FakeScanner(SimpleNamespace(signals_generated=[], monitor_result=_monitor_result(expired=(404,))))
    components = _components(scanner=scanner, signals={})
    runner = LiveRunner(components)

    await runner.run_one_cycle(now=datetime(2026, 6, 1, 10, 0, tzinfo=UTC))

    assert components.notifications.expired_calls == []


@pytest.mark.asyncio
async def test_run_one_cycle_dispatches_only_trade_closed_for_tp1():
    """
    Single-TP model: TP1_HIT is a full, final close, so it dispatches
    exactly one notify_trade_closed() -- no separate "hit" ping.
    """
    fake_trade = object()
    scanner = _FakeScanner(SimpleNamespace(signals_generated=[], monitor_result=_monitor_result(tp1=(1,))))
    components = _components(scanner=scanner, trades={1: fake_trade})
    runner = LiveRunner(components)

    await runner.run_one_cycle(now=datetime(2026, 6, 1, 10, 0, tzinfo=UTC))

    assert components.notifications.trade_closed_calls == [fake_trade]


@pytest.mark.asyncio
async def test_run_one_cycle_dispatches_only_trade_closed_for_stop_loss():
    """Same fix as the TP2 case above, for the stop-loss path."""
    fake_trade = object()
    scanner = _FakeScanner(SimpleNamespace(signals_generated=[], monitor_result=_monitor_result(sl=(9,))))
    components = _components(scanner=scanner, trades={9: fake_trade})
    runner = LiveRunner(components)

    await runner.run_one_cycle(now=datetime(2026, 6, 1, 10, 0, tzinfo=UTC))

    assert components.notifications.sl_calls == []
    assert components.notifications.trade_closed_calls == [fake_trade]


@pytest.mark.asyncio
async def test_run_one_cycle_skips_notification_for_untracked_trade_id():
    """Defensive: get_by_id() returning None (trade vanished/never existed) must not raise."""
    scanner = _FakeScanner(SimpleNamespace(signals_generated=[], monitor_result=_monitor_result(tp1=(999,))))
    components = _components(scanner=scanner, trades={})
    runner = LiveRunner(components)

    await runner.run_one_cycle(now=datetime(2026, 6, 1, 10, 0, tzinfo=UTC))

    assert components.notifications.tp2_calls == []
    assert components.notifications.trade_closed_calls == []


@pytest.mark.asyncio
async def test_run_one_cycle_runs_health_check_every_cycle():
    components = _components()
    runner = LiveRunner(components)

    await runner.run_one_cycle(now=datetime(2026, 6, 1, 10, 0, tzinfo=UTC))
    await runner.run_one_cycle(now=datetime(2026, 6, 1, 10, 1, tzinfo=UTC))

    assert components.production.health_check_count == 2


# ── run_one_cycle: recovery (general.auto_restart) ───────────────────────


@pytest.mark.asyncio
async def test_run_one_cycle_auto_restart_true_swallows_exception():
    scanner = _FakeScanner(raises=RuntimeError("boom"))
    components = _components(scanner=scanner, auto_restart=True)
    runner = LiveRunner(components)

    await runner.run_one_cycle(now=datetime(2026, 6, 1, 10, 0, tzinfo=UTC))  # must not raise

    assert components.production.watchdog.heartbeat_count == 0  # never reached after the raise


@pytest.mark.asyncio
async def test_run_one_cycle_auto_restart_false_reraises_exception():
    scanner = _FakeScanner(raises=RuntimeError("boom"))
    components = _components(scanner=scanner, auto_restart=False)
    runner = LiveRunner(components)

    with pytest.raises(RuntimeError):
        await runner.run_one_cycle(now=datetime(2026, 6, 1, 10, 0, tzinfo=UTC))


# ── scheduling predicates: daily ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_daily_report_fires_at_or_after_configured_time():
    components = _components(daily_report_time="12:00")
    runner = LiveRunner(components)

    await runner.run_one_cycle(now=datetime(2026, 6, 1, 12, 0, tzinfo=UTC))

    assert components.reporting.daily_calls
    assert components.notifications.daily_report_calls == ["DAILY_REPORT"]


@pytest.mark.asyncio
async def test_daily_report_does_not_fire_before_configured_time():
    components = _components(daily_report_time="12:00")
    runner = LiveRunner(components)

    await runner.run_one_cycle(now=datetime(2026, 6, 1, 11, 59, tzinfo=UTC))

    assert components.reporting.daily_calls == []


@pytest.mark.asyncio
async def test_daily_report_does_not_fire_twice_same_day():
    components = _components(daily_report_time="12:00")
    runner = LiveRunner(components)

    await runner.run_one_cycle(now=datetime(2026, 6, 1, 12, 0, tzinfo=UTC))
    await runner.run_one_cycle(now=datetime(2026, 6, 1, 18, 0, tzinfo=UTC))

    assert len(components.reporting.daily_calls) == 1


@pytest.mark.asyncio
async def test_daily_report_fires_again_the_next_day():
    components = _components(daily_report_time="12:00")
    runner = LiveRunner(components)

    await runner.run_one_cycle(now=datetime(2026, 6, 1, 12, 0, tzinfo=UTC))
    await runner.run_one_cycle(now=datetime(2026, 6, 2, 12, 0, tzinfo=UTC))

    assert len(components.reporting.daily_calls) == 2


def test_daily_report_period_is_the_full_previous_day():
    components = _components(daily_report_time="12:00", timezone_name="UTC")
    runner = LiveRunner(components)

    local_now = datetime(2026, 6, 2, tzinfo=ZoneInfo("UTC"))
    asyncio.run(runner._run_daily_report(local_now))

    (actual_start, actual_end), = components.reporting.daily_calls
    assert actual_start.date() == date(2026, 6, 1)
    assert actual_end.date() == date(2026, 6, 2)


# ── scheduling predicates: weekly ────────────────────────────────────────


@pytest.mark.asyncio
async def test_weekly_report_only_fires_on_configured_weekday():
    components = _components(weekly_report_day="sunday", daily_report_time="00:00")
    runner = LiveRunner(components)

    # 2026-06-01 is a Monday -- must NOT fire
    await runner.run_one_cycle(now=datetime(2026, 6, 1, 0, 0, tzinfo=UTC))
    assert components.reporting.weekly_calls == []

    # 2026-06-07 is a Sunday -- must fire
    await runner.run_one_cycle(now=datetime(2026, 6, 7, 0, 0, tzinfo=UTC))
    assert len(components.reporting.weekly_calls) == 1


@pytest.mark.asyncio
async def test_weekly_report_does_not_fire_twice_same_day():
    components = _components(weekly_report_day="sunday", daily_report_time="00:00")
    runner = LiveRunner(components)

    await runner.run_one_cycle(now=datetime(2026, 6, 7, 1, 0, tzinfo=UTC))
    await runner.run_one_cycle(now=datetime(2026, 6, 7, 2, 0, tzinfo=UTC))

    assert len(components.reporting.weekly_calls) == 1


# ── scheduling predicates: monthly ───────────────────────────────────────


@pytest.mark.asyncio
async def test_monthly_report_only_fires_on_configured_day_of_month():
    components = _components(monthly_report_day=1, daily_report_time="00:00")
    runner = LiveRunner(components)

    await runner.run_one_cycle(now=datetime(2026, 6, 15, 0, 0, tzinfo=UTC))
    assert components.reporting.monthly_calls == []

    await runner.run_one_cycle(now=datetime(2026, 7, 1, 0, 0, tzinfo=UTC))
    assert len(components.reporting.monthly_calls) == 1


def test_previous_month_range_handles_january_rollover_to_prior_year():
    start, end = _previous_month_range(date(2026, 1, 15))
    assert start == date(2025, 12, 1)
    assert end == date(2026, 1, 1)


def test_previous_month_range_normal_case():
    start, end = _previous_month_range(date(2026, 7, 1))
    assert start == date(2026, 6, 1)
    assert end == date(2026, 7, 1)


# ── scheduling predicates: backup ────────────────────────────────────────


@pytest.mark.asyncio
async def test_backup_fires_on_first_cycle_when_never_backed_up():
    components = _components()
    runner = LiveRunner(components)

    await runner.run_one_cycle(now=datetime(2026, 6, 1, 10, 0, tzinfo=UTC))

    assert len(components.production.backup_calls) == 1


@pytest.mark.asyncio
async def test_backup_does_not_fire_before_interval_elapsed():
    components = _components(backup_interval_hours=24)
    runner = LiveRunner(components)

    await runner.run_one_cycle(now=datetime(2026, 6, 1, 10, 0, tzinfo=UTC))
    await runner.run_one_cycle(now=datetime(2026, 6, 1, 20, 0, tzinfo=UTC))  # only 10h later

    assert len(components.production.backup_calls) == 1


@pytest.mark.asyncio
async def test_backup_fires_again_after_interval_elapsed():
    components = _components(backup_interval_hours=24)
    runner = LiveRunner(components)

    await runner.run_one_cycle(now=datetime(2026, 6, 1, 10, 0, tzinfo=UTC))
    await runner.run_one_cycle(now=datetime(2026, 6, 2, 11, 0, tzinfo=UTC))  # 25h later

    assert len(components.production.backup_calls) == 2


@pytest.mark.asyncio
async def test_backup_missing_database_file_is_logged_not_raised():
    components = _components(production=_FakeProduction(backup_raises=True))
    runner = LiveRunner(components)

    await runner.run_one_cycle(now=datetime(2026, 6, 1, 10, 0, tzinfo=UTC))  # must not raise


# ── lifecycle ──────────────────────────────────────────────────────────


def test_request_shutdown_sets_flag():
    runner = LiveRunner(_components())
    assert runner._shutdown_requested is False
    runner.request_shutdown()
    assert runner._shutdown_requested is True


@pytest.mark.asyncio
async def test_run_forever_stops_after_shutdown_requested(monkeypatch):
    components = _components()
    runner = LiveRunner(components)

    sleep_calls = []

    async def fake_sleep(seconds):
        sleep_calls.append(seconds)
        if len(sleep_calls) >= 2:
            runner.request_shutdown()

    monkeypatch.setattr("execution_modes.live.asyncio.sleep", fake_sleep)

    await runner.run_forever()

    # cycle 1 -> sleep (no shutdown yet) -> cycle 2 -> sleep (shutdown requested here)
    # -> loop condition re-checked at the top, exits before a 3rd cycle starts
    assert components.scanner.call_count == 2
    assert sleep_calls == [30, 30]


@pytest.mark.asyncio
async def test_run_forever_never_starts_a_cycle_if_shutdown_requested_first():
    components = _components()
    runner = LiveRunner(components)
    runner.request_shutdown()

    await runner.run_forever()

    assert components.scanner.call_count == 0
