"""
system/logging_setup.py

Categorized rotating file logging (SRS Part 18: LOGGING ENGINE -- "Separate
logs into Trading Logs, API Logs, Database Logs, Telegram Logs, System
Logs, Error Logs, Performance Logs. Never mix everything into one file.").

Usage:
    from system.logging_setup import configure_logging, get_logger

    configure_logging()          # call ONCE, from an entry point (main.py, a test fixture)
    log = get_logger("trading")
    log.info("Signal created for BTCUSDT")

`configure_logging()` is deliberately NOT invoked as a side effect of the
first `get_logger()` call. Implicit global logging configuration
triggered by import order is exactly the kind of "magic" this project
avoids elsewhere (see config/loader.py's explicit env-var mapping) --
an entry point must call it explicitly, exactly once per process.
"""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path
from typing import Final, Optional

from config.schema import LoggingConfig

CATEGORIES: Final[tuple[str, ...]] = (
    "trading",
    "api",
    "database",
    "telegram",
    "system",
    "error",
    "performance",
)

_LOG_FORMAT: Final[str] = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"

_configured: bool = False


def configure_logging(config: Optional[LoggingConfig] = None) -> None:
    """
    Attach one rotating file handler per category in CATEGORIES, plus one
    shared console handler on the root logger.

    Safe to call more than once: the second and later calls are no-ops,
    so re-entrant startup code (or a test suite that imports several
    modules that each try to configure logging) can never end up with
    duplicated handlers writing every line twice.

    Args:
        config: Logging settings to use. Defaults to `LoggingConfig()`
            (plain Pydantic defaults) rather than `config.loader.get_config()`,
            so this module never implicitly depends on the full
            application config being loadable.
    """
    global _configured
    if _configured:
        return

    cfg = config or LoggingConfig()
    log_dir = Path(cfg.log_directory)
    log_dir.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(_LOG_FORMAT)
    level = getattr(logging, cfg.level.value)

    root = logging.getLogger()
    root.setLevel(level)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)
    root.addHandler(console_handler)

    for category in CATEGORIES:
        category_logger = logging.getLogger(category)
        category_logger.setLevel(level)
        category_logger.propagate = True  # also reaches the shared console handler

        file_handler = logging.handlers.RotatingFileHandler(
            filename=log_dir / f"{category}.log",
            maxBytes=cfg.rotate_max_bytes,
            backupCount=cfg.rotate_backup_count,
            encoding="utf-8",
        )
        file_handler.setLevel(level)
        file_handler.setFormatter(formatter)
        category_logger.addHandler(file_handler)

    _configured = True


def get_logger(category: str) -> logging.Logger:
    """
    Return the logger for `category`.

    Does NOT call `configure_logging()` -- if the platform has not been
    configured yet, this returns a plain stdlib logger with no handlers
    of its own (Python's default "no handlers found" behavior applies).

    Args:
        category: Expected to be one of CATEGORIES. Unknown categories
            are still returned (this never raises), but are logged as a
            warning under the "system" category so a typo'd category
            name is visible instead of silently swallowing log lines.
    """
    if category not in CATEGORIES:
        logging.getLogger("system").warning(
            "get_logger() called with unknown category %r; expected one of %s",
            category,
            CATEGORIES,
        )
    return logging.getLogger(category)


def is_configured() -> bool:
    """Expose configuration state for diagnostics and tests."""
    return _configured


def reset_logging_state_for_tests() -> None:
    """
    Test-only helper. Clears the `_configured` flag and removes every
    handler from the root logger and each category logger, so a test can
    call `configure_logging()` again with a different `tmp_path` /
    `LoggingConfig` without leaking handlers (and open file descriptors)
    across tests. Not used by any production code path.
    """
    global _configured
    _configured = False

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    for category in CATEGORIES:
        category_logger = logging.getLogger(category)
        for handler in list(category_logger.handlers):
            category_logger.removeHandler(handler)
            handler.close()
