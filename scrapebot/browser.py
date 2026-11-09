"""One browser session = one identity (profile + proxy + cookie jar)."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass
from typing import Any

from .config import Config
from .detection import Detector, Outcome, Verdict
from .engine import Engine
from .human import Humanizer
from .identity import Identity
from .profiles import BrowserVersion, ResolvedProfile
from .stealth import StealthKit

log = logging.getLogger(__name__)

_PROXY_ERRORS = ("ERR_PROXY", "ERR_TUNNEL", "ERR_SOCKS", "ERR_NO_SUPPORTED_PROXIES", "ERR_HTTP_RESPONSE_CODE_FAILURE")


def _log_future_error(future: asyncio.Future) -> None:
    if not future.cancelled() and future.exception() is not None:
        log.debug("page preparation failed: %s", future.exception())


@dataclass
class FetchResult:
    requested_url: str
    final_url: str
    status: int | None
    verdict: Verdict
    html: str
    elapsed_ms: int
    proxy_error: bool = False


def launch_options(config: Config, kit: StealthKit | None, proxy: dict[str, str] | None) -> dict[str, Any]:
    browser = config.browser
    args = (kit.launch_args() if kit else []) + list(browser.args)
    options: dict[str, Any] = {
        "headless": browser.headless,
        "args": args,
        "ignore_default_args": list(browser.ignore_default_args),
    }
    if browser.channel:
        options["channel"] = browser.channel
    if browser.executable_path:
        options["executable_path"] = browser.executable_path
    if proxy:
        options["proxy"] = proxy
    return options


async def detect_browser_version(engine: Engine, playwright: Any, config: Config) -> BrowserVersion:
    if config.browser.version != "auto":
        return BrowserVersion.parse(config.browser.version)
    browser = await playwright.chromium.launch(**launch_options(config, None, None))
    try:
        return BrowserVersion.parse(browser.version)
    finally:
        await browser.close()


class BrowserSession:
    def __init__(
        self,
        engine: Engine,
        playwright: Any,
        config: Config,
        identity: Identity,
        profile: ResolvedProfile,
        detector: Detector,
        rng: random.Random,
    ):
        self.engine = engine
        self.playwright = playwright
        self.config = config
        self.identity = identity
        self.profile = profile
        self.detector = detector
        self.rng = rng
        self.kit = StealthKit(config.stealth, profile, headless_shell=config.browser.uses_headless_shell)
        self.browser: Any = None
        self.context: Any = None
        self.page: Any = None
        self.pages_fetched = 0
        self.started_at = 0.0
        self._page_ready: dict[Any, asyncio.Future] = {}
        self._cdp_sessions: dict[Any, Any] = {}
        self._last_nav_status: dict[Any, int] = {}
        vp = profile.config.viewport
        self.human = Humanizer(config.behavior, rng, (vp.width, vp.height))

    # ---------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        cfg = self.config
        self.browser = await self.playwright.chromium.launch(
            **launch_options(cfg, self.kit, self.identity.proxy.playwright_proxy)
        )
        options = self.profile.context_options()
        if cfg.browser.persist_storage and self.identity.storage_path.exists():
            options["storage_state"] = str(self.identity.storage_path)
        self.context = await self.browser.new_context(**options)
        self.context.set_default_navigation_timeout(cfg.browser.navigation_timeout_s * 1000)
        self.context.set_default_timeout(cfg.browser.navigation_timeout_s * 1000)
        await self.kit.apply_to_context(self.context)
        if cfg.browser.block_resources:
            blocked = set(cfg.browser.block_resources)

            async def _route(route: Any) -> None:
                if route.request.resource_type in blocked:
                    await route.abort()
                else:
                    await route.continue_()

            await self.context.route("**/*", _route)
        # Popups / target=_blank pages need the same CDP overrides.
        self.context.on("page", lambda page: asyncio.ensure_future(self._prepare_page(page)))
        self.page = await self.context.new_page()
        await self._prepare_page(self.page)
        self.started_at = time.monotonic()
        log.info(
            "session started: identity=%s exit=%s tz=%s ua=Chrome/%s",
            self.identity.key,
            self.identity.proxy.public_ip or self.identity.proxy.kind,
            self.profile.timezone,
            self.profile.version.major,
        )

    def _prepare_page(self, page: Any) -> asyncio.Future:
        future = self._page_ready.get(page)
        if future is None:
            future = asyncio.ensure_future(self._apply_page_overrides(page))
            future.add_done_callback(_log_future_error)
            self._page_ready[page] = future
        return future

    async def _apply_page_overrides(self, page: Any) -> None:
        session = await self.kit.apply_to_page(self.context, page)
        if session is not None:
            self._cdp_sessions[page] = session  # keep alive: overrides die with the session

        def on_response(response: Any) -> None:
            try:
                if response.request.is_navigation_request() and response.frame == page.main_frame:
                    self._last_nav_status[page] = response.status
            except Exception:  # page closing
                pass

        page.on("response", on_response)
        page.on("close", lambda _: self._forget(page))

    def _forget(self, page: Any) -> None:
        self._page_ready.pop(page, None)
        self._cdp_sessions.pop(page, None)
        self._last_nav_status.pop(page, None)

    async def close(self, *, save_storage: bool = True) -> None:
        try:
            if self.context is not None and save_storage and self.config.browser.persist_storage:
                self.identity.storage_path.parent.mkdir(parents=True, exist_ok=True)
                await self.context.storage_state(path=str(self.identity.storage_path))
        except Exception as exc:
            log.debug("could not save storage state for %s: %s", self.identity.key, exc)
        for closer in (self.context, self.browser):
            if closer is None:
                continue
            try:
                await closer.close()
            except Exception as exc:
                log.debug("error while closing browser: %s", exc)
        self.context = self.browser = self.page = None

    # ------------------------------------------------------------------- fetch
    async def _html(self) -> str:
        for _ in range(3):
            try:
                return await self.page.content()
            except self.engine.Error:
                # "Execution context was destroyed": a client-side navigation is in flight.
                await asyncio.sleep(0.5)
        return ""

    async def _resolve_challenge(self, verdict: Verdict, status: int | None) -> tuple[Verdict, int | None, str]:
        """Give an interstitial JS challenge time to solve itself and redirect."""
        deadline = time.monotonic() + self.config.detection.challenge_wait_s
        html = ""
        log.info("[%s] challenge detected (%s); waiting up to %.0fs", self.identity.key, verdict.reason, self.config.detection.challenge_wait_s)
        while time.monotonic() < deadline:
            await asyncio.sleep(self.rng.uniform(0.8, 1.6))
            try:
                await self.page.wait_for_load_state("domcontentloaded", timeout=5000)
            except self.engine.Error:
                pass
            html = await self._html()
            if self.detector.find_marker(html) is None:
                status = self._last_nav_status.get(self.page, status)
                new_verdict = self.detector.classify(status, html)
                log.info("[%s] challenge cleared -> %s", self.identity.key, new_verdict.outcome.value)
                return new_verdict, status, html
        return Verdict(Outcome.CHALLENGE, f"unresolved after wait: {verdict.reason}"), status, html

    async def fetch(
        self,
        url: str,
        *,
        referer: str | None = None,
        wait_for: str | None = None,
        wait_for_timeout_s: float = 15.0,
        humanize: bool = True,
    ) -> FetchResult:
        cfg = self.config
        page = self.page
        start = time.monotonic()
        status: int | None = None
        headers: dict[str, str] = {}
        try:
            response = await page.goto(url, wait_until=cfg.browser.wait_until, referer=referer)
            if response is not None:
                status = response.status
                headers = await response.all_headers()
        except self.engine.TimeoutError as exc:
            return self._failure(url, start, "navigation timeout: " + str(exc).splitlines()[0])
        except self.engine.Error as exc:
            message = str(exc).splitlines()[0]
            return self._failure(url, start, message, proxy_error=any(code in message for code in _PROXY_ERRORS))
        self.pages_fetched += 1

        html = await self._html()
        verdict = self.detector.classify(status, html, headers)
        if verdict.outcome is Outcome.CHALLENGE and cfg.detection.challenge_wait_s > 0:
            verdict, status, html = await self._resolve_challenge(verdict, status)

        if verdict.outcome is Outcome.OK:
            if wait_for:
                try:
                    await page.wait_for_selector(wait_for, timeout=wait_for_timeout_s * 1000, state="attached")
                except self.engine.Error:
                    # Expected content missing: often a soft block / empty shell.
                    marker = self.detector.find_marker(await self._html())
                    if marker:
                        verdict = Verdict(marker[0], f"marker {marker[1]!r} after load")
                    else:
                        verdict = Verdict(Outcome.SERVER_ERROR, f"wait_for selector {wait_for!r} not found")
            if verdict.outcome is Outcome.OK and humanize:
                try:
                    await self.human.perform(page)
                except self.engine.Error as exc:
                    log.debug("humanize interrupted: %s", exc)
            if verdict.outcome is Outcome.OK:
                html = await self._html()

        return FetchResult(
            requested_url=url,
            final_url=page.url,
            status=status,
            verdict=verdict,
            html=html,
            elapsed_ms=int((time.monotonic() - start) * 1000),
        )

    def _failure(self, url: str, start: float, reason: str, proxy_error: bool = False) -> FetchResult:
        return FetchResult(
            requested_url=url,
            final_url=url,
            status=None,
            verdict=Verdict(Outcome.NETWORK_ERROR, reason),
            html="",
            elapsed_ms=int((time.monotonic() - start) * 1000),
            proxy_error=proxy_error,
        )
