"""
Retry for transient provider failures (429s, 503s, dropped connections).

Separate from instructor's max_retries, which re-prompts on bad JSON. This
one waits out an unavailable service. Both are wanted.
"""

from __future__ import annotations

import random
import time
from typing import Callable, Optional, TypeVar

T = TypeVar("T")

MAX_ATTEMPTS = 5          # 1 attempt + 4 retries
INITIAL_BACKOFF_SECONDS = 2.0
MAX_BACKOFF_SECONDS = 60.0
BACKOFF_MULTIPLIER = 2.0

# Matched against str(exc). SDKs raise all sorts of exception types for the
# same condition, so the message is the only portable signal.
_TRANSIENT_MARKERS = (
    "429",
    "rate limit",
    "rate_limit",
    "ratelimit",
    "resource_exhausted",
    "resource exhausted",
    "quota exceeded",
    "too many requests",
    "503",
    "service unavailable",
    "unavailable",
    "overloaded",
    "500",
    "internal server error",
    "502",
    "bad gateway",
    "504",
    "gateway timeout",
    "deadline exceeded",
    "timed out",
    "timeout",
    "connection reset",
    "connection aborted",
    "connection error",
    "temporarily unavailable",
)


def is_transient_error(exc: BaseException) -> bool:
    """Would the same request plausibly work if we tried again?"""
    text = str(exc).lower()
    if any(marker in text for marker in _TRANSIENT_MARKERS):
        return True

    # some SDKs put the status on the exception instead of the message
    for attr in ("status_code", "code", "http_status"):
        status = getattr(exc, attr, None)
        if isinstance(status, int) and status in (408, 429, 500, 502, 503, 504):
            return True

    # ExtractionFailed and instructor both wrap the real cause
    for attr in ("underlying", "__cause__"):
        inner = getattr(exc, attr, None)
        if isinstance(inner, BaseException) and inner is not exc:
            return is_transient_error(inner)

    return False


def backoff_delay(attempt: int) -> float:
    """Exponential with full jitter, so parallel callers don't re-collide."""
    ceiling = min(INITIAL_BACKOFF_SECONDS * (BACKOFF_MULTIPLIER ** attempt), MAX_BACKOFF_SECONDS)
    return random.uniform(0.0, ceiling)


def call_with_backoff(
    fn: Callable[[], T],
    *,
    max_attempts: int = MAX_ATTEMPTS,
    on_retry: Optional[Callable[[int, float, BaseException], None]] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Run fn(), retrying transient failures only. Anything else raises
    straight through. on_retry(attempt, delay, exc) is for progress output."""
    last_exc: Optional[BaseException] = None
    for attempt in range(max_attempts):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 -- classified below, re-raised if not transient
            last_exc = exc
            if attempt >= max_attempts - 1 or not is_transient_error(exc):
                raise
            delay = backoff_delay(attempt)
            if on_retry is not None:
                on_retry(attempt + 1, delay, exc)
            sleep(delay)

    # unreachable, but avoids an implicit return None if max_attempts <= 0
    raise last_exc if last_exc is not None else RuntimeError("call_with_backoff exhausted with no attempts")
