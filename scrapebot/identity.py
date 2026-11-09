"""Identities: a browser profile bound to a proxy, leased to one worker at a time.

An identity is what the target site perceives as "one user": a fingerprint
(profile), an exit IP (proxy) and a cookie jar (storage state). Keeping the
three stable together — and retiring them together when blocked — is what
keeps per-user reputation scores healthy.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config, ProfileConfig
from .limits import RollingCounter
from .proxy import ProxyPool, ProxyState

log = logging.getLogger(__name__)


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name)


@dataclass(eq=False)
class Identity:
    profile: ProfileConfig
    proxy: ProxyState
    storage_dir: Path
    hourly: RollingCounter
    cooldown_until: float = 0.0
    leased: bool = False
    pages: int = 0
    blocks: int = 0
    sessions: int = 0
    last_used: float = field(default=0.0)

    @property
    def key(self) -> str:
        return f"{self.profile.name}@{self.proxy.name}"

    @property
    def storage_path(self) -> Path:
        return self.storage_dir / f"{_safe(self.key)}.json"

    def clear_storage(self) -> None:
        try:
            self.storage_path.unlink()
        except FileNotFoundError:
            pass


class IdentityManager:
    def __init__(
        self,
        config: Config,
        pool: ProxyPool,
        rng: random.Random,
        hourly_history: dict[str, list[float]] | None = None,
        clock=time.time,
    ):
        self.config = config
        self.pool = pool
        self.rng = rng
        self.clock = clock
        self.max_per_proxy = config.proxies.max_sessions_per_proxy
        storage_dir = config.run.data_dir / "identities"
        storage_dir.mkdir(parents=True, exist_ok=True)
        self.identities: list[Identity] = []
        for proxy in pool.states:
            for profile in config.profiles:
                counter = RollingCounter(3600, config.limits.per_identity_pages_per_hour, clock)
                identity = Identity(profile=profile, proxy=proxy, storage_dir=storage_dir, hourly=counter)
                counter.preload((hourly_history or {}).get(identity.key, []))
                self.identities.append(identity)
        self._cond = asyncio.Condition()
        self._last_wait_log = 0.0

    def _available(self, now: float) -> list[Identity]:
        return [
            i
            for i in self.identities
            if not i.leased
            and i.cooldown_until <= now
            and i.proxy.healthy
            and i.proxy.active_sessions < self.max_per_proxy
            and i.hourly.wait_time() == 0
        ]

    def _next_change_in(self, now: float) -> float:
        waits = [i.cooldown_until - now for i in self.identities if i.cooldown_until > now]
        waits += [w for w in (i.hourly.wait_time() for i in self.identities) if w > 0]
        return min(waits) if waits else 30.0

    def _pick(self, candidates: list[Identity]) -> Identity:
        # Spread load over exit IPs first, then pick a profile by weight,
        # preferring the identity that has rested longest.
        least = min(c.proxy.active_sessions for c in candidates)
        pool = [c for c in candidates if c.proxy.active_sessions == least]
        weights = [c.profile.weight * (1.0 + min(self.clock() - c.last_used, 3600) / 3600) for c in pool]
        return self.rng.choices(pool, weights=weights, k=1)[0]

    async def acquire(self, stop: asyncio.Event) -> Identity | None:
        async with self._cond:
            while not stop.is_set():
                now = self.clock()
                candidates = self._available(now)
                if candidates:
                    identity = self._pick(candidates)
                    identity.leased = True
                    identity.sessions += 1
                    identity.proxy.active_sessions += 1
                    return identity
                wait = max(1.0, min(self._next_change_in(now), 30.0))
                if now - self._last_wait_log > 60:
                    self._last_wait_log = now
                    log.warning(
                        "no identity available (cooling down, rate-limited or proxies unhealthy); "
                        "waiting up to %.0fs",
                        wait,
                    )
                try:
                    await asyncio.wait_for(self._cond.wait(), timeout=wait)
                except asyncio.TimeoutError:
                    pass
        return None

    async def release(self, identity: Identity, *, cooldown_s: float = 0.0, clear_storage: bool = False) -> None:
        async with self._cond:
            identity.leased = False
            identity.last_used = self.clock()
            identity.proxy.active_sessions = max(0, identity.proxy.active_sessions - 1)
            if cooldown_s > 0:
                identity.cooldown_until = self.clock() + cooldown_s
                log.info("identity %s cooling down for %.0fs", identity.key, cooldown_s)
            if clear_storage:
                identity.clear_storage()
            self._cond.notify_all()

    async def notify(self) -> None:
        async with self._cond:
            self._cond.notify_all()

    def snapshot(self) -> list[dict]:
        now = self.clock()
        return [
            {
                "identity": i.key,
                "leased": i.leased,
                "pages": i.pages,
                "blocks": i.blocks,
                "pages_last_hour": i.hourly.count(),
                "cooldown_s": max(0, round(i.cooldown_until - now)),
            }
            for i in self.identities
        ]
