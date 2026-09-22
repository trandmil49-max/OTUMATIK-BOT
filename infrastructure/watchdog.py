"""
infrastructure/watchdog.py

Generic heartbeat/staleness detector (SRS Part 18 WATCHDOG).

SCOPE: this class detects staleness only. It does not restart anything.
There is no main scan loop yet (Module 22 / main.py is not built), so
there is nothing concrete to restart, and "how" a restart should behave
-- immediate retry vs. backoff, how many attempts before giving up and
alerting a human instead of trying again -- is not specified anywhere
this module can see. Inventing that policy now would be exactly the
kind of unspecified business logic this pass was told not to invent.
`Watchdog` is the detection primitive Module 22's main loop is expected
to wrap: call `record_heartbeat()` once per scan cycle, check
`is_stale()` on an external timer, and decide what to do about it there.

Not tied to any specific loop or domain object -- like
`infrastructure/rate_limiter.py`, this is a general-purpose utility, not
an adapter to a specific external system, which is why it lives at the
top level of `infrastructure/` rather than under `engines/`.
"""

from __future__ import annotations

import time
from typing import Callable


class Watchdog:
    """
    Tracks time since the last `record_heartbeat()` call and reports
    whether that exceeds `stale_after_seconds`.

    `clock` is injectable (defaults to `time.monotonic`) so tests never
    need a real `time.sleep()` -- mirrors how
    `infrastructure/rate_limiter.py`'s tests patch `time.monotonic`
    rather than sleeping for real.
    """

    def __init__(self, stale_after_seconds: float, *, clock: Callable[[], float] = time.monotonic) -> None:
        if stale_after_seconds <= 0:
            raise ValueError(f"stale_after_seconds must be positive, got {stale_after_seconds}")
        self._stale_after_seconds = stale_after_seconds
        self._clock = clock
        self._last_heartbeat = clock()

    def record_heartbeat(self) -> None:
        """Call once per completed unit of work (e.g. once per scan cycle)."""
        self._last_heartbeat = self._clock()

    def seconds_since_last_heartbeat(self) -> float:
        return self._clock() - self._last_heartbeat

    def is_stale(self) -> bool:
        return self.seconds_since_last_heartbeat() > self._stale_after_seconds
