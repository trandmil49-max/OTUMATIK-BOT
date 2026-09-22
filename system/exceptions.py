"""
system/exceptions.py

Custom exception hierarchy for the Binance Futures Analysis Platform
(SRS Part 18: ERROR CLASSIFICATION -- "Classify Info, Warning, Error,
Critical, Fatal. Only Critical and Fatal should trigger urgent alerts."
and SRS Rule 11: "NEVER HIDE ERRORS").

Every exception raised anywhere in the platform that represents a
recognized failure mode should be one of these types (or a subclass),
never a bare Exception/RuntimeError. This lets `system/error_handler.py`
make one consistent, severity-based decision about logging and alerting
without every call site re-deciding "is this bad enough to page someone".

Design notes:
  * Severity is carried on the exception INSTANCE (constructor arg,
    defaulting to a class-level `default_severity`), not inferred from
    the exception's *type* by the handler. A call site with better
    context than the class default may override it per-instance
    (e.g. the 10th consecutive BinanceConnectionError might be raised
    with severity=Severity.CRITICAL instead of the class default of
    WARNING).
  * There is deliberately no `retryable: bool` attribute on any of these
    classes. Retry policy is a *caller* concern (see `system/retry.py`),
    not an intrinsic property of an error type -- the same
    BinanceConnectionError might be retryable when fetching a candle but
    not when re-validating a signal immediately before it is sent.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Optional


class Severity(str, Enum):
    """SRS Part 18 error classification: Info / Warning / Error / Critical / Fatal."""

    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"
    FATAL = "FATAL"


class PlatformError(Exception):
    """
    Base class for every platform-recognized exception.

    Attributes:
        message: Human-readable description of what went wrong.
        severity: One of `Severity`. Defaults to the concrete subclass's
            `default_severity`, but may be overridden per-instance.
        context: Arbitrary structured data useful for debugging and
            logging, e.g. {"symbol": "BTCUSDT", "endpoint": "/fapi/v1/klines"}.
        cause: The original exception this one wraps, if any. Stored
            explicitly (in addition to Python's own `__cause__` via
            `raise X from cause`) so `error_handler.handle_error()` can
            log it without depending on the raise-site using `from`.
    """

    default_severity: Severity = Severity.ERROR

    def __init__(
        self,
        message: str,
        *,
        severity: Optional[Severity] = None,
        context: Optional[dict[str, Any]] = None,
        cause: Optional[BaseException] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.severity: Severity = severity or self.default_severity
        self.context: dict[str, Any] = context or {}
        self.cause = cause

    def __str__(self) -> str:
        text = self.message
        if self.context:
            text = f"{text} | context={self.context}"
        if self.cause is not None:
            text = f"{text} | caused_by={self.cause!r}"
        return text


class ConfigurationError(PlatformError):
    """Configuration failed to load or validate. Startup-blocking -> FATAL."""

    default_severity = Severity.FATAL


class BinanceAPIError(PlatformError):
    """Base class for all Binance API related failures."""

    default_severity = Severity.WARNING


class BinanceRateLimitError(BinanceAPIError):
    """Binance signalled a rate limit (HTTP 429, error code -1003, etc.)."""


class BinanceConnectionError(BinanceAPIError):
    """Network-level failure talking to Binance: timeout, DNS, reset, etc."""


class BinanceDataError(BinanceAPIError):
    """Binance responded, but the payload was missing, malformed, or invalid."""


class DatabaseError(PlatformError):
    """Base class for all SQLite / repository-layer failures."""

    default_severity = Severity.WARNING


class DatabaseLockError(DatabaseError):
    """SQLite reported the database is locked (sqlite3.OperationalError)."""


class TelegramError(PlatformError):
    """A Telegram Bot API call failed: network, auth, rate limit, etc."""

    default_severity = Severity.WARNING


class TelegramRateLimitError(TelegramError):
    """Telegram signalled a rate limit (HTTP 429)."""


class TelegramConnectionError(TelegramError):
    """Network-level failure talking to Telegram: timeout, DNS, reset, etc."""


class DataValidationError(PlatformError):
    """Incoming market/candle/derivatives data failed validation (SRS Part 6/18)."""

    default_severity = Severity.WARNING


class RiskManagementError(PlatformError):
    """A risk-management invariant was violated (e.g. missing SL, unset RR)."""

    default_severity = Severity.ERROR
