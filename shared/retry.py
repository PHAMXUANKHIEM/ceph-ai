"""Bounded retry policy for transient Ceph collection failures.

This module is intentionally side-effect free apart from the injected sleep
function. Collection code can use it without coupling retry decisions to
FastAPI, Watcher or Worker lifecycle code.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from dataclasses import dataclass
from threading import Lock
from typing import Awaitable, Callable, TypeVar

import paramiko


T = TypeVar("T")

_METRICS_LOCK = Lock()
_METRICS = {
    "attempts_total": 0,
    "retries_total": 0,
    "retry_success_total": 0,
    "retry_exhausted_total": 0,
    "non_retryable_total": 0,
    "deadline_exhausted_total": 0,
    "retry_delay_seconds_total": 0.0,
}


def _record_metric(name: str, value: float = 1) -> None:
    with _METRICS_LOCK:
        _METRICS[name] += value


def get_metrics() -> dict[str, int | float]:
    """Return bounded retry counters without operation arguments or secrets."""
    with _METRICS_LOCK:
        return dict(_METRICS)


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """A finite exponential-backoff policy.

    ``max_retries`` means retries after the first attempt. Jitter is bounded
    around the calculated delay to avoid synchronized retries across nodes.
    """

    max_retries: int = 2
    base_delay_seconds: float = 0.25
    max_delay_seconds: float = 5.0
    jitter_ratio: float = 0.25

    def __post_init__(self) -> None:
        if not 0 <= self.max_retries <= 10:
            raise ValueError("max_retries must be between 0 and 10")
        if not 0 < self.base_delay_seconds <= 60:
            raise ValueError("base_delay_seconds must be in (0, 60]")
        if not 0 < self.max_delay_seconds <= 300:
            raise ValueError("max_delay_seconds must be in (0, 300]")
        if not 0 <= self.jitter_ratio <= 1:
            raise ValueError("jitter_ratio must be between 0 and 1")

    @property
    def max_attempts(self) -> int:
        return self.max_retries + 1

    def delay(self, retry_number: int, *, random_fn: Callable[[], float] = random.random) -> float:
        """Return the bounded delay before retry ``retry_number`` (1-based)."""
        if retry_number < 1 or retry_number > self.max_retries:
            raise ValueError("retry_number is outside this policy")
        exponential = min(
            self.max_delay_seconds,
            self.base_delay_seconds * (2 ** (retry_number - 1)),
        )
        jitter = exponential * self.jitter_ratio * (random_fn() * 2 - 1)
        return max(0.0, min(self.max_delay_seconds, exponential + jitter))


def is_retryable_error(exc: BaseException) -> bool:
    """Classify only transport/timeouts as retryable.

    Authentication, host-key, malformed JSON and invalid local arguments are
    deterministic failures; retrying them adds latency without recovery.
    """
    if isinstance(
        exc,
        (
            paramiko.AuthenticationException,
            paramiko.BadHostKeyException,
            json.JSONDecodeError,
            ValueError,
        ),
    ):
        return False
    return isinstance(exc, (asyncio.TimeoutError, TimeoutError, OSError, EOFError, paramiko.SSHException))


async def retry_async(
    operation: Callable[[], Awaitable[T]],
    policy: RetryPolicy,
    *,
    should_retry: Callable[[BaseException], bool] = is_retryable_error,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    random_fn: Callable[[], float] = random.random,
) -> T:
    """Run an async operation with bounded, classified retries."""
    for attempt in range(policy.max_attempts):
        _record_metric("attempts_total")
        try:
            result = await operation()
            if attempt:
                _record_metric("retry_success_total")
            return result
        except Exception as exc:
            retry_number = attempt + 1
            if not should_retry(exc):
                _record_metric("non_retryable_total")
                raise
            if retry_number > policy.max_retries:
                _record_metric("retry_exhausted_total")
                raise
            delay = policy.delay(retry_number, random_fn=random_fn)
            _record_metric("retries_total")
            _record_metric("retry_delay_seconds_total", delay)
            await sleep(delay)
    raise AssertionError("retry loop exhausted without returning or raising")


def retry_sync(
    operation: Callable[[], T],
    policy: RetryPolicy,
    *,
    should_retry: Callable[[BaseException], bool] = is_retryable_error,
    sleep: Callable[[float], None] = time.sleep,
    random_fn: Callable[[], float] = random.random,
    deadline: float | None = None,
) -> T:
    """Run a synchronous operation with bounded retries and an optional deadline.

    The operation is responsible for applying the remaining deadline to its
    own I/O timeout. The helper only prevents backoff from extending beyond
    that deadline.
    """
    for attempt in range(policy.max_attempts):
        _record_metric("attempts_total")
        try:
            result = operation()
            if attempt:
                _record_metric("retry_success_total")
            return result
        except Exception as exc:
            retry_number = attempt + 1
            if not should_retry(exc):
                _record_metric("non_retryable_total")
                raise
            if retry_number > policy.max_retries:
                _record_metric("retry_exhausted_total")
                raise
            delay = policy.delay(retry_number, random_fn=random_fn)
            if deadline is not None and time.monotonic() + delay >= deadline:
                _record_metric("deadline_exhausted_total")
                raise
            _record_metric("retries_total")
            _record_metric("retry_delay_seconds_total", delay)
            sleep(delay)
    raise AssertionError("retry loop exhausted without returning or raising")
