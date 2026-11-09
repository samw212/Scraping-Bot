"""Gluetun client and proxy pool against a fake Gluetun control server."""

from __future__ import annotations

import itertools
import json

import httpx
import pytest

from scrapebot.config import ProxiesConfig
from scrapebot.proxy import GluetunClient, GluetunError, ProxyPool


class FakeGluetun:
    """Mimics Gluetun's control server: /v1/vpn/status and /v1/publicip/ip."""

    def __init__(self, api_key: str = "k3y"):
        self.api_key = api_key
        self.status = "running"
        self._ips = (f"203.0.113.{i}" for i in itertools.count(10))
        self.ip = next(self._ips)
        self.calls: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(f"{request.method} {request.url.path}")
        if request.headers.get("X-API-Key") != self.api_key:
            return httpx.Response(401, text="unauthorized")
        if request.url.path == "/v1/vpn/status":
            if request.method == "PUT":
                wanted = json.loads(request.content)["status"]
                if wanted == "running" and self.status == "stopped":
                    self.ip = next(self._ips)  # new server -> new exit IP
                self.status = wanted
                return httpx.Response(200, json={"outcome": wanted})
            return httpx.Response(200, json={"status": self.status})
        if request.url.path == "/v1/publicip/ip":
            if self.status != "running":
                return httpx.Response(200, json={"public_ip": ""})
            return httpx.Response(
                200, json={"public_ip": self.ip, "country": "Germany", "city": "Berlin", "timezone": "Europe/Berlin"}
            )
        return httpx.Response(404)


def _pool(fake: FakeGluetun, api_key: str = "k3y", **extra) -> ProxyPool:
    config = ProxiesConfig.model_validate(
        {
            "gluetun": [
                {"name": "vpn-1", "proxy_url": "http://g1:8888", "control_url": "http://g1:8000", "api_key": api_key,
                 "username": "u", "password": "p"},
            ],
            "static": [{"name": "res", "url": "http://res.example:3128"}],
            "min_rotation_interval_s": 0,
            **extra,
        }
    )
    return ProxyPool(config, transport=httpx.MockTransport(fake.handler))


async def test_check_reads_geo_and_auth():
    fake = FakeGluetun()
    pool = _pool(fake)
    await pool.check_all()
    vpn = pool.get("vpn-1")
    assert vpn.healthy and vpn.public_ip == "203.0.113.10"
    assert vpn.timezone == "Europe/Berlin" and vpn.country == "Germany"
    assert vpn.playwright_proxy == {"server": "http://g1:8888", "username": "u", "password": "p"}
    assert pool.get("res").playwright_proxy == {"server": "http://res.example:3128"}
    await pool.aclose()

    bad = _pool(FakeGluetun(), api_key="wrong")
    await bad.check_all()
    assert not bad.get("vpn-1").healthy
    await bad.aclose()


async def test_rotate_gets_new_ip():
    fake = FakeGluetun()
    pool = _pool(fake, rotation_timeout_s=5)
    await pool.check_all()
    vpn = pool.get("vpn-1")
    vpn.pages_since_rotation = 9
    assert await pool.rotate(vpn, "test")
    assert vpn.public_ip == "203.0.113.11"
    assert vpn.rotations == 1 and vpn.pages_since_rotation == 0
    assert "PUT /v1/vpn/status" in fake.calls
    # Static proxies cannot rotate.
    assert not await pool.rotate(pool.get("res"), "test")
    await pool.aclose()


async def test_rotation_is_rate_limited():
    fake = FakeGluetun()
    pool = _pool(fake, min_rotation_interval_s=3600, rotation_timeout_s=5)
    vpn = pool.get("vpn-1")
    assert await pool.rotate(vpn, "first")
    assert not await pool.rotate(vpn, "second, too soon")
    assert vpn.rotations == 1
    await pool.aclose()


async def test_failure_accounting_and_probation():
    pool = _pool(FakeGluetun(), unhealthy_after_failures=2)
    res = pool.get("res")
    pool.report_failure(res)
    assert res.healthy
    pool.report_failure(res)
    assert not res.healthy
    await pool.check(res)  # no control API -> probation
    assert res.healthy
    await pool.aclose()


async def test_client_errors_are_wrapped():
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    from scrapebot.config import GluetunEndpoint

    client = GluetunClient(
        GluetunEndpoint(name="x", proxy_url="http://x:8888", control_url="http://x:8000"),
        transport=httpx.MockTransport(boom),
    )
    with pytest.raises(GluetunError):
        await client.public_ip()
    await client.aclose()
