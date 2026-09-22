"""
Unit tests for application/main.py (Module 22).

`run_platform()` tests use a minimal fake `PlatformComponents`-like
object (just enough attributes for the code under test to read) so
RunMode branching is tested without a real database or network client.
`_run_live_or_paper()`'s sequencing is tested with fake async-context-
manager clients and injected fake `LiveRunner`/`CommandPoller`
instances, so it runs instantly instead of forever and touches no real
network.

Run with:
    pytest tests/unit/test_application_main.py -v
"""

import logging
from types import SimpleNamespace

import pytest

from application.main import _run_live_or_paper, _send_startup_notification, run_platform
from config.schema import GeneralConfig, PlatformConfig, RunMode


# ── fakes ──────────────────────────────────────────────────────────────


class _FakeAsyncClient:
    def __init__(self, *, is_configured: bool = True, healthy: bool = True, health_check_raises: bool = False):
        self.entered = False
        self.exited = False
        self.is_configured = is_configured
        self._healthy = healthy
        self._health_check_raises = health_check_raises
        self.check_health_called = False

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, *exc_info):
        self.exited = True
        return False

    async def check_health(self) -> bool:
        self.check_health_called = True
        if self._health_check_raises:
            raise RuntimeError("simulated health-check failure")
        return self._healthy


class _FakeLiveRunner:
    def __init__(self):
        self.run_forever_called = False
        self.shutdown_requested = False

    async def run_forever(self):
        self.run_forever_called = True

    def request_shutdown(self):
        self.shutdown_requested = True


class _FakePoller:
    def __init__(self):
        self.run_forever_called = False
        self.shutdown_requested = False

    async def run_forever(self):
        self.run_forever_called = True

    def request_shutdown(self):
        self.shutdown_requested = True


class _FakeNotifications:
    def __init__(self):
        self.bot_started_calls: list[dict] = []
        self.bot_stopped_calls: list[dict] = []

    async def notify_bot_started(self, **kwargs) -> bool:
        self.bot_started_calls.append(kwargs)
        return True

    async def notify_bot_stopped(self, **kwargs) -> bool:
        self.bot_stopped_calls.append(kwargs)
        return True


def _components(run_mode: RunMode, *, telegram_configured: bool = True, trading_enabled: bool = False) -> SimpleNamespace:
    config = PlatformConfig(general=GeneralConfig(run_mode=run_mode))
    return SimpleNamespace(
        config=config,
        binance_client=_FakeAsyncClient(),
        telegram_client=_FakeAsyncClient(is_configured=telegram_configured),
        macro_client=_FakeAsyncClient(),
        bingx_client=_FakeAsyncClient(),
        trade_execution=SimpleNamespace(trading_is_enabled=trading_enabled),
        notifications=_FakeNotifications(),
    )


# ── run_platform: RunMode branching ─────────────────────────────────────


@pytest.mark.asyncio
async def test_live_mode_runs_the_live_runner():
    components = _components(RunMode.LIVE)
    runner = _FakeLiveRunner()

    await run_platform(components, runner=runner, poller=_FakePoller())

    assert runner.run_forever_called is True
    assert components.binance_client.entered is True
    assert components.telegram_client.entered is True


@pytest.mark.asyncio
async def test_paper_mode_runs_the_same_live_runner_as_live_mode():
    """PAPER and LIVE are documented as behaving identically at this layer -- see execution_modes/live.py's module docstring."""
    components = _components(RunMode.PAPER)
    runner = _FakeLiveRunner()

    await run_platform(components, runner=runner, poller=_FakePoller())

    assert runner.run_forever_called is True


