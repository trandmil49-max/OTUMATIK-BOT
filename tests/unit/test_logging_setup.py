"""
Unit tests for system/logging_setup.py (Module 2).

Run with:
    pytest tests/unit/test_logging_setup.py -v
"""

import logging

import pytest

from config.schema import LoggingConfig
from system import logging_setup


@pytest.fixture(autouse=True)
def _reset_logging_state():
    """Every test starts and ends with a clean, unconfigured logging state."""
    logging_setup.reset_logging_state_for_tests()
    yield
    logging_setup.reset_logging_state_for_tests()


def test_configure_logging_creates_one_file_per_category(tmp_path):
    logging_setup.configure_logging(LoggingConfig(log_directory=str(tmp_path)))

    for category in logging_setup.CATEGORIES:
        assert (tmp_path / f"{category}.log").exists()


def test_configure_logging_is_idempotent(tmp_path):
    cfg = LoggingConfig(log_directory=str(tmp_path))

    logging_setup.configure_logging(cfg)
    handlers_after_first_call = len(logging.getLogger("trading").handlers)

    logging_setup.configure_logging(cfg)
    handlers_after_second_call = len(logging.getLogger("trading").handlers)

    assert handlers_after_first_call == handlers_after_second_call


def test_root_logger_gets_a_console_handler(tmp_path):
    logging_setup.configure_logging(LoggingConfig(log_directory=str(tmp_path)))

    root_handlers = logging.getLogger().handlers
    assert any(
        isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
        for h in root_handlers
    )


def test_get_logger_returns_usable_logger_and_writes_to_its_file(tmp_path):
    logging_setup.configure_logging(LoggingConfig(log_directory=str(tmp_path)))

    trading_logger = logging_setup.get_logger("trading")
    trading_logger.info("signal created for BTCUSDT")
    for handler in trading_logger.handlers:
        handler.flush()

    content = (tmp_path / "trading.log").read_text(encoding="utf-8")
    assert "signal created for BTCUSDT" in content


def test_get_logger_with_unknown_category_warns_but_does_not_raise(tmp_path, caplog):
    logging_setup.configure_logging(LoggingConfig(log_directory=str(tmp_path)))

    with caplog.at_level(logging.WARNING):
        logger = logging_setup.get_logger("not_a_real_category")

    assert isinstance(logger, logging.Logger)
    assert any("unknown category" in record.message for record in caplog.records)


def test_log_level_filters_out_lower_severity_messages(tmp_path):
    logging_setup.configure_logging(LoggingConfig(log_directory=str(tmp_path), level="WARNING"))

    system_logger = logging_setup.get_logger("system")
    system_logger.debug("this should be filtered out")
    system_logger.warning("this should appear")
    for handler in system_logger.handlers:
        handler.flush()

    content = (tmp_path / "system.log").read_text(encoding="utf-8")
    assert "this should be filtered out" not in content
    assert "this should appear" in content


def test_is_configured_reflects_current_state(tmp_path):
    assert logging_setup.is_configured() is False
    logging_setup.configure_logging(LoggingConfig(log_directory=str(tmp_path)))
    assert logging_setup.is_configured() is True
