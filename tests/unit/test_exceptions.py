"""
Unit tests for system/exceptions.py (Module 2).

Run with:
    pytest tests/unit/test_exceptions.py -v
"""

from system.exceptions import (
    BinanceAPIError,
    BinanceConnectionError,
    BinanceDataError,
    BinanceRateLimitError,
    ConfigurationError,
    DatabaseError,
    DatabaseLockError,
    DataValidationError,
    PlatformError,
    RiskManagementError,
    Severity,
    TelegramError,
)


def test_platform_error_default_severity_and_message():
    err = PlatformError("something broke")
    assert err.message == "something broke"
    assert err.severity == Severity.ERROR
    assert err.context == {}
    assert err.cause is None


def test_platform_error_carries_context():
    err = PlatformError("bad candle", context={"symbol": "BTCUSDT"})
    assert err.context == {"symbol": "BTCUSDT"}
    assert "symbol" in str(err)


def test_platform_error_carries_cause():
    original = ValueError("root cause")
    err = PlatformError("wrapped failure", cause=original)
    assert err.cause is original
    assert "root cause" in str(err)


def test_configuration_error_is_fatal_by_default():
    err = ConfigurationError("STRATEGY_PROFILE is not set")
    assert err.severity == Severity.FATAL


def test_binance_error_hierarchy_and_default_severity():
    rate_limit = BinanceRateLimitError("HTTP 429")
    connection = BinanceConnectionError("timeout")
    data = BinanceDataError("malformed candle payload")

    for err in (rate_limit, connection, data):
        assert isinstance(err, BinanceAPIError)
        assert isinstance(err, PlatformError)
        assert err.severity == Severity.WARNING


def test_database_lock_error_is_subclass_of_database_error():
    err = DatabaseLockError("database is locked")
    assert isinstance(err, DatabaseError)
    assert err.severity == Severity.WARNING


def test_telegram_data_validation_and_risk_management_default_severities():
    assert TelegramError("send failed").severity == Severity.WARNING
    assert DataValidationError("negative price rejected").severity == Severity.WARNING
    assert RiskManagementError("missing stop loss").severity == Severity.ERROR


def test_severity_can_be_overridden_per_instance():
    err = BinanceConnectionError("timeout", severity=Severity.CRITICAL)
    assert err.severity == Severity.CRITICAL
