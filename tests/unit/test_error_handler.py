"""
Unit tests for system/error_handler.py (Module 2).

Run with:
    pytest tests/unit/test_error_handler.py -v
"""

import logging

import pytest

from config.schema import LoggingConfig
from system import error_handler, logging_setup
from system.exceptions import BinanceConnectionError, ConfigurationError, PlatformError, Severity


@pytest.fixture(autouse=True)
def _reset_state(tmp_path):
    """Clean logging + callback state before and after every test."""
    logging_setup.reset_logging_state_for_tests()
    error_handler.clear_alert_callbacks_for_tests()
    logging_setup.configure_logging(LoggingConfig(log_directory=str(tmp_path)))
    yield
    logging_setup.reset_logging_state_for_tests()
    error_handler.clear_alert_callbacks_for_tests()


def test_handle_error_logs_message_with_context(caplog):
    err = PlatformError("db write failed", context={"table": "signals"})
    with caplog.at_level(logging.ERROR):
        error_handler.handle_error(err, category="database")

    assert any("db write failed" in r.message for r in caplog.records)
    assert any("signals" in r.message for r in caplog.records)


def test_handle_error_writes_to_the_requested_category_file(tmp_path):
    err = BinanceConnectionError("timeout contacting Binance")
    error_handler.handle_error(err, category="api")

    for handler in logging_setup.get_logger("api").handlers:
        handler.flush()
    content = (tmp_path / "api.log").read_text(encoding="utf-8")
    assert "timeout contacting Binance" in content


def test_critical_severity_triggers_registered_callback():
    received: list[PlatformError] = []
    error_handler.register_alert_callback(received.append)

    err = ConfigurationError("STRATEGY_PROFILE missing")  # default_severity == FATAL
    error_handler.handle_error(err)

    assert received == [err]


def test_warning_severity_triggers_registered_callback():
    """
    Regression test: WARNING/ERROR used to be excluded from the fan-out
    gate entirely, which made engines/telegram_notifications.py's
    notify_warning() (added to surface SRS Part 18's WARNING/ERROR band
    to Telegram) unreachable in practice -- its own routing logic could
    never run because handle_error() never called any callback for
    anything below CRITICAL. See _ALERTABLE_SEVERITIES's comment.
    """
    received: list[PlatformError] = []
    error_handler.register_alert_callback(received.append)

    err = BinanceConnectionError("temporary timeout")  # default_severity == WARNING
    error_handler.handle_error(err)

    assert received == [err]


def test_error_severity_triggers_registered_callback():
    received: list[PlatformError] = []
    error_handler.register_alert_callback(received.append)

    err = PlatformError("recoverable failure", severity=Severity.ERROR)
    error_handler.handle_error(err)

    assert received == [err]


def test_info_severity_does_not_trigger_callback():
    """INFO is the one severity that never fans out -- too low-value for a push notification or a persisted error_events row."""
    received: list[PlatformError] = []
    error_handler.register_alert_callback(received.append)

    err = PlatformError("routine informational event", severity=Severity.INFO)
    error_handler.handle_error(err)

    assert received == []


def test_severity_override_takes_precedence_without_mutating_the_original():
    received: list[PlatformError] = []
    error_handler.register_alert_callback(received.append)

    err = BinanceConnectionError("timeout")  # normally WARNING
    error_handler.handle_error(err, severity=Severity.CRITICAL)

    assert received == [err]
    assert err.severity == Severity.WARNING  # the instance itself is untouched


def test_callback_exception_is_isolated_and_logged(caplog):
    def broken_callback(_error: PlatformError) -> None:
        raise RuntimeError("telegram is down")

    error_handler.register_alert_callback(broken_callback)
    err = ConfigurationError("bad config")

    with caplog.at_level(logging.ERROR):
        error_handler.handle_error(err)  # must not raise despite the broken callback

    assert any(
        "broken_callback" in r.message or "telegram is down" in r.message for r in caplog.records
    )


def test_multiple_callbacks_are_all_invoked_in_registration_order():
    calls: list[str] = []
    error_handler.register_alert_callback(lambda e: calls.append("first"))
    error_handler.register_alert_callback(lambda e: calls.append("second"))

    error_handler.handle_error(ConfigurationError("bad config"))

    assert calls == ["first", "second"]
