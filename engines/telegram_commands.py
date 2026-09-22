"""
engines/telegram_commands.py

Command Poller: makes /scan, /status, /rapor, /help actual, live,
answering commands instead of just text Telegram's own BotFather command
menu displays as a UI hint. Nothing in this codebase listened for
incoming Telegram messages before this file existed -- the platform was
send-only (engines/telegram_notifications.py -> infrastructure/telegram
/client.py's send_message/send_html_message). This is the first
consumer of TelegramClient.get_updates().

Design choices, each deliberate:

  * SHORT-POLLING every `_POLL_INTERVAL_SECONDS`, not Telegram's own
    long-polling. TelegramClient's aiohttp session timeout is
    `config.api.request_timeout_seconds` (10s default, shared with the
    Binance client) -- a long-poll `timeout` anywhere near that would
    make the HTTP request itself time out before Telegram's long-poll
    window completes. A few seconds of added latency on a command reply
    is a non-issue for a human checking bot status; it would not be for
    signal delivery, which is why this is a wholly separate mechanism
    from the scan loop, not a replacement for its urgency.

  * Runs CONCURRENTLY with `LiveRunner.run_forever()`
    (`asyncio.gather()` in `application/main.py`), not serialized with
    it. A person typing /status should not have to wait for the next
    (up to `fast_scan_interval_seconds`-long) scan cycle to get a reply.

  * Only responds to the configured `config.telegram.chat_id` --
    anyone else's message is logged and silently ignored. This bot's
    status/report data is private; getUpdates has no concept of "who is
    allowed", so this engine is the one place that enforces it.

  * /scan reports the most recently COMPLETED scan cycle's funnel
    numbers (eligible -> Stage 1 survivors -> analyzed -> signals /
    rejected / failed) rather than triggering a brand new cycle
    on-demand. Triggering a fresh cycle from a command handler running
    concurrently with the scheduled loop would mean two cycles able to
    run at once (double API load, two things writing to the database at
    the same moment) for no real benefit -- the scheduled loop already
    runs every `fast_scan_interval_seconds`, so the "last" result is
    never far stale. `LiveRunner.run_one_cycle()` calls
    `record_scan_summary()` after every cycle to keep this current.

  * /rapor reuses `ReportingEngine.generate_daily_report()` +
    `TelegramNotificationEngine.notify_daily_report()` exactly as the
    scheduled daily report does, for "local midnight today -> now"
    rather than inventing a second report format -- same numbers, same
    look, just on demand instead of at `reports.daily_report_time`.
"""

from __future__ import annotations

from datetime import datetime, time as time_, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from application.composition_root import PlatformComponents
from core.models import BotHealthSnapshot
from engines.scanner_orchestrator import ScanCycleResult
from system.error_handler import handle_error
from system.exceptions import PlatformError, Severity
from system.logging_setup import get_logger

_logger = get_logger("telegram")

_POLL_INTERVAL_SECONDS = 3.0

_HELP_TEXT = (
    "🤖 <b>KOMUTLAR</b>\n\n"
    "/status - Botun anlık sağlık durumu\n"
    "/rapor - Bugünün özet raporu (gece yarısından şu ana kadar)\n"
    "/scan - En son tarama döngüsünün sonucu\n"
    "/help - Bu mesaj"
)


