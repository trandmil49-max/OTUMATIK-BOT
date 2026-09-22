"""
execution_modes/live.py

Live/Paper Runner (Module 22) -- the long-running scan loop that both
`RunMode.LIVE` and `RunMode.PAPER` share.

Autonomous trading pivot: RunMode.LIVE (with `config.api.trading_enabled`
ALSO true -- see `TradeExecutionEngine.trading_is_enabled`) now places
real Binance orders for new signals via `TradeExecutionEngine`, and
cancels leftover protective orders when a trade closes -- see
`_dispatch_notifications()`'s own docstring comments for exactly how
that's sequenced against the existing Telegram notifications. Every
OTHER RunMode (PAPER, or LIVE with trading still switched off) behaves
exactly as before this pivot: signal-only, notify immediately, no order
ever placed -- both PAPER and un-flagged LIVE still run the exact same
`run_one_cycle()` against real market data, differing only in whether
`trade_execution.trading_is_enabled` happens to be true.

Testability: `run_one_cycle()` is one complete iteration (scan, notify,
scheduled tasks) and is what unit tests exercise directly.
`run_forever()` is a thin `while` loop around it plus a sleep -- the one
piece of this module genuinely hard to unit test in the normal sense,
so its own tests prove the loop mechanics (calls the cycle, respects
shutdown, sleeps between cycles) with a stubbed cycle rather than
re-testing cycle logic that's already covered directly.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, time as time_, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from application.composition_root import PlatformComponents
from engines.telegram_commands import CommandPoller
from system.error_handler import handle_error
from system.exceptions import PlatformError, Severity
from system.logging_setup import get_logger

_logger = get_logger("trading")

_WEEKDAY_NAMES = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def _parse_hhmm(value: str) -> time_:
    """`ReportConfig.daily_report_time` is validated as HH:MM at the config layer; parsing it here trusts that validation rather than re-checking it."""
    hour, minute = value.split(":")
    return time_(int(hour), int(minute))


def _previous_month_range(today: date) -> tuple[date, date]:
    """Standard calendar arithmetic (stdlib only, no invented business rule): the full previous calendar month as a half-open [start, end) range of dates."""
    end = today.replace(day=1)
    last_day_of_prev_month = end - timedelta(days=1)
    start = last_day_of_prev_month.replace(day=1)
    return start, end


class LiveRunner:
    """
    Owns the loop's mutable scheduling state (last-run trackers) so that
    `_is_due_*` predicates stay pure functions of an explicit `now` --
    easy to test without patching global time, matching
    `infrastructure/watchdog.py` and `infrastructure/rate_limiter.py`'s
    own injectable-time conventions.
    """

    def __init__(self, components: PlatformComponents, *, command_poller: Optional[CommandPoller] = None) -> None:
        self._c = components
        self._config = components.config
        self._timezone = ZoneInfo(self._config.general.timezone)
        self._command_poller = command_poller
        self._shutdown_requested = False

        self._last_daily_report_date: Optional[date] = None
        self._last_weekly_report_date: Optional[date] = None
        self._last_monthly_report_date: Optional[date] = None
        self._last_backup_at: Optional[datetime] = None

    # ─────────────────────────────────────────────────────────────────
    # LIFECYCLE
    # ─────────────────────────────────────────────────────────────────

    def request_shutdown(self) -> None:
        """SRS: graceful shutdown. Called from a signal handler (SIGTERM/SIGINT) -- see application/main.py."""
        _logger.info("Shutdown requested -- will stop after the current cycle completes")
        self._shutdown_requested = True

    async def run_forever(self) -> None:
        """The actual long-running loop. Thin by design -- see module docstring."""
        interval = self._config.scanner.fast_scan_interval_seconds
        _logger.info(
            "Live runner starting (run_mode=%s, cycle interval=%ds)",
            self._config.general.run_mode.value, interval,
        )
        while not self._shutdown_requested:
            await self.run_one_cycle()
            if self._shutdown_requested:
                break
            await asyncio.sleep(interval)
        _logger.info("Live runner stopped")

    # ─────────────────────────────────────────────────────────────────
    # ONE CYCLE
    # ─────────────────────────────────────────────────────────────────

    async def run_one_cycle(self, *, now: Optional[datetime] = None) -> None:
        """
        One full iteration. `run_scan_cycle()` already isolates a single
        symbol's failure internally (see its own docstring) -- an
        exception reaching the try/except here means something broke
        below that isolation (e.g. a database or network failure that
        affects the whole cycle), and is handled per
        `config.general.auto_restart` rather than left to crash the
        process outright or silently retried with an undocumented
        policy.
        """
        try:
            result = await self._c.scanner.run_scan_cycle()
            if self._command_poller is not None:
                self._command_poller.record_scan_summary(result, at=now)
            self._c.production.watchdog.record_heartbeat()
            await self._dispatch_notifications(result)
            await self._run_scheduled_tasks(now=now)
        except Exception as exc:
            error = PlatformError("Unhandled exception in scan cycle", severity=Severity.CRITICAL, cause=exc)
            handle_error(error, category="system")
            if not self._config.general.auto_restart:
                raise
            _logger.warning("general.auto_restart=True: continuing after unhandled cycle exception: %s", exc)

    async def _dispatch_notifications(self, result: Any) -> None:
        for new_signal in result.signals_generated:
            # Autonomous trading pivot: when both safety switches are on
            # (trade_execution.trading_is_enabled -- see that engine's own
            # docstring), try to open a REAL position first, and only tell
            # the person a signal fired if a Trade actually got opened for
            # it -- an empty "trade opened" message for a signal that was
            # silently skipped (no balance, size rounds to zero, symbol
            # missing) would be actively misleading. PAPER mode (or LIVE
            # with trading still switched off) keeps the original
            # signal-only behavior unchanged: notify immediately, exactly
            # like before this pivot.
            if self._c.trade_execution.trading_is_enabled:
                trade = await self._c.trade_execution.open_position(new_signal)
                if trade is not None:
                    await self._c.notifications.notify_new_signal(new_signal)
            else:
                await self._c.notifications.notify_new_signal(new_signal)

        monitor_result = result.monitor_result
        if monitor_result is None:
            return

        for signal_id in monitor_result.expired_signal_ids:
            signal = self._c.signal_repository.get_by_id(signal_id)
            if signal is not None:
                await self._c.notifications.notify_signal_expired(signal)

        # Single-TP model: TP1_HIT and STOP_LOSS are both simultaneously a
        # "level hit" AND a trade closure -- there is no intermediate,
        # non-terminal TP1 state anymore, so notify_trade_closed() alone
        # (one message per closed trade) is correct for both. It already
        # carries a status-specific icon/label (see
        # engines/telegram_notifications.py's _CLOSED_STATUS_ICON/
        # _CLOSED_STATUS_LABEL_TR) that says which outcome closed the
        # trade, so a combined loop loses no information.
        #
        # close_position() runs AFTER the notification, not before: the
        # trade already closed the instant Binance's own STOP_MARKET/
        # TAKE_PROFIT_MARKET order filled (PositionMonitorEngine is only
        # observing that after the fact here) -- all that is left to do is
        # cancel whichever protective order did NOT fire, which is
        # cleanup, not something the person needs to wait on before being
        # told the trade closed. It is also a safe no-op call in PAPER
        # mode / with trading switched off (trading_is_enabled gates it
        # internally), so it is never skipped or conditioned here.
        for trade_id in (
            *monitor_result.tp1_hit_trade_ids,
            *monitor_result.stop_loss_trade_ids,
            *monitor_result.trailing_stop_exit_trade_ids,
        ):
            trade = self._c.trade_repository.get_by_id(trade_id)
            if trade is not None:
                await self._c.notifications.notify_trade_closed(trade)
                await self._c.trade_execution.close_position(trade)

    # ─────────────────────────────────────────────────────────────────
    # SCHEDULED TASKS
    # ─────────────────────────────────────────────────────────────────

    async def _run_scheduled_tasks(self, *, now: Optional[datetime] = None) -> None:
        now = now or datetime.now(timezone.utc)
        local_now = now.astimezone(self._timezone)

        if self._is_due_daily(local_now):
            await self._run_daily_report(local_now)
        if self._is_due_weekly(local_now):
            await self._run_weekly_report(local_now)
        if self._is_due_monthly(local_now):
            await self._run_monthly_report(local_now)
        if self._is_due_backup(now):
            self._run_backup(now)
        # Health check runs every cycle rather than on its own schedule:
        # no `health_check_interval_seconds`-style config field exists
        # anywhere in ReportConfig/PerformanceConfig, and a lightweight
        # check (one lightweight ping each to Binance/Telegram, see
        # engines/production.py) every fast_scan_interval_seconds is not
        # an unreasonable default given no configured alternative.
        await self._c.production.run_health_check()

    def _is_due_daily(self, local_now: datetime) -> bool:
        if self._last_daily_report_date == local_now.date():
            return False
        return local_now.time() >= _parse_hhmm(self._config.reports.daily_report_time)

    def _is_due_weekly(self, local_now: datetime) -> bool:
        if self._last_weekly_report_date == local_now.date():
            return False
        target_weekday = _WEEKDAY_NAMES.index(self._config.reports.weekly_report_day.lower())
        if local_now.weekday() != target_weekday:
            return False
        # No separate weekly_report_time field exists in ReportConfig --
        # reusing daily_report_time as the time-of-day is the minimal,
        # documented interpretation of that gap rather than inventing an
        # unrelated one.
        return local_now.time() >= _parse_hhmm(self._config.reports.daily_report_time)

    def _is_due_monthly(self, local_now: datetime) -> bool:
        if self._last_monthly_report_date == local_now.date():
            return False
        if local_now.day != self._config.reports.monthly_report_day:
            return False
        return local_now.time() >= _parse_hhmm(self._config.reports.daily_report_time)

    def _is_due_backup(self, now: datetime) -> bool:
        if self._last_backup_at is None:
            return True
        elapsed_hours = (now - self._last_backup_at).total_seconds() / 3600
        return elapsed_hours >= self._config.database.backup_interval_hours

    async def _run_daily_report(self, local_now: datetime) -> None:
        period_end = datetime.combine(local_now.date(), time_.min, tzinfo=self._timezone)
        period_start = period_end - timedelta(days=1)
        report = await self._c.reporting.generate_daily_report(period_start=period_start, period_end=period_end)
        await self._c.notifications.notify_daily_report(report)
        self._last_daily_report_date = local_now.date()
        _logger.info("Daily report generated and sent for %s", period_start.date())

    async def _run_weekly_report(self, local_now: datetime) -> None:
        period_end = datetime.combine(local_now.date(), time_.min, tzinfo=self._timezone)
        period_start = period_end - timedelta(days=7)
        report = await self._c.reporting.generate_weekly_report(period_start=period_start, period_end=period_end)
        await self._c.notifications.notify_weekly_report(report)
        self._last_weekly_report_date = local_now.date()
        _logger.info("Weekly report generated and sent for %s..%s", period_start.date(), period_end.date())

    async def _run_monthly_report(self, local_now: datetime) -> None:
        start_date, end_date = _previous_month_range(local_now.date())
        period_start = datetime.combine(start_date, time_.min, tzinfo=self._timezone)
        period_end = datetime.combine(end_date, time_.min, tzinfo=self._timezone)
        report = await self._c.reporting.generate_monthly_report(period_start=period_start, period_end=period_end)
        await self._c.notifications.notify_monthly_report(report)
        self._last_monthly_report_date = local_now.date()
        _logger.info("Monthly report generated and sent for %s..%s", start_date, end_date)

    def _run_backup(self, now: datetime) -> None:
        try:
            backup_path = self._c.production.backup_database(destination_dir=self._backup_directory())
            _logger.info("Database backup created: %s", backup_path)
        except FileNotFoundError:
            _logger.warning("Database backup skipped: source database file does not exist yet")
        self._last_backup_at = now

    def _backup_directory(self) -> Path:
        """
        No `backup_directory`-style config field exists anywhere in
        `DatabaseConfig` (only `path`, `backup_interval_hours`,
        `retention_policy_days`) -- placing backups in a `backups/`
        subdirectory next to the live database file is this module's own
        reasonable, documented default given that gap, not something the
        SRS specifies. Easy to change if a real requirement surfaces.
        """
        return Path(self._config.database.path).parent / "backups"
