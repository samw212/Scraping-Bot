"""A worker bot: owns one browser session at a time and processes jobs."""

from __future__ import annotations

import logging
import random
import time
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from .browser import BrowserSession, FetchResult
from .config import TargetConfig
from .detection import Outcome
from .extract import ExtractContext, extract_links, run_extractors
from .limits import StopRequested
from .profiles import resolve_profile
from .storage import Job

if TYPE_CHECKING:
    from .orchestrator import Orchestrator

log = logging.getLogger(__name__)


class Worker:
    def __init__(self, index: int, orchestrator: Orchestrator):
        self.name = f"worker-{index}"
        self.orch = orchestrator
        self.config = orchestrator.config
        self.rng = random.Random(orchestrator.rng.random())
        self.session: BrowserSession | None = None

    # ------------------------------------------------------------------ loop
    async def run(self) -> None:
        orch = self.orch
        try:
            while not orch.stop_event.is_set():
                await orch.breaker.wait_closed(orch.stop_event)
                if orch.stop_event.is_set():
                    break
                job = orch.store.claim()
                if job is None:
                    if orch.store.is_drained():
                        log.info("%s: queue drained", self.name)
                        break
                    await orch.sleep(self.config.orchestrator.idle_poll_s)
                    continue
                try:
                    if not await self.process(job):
                        break
                except StopRequested:
                    orch.store.release(job)
                    break
                except Exception as exc:  # keep the fleet alive on unexpected errors
                    log.exception("%s: unexpected error on %s", self.name, job.url)
                    orch.store.retry(job, self._backoff(job), f"internal error: {exc}", self.config.limits.max_retries)
                    await self.close_session(burned=False, save_storage=False)
        finally:
            await self.close_session()

    def _backoff(self, job: Job) -> float:
        base = self.config.limits.retry_backoff_s
        return base * (2 ** min(job.attempts, 6)) * self.rng.uniform(0.8, 1.25)

    # --------------------------------------------------------------- session
    async def ensure_session(self) -> BrowserSession | None:
        session = self.session
        if session is not None:
            identity = session.identity
            reason = None
            if session.pages_fetched >= self.config.limits.pages_per_session:
                reason = "session page budget reached"
            elif identity.hourly.wait_time() > 0:
                reason = "identity hourly limit reached"
            elif not identity.proxy.healthy:
                reason = "proxy unhealthy"
            elif self.orch.pool.needs_scheduled_rotation(identity.proxy):
                reason = "scheduled IP rotation"
            if reason is None:
                return session
            log.info("%s: recycling session %s (%s)", self.name, identity.key, reason)
            await self.close_session()

        # Loop until a session survives warm-up. Each failed warm-up benches
        # an identity and feeds the circuit breaker, so this cannot hammer.
        while not self.orch.stop_event.is_set():
            await self.orch.breaker.wait_closed(self.orch.stop_event)
            identity = await self.orch.identities.acquire(self.orch.stop_event)
            if identity is None:
                return None
            profile = resolve_profile(
                identity.profile,
                self.orch.browser_version,
                timezone=identity.proxy.timezone,
                identity_key=identity.key,
            )
            session = BrowserSession(
                self.orch.engine,
                self.orch.playwright,
                self.config,
                identity,
                profile,
                self.orch.detector,
                random.Random(self.rng.random()),
            )
            try:
                await session.start()
            except Exception:
                await session.close(save_storage=False)
                await self.orch.identities.release(identity, cooldown_s=60)
                raise
            self.session = session
            if await self._warmup(session):
                return session
        return None

    async def _warmup(self, session: BrowserSession) -> bool:
        """Visit warm-up URLs. Returns False if the identity got blocked doing so."""
        for url in self.config.behavior.warmup_urls:
            result = await self._fetch(session, url, job=None)
            if result is None:
                raise StopRequested()
            if result.verdict.outcome.is_block:
                log.warning("%s: blocked during warm-up on %s (%s)", self.name, url, result.verdict.reason)
                session.identity.blocks += 1
                await self.close_session(burned=True)
                return False
            await self.orch.sleep(self.rng.uniform(*self.config.limits.delay_s))
        return True

    async def close_session(self, *, burned: bool = False, save_storage: bool = True) -> None:
        session, self.session = self.session, None
        if session is None:
            return
        identity = session.identity
        clear = burned and self.config.browser.clear_storage_on_block
        await session.close(save_storage=save_storage and not clear)
        pool = self.orch.pool
        proxy = identity.proxy
        if burned and self.config.proxies.rotate_on_block:
            await pool.rotate(proxy, "blocked")
        elif pool.needs_scheduled_rotation(proxy) and proxy.active_sessions <= 1:
            await pool.rotate(proxy, f"{proxy.pages_since_rotation} pages since last rotation")
        await self.orch.identities.release(
            identity,
            cooldown_s=self.config.limits.identity_cooldown_s if burned else 0.0,
            clear_storage=clear,
        )

    # ----------------------------------------------------------------- fetch
    async def _fetch(self, session: BrowserSession, url: str, *, job: Job | None, target: TargetConfig | None = None) -> FetchResult | None:
        orch = self.orch
        domain = urlsplit(url).hostname or ""
        async with orch.domains.slot(domain, orch.stop_event):
            if not await orch.acquire_budget():
                return None
            session.identity.hourly.record()
            result = await session.fetch(
                url,
                referer=job.parent_url if job else None,
                wait_for=target.wait_for if target else None,
                wait_for_timeout_s=target.wait_for_timeout_s if target else 15.0,
                humanize=self.config.behavior.humanize,
            )
        orch.record_fetch(result, session.identity, job)
        return result

    async def process(self, job: Job) -> bool:
        """Process one job. Returns False when the worker should stop."""
        orch = self.orch
        target = self.config.target(job.target)
        session = await self.ensure_session()
        if session is None:
            orch.store.release(job)
            return False
        if orch.robots is not None and not await orch.robots.allowed(session.context.request, job.url):
            orch.store.fail(job, "disallowed by robots.txt")
            log.info("%s SKIP %s (disallowed by robots.txt)", self.name, job.url)
            return True
        result = await self._fetch(session, job.url, job=job, target=target)
        if result is None:  # fleet budget exhausted or stopping
            orch.store.release(job)
            return False
        await self._handle(job, target, session, result)
        await orch.sleep(self.rng.uniform(*self.config.limits.delay_s))
        return True

    async def _handle(self, job: Job, target: TargetConfig, session: BrowserSession, result: FetchResult) -> None:
        orch = self.orch
        limits = self.config.limits
        identity = session.identity
        verdict = result.verdict
        outcome = verdict.outcome
        tag = f"{self.name} [{identity.key}]"

        if outcome is Outcome.OK:
            identity.pages += 1
            orch.pool.report_success(identity.proxy)
            ctx = ExtractContext(url=result.final_url, html=result.html, target=target, page=session.page, job=job)
            items = await run_extractors(ctx)
            if self.config.output.include_metadata:
                now = time.time()
                for item in items:
                    item.setdefault("_url", result.final_url)
                    item.setdefault("_target", target.name)
                    item.setdefault("_scraped_at", now)
            orch.sink.write(target.name, items)
            orch.items_written += len(items)
            new_links = self._discover(ctx, job, target)
            orch.store.complete(job)
            log.info("%s OK %s items=%d new=%d %dms", tag, job.url, len(items), new_links, result.elapsed_ms)
            return

        if outcome is Outcome.NOT_FOUND:
            orch.store.fail(job, verdict.reason)
            log.info("%s NOT FOUND %s (%s)", tag, job.url, verdict.reason)
            return

        if outcome is Outcome.RATE_LIMITED:
            pause = verdict.retry_after_s or limits.rate_limit_pause_s
            orch.domains.pause(urlsplit(job.url).hostname or "", pause)
            orch.store.retry(job, pause, verdict.reason, limits.max_retries)
            log.warning("%s RATE LIMITED %s; domain paused %.0fs", tag, job.url, pause)
            return

        if outcome is Outcome.SERVER_ERROR:
            delay = verdict.retry_after_s or self._backoff(job)
            retried = orch.store.retry(job, delay, verdict.reason, limits.max_retries)
            log.warning("%s %s %s (%s)", tag, "RETRY" if retried else "GAVE UP", job.url, verdict.reason)
            return

        if outcome is Outcome.NETWORK_ERROR:
            orch.pool.report_failure(identity.proxy)
            retried = orch.store.retry(job, self._backoff(job), verdict.reason, limits.max_retries)
            log.warning("%s NETWORK ERROR %s (%s)%s", tag, job.url, verdict.reason, "" if retried else " — giving up")
            await self.close_session(save_storage=False)
            return

        # CHALLENGE / BLOCKED: this identity + exit IP is burned for now.
        identity.blocks += 1
        retried = orch.store.retry(job, self._backoff(job), f"{outcome.value}: {verdict.reason}", limits.max_retries)
        log.warning(
            "%s %s %s (%s)%s",
            tag,
            outcome.value.upper(),
            job.url,
            verdict.reason,
            "" if retried else " — giving up on URL",
        )
        await self.close_session(burned=True)

    def _discover(self, ctx: ExtractContext, job: Job, target: TargetConfig) -> int:
        store = self.orch.store
        added = 0
        if target.next_page and job.page_no < target.next_page.max_pages:
            links = extract_links(ctx, target.next_page.selector, same_domain=True, allowed_domains=target.allowed_domains, limit=1)
            for link in links:
                added += store.add_job(link, target.name, depth=job.depth, page_no=job.page_no + 1, parent_url=ctx.url)
        if target.follow and job.depth < target.follow.max_depth:
            follow = target.follow
            links = extract_links(
                ctx,
                follow.selector,
                same_domain=follow.same_domain,
                allowed_domains=target.allowed_domains,
                limit=follow.limit_per_page,
            )
            added += store.add_jobs(links, follow.target or target.name, depth=job.depth + 1, parent_url=ctx.url)
        return added
