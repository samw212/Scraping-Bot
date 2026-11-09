"""The orchestrator: wires shared services together and runs the worker fleet.

Shared, fleet-wide state lives here:
  * the persistent job queue and output sink,
  * the Gluetun proxy pool and the identity manager,
  * scrape limits (per-domain rate/concurrency, hourly/daily/total budgets),
  * the circuit breaker that pauses everyone when blocks spike.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import signal
import time
from collections import Counter
from pathlib import Path
from typing import Any

from .browser import FetchResult, detect_browser_version
from .config import Config
from .detection import Detector, Outcome
from .engine import Engine, load_engine
from .identity import Identity, IdentityManager
from .limits import CircuitBreaker, DomainLimiter, RollingCounter
from .proxy import ProxyPool
from .robots import RobotsCache
from .storage import Job, JsonlSink, Store
from .worker import Worker

log = logging.getLogger(__name__)


def read_url_file(path: Path) -> list[str]:
    urls = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            urls.append(line)
    return urls


class Orchestrator:
    def __init__(self, config: Config, *, engine: Engine | None = None, proxy_transport: Any = None):
        self.config = config
        self.engine = engine or load_engine(config.browser.engine)
        self.proxy_transport = proxy_transport
        self.rng = random.Random(config.run.seed)
        self.stop_event = asyncio.Event()
        self.detector = Detector(config.detection)
        self.breaker = CircuitBreaker(config.limits.circuit_breaker)
        self.domains = DomainLimiter(config.limits.per_domain, config.limits.domain_overrides)
        self.robots = RobotsCache(config.limits.robots_user_agent) if config.limits.respect_robots_txt else None
        self.outcomes: Counter[str] = Counter()
        self.items_written = 0
        self.pages_reserved = 0
        self.started_at = 0.0
        self._last_budget_log = 0.0
        self.stop_reason: str | None = None
        # Populated in run()
        self.store: Store
        self.sink: JsonlSink
        self.pool: ProxyPool
        self.identities: IdentityManager
        self.hourly: RollingCounter
        self.daily: RollingCounter
        self.playwright: Any = None
        self.browser_version: Any = None

    # ------------------------------------------------------------- control
    def request_stop(self, reason: str = "stop requested") -> None:
        if not self.stop_event.is_set():
            self.stop_reason = reason
            log.warning("stopping: %s (workers finish their current page)", reason)
            self.stop_event.set()

    async def sleep(self, seconds: float) -> None:
        """Sleep that wakes up early on shutdown."""
        if seconds <= 0:
            return
        try:
            await asyncio.wait_for(self.stop_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    # ------------------------------------------------------------- budgets
    async def acquire_budget(self) -> bool:
        """Reserve one page load against fleet-wide limits.

        Returns False when the run must stop (total budget spent or shutdown).
        Check-and-record happens without an ``await`` in between, so concurrent
        workers can never overshoot a limit.
        """
        limits = self.config.limits
        while not self.stop_event.is_set():
            if limits.max_pages_total is not None and self.pages_reserved >= limits.max_pages_total:
                self.request_stop(f"max_pages_total={limits.max_pages_total} reached")
                return False
            wait = max(self.hourly.wait_time(), self.daily.wait_time())
            if wait <= 0:
                self.pages_reserved += 1
                self.hourly.record()
                self.daily.record()
                return True
            now = time.monotonic()
            if now - self._last_budget_log > 60:
                self._last_budget_log = now
                log.warning("fleet page limit reached (hourly/daily); next slot in %.0fs", wait)
            await self.sleep(min(wait, 30.0))
        return False

    def record_fetch(self, result: FetchResult, identity: Identity, job: Job | None) -> None:
        outcome = result.verdict.outcome
        self.outcomes[outcome.value] += 1
        self.store.log_fetch(
            url=result.requested_url,
            outcome=outcome.value,
            job_id=job.id if job else None,
            identity=identity.key,
            proxy=identity.proxy.name,
            exit_ip=identity.proxy.public_ip,
            http_status=result.status,
            reason=result.verdict.reason,
            elapsed_ms=result.elapsed_ms,
        )
        if outcome is not Outcome.NETWORK_ERROR:
            self.breaker.record(outcome.is_block)

    # ---------------------------------------------------------------- setup
    def seed(self) -> int:
        added = 0
        for target in self.config.targets:
            urls = list(target.start_urls)
            if target.url_file:
                urls += read_url_file(target.url_file)
            if urls:
                added += self.store.add_jobs(urls, target.name)
        return added

    def _open_state(self) -> None:
        cfg = self.config
        cfg.run.data_dir.mkdir(parents=True, exist_ok=True)
        self.store = Store(cfg.run.data_dir / "state.db")
        recovered = self.store.reset_in_progress()
        if recovered:
            log.info("recovered %d interrupted jobs from a previous run", recovered)
        seeded = self.seed()
        log.info("queue: %d new start URLs; %s", seeded, self.store.counts())
        self.sink = JsonlSink(cfg.output_dir)
        now = time.time()
        self.hourly = RollingCounter(3600, cfg.limits.max_pages_per_hour)
        self.hourly.preload(self.store.fetch_times(now - 3600))
        self.daily = RollingCounter(86400, cfg.limits.max_pages_per_day)
        self.daily.preload(self.store.fetch_times(now - 86400))

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._on_signal, sig)
            except (NotImplementedError, RuntimeError):  # Windows / non-main thread
                pass

    def _on_signal(self, sig: signal.Signals) -> None:
        if self.stop_event.is_set():
            log.error("second %s: forcing shutdown", sig.name)
            for task in asyncio.all_tasks():
                if task.get_name().startswith("worker-"):
                    task.cancel()
        else:
            self.request_stop(f"received {sig.name}")

    # ------------------------------------------------------------------ run
    async def run(self) -> dict[str, Any]:
        cfg = self.config
        self.started_at = time.monotonic()
        self._open_state()
        self.pool = ProxyPool(cfg.proxies, self.proxy_transport)
        aux: list[asyncio.Task] = []
        try:
            await self.pool.check_all()
            for state in self.pool.states:
                log.info(
                    "proxy %-12s %-8s healthy=%s exit=%s country=%s tz=%s",
                    state.name, state.kind, state.healthy, state.public_ip, state.country, state.timezone,
                )
            if not self.pool.healthy():
                raise RuntimeError("no healthy proxies; refusing to start (see `scrapebot proxies`)")
            self.identities = IdentityManager(
                cfg, self.pool, self.rng, self.store.fetch_times_by_identity(time.time() - 3600)
            )
            async with self.engine.async_playwright() as playwright:
                self.playwright = playwright
                self.browser_version = await detect_browser_version(self.engine, playwright, cfg)
                log.info(
                    "engine=%s browser=%s workers=%d identities=%d",
                    self.engine.name, self.browser_version.full, cfg.orchestrator.workers, len(self.identities.identities),
                )
                self._install_signal_handlers()
                aux = [
                    asyncio.create_task(self._stats_loop(), name="stats"),
                    asyncio.create_task(self.pool.health_loop(self.stop_event, self.identities.notify), name="proxy-health"),
                ]
                workers = [
                    asyncio.create_task(Worker(i, self).run(), name=f"worker-{i}")
                    for i in range(cfg.orchestrator.workers)
                ]
                await self._supervise(workers)
        finally:
            self.stop_event.set()
            for task in aux:
                task.cancel()
            await asyncio.gather(*aux, return_exceptions=True)
            await self.pool.aclose()
            summary = self.summary()
            self._write_stats(summary)
            if hasattr(self, "sink"):
                self.sink.close()
            self.store.close()
        log.info("run finished: %s", json.dumps(summary["outcomes"]))
        return summary

    async def _supervise(self, workers: list[asyncio.Task]) -> None:
        all_done = asyncio.gather(*workers, return_exceptions=True)
        stop_wait = asyncio.create_task(self.stop_event.wait())
        await asyncio.wait({all_done, stop_wait}, return_when=asyncio.FIRST_COMPLETED)
        if not all_done.done():
            grace = self.config.orchestrator.shutdown_grace_s
            try:
                await asyncio.wait_for(asyncio.shield(all_done), timeout=grace)
            except asyncio.TimeoutError:
                log.error("workers did not stop within %.0fs; cancelling", grace)
                for task in workers:
                    task.cancel()
        stop_wait.cancel()
        for result in await all_done:
            if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                log.error("worker crashed: %r", result)

    # ---------------------------------------------------------------- stats
    def summary(self) -> dict[str, Any]:
        store_counts = self.store.counts() if hasattr(self, "store") else {}
        return {
            "run": self.config.run.name,
            "elapsed_s": round(time.monotonic() - self.started_at, 1),
            "stop_reason": self.stop_reason,
            "outcomes": dict(self.outcomes),
            "items_written": self.items_written,
            "pages_reserved": self.pages_reserved,
            "pages_last_hour": self.hourly.count() if hasattr(self, "hourly") else 0,
            "queue": store_counts,
            "circuit_breaker": {"open": self.breaker.is_open(), "remaining_s": round(self.breaker.remaining()), "trips": self.breaker.trips},
            "proxies": [s.snapshot() for s in self.pool.states] if hasattr(self, "pool") else [],
            "identities": self.identities.snapshot() if hasattr(self, "identities") else [],
        }

    def _write_stats(self, summary: dict[str, Any]) -> None:
        path = self.config.run.data_dir / "stats.json"
        try:
            path.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
        except OSError as exc:
            log.debug("could not write stats: %s", exc)

    async def _stats_loop(self) -> None:
        while not self.stop_event.is_set():
            await self.sleep(self.config.run.stats_interval_s)
            summary = self.summary()
            self._write_stats(summary)
            q = summary["queue"]
            log.info(
                "stats: outcomes=%s items=%d | queue pending=%d active=%d done=%d failed=%d | last hour=%d | breaker=%s",
                summary["outcomes"],
                summary["items_written"],
                q.get("pending", 0),
                q.get("in_progress", 0),
                q.get("done", 0),
                q.get("failed", 0),
                summary["pages_last_hour"],
                f"OPEN {summary['circuit_breaker']['remaining_s']}s" if summary["circuit_breaker"]["open"] else "closed",
            )
