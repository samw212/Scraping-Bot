from __future__ import annotations

import asyncio
import threading
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import pytest

ROOT = Path(__file__).resolve().parent.parent

PROFILE = {
    "name": "test-win",
    "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{chrome_major}.0.0.0 Safari/537.36",
    "platform": "Win32",
    "ua_platform": "Windows",
    "ua_platform_version": "15.0.0",
    "locale": "en-US",
    "languages": ["en-US", "en"],
    "timezone": "Europe/Berlin",
    "viewport": {"width": 1280, "height": 650},
    "screen": {"width": 1366, "height": 768, "avail_height": 728, "window_chrome_height": 78},
    "hardware_concurrency": 6,
    "device_memory": 4,
    "webgl": {
        "vendor": "Google Inc. (NVIDIA)",
        "renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1650 (0x00001F82) Direct3D11 vs_5_0 ps_5_0, D3D11)",
        "parameters": {"3379": 16384},
    },
}


def base_config(tmp_path: Path, **overrides) -> dict:
    cfg = {
        "run": {"name": "test", "data_dir": str(tmp_path / "data"), "stats_interval_s": 60},
        "profiles": [PROFILE, {**PROFILE, "name": "test-win-2", "hardware_concurrency": 8}],
        "proxies": {"allow_direct": True, "max_sessions_per_proxy": 2},
        "targets": [{"name": "t", "start_urls": ["http://example.invalid/"], "fields": {"title": "h1"}}],
    }
    for key, value in overrides.items():
        cfg[key] = value
    return cfg


# ----------------------------------------------------------------- mock site


class SiteState:
    def __init__(self) -> None:
        self.hits: Counter[str] = Counter()
        self.headers: dict[str, dict[str, str]] = {}
        self.lock = threading.Lock()


def _page(title: str, body: str) -> bytes:
    return f"<!doctype html><html><head><title>{title}</title></head><body>{body}</body></html>".encode()


def make_handler(state: SiteState):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # silence
            pass

        def _send(self, status: int, body: bytes, headers: dict[str, str] | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            parts = urlsplit(self.path)
            path = parts.path
            with state.lock:
                state.hits[path] += 1
                hits = state.hits[path]
                state.headers[path] = dict(self.headers)
            if path == "/favicon.ico":
                return self._send(404, b"")
            if path == "/robots.txt":
                body = b"User-agent: *\nDisallow: /private/\n"
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if path.startswith("/list/"):
                n = int(path.rsplit("/", 1)[1])
                items = "".join(
                    f'<div class="item"><a class="link" href="/item/{i}">Item {i}</a><span class="sku">SKU-{i}</span></div>'
                    for i in range(3 * (n - 1) + 1, 3 * n + 1)
                )
                nxt = f'<a class="next" href="/list/{n + 1}">next</a>' if n < 3 else ""
                return self._send(200, _page(f"List {n}", f'<div id="list">{items}</div>{nxt}'))
            if path.startswith("/item/"):
                n = int(path.rsplit("/", 1)[1])
                if n == 4 and hits == 1:
                    return self._send(403, _page("Access Denied", "<h1>Access Denied</h1>"))
                if n == 5 and hits == 1:
                    return self._send(429, _page("Too Many Requests", "slow down"), {"Retry-After": "1"})
                if n == 6 and "ok=1" not in parts.query:
                    js = "setTimeout(() => location.replace('/item/6?ok=1'), 800);"
                    return self._send(
                        503, _page("Just a moment...", f'<div id="challenge-platform">checking</div><script>{js}</script>')
                    )
                detail = f'<h1 class="name">Item {n}</h1><span class="price">${n}.99</span>'
                return self._send(200, _page(f"Item {n}", f'<div class="detail">{detail}</div>'))
            if path == "/missing":
                return self._send(404, _page("Not Found", "nope"))
            return self._send(200, _page("Home", "<h1>Home</h1>"))

    return Handler


@pytest.fixture
def mock_site():
    state = SiteState()
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()


# ------------------------------------------------------------ browser check

_CHROMIUM_OK: bool | None = None


def chromium_available() -> bool:
    global _CHROMIUM_OK
    if _CHROMIUM_OK is None:

        async def _probe() -> bool:
            from playwright.async_api import async_playwright

            async with async_playwright() as p:
                browser = await p.chromium.launch(channel="chromium")
                await browser.close()
            return True

        try:
            _CHROMIUM_OK = asyncio.run(_probe())
        except Exception:
            _CHROMIUM_OK = False
    return _CHROMIUM_OK


def pytest_collection_modifyitems(config, items):
    browser_items = [i for i in items if "browser" in i.keywords]
    if browser_items and not chromium_available():
        skip = pytest.mark.skip(reason="Playwright Chromium is not installed")
        for item in browser_items:
            item.add_marker(skip)
