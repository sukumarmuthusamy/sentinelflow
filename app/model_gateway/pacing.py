"""Pacing helpers for provider rate limits (free tier)."""

from __future__ import annotations

import asyncio
import time


def min_interval_for_rpm(requests_per_minute: int) -> float:
    """Seconds between call starts to stay at or below requests_per_minute."""
    if requests_per_minute < 1:
        msg = "requests_per_minute must be >= 1"
        raise ValueError(msg)
    # Small buffer so rolling windows do not clip the 5th call in a minute.
    return (60.0 / requests_per_minute) + 0.5


class AsyncMinIntervalPacer:
    """Serialize async work with a minimum gap between call start times."""

    def __init__(self, min_interval_seconds: float) -> None:
        self._min_interval = min_interval_seconds
        self._lock = asyncio.Lock()
        self._last_start: float | None = None

    @property
    def min_interval_seconds(self) -> float:
        return self._min_interval

    async def wait_turn(self) -> None:
        """Wait until the next call may start under the interval limit."""
        async with self._lock:
            now = time.monotonic()
            if self._last_start is not None:
                elapsed = now - self._last_start
                remaining = self._min_interval - elapsed
                if remaining > 0:
                    await asyncio.sleep(remaining)
            self._last_start = time.monotonic()


def estimate_paced_wall_seconds(
    *,
    call_count: int,
    min_interval_seconds: float,
    assumed_latency_seconds: float = 5.0,
) -> float:
    """Rough lower bound for paced generation sweeps."""
    if call_count <= 0:
        return 0.0
    gaps = max(0, call_count - 1)
    return gaps * min_interval_seconds + call_count * assumed_latency_seconds
