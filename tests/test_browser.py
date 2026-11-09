"""Browser-level tests: stealth fingerprint and an end-to-end fleet run.

These launch real Chromium (skipped automatically when it is not installed).
"""

from __future__ import annotations

import json
import random

import pytest

from scrapebot.browser import BrowserSession, detect_browser_version
from scrapebot.config import Config
from scrapebot.detection import Detector, Outcome
from scrapebot.diagnostics import open_probe_page, probe
from scrapebot.engine import load_engine
from scrapebot.identity import Identity
from scrapebot.limits import RollingCounter
from scrapebot.orchestrator import Orchestrator
from scrapebot.profiles import resolve_profile
from scrapebot.proxy import ProxyPool

from .conftest import base_config

pytestmark = pytest.mark.browser


async def _session(config: Config, tmp_path):
    engine = load_engine("playwright")
    manager = engine.async_playwright()
    playwright = await manager.__aenter__()
    version = await detect_browser_version(engine, playwright, config)
    pool = ProxyPool(config.proxies)
    identity = Identity(profile=config.profiles[0], proxy=pool.states[0], storage_dir=tmp_path, hourly=RollingCounter(3600, None))
    profile = resolve_profile(config.profiles[0], version, timezone=None, identity_key=identity.key)
    session = BrowserSession(engine, playwright, config, identity, profile, Detector(config.detection), random.Random(1))
    await session.start()
    return manager, session, version


async def test_stealth_fingerprint_matches_profile(tmp_path, mock_site):
    config = Config.model_validate(base_config(tmp_path))
    manager, session, version = await _session(config, tmp_path)
    try:
        await open_probe_page(session.page)
        seen = await probe(session.page)
        # Native-level overrides (CDP + context options).
        assert seen["userAgent"] == f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{version.major}.0.0.0 Safari/537.36"
        assert seen["platform"] == "Win32"
        assert seen["webdriver"] is False
        assert seen["languages"] == ["en-US", "en"]
        assert seen["timezone"] == "Europe/Berlin"
        brands = {b["brand"] for b in seen["userAgentData"]["brands"]}
        assert {"Google Chrome", "Chromium"} <= brands and "HeadlessChrome" not in brands
        assert seen["userAgentData"]["highEntropy"]["platformVersion"] == "15.0.0"
        # Profile layer (fingerprint.js).
        assert seen["hardwareConcurrency"] == 6
        assert seen["deviceMemory"] == 4
        assert seen["screen"]["availHeight"] == 728
        assert seen["window"]["outerHeight"] == seen["window"]["innerHeight"] + 78
        assert seen["webgl"]["unmaskedRenderer"].startswith("ANGLE (NVIDIA, NVIDIA GeForce GTX 1650")
        assert seen["webgl"]["maxTextureSize"] == 16384
        assert seen["canvasSolidFillClean"] is True
        # Consistency / anti-tamper checks a detector would run.
        assert seen["pdfViewerEnabled"] is True and seen["pluginsLength"] == 5
        assert (seen["notificationPermission"], seen["permissionsNotifications"]) == ("default", "prompt")
        assert all("[native code]" in v for v in seen["nativeLooking"].values())
        assert seen["illegalInvocationPreserved"] is True
        assert seen["ownWindowProps"] == []

        # Headers sent to a real server agree with the JS view.
        result = await session.fetch(mock_site.base + "/list/1", humanize=False)
        assert result.verdict.outcome is Outcome.OK
        headers = {k.lower(): v for k, v in mock_site.headers["/list/1"].items()}
        assert headers["user-agent"] == seen["userAgent"]
        assert headers["accept-language"] == "en-US,en;q=0.9"
        assert '"Google Chrome"' in headers["sec-ch-ua"]
        assert headers["sec-ch-ua-platform"] == '"Windows"'
    finally:
        await session.close(save_storage=False)
        await manager.__aexit__(None, None, None)


