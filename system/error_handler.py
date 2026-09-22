"""
system/error_handler.py

Single choke point for handling PlatformError instances (SRS Part 18:
"Every important error must be logged. Every critical error must notify
the user. Silent failures are unacceptable." / SRS Rule 11: "NEVER HIDE
ERRORS").

Every module should route real failures through `handle_error()` instead
of ad-hoc `logging.error(...)` calls, so that:
  * logging category + severity are always handled the same way, and
  * WARNING/ERROR/CRITICAL/FATAL errors always fan out to registered
    alert callbacks (e.g. the Telegram Notification Engine's
    notify_warning()/notify_error(), or a repository that persists
    CRITICAL/FATAL as an ErrorEvent), without this module needing to
    know Telegram -- or any other infrastructure -- exists. Each
    callback decides for itself which of those four severities it acts
    on; INFO never fans out (too low-value for a push notification or a
    persisted row).

This module deliberately imports nothing from `infrastructure/`. The
dependency points the other way: infrastructure modules call
`register_alert_callback()` to subscribe; they are never imported here.
That is what lets Module 2 be built and fully tested before Module 15
(Telegram) exists at all.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from system.exceptions import PlatformError, Severity
from system.logging_setup import get_logger

AlertCallback = Callable[[PlatformError], None]

_alert_callbacks: list[AlertCallback] = []

# Every severity except INFO reaches registered callbacks -- INFO is too
# low-value for a push notification or a persisted error_events row (SRS
# Part 18: "Classify Info, Warning, Error, Critical, Fatal"). CRITICAL/FATAL
# vs. WARNING/ERROR is then each individual callback's own decision, not
# this module's: engines/telegram_notifications.py's as_alert_callback()
# routes CRITICAL/FATAL to notify_error() (always sends, bypassing
# notification_level) and WARNING/ERROR to notify_warning() (respects
# notification_level, so a "quiet" operator can still silence routine
# warnings). A callback that only ever wants CRITICAL/FATAL -- e.g. the
# composition root's error_events-persistence callback -- filters for that
# itself; see register_alert_callback()'s docstring.
_ALERTABLE_SEVERITIES: frozenset[Severity] = frozenset(
    {Severity.WARNING, Severity.ERROR, Severity.CRITICAL, Severity.FATAL}
)

_SEVERITY_TO_LOG_LEVEL: dict[Severity, int] = {
    Severity.INFO: logging.INFO,
    Severity.WARNING: logging.WARNING,
    Severity.ERROR: logging.ERROR,
    Severity.CRITICAL: logging.CRITICAL,
    Severity.FATAL: logging.CRITICAL,
}


def register_alert_callback(callback: AlertCallback) -> None:
    """
    Subscribe `callback` to be invoked for every WARNING/ERROR/CRITICAL
    /FATAL error handled via `handle_error()` (INFO never fans out --
    see `_ALERTABLE_SEVERITIES`). `callback` decides for itself which of
    those severities it actually acts on; two different callbacks can
    each care about a different subset, e.g.:

        from system.error_handler import register_alert_callback
        register_alert_callback(notification_engine.as_alert_callback())  # CRITICAL/FATAL -> notify_error(), WARNING/ERROR -> notify_warning()
        register_alert_callback(error_event_persistence_callback)          # CRITICAL/FATAL only, no-ops otherwise

    A callback that itself raises is caught and logged under the "error"
    category so one broken alert channel can never prevent the others
    from running, and can never crash the caller of `handle_error()`.
    """
    _alert_callbacks.append(callback)


def clear_alert_callbacks_for_tests() -> None:
    """Test-only helper to reset global callback state between tests."""
    _alert_callbacks.clear()


def handle_error(
    error: PlatformError,
    *,
    category: str = "error",
    severity: Optional[Severity] = None,
    context: Optional[dict[str, Any]] = None,
) -> None:
    """
    Log `error` with full traceback and fan out to registered alert
    callbacks if the effective severity is WARNING, ERROR, CRITICAL, or
    FATAL (everything except INFO -- see `_ALERTABLE_SEVERITIES`).

    Args:
        error: The PlatformError instance to handle.
        category: Which of the 7 logging categories to log under.
            Defaults to "error"; callers handling e.g. a Binance failure
            typically pass category="api".
        severity: Overrides `error.severity` for this specific handling
            call only -- the original exception instance's `.severity`
            is never mutated. Useful when the call site has more context
            than the exception did when it was raised (for example,
            escalating a normally-WARNING BinanceConnectionError to
            CRITICAL after N consecutive failures). KNOWN LIMITATION (no
            current call site uses this parameter, so nothing observable
            changes today): the override is used for the LOG level and
            for deciding whether to fan out at all, but callbacks still
            receive the original `error` object, so a callback that reads
            `error.severity` to decide its own routing (e.g.
            TelegramNotificationEngine.as_alert_callback()) sees the
            un-escalated value. If a future call site actually escalates
            severity and needs that escalation to also change callback
            routing, `_fan_out_alerts` will need to pass the effective
            severity through explicitly rather than relying on `error.severity`.
        context: Additional context merged with `error.context` for this
            specific handling call, without mutating the original
            exception's stored context.
    """
    effective_severity = severity or error.severity
    logger = get_logger(category)
    merged_context = {**error.context, **(context or {})}

    log_message = f"{error.message} | severity={effective_severity.value}"
    if merged_context:
        log_message = f"{log_message} | context={merged_context}"

    exc_info = (
        (type(error.cause), error.cause, error.cause.__traceback__)
        if error.cause is not None
        else (type(error), error, error.__traceback__)
    )

    logger.log(_SEVERITY_TO_LOG_LEVEL[effective_severity], log_message, exc_info=exc_info)

    if effective_severity in _ALERTABLE_SEVERITIES:
        _fan_out_alerts(error)


def _fan_out_alerts(error: PlatformError) -> None:
    """Invoke every registered alert callback, isolating failures from each other."""
    error_logger = get_logger("error")
    for callback in _alert_callbacks:
        try:
            callback(error)
        except Exception as callback_exc:  # noqa: BLE001 - isolate one bad callback from the rest
            error_logger.error(
                "Alert callback %r raised while handling %r: %s",
                callback,
                error,
                callback_exc,
                exc_info=True,
            )