class CommandPoller:
    """Owns its own shutdown flag and update-id cursor, mirroring `execution_modes.live.LiveRunner`'s shape."""

    def __init__(self, components: PlatformComponents) -> None:
        self._c = components
        self._timezone = ZoneInfo(components.config.general.timezone)
        self._shutdown_requested = False
        self._update_offset: Optional[int] = None
        self._last_scan_result: Optional[ScanCycleResult] = None
        self._last_scan_at: Optional[datetime] = None

    def request_shutdown(self) -> None:
        self._shutdown_requested = True

    def record_scan_summary(self, result: ScanCycleResult, *, at: Optional[datetime] = None) -> None:
        """Called by `LiveRunner` after every completed cycle so /scan has something current to report."""
        self._last_scan_result = result
        self._last_scan_at = at or datetime.now(timezone.utc)

    async def run_forever(self) -> None:
        _logger.info("Command poller starting (interval=%.0fs)", _POLL_INTERVAL_SECONDS)
        try:
            await self._c.telegram_client.delete_webhook()
        except Exception as exc:  # a failed clear must not prevent polling attempts -- get_updates will just keep failing loudly (logged) if a webhook really is still stuck
            handle_error(PlatformError("delete_webhook failed", severity=Severity.WARNING, cause=exc), category="telegram")
        while not self._shutdown_requested:
            try:
                await self._poll_once()
            except Exception as exc:  # a poll failure must never kill the loop or the process
                handle_error(PlatformError("Command poll failed", severity=Severity.WARNING, cause=exc), category="telegram")
            if self._shutdown_requested:
                break
            await self._sleep(_POLL_INTERVAL_SECONDS)
        _logger.info("Command poller stopped")

    @staticmethod
    async def _sleep(seconds: float) -> None:
        import asyncio

        await asyncio.sleep(seconds)

    async def _poll_once(self) -> None:
        updates = await self._c.telegram_client.get_updates(offset=self._update_offset)
        for update in updates:
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                self._update_offset = update_id + 1
            await self._handle_update(update)

    async def _handle_update(self, update: dict[str, Any]) -> None:
        message = update.get("message")
        if not isinstance(message, dict):
            return  # not every update is a message (could be an edited_message, a poll answer, etc.) -- ignore

        incoming_chat_id = str(message.get("chat", {}).get("id", ""))
        if incoming_chat_id != str(self._c.config.telegram.chat_id):
            _logger.warning("Ignoring Telegram message from unrecognized chat_id=%s", incoming_chat_id)
            return

        text = (message.get("text") or "").strip()
        if not text.startswith("/"):
            return  # not a command attempt -- stay silent rather than react to every message in the chat

        command = text.split()[0].split("@")[0].lower()  # "/status@my_bot arg" -> "/status"
        handler = {
            "/help": self._handle_help,
            "/status": self._handle_status,
            "/rapor": self._handle_rapor,
            "/scan": self._handle_scan,
        }.get(command)

        if handler is None:
            await self._c.telegram_client.send_html_message(
                f"❓ Tanımadığım bir komut: <code>{command}</code>\n{_HELP_TEXT}"
            )
            return
        await handler()

    async def _handle_help(self) -> None:
        await self._c.telegram_client.send_html_message(_HELP_TEXT)

    async def _handle_status(self) -> None:
        snapshot: BotHealthSnapshot = await self._c.production.run_health_check()
        lines = [
            "⚙️ <b>BOT DURUMU</b>",
            "",
            f"🩺 Sağlık: <code>{snapshot.status}</code>",
            f"💼 Açık İşlem: <code>{snapshot.active_trades_count}</code>",
            f"⚠️ Hata Sayısı: <code>{snapshot.error_count}</code>",
        ]
        if snapshot.database_size_mb is not None:
            lines.append(f"🗂 Veritabanı: <code>{snapshot.database_size_mb:.1f} MB</code>")
        lines.append("")
        lines.append(f"🕒 {snapshot.snapshot_time.astimezone(self._timezone).strftime('%Y-%m-%d %H:%M')}")
        await self._c.telegram_client.send_html_message("\n".join(lines))

    async def _handle_rapor(self) -> None:
        local_now = datetime.now(self._timezone)
        period_start = datetime.combine(local_now.date(), time_.min, tzinfo=self._timezone)
        report = await self._c.reporting.generate_daily_report(period_start=period_start, period_end=local_now)
        await self._c.notifications.notify_daily_report(report)

    async def _handle_scan(self) -> None:
        if self._last_scan_result is None or self._last_scan_at is None:
            await self._c.telegram_client.send_html_message("🔍 Henüz tamamlanmış bir tarama döngüsü yok.")
            return
        result = self._last_scan_result
        lines = [
            "🔍 <b>SON TARAMA</b>",
            "",
            f"🪙 Bulunan sembol: <code>{result.symbols_discovered}</code>",
            f"✅ Stage 1'i geçen: <code>{result.symbols_after_fast_filter}</code>",
            f"🟢 Üretilen sinyal: <code>{len(result.signals_generated)}</code>",
            f"🚫 Reddedilen: <code>{len(result.symbols_rejected)}</code>",
            f"❌ Hata: <code>{len(result.symbols_failed)}</code>",
            "",
            f"🕒 {self._last_scan_at.astimezone(self._timezone).strftime('%Y-%m-%d %H:%M:%S')}",
        ]
        await self._c.telegram_client.send_html_message("\n".join(lines))
