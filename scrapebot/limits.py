"""Scrape limits: token buckets, rolling-window budgets and a circuit breaker.

All primitives take an injectable ``clock`` so they are deterministic in tests.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Callable, Iterable
from contextlib import asynccontextmanager
from typing import AsyncIterator

from .config import CircuitBreakerConfig, RateSpec

log = logging.getLogger(__name__)

Clock = Callable[[], float]


class StopRequested(Exception):
    """Raised by waiting primitives when a shutdown was requested."""


class TokenBucket:
    """Classic token bucket: ``rate`` tokens per second, capacity ``burst``."""

    def __init__(self, rate_per_s: float, burst: int, clock: Clock = time.monotonic):
        self.rate = rate_per_s
        self.capacity = float(burst)
        self.tokens = float(burst)
        self.clock = clock
        self.updated = clock()
        self.paused_until = 0.0

    def _refill(self) -> None:
        now = self.clock()
        if now > self.updated:
            self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
            self.updated = now

    def try_acquire(self) -> float:
        """Take a token if possible. Returns 0 on success, else seconds to wait."""
        now = self.clock()
        if now < self.paused_until:
            return self.paused_until - now
        self._refill()
        if self.tokens >= 1:
            self.tokens -= 1
            return 0.0
        return (1 - self.tokens) / self.rate

    def pause(self, seconds: float) -> None:
        """Stop issuing tokens for ``seconds`` and drain the bucket."""
        self.paused_until = max(self.paused_until, self.clock() + seconds)
        self.tokens = 0.0
        self.updated = max(self.updated, self.paused_until)


class RollingCounter:
    """Counts events in a sliding time window (e.g. pages in the last hour)."""

    def __init__(self, window_s: float, limit: int | None, clock: Clock = time.time):
        self.window = window_s
        self.limit = limit
        self.clock = clock
        self.events: deque[float] = deque()

    def preload(self, timestamps: Iterable[float]) -> None:
        for ts in sorted(timestamps):
            self.events.append(ts)
        self._trim()

    def _trim(self) -> None:
        cutoff = self.clock() - self.window
        while self.events and self.events[0] <= cutoff:
            self.events.popleft()

    def count(self) -> int:
        self._trim()
        return len(self.events)

    def wait_time(self) -> float:
        """Seconds until one more event is allowed (0 if allowed now)."""
        if self.limit is None:
            return 0.0
        self._trim()
        if len(self.events) < self.limit:
            return 0.0
        # The oldest event that must expire to make room.
        oldest = self.events[len(self.events) - self.limit]
        return max(0.0, oldest + self.window - self.clock())

    def record(self, ts: float | None = None) -> None:
        self.events.append(self.clock() if ts is None else ts)


class DomainLimiter:
    """Per-domain politeness: request rate (token bucket) and concurrency cap."""

    def __init__(self, default: RateSpec, overrides: dict[str, RateSpec] | None = None, clock: Clock = time.monotonic):
        self.default = default
        self.overrides = overrides or {}
        self.clock = clock
        self._buckets: dict[str, TokenBucket] = {}
        self._semaphores: dict[str, asyncio.Semaphore] = {}

    def spec(self, domain: str) -> RateSpec:
        if domain in self.overrides:
            return self.overrides[domain]
        # Allow overrides on a parent domain ("example.com" covers "www.example.com").
        for key, spec in self.overrides.items():
            if domain.endswith("." + key):
                return spec
        return self.default

    def _bucket(self, domain: str) -> TokenBucket:
        if domain not in self._buckets:
            spec = self.spec(domain)
            self._buckets[domain] = TokenBucket(spec.requests_per_minute / 60.0, spec.burst, self.clock)
        return self._buckets[domain]

    def _semaphore(self, domain: str) -> asyncio.Semaphore:
        if domain not in self._semaphores:
            self._semaphores[domain] = asyncio.Semaphore(self.spec(domain).max_concurrency)
        return self._semaphores[domain]

    def pause(self, domain: str, seconds: float) -> None:
        log.warning("pausing domain %s for %.0fs", domain, seconds)
        self._bucket(domain).pause(seconds)

    @asynccontextmanager
    async def slot(self, domain: str, stop: asyncio.Event | None = None) -> AsyncIterator[None]:
        """Hold a concurrency slot and spend one rate token for ``domain``."""
        semaphore = self._semaphore(domain)
        async with semaphore:
            bucket = self._bucket(domain)
            while True:
                wait = bucket.try_acquire()
                if wait <= 0:
                    break
                if stop is not None and stop.is_set():
                    raise StopRequested()
                await asyncio.sleep(min(wait, 5.0))
            yield


class CircuitBreaker:
    """Fleet-wide brake: if too many recent fetches were blocked, pause everyone.

    Hammering a site that has started blocking burns proxies and identities;
    backing off lets reputation scores recover.
    """

    def __init__(self, config: CircuitBreakerConfig, clock: Clock = time.monotonic):
        self.config = config
        self.clock = clock
        self.results: deque[bool] = deque(maxlen=config.window)
        self.open_until = 0.0
        self.trips = 0

    def record(self, blocked: bool) -> bool:
        """Record an outcome. Returns True if this call tripped the breaker."""
        self.results.append(blocked)
        if self.is_open():
            return False
        if len(self.results) >= self.config.min_samples:
            ratio = sum(self.results) / len(self.results)
            if ratio >= self.config.block_ratio:
                self.open_until = self.clock() + self.config.pause_s
                self.trips += 1
                self.results.clear()
                log.error(
                    "circuit breaker OPEN: %.0f%% of recent fetches blocked; pausing fleet for %.0fs",
                    ratio * 100,
                    self.config.pause_s,
                )
                return True
        return False

    def is_open(self) -> bool:
        return self.clock() < self.open_until

    def remaining(self) -> float:
        return max(0.0, self.open_until - self.clock())

    async def wait_closed(self, stop: asyncio.Event | None = None) -> None:
        while self.is_open():
            if stop is not None and stop.is_set():
                return
            await asyncio.sleep(min(self.remaining(), 5.0))
