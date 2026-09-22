"""
infrastructure/rate_limiter.py

Generic sliding-window rate limiter (SRS Part 6 API OPTIMIZATION,
extended to every outbound adapter per Part 1's "avoid duplicated
logic"). Originally written specifically for Binance's weight-budgeted
REST API, then genericized so that infrastructure adapters with nothing
to do with each other -- the Binance REST client and the Telegram Bot
API client (Module 15) -- can each throttle their own outbound calls
against their own configured budget, without either one depending on
the other or on Binance-specific concepts like "weight".

Binance and Telegram must never import from each other (the hexagonal
boundary documented in PROJECT_STATUS.md, extended here to sibling
infrastructure adapters). Both depend on this module instead:

    * Binance spends Binance-documented request WEIGHT per call, inside
      a 60s window, budgeted via `config.scanner.max_api_requests_per_minute`.
    * Telegram spends 1 unit per message, inside a 60s window, budgeted
      via `config.telegram.max_messages_per_minute`.

Both are "N units per window" -- the only thing that differs is what a
unit means to the caller, which this module does not need to know.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Optional

from system.logging_setup import get_logger

DEFAULT_WINDOW_SECONDS = 60.0


class SlidingWindowRateLimiter:
    """
    Sliding-window rate limiter, safe for concurrent `asyncio` callers
    (an internal `asyncio.Lock` serializes the check-and-record step).

    Generic over "units": Binance spends per-call request weight;
    Telegram spends one unit per message. Each caller supplies its own
    budget (`max_units_per_window`) and, optionally, its own window
    length and logging category.

    `record_external_usage()` records what the remote service itself
    reports (e.g. Binance's `X-MBX-USED-WEIGHT-1M` header), purely for
    logging/diagnostics -- it deliberately does NOT feed back into
    `acquire()`'s sleep calculation. Reconciling a local sliding-window
    estimate against a server-reported single-counter value precisely is
    significantly more complex than the benefit justifies here; the
    local estimate alone is already conservative (every acquired unit
    counts for the full window even if the remote service's own window
    resets slightly differently), so it errs toward throttling a bit
    more than strictly necessary rather than less.
    """

    def __init__(
        self,
        max_units_per_window: int,
        window_seconds: float = DEFAULT_WINDOW_SECONDS,
        *,
        log_category: str = "api",
    ) -> None:
        self.max_units_per_window = max_units_per_window
        self.window_seconds = window_seconds
        self._usage: deque[tuple[float, int]] = deque()
        self._lock = asyncio.Lock()
        self._last_external_usage: Optional[int] = None
        self._logger = get_logger(log_category)

    async def acquire(self, units: int = 1) -> None:
        """Block until spending `units` keeps the trailing window's total within budget, then record the spend."""
        async with self._lock:
            while True:
                now = time.monotonic()
                self._purge_expired(now)
                current_usage = sum(u for _, u in self._usage)

                if current_usage + units <= self.max_units_per_window:
                    self._usage.append((now, units))
                    return

                oldest_timestamp, _ = self._usage[0]
                sleep_seconds = (oldest_timestamp + self.window_seconds) - now
                if sleep_seconds > 0:
                    self._logger.warning(
                        "Rate limiter throttling: %d/%d units used, sleeping %.2fs",
                        current_usage,
                        self.max_units_per_window,
                        sleep_seconds,
                    )
                    await asyncio.sleep(sleep_seconds)
                # loop back around: re-purge expired entries and re-check

    def record_external_usage(self, used_units: int) -> None:
        """Record the remote service's own reported usage for diagnostics (see class docstring)."""
        self._last_external_usage = used_units
        if used_units >= self.max_units_per_window * 0.8:
            self._logger.warning(
                "Externally-reported usage (%d) is approaching the configured budget (%d)",
                used_units,
                self.max_units_per_window,
            )

    @property
    def last_external_usage(self) -> Optional[int]:
        return self._last_external_usage

    def current_usage(self) -> int:
        """Current rolling-window usage per this instance's own accounting (tests/diagnostics)."""
        now = time.monotonic()
        self._purge_expired(now)
        return sum(u for _, u in self._usage)

    def _purge_expired(self, now: float) -> None:
        while self._usage and now - self._usage[0][0] >= self.window_seconds:
            self._usage.popleft()
