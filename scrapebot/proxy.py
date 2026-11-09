"""Proxy pool backed by Gluetun VPN containers.

Each Gluetun container exposes:
  * an HTTP proxy (``HTTPPROXY=on``, default ``:8888``) that tunnels traffic
    through its VPN connection — this is what Chromium is pointed at;
  * a control server (``:8000``) used here to read the exit IP / geo data
    (``GET /v1/publicip/ip``) and to rotate the exit IP by restarting the VPN
    (``PUT /v1/vpn/status``), which makes Gluetun pick a new server from its
    ``SERVER_*`` filters.

Static proxies (any HTTP/SOCKS proxy URL) and an explicit "direct" mode are
also supported, but cannot rotate.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from .config import GluetunEndpoint, ProxiesConfig

log = logging.getLogger(__name__)


class GluetunError(RuntimeError):
    pass


class GluetunClient:
    """Minimal async client for the Gluetun HTTP control server."""

    def __init__(self, endpoint: GluetunEndpoint, timeout_s: float = 10.0, transport: httpx.AsyncBaseTransport | None = None):
        if not endpoint.control_url:
            raise ValueError(f"gluetun endpoint {endpoint.name!r} has no control_url")
        headers = {}
        if endpoint.api_key:
            headers["X-API-Key"] = endpoint.api_key
        auth = None
        if endpoint.control_username:
            auth = httpx.BasicAuth(endpoint.control_username, endpoint.control_password or "")
        self.name = endpoint.name
        self._client = httpx.AsyncClient(
            base_url=endpoint.control_url.rstrip("/"),
            headers=headers,
            auth=auth,
            timeout=timeout_s,
            transport=transport,
            # The control server lives on the private Docker network; never
            # route it through HTTP(S)_PROXY from the environment.
            trust_env=False,
        )

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = await self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise GluetunError(f"{self.name}: {method} {path} failed: {exc}") from exc
        if response.status_code in (401, 403):
            raise GluetunError(
                f"{self.name}: {method} {path} unauthorized (HTTP {response.status_code}); "
                "check api_key and the route list in gluetun's auth config.toml"
            )
        if response.status_code >= 400:
            raise GluetunError(f"{self.name}: {method} {path} -> HTTP {response.status_code}: {response.text[:200]}")
        try:
            return response.json()
        except ValueError:
            return {"raw": response.text}

    async def public_ip(self) -> dict[str, Any]:
        return await self._request("GET", "/v1/publicip/ip")

    async def vpn_status(self) -> str:
        data = await self._request("GET", "/v1/vpn/status")
        return str(data.get("status", "unknown"))

    async def set_vpn_status(self, status: str) -> dict[str, Any]:
        return await self._request("PUT", "/v1/vpn/status", json={"status": status})

    async def _wait_status(self, wanted: str, deadline: float, poll_s: float) -> None:
        while True:
            try:
                if await self.vpn_status() == wanted:
                    return
            except GluetunError:
                pass
            if time.monotonic() > deadline:
                raise GluetunError(f"{self.name}: VPN did not reach status {wanted!r} in time")
            await asyncio.sleep(poll_s)

    async def rotate(self, timeout_s: float = 120.0, poll_s: float = 2.0) -> dict[str, Any]:
        """Restart the VPN tunnel to obtain a new exit IP. Returns new public IP info."""
        deadline = time.monotonic() + timeout_s
        old_ip = None
        try:
            old_ip = (await self.public_ip()).get("public_ip") or None
        except GluetunError:
            pass
        await self.set_vpn_status("stopped")
        await self._wait_status("stopped", deadline, poll_s)
        await self.set_vpn_status("running")
        await self._wait_status("running", deadline, poll_s)
        info: dict[str, Any] = {}
        # Gluetun refreshes its public IP info asynchronously after reconnecting.
        while time.monotonic() < deadline:
            try:
                info = await self.public_ip()
            except GluetunError:
                info = {}
            ip = info.get("public_ip")
            if ip and ip != old_ip:
                return info
            await asyncio.sleep(poll_s)
        if info.get("public_ip"):
            log.warning("%s: VPN restarted but exit IP is unchanged (%s)", self.name, info.get("public_ip"))
            return info
        raise GluetunError(f"{self.name}: no public IP after VPN restart")

    async def aclose(self) -> None:
        await self._client.aclose()


@dataclass(eq=False)
class ProxyState:
    name: str
    kind: str  # "gluetun" | "static" | "direct"
    playwright_proxy: dict[str, str] | None
    gluetun: GluetunClient | None = None
    healthy: bool = True
    public_ip: str | None = None
    country: str | None = None
    city: str | None = None
    timezone: str | None = None
    failures: int = 0
    last_check: float = 0.0
    last_rotation: float = 0.0
    rotations: int = 0
    active_sessions: int = 0
    pages_since_rotation: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    def update_geo(self, info: dict[str, Any]) -> None:
        self.public_ip = info.get("public_ip") or self.public_ip
        self.country = info.get("country") or self.country
        self.city = info.get("city") or self.city
        # Recent Gluetun versions include the exit IP's timezone; it is used to
        # keep the browser clock consistent with the IP's location.
        self.timezone = info.get("timezone") or self.timezone

    def snapshot(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "healthy": self.healthy,
            "public_ip": self.public_ip,
            "country": self.country,
            "city": self.city,
            "timezone": self.timezone,
            "active_sessions": self.active_sessions,
            "rotations": self.rotations,
            "failures": self.failures,
        }


def _playwright_proxy(server: str, username: str | None, password: str | None) -> dict[str, str]:
    proxy = {"server": server}
    if username:
        proxy["username"] = username
        proxy["password"] = password or ""
    return proxy


class ProxyPool:
    def __init__(self, config: ProxiesConfig, transport: httpx.AsyncBaseTransport | None = None):
        self.config = config
        self.states: list[ProxyState] = []
        for endpoint in config.gluetun:
            client = GluetunClient(endpoint, config.request_timeout_s, transport) if endpoint.control_url else None
            self.states.append(
                ProxyState(
                    name=endpoint.name,
                    kind="gluetun",
                    playwright_proxy=_playwright_proxy(endpoint.proxy_url, endpoint.username, endpoint.password),
                    gluetun=client,
                )
            )
        for static in config.static:
            self.states.append(
                ProxyState(
                    name=static.name,
                    kind="static",
                    playwright_proxy=_playwright_proxy(static.url, static.username, static.password),
                    timezone=static.timezone,
                )
            )
        if not self.states and config.allow_direct:
            self.states.append(ProxyState(name="direct", kind="direct", playwright_proxy=None))

    def get(self, name: str) -> ProxyState:
        for state in self.states:
            if state.name == name:
                return state
        raise KeyError(name)

    def healthy(self) -> list[ProxyState]:
        return [s for s in self.states if s.healthy]

    async def check(self, state: ProxyState) -> bool:
        state.last_check = time.monotonic()
        if state.gluetun is None:
            # No control API: health is inferred from fetch failures only, so
            # give an unhealthy proxy another chance once per check interval.
            if not state.healthy:
                log.info("proxy %s re-enabled on probation", state.name)
                state.healthy = True
                state.failures = max(0, self.config.unhealthy_after_failures - 1)
            return True
        try:
            status = await state.gluetun.vpn_status()
            info = await state.gluetun.public_ip()
        except GluetunError as exc:
            if state.healthy:
                log.warning("proxy %s unhealthy: %s", state.name, exc)
            state.healthy = False
            return False
        state.update_geo(info)
        ok = status == "running" and bool(state.public_ip)
        if ok and not state.healthy:
            log.info("proxy %s recovered (exit %s)", state.name, state.public_ip)
        if ok:
            state.failures = 0
        state.healthy = ok
        return ok

    async def check_all(self) -> None:
        await asyncio.gather(*(self.check(s) for s in self.states))

    def report_success(self, state: ProxyState) -> None:
        state.failures = 0
        state.pages_since_rotation += 1

    def report_failure(self, state: ProxyState) -> None:
        state.failures += 1
        if state.healthy and state.failures >= self.config.unhealthy_after_failures:
            log.warning("proxy %s marked unhealthy after %d consecutive network errors", state.name, state.failures)
            state.healthy = False

    def needs_scheduled_rotation(self, state: ProxyState) -> bool:
        every = self.config.rotate_every_pages
        return bool(every) and state.pages_since_rotation >= every

    async def rotate(self, state: ProxyState, reason: str) -> bool:
        """Rotate the exit IP. Concurrent callers coalesce into one rotation."""
        if state.gluetun is None:
            return False
        async with state.lock:
            since = time.monotonic() - state.last_rotation
            if state.last_rotation and since < self.config.min_rotation_interval_s:
                log.info("proxy %s rotated %.0fs ago; skipping rotation (%s)", state.name, since, reason)
                return False
            old_ip = state.public_ip
            log.info("rotating proxy %s (%s); current exit %s", state.name, reason, old_ip)
            try:
                info = await state.gluetun.rotate(self.config.rotation_timeout_s)
            except GluetunError as exc:
                log.error("rotation of %s failed: %s", state.name, exc)
                state.healthy = False
                return False
            finally:
                state.last_rotation = time.monotonic()
            state.update_geo(info)
            state.public_ip = info.get("public_ip") or state.public_ip
            state.rotations += 1
            state.pages_since_rotation = 0
            state.failures = 0
            state.healthy = True
            log.info("proxy %s new exit %s (%s %s)", state.name, state.public_ip, state.country or "?", state.timezone or "")
            return True

    async def health_loop(self, stop: asyncio.Event, on_change: Any = None) -> None:
        interval = self.config.health_check_interval_s
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
                return
            except asyncio.TimeoutError:
                pass
            before = [s.healthy for s in self.states]
            await self.check_all()
            if on_change is not None and before != [s.healthy for s in self.states]:
                await on_change()

    async def aclose(self) -> None:
        for state in self.states:
            if state.gluetun is not None:
                await state.gluetun.aclose()