@pytest.mark.asyncio
async def test_warns_when_telegram_not_configured(caplog):
    components = _components(RunMode.LIVE, telegram_configured=False)
    runner = _FakeLiveRunner()

    with caplog.at_level(logging.WARNING):
        await run_platform(components, runner=runner, poller=_FakePoller())

    assert any("TELEGRAM_BOT_TOKEN" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_no_warning_when_telegram_is_configured(caplog):
    components = _components(RunMode.LIVE, telegram_configured=True)
    runner = _FakeLiveRunner()

    with caplog.at_level(logging.WARNING):
        await run_platform(components, runner=runner, poller=_FakePoller())

    assert not any("TELEGRAM_BOT_TOKEN" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_backtest_mode_does_not_run_the_live_runner():
    components = _components(RunMode.BACKTEST)
    runner = _FakeLiveRunner()

    await run_platform(components, runner=runner, poller=_FakePoller())

    assert runner.run_forever_called is False
    assert components.binance_client.entered is False  # never even opens the network clients


@pytest.mark.asyncio
async def test_unknown_run_mode_raises():
    """Defensive completeness check: a run_mode outside the three known values must not silently no-op."""
    fake_config = SimpleNamespace(
        general=SimpleNamespace(
            run_mode=SimpleNamespace(value="not_a_real_mode"), bot_name="x", version="1",
        ),
        metadata=SimpleNamespace(strategy_profile=SimpleNamespace(value="x")),
    )
    components = SimpleNamespace(
        config=fake_config, binance_client=_FakeAsyncClient(), telegram_client=_FakeAsyncClient(), macro_client=_FakeAsyncClient(),
    )

    with pytest.raises(ValueError):
        await run_platform(components, runner=_FakeLiveRunner(), poller=_FakePoller())


# ── _run_live_or_paper: sequencing ──────────────────────────────────────


@pytest.mark.asyncio
async def test_run_live_or_paper_opens_all_four_clients_before_running():
    components = _components(RunMode.LIVE)
    runner = _FakeLiveRunner()

    await _run_live_or_paper(components, runner=runner, poller=_FakePoller())

    assert components.binance_client.entered is True
    assert components.telegram_client.entered is True
    assert components.macro_client.entered is True
    assert components.bingx_client.entered is True


@pytest.mark.asyncio
async def test_run_live_or_paper_closes_all_four_clients_after_running():
    components = _components(RunMode.LIVE)
    runner = _FakeLiveRunner()

    await _run_live_or_paper(components, runner=runner, poller=_FakePoller())

    assert components.binance_client.exited is True
    assert components.telegram_client.exited is True
    assert components.macro_client.exited is True
    assert components.bingx_client.exited is True


@pytest.mark.asyncio
async def test_run_live_or_paper_closes_clients_even_if_runner_raises():
    """Graceful shutdown: an exception from the loop must not leave HTTP sessions open."""
    components = _components(RunMode.LIVE)

    class _RaisingRunner(_FakeLiveRunner):
        async def run_forever(self):
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await _run_live_or_paper(components, runner=_RaisingRunner(), poller=_FakePoller())

    assert components.binance_client.exited is True
    assert components.telegram_client.exited is True
    assert components.macro_client.exited is True
    assert components.bingx_client.exited is True


@pytest.mark.asyncio
async def test_run_live_or_paper_registers_signal_handlers_without_error():
    """Sanity check that signal handler registration (SIGTERM/SIGINT -> request_shutdown) doesn't error inside a real running event loop."""
    components = _components(RunMode.LIVE)
    runner = _FakeLiveRunner()

    await _run_live_or_paper(components, runner=runner, poller=_FakePoller())  # must not raise

    assert runner.run_forever_called is True


# ── _run_live_or_paper: bot started / bot stopped notifications ─────────


@pytest.mark.asyncio
async def test_run_live_or_paper_sends_exactly_one_bot_started_notification():
    components = _components(RunMode.LIVE)
    runner = _FakeLiveRunner()

    await _run_live_or_paper(components, runner=runner, poller=_FakePoller())

    assert len(components.notifications.bot_started_calls) == 1
    call = components.notifications.bot_started_calls[0]
    assert call["run_mode"] == "live"
    assert call["exchange_ok"] is True
    assert call["telegram_ok"] is True


@pytest.mark.asyncio
async def test_run_live_or_paper_sends_bot_started_before_run_forever():
    """The startup message should reflect a bot that is about to run, not one that already finished."""
    order: list[str] = []

    class _OrderTrackingNotifications(_FakeNotifications):
        async def notify_bot_started(self, **kwargs):
            order.append("started")
            return await super().notify_bot_started(**kwargs)

    class _OrderTrackingRunner(_FakeLiveRunner):
        async def run_forever(self):
            order.append("run_forever")
            await super().run_forever()

    components = _components(RunMode.LIVE)
    components.notifications = _OrderTrackingNotifications()

    await _run_live_or_paper(components, runner=_OrderTrackingRunner(), poller=_FakePoller())

    assert order == ["started", "run_forever"]


@pytest.mark.asyncio
async def test_run_live_or_paper_sends_bot_stopped_after_graceful_shutdown():
    components = _components(RunMode.LIVE)
    runner = _FakeLiveRunner()

    await _run_live_or_paper(components, runner=runner, poller=_FakePoller())

    assert len(components.notifications.bot_stopped_calls) == 1
    assert components.notifications.bot_stopped_calls[0]["run_mode"] == "live"


@pytest.mark.asyncio
async def test_run_live_or_paper_does_not_send_bot_stopped_when_runner_raises():
    """A crash gets its own CRITICAL notify_error() alert elsewhere -- not a redundant calm 'stopped' message."""
    components = _components(RunMode.LIVE)

    class _RaisingRunner(_FakeLiveRunner):
        async def run_forever(self):
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await _run_live_or_paper(components, runner=_RaisingRunner(), poller=_FakePoller())

    assert components.notifications.bot_stopped_calls == []
    # ...but the startup message still went out before the crash.
    assert len(components.notifications.bot_started_calls) == 1


@pytest.mark.asyncio
async def test_send_startup_notification_treats_a_failed_health_probe_as_not_ok():
    """A raising check_health() must not abort startup -- it should just be reported as 'not ok'."""
    components = _components(RunMode.LIVE)
    components.binance_client = _FakeAsyncClient(health_check_raises=True)

    await _send_startup_notification(components)

    assert components.notifications.bot_started_calls[0]["exchange_ok"] is False


@pytest.mark.asyncio
async def test_send_startup_notification_reports_an_unhealthy_service():
    components = _components(RunMode.LIVE)
    components.telegram_client = _FakeAsyncClient(healthy=False)

    await _send_startup_notification(components)

    assert components.notifications.bot_started_calls[0]["telegram_ok"] is False


@pytest.mark.asyncio
async def test_send_startup_notification_omits_execution_exchange_check_when_trading_disabled():
    """Signal-only PAPER mode (or LIVE with trading_enabled still False): BingX connectivity is irrelevant, so it's never even probed."""
    components = _components(RunMode.PAPER, trading_enabled=False)

    await _send_startup_notification(components)

    assert components.bingx_client.check_health_called is False
    assert components.notifications.bot_started_calls[0]["execution_exchange_ok"] is None


@pytest.mark.asyncio
async def test_send_startup_notification_probes_execution_exchange_when_trading_enabled():
    components = _components(RunMode.LIVE, trading_enabled=True)

    await _send_startup_notification(components)

    assert components.bingx_client.check_health_called is True
    assert components.notifications.bot_started_calls[0]["execution_exchange_ok"] is True


@pytest.mark.asyncio
async def test_send_startup_notification_reports_a_failed_execution_exchange_probe():
    components = _components(RunMode.LIVE, trading_enabled=True)
    components.bingx_client = _FakeAsyncClient(health_check_raises=True)

    await _send_startup_notification(components)

    assert components.notifications.bot_started_calls[0]["execution_exchange_ok"] is False
