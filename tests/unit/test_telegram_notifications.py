"""
Unit tests for engines/telegram_notifications.py's balance-line feature
(`TelegramNotificationEngine._current_balance_line()`), added for the
autonomous-trading pivot.

Not a full retrofit of every notify_* method (no dedicated test file for
this engine existed in the 362-test baseline) -- scoped to the one new
behavior: the "💼 Bakiye: $X.XX" line and its graceful-degradation rules.

Run with:
    pytest tests/unit/test_telegram_notifications.py -v
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from config.schema import PlatformConfig
from core.models import ConfidenceGrade, Signal, SignalDirection, Trade, TradeStatus
from engines.telegram_notifications import TelegramNotificationEngine
from infrastructure.binance.models import AccountBalance


class _FakeTelegramClient:
    def __init__(self) -> None:
        self.sent_messages: list[str] = []

    async def send_html_message(self, html: str, *, disable_notification: bool = False):
        self.sent_messages.append(html)
        return {"ok": True}


class _FakeBinanceClient:
    def __init__(self, balances=None, raise_error: bool = False) -> None:
        self._balances = balances if balances is not None else [AccountBalance("USDT", 100.0, 87.65)]
        self._raise_error = raise_error
        self.call_count = 0

    async def get_account_balance(self):
        self.call_count += 1
        if self._raise_error:
            raise RuntimeError("simulated Binance outage")
        return self._balances


def _signal() -> Signal:
    return Signal(
        symbol="BTCUSDT", direction=SignalDirection.LONG, entry_price=50000.0, stop_loss=49000.0,
        take_profit_1=52000.0, risk_reward_ratio=2.0, confidence_score=85.0,
        confidence_grade=ConfidenceGrade.STRONG, created_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
    )


def _closed_trade(status: TradeStatus) -> Trade:
    return Trade(
        signal_id=1, symbol="BTCUSDT", direction=SignalDirection.LONG, entry_price=50000.0,
        entry_time=datetime(2026, 6, 1, tzinfo=timezone.utc), initial_stop_loss=49000.0,
        current_stop_loss=49000.0, take_profit_1=52000.0, confidence_score=85.0,
        status=status, exit_price=51000.0, exit_time=datetime(2026, 6, 1, 2, tzinfo=timezone.utc),
        realized_pnl_percent=2.0 if status == TradeStatus.TP1_HIT else -2.0,
    )


# ─────────────────────────────────────────────────────────────────────────
# _current_balance_line() IN ISOLATION
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_balance_line_is_none_when_no_binance_client_was_injected():
    engine = TelegramNotificationEngine(client=_FakeTelegramClient(), config=PlatformConfig())
    assert await engine._current_balance_line() is None


@pytest.mark.asyncio
async def test_balance_line_formats_the_usdt_available_balance():
    binance = _FakeBinanceClient(balances=[AccountBalance("USDT", 100.0, 87.65)])
    engine = TelegramNotificationEngine(client=_FakeTelegramClient(), config=PlatformConfig(), balance_client=binance)

    line = await engine._current_balance_line()

    assert line == "💼 Bakiye: $87.65"


@pytest.mark.asyncio
async def test_balance_line_is_none_when_account_has_no_usdt_row():
    binance = _FakeBinanceClient(balances=[AccountBalance("BNB", 0.5, 0.5)])
    engine = TelegramNotificationEngine(client=_FakeTelegramClient(), config=PlatformConfig(), balance_client=binance)

    assert await engine._current_balance_line() is None


@pytest.mark.asyncio
async def test_balance_line_is_none_and_never_raises_when_the_fetch_fails():
    binance = _FakeBinanceClient(raise_error=True)
    engine = TelegramNotificationEngine(client=_FakeTelegramClient(), config=PlatformConfig(), balance_client=binance)

    assert await engine._current_balance_line() is None


# ─────────────────────────────────────────────────────────────────────────
# INTEGRATION INTO notify_new_signal() / notify_trade_closed()
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_notify_new_signal_includes_balance_line_when_binance_client_present():
    telegram = _FakeTelegramClient()
    binance = _FakeBinanceClient(balances=[AccountBalance("USDT", 12.0, 11.50)])
    engine = TelegramNotificationEngine(client=telegram, config=PlatformConfig(), balance_client=binance)

    sent = await engine.notify_new_signal(_signal())

    assert sent is True
    assert "💼 Bakiye: $11.50" in telegram.sent_messages[0]


@pytest.mark.asyncio
async def test_notify_new_signal_omits_balance_line_and_still_sends_without_binance_client():
    telegram = _FakeTelegramClient()
    engine = TelegramNotificationEngine(client=telegram, config=PlatformConfig())

    sent = await engine.notify_new_signal(_signal())

    assert sent is True
    assert "💼 Bakiye" not in telegram.sent_messages[0]


@pytest.mark.asyncio
async def test_notify_new_signal_still_sends_when_balance_fetch_fails():
    telegram = _FakeTelegramClient()
    binance = _FakeBinanceClient(raise_error=True)
    engine = TelegramNotificationEngine(client=telegram, config=PlatformConfig(), balance_client=binance)

    sent = await engine.notify_new_signal(_signal())

    assert sent is True
    assert "💼 Bakiye" not in telegram.sent_messages[0]


@pytest.mark.asyncio
async def test_notify_trade_closed_tp1_includes_balance_line():
    telegram = _FakeTelegramClient()
    binance = _FakeBinanceClient(balances=[AccountBalance("USDT", 15.0, 14.20)])
    engine = TelegramNotificationEngine(client=telegram, config=PlatformConfig(), balance_client=binance)

    sent = await engine.notify_trade_closed(_closed_trade(TradeStatus.TP1_HIT))

    assert sent is True
    assert "💼 Bakiye: $14.20" in telegram.sent_messages[0]


@pytest.mark.asyncio
async def test_notify_trade_closed_stop_loss_includes_balance_line():
    telegram = _FakeTelegramClient()
    binance = _FakeBinanceClient(balances=[AccountBalance("USDT", 9.0, 8.75)])
    engine = TelegramNotificationEngine(client=telegram, config=PlatformConfig(), balance_client=binance)

    sent = await engine.notify_trade_closed(_closed_trade(TradeStatus.STOP_LOSS))

    assert sent is True
    assert "💼 Bakiye: $8.75" in telegram.sent_messages[0]


@pytest.mark.asyncio
async def test_notify_trade_closed_expired_does_not_use_the_reference_branch_or_balance_line():
    """EXPIRED goes through the generic branch, which never had (and still doesn't get) a balance line."""
    telegram = _FakeTelegramClient()
    binance = _FakeBinanceClient(balances=[AccountBalance("USDT", 9.0, 8.75)])
    engine = TelegramNotificationEngine(client=telegram, config=PlatformConfig(), balance_client=binance)

    sent = await engine.notify_trade_closed(_closed_trade(TradeStatus.EXPIRED))

    assert sent is True
    assert binance.call_count == 0
    assert "💼 Bakiye" not in telegram.sent_messages[0]
