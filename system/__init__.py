"""
system/ -- cross-cutting concerns (SRS Part 18: PRODUCTION ENGINE / STABILITY
ENGINE) with zero knowledge of infrastructure/:

    exceptions      -- typed exception hierarchy + Severity
    logging_setup   -- categorized rotating file logging
    error_handler   -- single choke point for handling PlatformErrors
    retry           -- retry_sync / retry_async decorators
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
from system.error_handler import handle_error, register_alert_callback
from system.logging_setup import configure_logging, get_logger
from system.retry import retry_async, retry_sync

__all__ = [
    "Severity",
    "PlatformError",
    "ConfigurationError",
    "BinanceAPIError",
    "BinanceRateLimitError",
    "BinanceConnectionError",
    "BinanceDataError",
    "DatabaseError",
    "DatabaseLockError",
    "TelegramError",
    "DataValidationError",
    "RiskManagementError",
    "configure_logging",
    "get_logger",
    "handle_error",
    "register_alert_callback",
    "retry_sync",
    "retry_async",
]
