"""
application/main.py

Module 22 entry point -- SRS: main application entry point, startup
sequence, RunMode selection, graceful shutdown.

`run_platform(components)` takes already-built `PlatformComponents`
rather than calling `build_platform()` itself, specifically so tests can
exercise RunMode branching with fake components instead of needing a
real database file. `async_main()` is the thin, effectively-untested
glue that actually calls `build_platform()` for a real run.
"""

from __future__ import annotations

import asyncio
import signal
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from application.composition_root import PlatformComponents, build_platform
from config.schema import RunMode
from engines.telegram_commands import CommandPoller
from execution_modes.live import LiveRunner
from system.logging_setup import get_logger

_logger = get_logger("system")


async def _run_live_or_paper(
    components: PlatformComponents, *, runner: Optional[LiveRunner] = None, poller: Optional[CommandPoller] = None
) -> None:
    """
    SRS startup sequence: open the async clients, register SIGTERM/SIGINT
    handlers for graceful shutdown, run the loop. The `async with` here
    is also the shutdown sequence's other half -- whether the loop ends
    via a signal or via an unhandled exception with `auto_restart=False`
    propagating out of it, both clients are closed on the way out.

    `runner`/`poller` are injectable (default to real instances) so
    tests can verify this function's sequencing -- clients entered,
    signal handlers registered, both loops awaited concurrently --
    without an actual forever-loop or real network clients.

    The command poller (/scan /status /rapor /help -- engines
    /telegram_commands.py) runs concurrently with the scan loop, not
    serialized after it: a person checking /status should not have to
    wait out the rest of the current scan cycle for a reply. Wired into
    `runner` here (not inside `LiveRunner.__init__`'s default) so the
    scan loop's own tests never need a real/fake Telegram client just to
    construct a `LiveRunner`.

    Sends exactly one "🚀 Bot Started" Telegram message once both clients
    are open (so its own Exchange/Telegram/Database status checks are
    meaningful), and one "🛑 Bot Stopped" message after both loops
    return -- deliberately only on a GRACEFUL return, not when one
    raises (an unhandled exception already produces its own CRITICAL
    `notify_error()` alert via `handle_error()` inside the scan loop
    before propagating; a calm "stopped" message on top of that would be
    redundant, not additionally informative).
    """
    poller = poller or CommandPoller(components)
    runner = runner or LiveRunner(components, command_poller=poller)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, runner.request_shutdown)
        loop.add_signal_handler(sig, poller.request_shutdown)

    async with (
        components.binance_client, components.telegram_client,
        components.macro_client, components.bingx_client,
    ):
        started_at = datetime.now(timezone.utc)
        await _send_startup_notification(components)
        await asyncio.gather(runner.run_forever(), poller.run_forever())
        uptime_seconds = (datetime.now(timezone.utc) - started_at).total_seconds()
        await components.notifications.notify_bot_stopped(
            version=components.config.general.version,
            run_mode=components.config.general.run_mode.value,
            uptime_seconds=uptime_seconds,
        )


async def _send_startup_notification(components: PlatformComponents) -> None:
    """
    Gathers the Exchange/Telegram/Database status doc the "🚀 Bot
    Started" message shows. Best-effort: a health-check failure here
    (rather than a genuinely down service) should not prevent the
    startup message from going out at all, so each probe is guarded
    individually and treated as "not ok" rather than left to raise.
    """
    exchange_ok = await _probe(components.binance_client.check_health)
    telegram_ok = await _probe(components.telegram_client.check_health)
    database_ok = Path(components.config.database.path).exists()
    execution_exchange_ok = (
        await _probe(components.bingx_client.check_health)
        if components.trade_execution.trading_is_enabled
        else None
    )
    await components.notifications.notify_bot_started(
        version=components.config.general.version,
        run_mode=components.config.general.run_mode.value,
        exchange_ok=exchange_ok,
        telegram_ok=telegram_ok,
        database_ok=database_ok,
        execution_exchange_ok=execution_exchange_ok,
    )


async def _probe(check: Callable[[], "asyncio.Future[bool]"]) -> bool:
    try:
        return bool(await check())
    except Exception as exc:  # a startup health probe must never abort the boot sequence
        _logger.warning("Startup health probe failed: %s", exc)
        return False


def _explain_backtest_mode() -> None:
    """
    RunMode.BACKTEST does not start a long-running loop here. A backtest
    is a finite, parameterized replay of one signal against a historical
    candle range (`execution_modes.backtest.BacktestRunner`), not a
    process that runs forever -- and no SRS-specified CLI/scheduling
    surface for invoking one through this entry point exists anywhere
    this module can see. Documented rather than fabricated: inventing
    argument parsing for a date range/symbol here would be guessing at
    an interface no spec describes.
    """
    _logger.info(
        "run_mode=backtest: this entry point does not run a long-running loop for "
        "backtests. Use execution_modes.backtest.BacktestRunner directly (see its "
        "module docstring) for a finite, parameterized replay."
    )


async def run_platform(
    components: PlatformComponents, *, runner: Optional[LiveRunner] = None, poller: Optional[CommandPoller] = None
) -> None:
    """Branches on RunMode. Separated from async_main() so tests can pass fake components."""
    config = components.config
    _logger.info(
        "Starting %s v%s (run_mode=%s, strategy_profile=%s)",
        config.general.bot_name, config.general.version,
        config.general.run_mode.value, config.metadata.strategy_profile.value,
    )

    if config.general.run_mode in (RunMode.LIVE, RunMode.PAPER):
        if not components.telegram_client.is_configured:
            _logger.warning(
                "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set -- the platform will keep "
                "scanning and generating signals, but no Telegram message will be sent "
                "until both are configured. Set them as environment variables and redeploy."
            )
        await _run_live_or_paper(components, runner=runner, poller=poller)
    elif config.general.run_mode == RunMode.BACKTEST:
        _explain_backtest_mode()
    else:
        raise ValueError(f"Unknown run_mode: {config.general.run_mode!r}")


async def async_main() -> None:
    components = build_platform()
    await run_platform(components)


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
