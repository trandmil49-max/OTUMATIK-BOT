"""
infrastructure/telegram/client.py

Async Telegram Bot API client (Module 15).

Signal-only scope: this client only ever SENDS/EDITS/DELETES messages on
one configured chat. It has no concept of Signal, Trade, Report, or any
other domain object -- that belongs to engines/telegram_notifications.py
(SRS Part 20 Clean Architecture: infrastructure adapters talk to the
outside world, engines own business logic/formatting/routing). This
module talks to exactly one outside world: the Telegram Bot API.

Cross-cutting concerns composed here rather than duplicated per call
(mirrors infrastructure/binance/client.py's proven shape):
    * `SlidingWindowRateLimiter` (`infrastructure/rate_limiter.py`) --
      proactive throttling against `config.telegram.max_messages_per_minute`,
      shared with the Binance client rather than duplicated.
    * `system.retry.retry_async` -- transient-failure retry with
      backoff, applied ONLY to `TelegramConnectionError` /
      `TelegramRateLimitError` (never to a bare `TelegramError` --
      Telegram rejecting a malformed request, e.g. an invalid chat_id,
      will not fix itself by retrying the identical request).

Binance and Telegram never import from each other (PROJECT_STATUS.md's
hexagonal boundary, extended to sibling infrastructure adapters).
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

import aiohttp

from config.loader import get_config
from config.schema import PlatformConfig
from infrastructure.rate_limiter import SlidingWindowRateLimiter
from system.exceptions import TelegramConnectionError, TelegramError, TelegramRateLimitError
from system.logging_setup import get_logger
from system.retry import retry_async

_logger = get_logger("telegram")

_TELEGRAM_API_BASE = "https://api.telegram.org/bot"


class TelegramClient:
    """
    Async context manager wrapping one `aiohttp.ClientSession`:

        async with TelegramClient() as client:
            await client.send_html_message("<b>Signal ready</b>")

    All configuration (bot token, chat id, timeout, retry count,
    message-rate budget) is read from `PlatformConfig` -- never
    hardcoded -- so a strategy profile change or `.env` override applies
    here automatically. This client only knows how to talk to the
    Telegram Bot API; it has no notification-formatting or business-
    routing logic (that lives in engines/telegram_notifications.py).
    """

    def __init__(
        self,
        config: Optional[PlatformConfig] = None,
        session: Optional[aiohttp.ClientSession] = None,
    ) -> None:
        self._config = config or get_config()
        self._bot_token = self._config.telegram.bot_token
        self._chat_id = self._config.telegram.chat_id
        self._base_url = f"{_TELEGRAM_API_BASE}{self._bot_token}"
        self._timeout = aiohttp.ClientTimeout(total=self._config.api.request_timeout_seconds)
        self._session = session
        self._owns_session = session is None

        self._rate_limiter = SlidingWindowRateLimiter(
            max_units_per_window=self._config.telegram.max_messages_per_minute,
            log_category="telegram",
        )

        # Built once per instance (not as a class-level decorator) so the
        # retry policy honors *this* config's max_retries -- mirrors
        # infrastructure/binance/client.py's `_send_with_retry`.
        self._send_with_retry = retry_async(
            max_attempts=self._config.api.max_retries + 1,
            base_delay_seconds=1.0,
            retryable_exceptions=(TelegramConnectionError, TelegramRateLimitError),
        )(self._send_once)

    async def __aenter__(self) -> "TelegramClient":
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()

    @property
    def rate_limiter(self) -> SlidingWindowRateLimiter:
        """Exposed for diagnostics (e.g. a future `bot_health` snapshot reading current usage)."""
        return self._rate_limiter

    @property
    def is_configured(self) -> bool:
        """True once a bot token and chat id are both present -- callers can no-op gracefully otherwise."""
        return bool(self._bot_token) and bool(self._chat_id)

    # ─────────────────────────────────────────────────────────────────
    # PUBLIC API
    # ─────────────────────────────────────────────────────────────────

    async def send_message(self, text: str, *, disable_notification: bool = False) -> dict[str, Any]:
        """Send a plain-text message (no HTML parsing) via sendMessage."""
        return await self._send(text, parse_mode=None, disable_notification=disable_notification)

    async def send_html_message(self, html: str, *, disable_notification: bool = False) -> dict[str, Any]:
        """
        Send an HTML-formatted message via sendMessage (parse_mode=HTML).

        The caller is responsible for escaping any untrusted substrings
        before calling this -- see engines/telegram_notifications.py's
        `escape_html()`. This client has no business logic and does not
        know which parts of `html` are literal tags versus interpolated
        data.
        """
        return await self._send(html, parse_mode="HTML", disable_notification=disable_notification)

    async def edit_message(
        self, message_id: int, text: str, *, parse_mode: Optional[str] = "HTML"
    ) -> dict[str, Any]:
        """Edit a previously-sent message via editMessageText."""
        self._require_configured()
        payload: dict[str, Any] = {"chat_id": self._chat_id, "message_id": message_id, "text": text}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        return await self._call("editMessageText", payload, weight=1)

    async def delete_message(self, message_id: int) -> dict[str, Any]:
        """Delete a previously-sent message via deleteMessage."""
        self._require_configured()
        payload = {"chat_id": self._chat_id, "message_id": message_id}
        return await self._call("deleteMessage", payload, weight=1)

    async def check_health(self) -> bool:
        """
        Lightweight connectivity/credential check via getMe. Does not
        require a chat_id (getMe is not chat-scoped) and does not spend
        the message-rate budget, so a health check can never itself
        crowd out real notifications.
        """
        if not self._bot_token:
            return False
        try:
            await self._call("getMe", {}, weight=0)
            return True
        except TelegramError:
            return False

    # ─────────────────────────────────────────────────────────────────
    # REQUEST PLUMBING (config guard -> rate limit -> retry -> parse)
    # ─────────────────────────────────────────────────────────────────

    def _require_configured(self) -> None:
        if not self.is_configured:
            raise TelegramError(
                "Telegram is not configured (missing bot_token or chat_id)",
                context={"has_bot_token": bool(self._bot_token), "has_chat_id": bool(self._chat_id)},
            )

    async def delete_webhook(self) -> bool:
        """
        Telegram refuses `getUpdates` (409 Conflict) while ANY webhook is
        registered for this bot token, even a stale one left over from
        unrelated earlier testing that no longer receives anything --
        the registration lives on Telegram's servers, not this codebase,
        so nothing here would have caused or removed it. Idempotent and
        safe to call even when no webhook was ever set (still returns
        True). `CommandPoller` calls this once before its first poll.

        weight=0: does not send a message to the chat, so (like getMe
        and getUpdates below) it must not spend `config.telegram
        .max_messages_per_minute` -- that budget exists specifically to
        avoid spamming the chat with outbound messages, which this is not.
        """
        self._require_configured()
        result = await self._call("deleteWebhook", {"drop_pending_updates": False}, weight=0)
        return bool(result)

    async def get_updates(self, *, offset: Optional[int] = None, timeout_seconds: int = 0) -> list[dict[str, Any]]:
        """
        Fetch incoming messages sent to the bot since `offset` (Telegram's
        own `update_id` cursor -- pass the highest `update_id` seen + 1 to
        avoid seeing the same update twice; `None` returns whatever is
        currently pending). Short-polling by default (see docstring
        above) -- callers wanting near-real-time responses should poll
        this every few seconds rather than raise `timeout_seconds`.

        weight=0: getUpdates does not send anything to the chat, so it
        must never compete with `config.telegram.max_messages_per_minute`
        -- the budget that exists specifically to avoid spamming the
        chat with outbound messages. Sharing it was a real bug found
        live: `CommandPoller` polls every few seconds (~20 calls/minute
        at the default interval), which alone could consume the ENTIRE
        default budget of 20/minute, starving actual notifications
        (signals, reports, command replies) of room to ever send. Same
        reasoning `check_health()` already applies to `getMe` above.
        """
        self._require_configured()
        payload: dict[str, Any] = {"timeout": timeout_seconds, "allowed_updates": ["message"]}
        if offset is not None:
            payload["offset"] = offset
        result = await self._call("getUpdates", payload, weight=0)
        return result if isinstance(result, list) else []

    async def _send(
        self, text: str, *, parse_mode: Optional[str], disable_notification: bool
    ) -> dict[str, Any]:
        self._require_configured()
        payload: dict[str, Any] = {
            "chat_id": self._chat_id,
            "text": text,
            "disable_notification": disable_notification,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        return await self._call("sendMessage", payload, weight=1)

    async def _call(self, method: str, payload: dict[str, Any], *, weight: int) -> dict[str, Any]:
        if weight:
            await self._rate_limiter.acquire(weight)
        return await self._send_with_retry(method, payload)

    async def _send_once(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        """
        One HTTP round-trip, with no retry of its own -- retry is applied
        by wrapping this method in `system.retry.retry_async` inside
        `__init__` (see there for why; mirrors
        infrastructure/binance/client.py's `_send_once`).
        """
        if self._session is None:
            raise RuntimeError(
                "TelegramClient must be used as an async context manager "
                "(`async with TelegramClient() as client:`)"
            )

        url = f"{self._base_url}/{method}"
        try:
            async with self._session.post(url, json=payload) as response:
                body = await response.json(content_type=None)

                if response.status == 429:
                    retry_after = None
                    if isinstance(body, dict):
                        retry_after = body.get("parameters", {}).get("retry_after")
                    raise TelegramRateLimitError(
                        f"Telegram rate limit hit calling {method}",
                        context={"method": method, "retry_after": retry_after},
                    )

                if not isinstance(body, dict) or not body.get("ok", False):
                    description = (
                        body.get("description", "unknown error")
                        if isinstance(body, dict)
                        else "invalid response body"
                    )
                    error_code = body.get("error_code") if isinstance(body, dict) else response.status
                    raise TelegramError(
                        f"Telegram API rejected {method}: {description}",
                        context={"method": method, "error_code": error_code, "status": response.status},
                    )

                _logger.debug("Telegram %s succeeded", method)
                return body.get("result", {})
        except asyncio.TimeoutError as exc:
            raise TelegramConnectionError(
                f"Timed out calling Telegram {method}", cause=exc, context={"method": method}
            ) from exc
        except aiohttp.ClientError as exc:
            raise TelegramConnectionError(
                f"Connection error calling Telegram {method}", cause=exc, context={"method": method}
            ) from exc
