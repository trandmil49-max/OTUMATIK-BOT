"""
system/retry.py

Generic retry-with-backoff decorators for synchronous and asynchronous
callables (SRS Part 6: RETRY SYSTEM -- "Every network request must
support retry logic. Retry only temporary failures. Never retry invalid
requests forever. Use exponential backoff. Maximum retry count should be
configurable.").

Built once, centrally, per SRS Part 1's "avoid duplicated logic" -- the
future Binance client, database writes, and Telegram sends all reuse
these same two decorators rather than each hand-rolling their own
retry loop.

Design note: retryability is supplied by the CALLER as an explicit
`retryable_exceptions` tuple at each use site, not inferred from a
`.retryable` attribute on a custom exception (see system/exceptions.py's
module docstring for why). This keeps the decorators fully generic --
they know nothing about PlatformError at all.
"""

from __future__ import annotations

import asyncio
import functools
import time
from typing import Any, Callable, Optional, ParamSpec, TypeVar

from system.logging_setup import get_logger

P = ParamSpec("P")
T = TypeVar("T")

_logger = get_logger("system")

DEFAULT_RETRYABLE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    ConnectionError,
    TimeoutError,
    OSError,
)


def retry_sync(
    max_attempts: int = 3,
    base_delay_seconds: float = 1.0,
    backoff_multiplier: float = 2.0,
    retryable_exceptions: tuple[type[BaseException], ...] = DEFAULT_RETRYABLE_EXCEPTIONS,
) -> Callable[[Callable[P, T]], Callable[P, T]]:
    """
    Decorator: retry a synchronous callable up to `max_attempts` times
    whenever it raises one of `retryable_exceptions`, sleeping
    `base_delay_seconds * backoff_multiplier ** (attempt - 1)` between
    attempts. Re-raises the final exception if every attempt fails.
    Exceptions not in `retryable_exceptions` propagate immediately,
    without consuming a retry attempt.

    Args:
        max_attempts: Total number of attempts, including the first
            (not "number of retries"). Must be >= 1 for the decorator to
            make sense; a value of 1 behaves like no retry at all.
        base_delay_seconds: Delay before the second attempt.
        backoff_multiplier: Multiplier applied to the delay after every
            failed attempt (exponential backoff).
        retryable_exceptions: Exception types that should trigger a
            retry. Supplied by the caller at each use site -- see the
            module docstring for why this is not inferred automatically.
    """

    def decorator(func: Callable[P, T]) -> Callable[P, T]:
        @functools.wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            last_exc: Optional[BaseException] = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except retryable_exceptions as exc:
                    last_exc = exc
                    if attempt == max_attempts:
                        _logger.warning(
                            "retry_sync: %s failed on final attempt %d/%d: %s",
                            func.__qualname__, attempt, max_attempts, exc,
                        )
                        raise
                    delay = base_delay_seconds * (backoff_multiplier ** (attempt - 1))
                    _logger.warning(
                        "retry_sync: %s failed on attempt %d/%d (%s); retrying in %.2fs",
                        func.__qualname__, attempt, max_attempts, exc, delay,
                    )
                    time.sleep(delay)
            assert last_exc is not None  # unreachable; satisfies type checkers
            raise last_exc

        return wrapper

    return decorator


def retry_async(
    max_attempts: int = 3,
    base_delay_seconds: float = 1.0,
    backoff_multiplier: float = 2.0,
    retryable_exceptions: tuple[type[BaseException], ...] = DEFAULT_RETRYABLE_EXCEPTIONS,
) -> Callable[[Callable[P, Any]], Callable[P, Any]]:
    """Async counterpart of `retry_sync`; sleeps via `asyncio.sleep` instead of blocking."""

    def decorator(func: Callable[P, Any]) -> Callable[P, Any]:
        @functools.wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> Any:
            last_exc: Optional[BaseException] = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return await func(*args, **kwargs)
                except retryable_exceptions as exc:
                    last_exc = exc
                    if attempt == max_attempts:
                        _logger.warning(
                            "retry_async: %s failed on final attempt %d/%d: %s",
                            func.__qualname__, attempt, max_attempts, exc,
                        )
                        raise
                    delay = base_delay_seconds * (backoff_multiplier ** (attempt - 1))
                    _logger.warning(
                        "retry_async: %s failed on attempt %d/%d (%s); retrying in %.2fs",
                        func.__qualname__, attempt, max_attempts, exc, delay,
                    )
                    await asyncio.sleep(delay)
            assert last_exc is not None  # unreachable; satisfies type checkers
            raise last_exc

        return wrapper

    return decorator
