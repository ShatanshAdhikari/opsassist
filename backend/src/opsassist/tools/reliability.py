"""Retry and circuit breaking for provider calls.

State is per process, like the rate limiters. A second replica has its own view of a server,
which makes the breaker slower to open but never wrong about permissions.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from opsassist.tools.base import ProviderRateLimited, ProviderTimeout, ToolProviderError


@dataclass
class RetryOutcome[T]:
    value: T
    attempts: int


async def call_with_retry[T](
    call: Callable[[float], Awaitable[T]],
    *,
    budget_seconds: float,
    retries: int,
    base_delay_seconds: float,
) -> RetryOutcome[T]:
    """Run `call(remaining_seconds)` with backoff and full jitter, within one overall budget.

    The budget covers all attempts, so retries cannot stretch a call past its stated timeout.
    Only read calls may use this: repeating a write could repeat its effect.
    """
    deadline = time.monotonic() + budget_seconds
    attempt = 0
    while True:
        attempt += 1
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ProviderTimeout()
        try:
            return RetryOutcome(await call(remaining), attempt)
        except ToolProviderError as exc:
            if not exc.retryable or attempt > retries:
                exc.attempts = attempt  # type: ignore[attr-defined]
                raise
            # Jitter avoids synchronized retries, so it has no security role.
            delay = random.uniform(0, base_delay_seconds * 2 ** (attempt - 1))  # noqa: S311
            if isinstance(exc, ProviderRateLimited):
                delay = max(delay, exc.retry_after_seconds)
            if deadline - time.monotonic() <= delay:
                exc.attempts = attempt  # type: ignore[attr-defined]
                raise
            await asyncio.sleep(delay)


class CircuitBreaker:
    """Stops calling a server after consecutive failures, then lets one call test it."""

    def __init__(self, failure_threshold: int, cooldown_seconds: float) -> None:
        self._threshold = failure_threshold
        self._cooldown = cooldown_seconds
        self._failures: dict[str, int] = {}
        self._opened_at: dict[str, float] = {}

    def allow(self, key: str) -> bool:
        opened_at = self._opened_at.get(key)
        if opened_at is None:
            return True
        if time.monotonic() - opened_at < self._cooldown:
            return False
        # Restart the cooldown, so only this one caller probes while the rest stay refused.
        self._opened_at[key] = time.monotonic()
        return True

    def record_success(self, key: str) -> None:
        self._failures.pop(key, None)
        self._opened_at.pop(key, None)

    def record_failure(self, key: str) -> None:
        failures = self._failures.get(key, 0) + 1
        self._failures[key] = failures
        if failures >= self._threshold:
            self._opened_at[key] = time.monotonic()