async def test_canvas_noise_differs_between_identities(tmp_path):
    hashes = []
    for key in ("a", "b"):
        raw = base_config(tmp_path)
        raw["profiles"][0] = {**raw["profiles"][0], "name": f"p-{key}"}
        config = Config.model_validate(raw)
        manager, session, _ = await _session(config, tmp_path)
        try:
            await open_probe_page(session.page)
            first = (await probe(session.page))["canvasHash"]
            second = (await probe(session.page))["canvasHash"]
            assert first == second  # stable within one identity
            hashes.append(first)
        finally:
            await session.close(save_storage=False)
            await manager.__aexit__(None, None, None)
    assert hashes[0] != hashes[1]


async def test_fleet_end_to_end(tmp_path, mock_site):
    base = mock_site.base
    raw = base_config(
        tmp_path,
        orchestrator={"workers": 2, "idle_poll_s": 0.2, "shutdown_grace_s": 10},
        limits={
            "per_domain": {"requests_per_minute": 6000, "burst": 10, "max_concurrency": 2},
            "delay_s": [0, 0.05],
            "pages_per_session": 4,
            "retry_backoff_s": 0.2,
            "identity_cooldown_s": 0.5,
            "rate_limit_pause_s": 1,
            "max_retries": 2,
            "circuit_breaker": {"min_samples": 100},
        },
        behavior={"humanize": True, "mouse_moves": [1, 1], "scrolls": [1, 1], "dwell_s": [0, 0.05], "warmup_urls": [base + "/"]},
        detection={"challenge_wait_s": 8},
        targets=[
            {
                "name": "listing",
                "start_urls": [base + "/list/1", base + "/missing", base + "/private/secret"],
                "wait_for": "#list",
                "item_selector": ".item",
                "fields": {"name": "a.link", "sku": ".sku", "url": {"selector": "a.link", "attr": "href"}},
                "next_page": {"selector": "a.next", "max_pages": 3},
                "follow": {"selector": "a.link", "target": "detail", "max_depth": 1},
            },
            {"name": "detail", "wait_for": ".detail", "fields": {"name": "h1.name", "price": ".price"}},
        ],
    )
    config = Config.model_validate(raw)
    summary = await Orchestrator(config).run()

    outcomes = summary["outcomes"]
    # done: 3 list pages + 9 items; failed: /missing (404) and /private/secret (robots.txt)
    assert summary["queue"] == {"pending": 0, "in_progress": 0, "done": 12, "failed": 2}
    assert mock_site.hits["/private/secret"] == 0 and mock_site.hits["/robots.txt"] == 1
    assert outcomes.get("blocked", 0) == 1  # /item/4 first hit
    assert outcomes.get("rate_limited", 0) == 1  # /item/5 first hit
    assert outcomes.get("not_found", 0) == 1
    assert mock_site.hits["/item/4"] == 2 and mock_site.hits["/item/5"] == 2

    out = config.output_dir
    listing = [json.loads(line) for line in (out / "listing.jsonl").read_text().splitlines()]
    detail = [json.loads(line) for line in (out / "detail.jsonl").read_text().splitlines()]
    assert sorted(i["sku"] for i in listing) == sorted(f"SKU-{i}" for i in range(1, 10))
    assert listing[0]["url"].startswith(base + "/item/")
    by_name = {d["name"]: d for d in detail}
    assert set(by_name) == {f"Item {i}" for i in range(1, 10)}
    # /item/6 served a JS challenge first; it resolved and redirected.
    assert by_name["Item 6"]["_url"].endswith("/item/6?ok=1")
    assert by_name["Item 6"]["price"] == "$6.99"
    # The blocked identity was benched, and its cookie jar cleared.
    assert any(i["blocks"] == 1 for i in summary["identities"])
    assert (config.run.data_dir / "stats.json").exists()

    # A second run resumes from the persistent queue: nothing left to do.
    again = await Orchestrator(config).run()
    assert again["outcomes"] == {}
    assert again["queue"]["done"] == 12
