"""Unit tests: limits, detection, storage, extraction."""

from __future__ import annotations

import asyncio

import pytest

from scrapebot.config import CircuitBreakerConfig, DetectionConfig, RateSpec, TargetConfig
from scrapebot.detection import Detector, Outcome, parse_retry_after
from scrapebot.extract import ExtractContext, extract_items, extract_links
from scrapebot.limits import CircuitBreaker, DomainLimiter, RollingCounter, StopRequested, TokenBucket
from scrapebot.robots import RobotsCache
from scrapebot.storage import JsonlSink, Store, normalize_url


class FakeClock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


# ------------------------------------------------------------------- limits


def test_token_bucket_rate_and_pause():
    clock = FakeClock()
    bucket = TokenBucket(rate_per_s=0.5, burst=2, clock=clock)
    assert bucket.try_acquire() == 0
    assert bucket.try_acquire() == 0
    assert bucket.try_acquire() == pytest.approx(2.0)  # 1 token / 0.5 per s
    clock.now += 2
    assert bucket.try_acquire() == 0
    bucket.pause(30)
    assert bucket.try_acquire() == pytest.approx(30)
    clock.now += 30
    # No tokens accrue while paused.
    assert bucket.try_acquire() == pytest.approx(2.0)


def test_rolling_counter():
    clock = FakeClock()
    counter = RollingCounter(window_s=3600, limit=2, clock=clock)
    counter.preload([clock.now - 4000, clock.now - 100])  # first is outside the window
    assert counter.count() == 1
    counter.record()
    assert counter.wait_time() == pytest.approx(3500)
    clock.now += 3500
    assert counter.wait_time() == 0
    assert RollingCounter(60, None, clock).wait_time() == 0


def test_circuit_breaker_trips_and_recovers():
    clock = FakeClock()
    breaker = CircuitBreaker(CircuitBreakerConfig(window=10, min_samples=4, block_ratio=0.5, pause_s=60), clock=clock)
    assert not breaker.record(False)
    assert not breaker.record(True)
    assert not breaker.record(False)
    assert breaker.record(True)  # 2/4 blocked -> trip
    assert breaker.is_open() and breaker.remaining() == pytest.approx(60)
    clock.now += 61
    assert not breaker.is_open()


async def test_domain_limiter_concurrency_and_stop():
    limiter = DomainLimiter(RateSpec(requests_per_minute=6000, burst=10, max_concurrency=1), {"slow.com": RateSpec(requests_per_minute=1, burst=1, max_concurrency=1)})
    assert limiter.spec("www.slow.com").requests_per_minute == 1
    active = 0
    peak = 0

    async def use() -> None:
        nonlocal active, peak
        async with limiter.slot("a.com"):
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1

    await asyncio.gather(*(use() for _ in range(4)))
    assert peak == 1

    stop = asyncio.Event()
    async with limiter.slot("slow.com", stop):
        pass  # burst token
    stop.set()
    with pytest.raises(StopRequested):
        async with limiter.slot("slow.com", stop):
            pass


# ---------------------------------------------------------------- detection


@pytest.mark.parametrize(
    "status, html, headers, outcome",
    [
        (200, "<html><title>Shop</title></html>", {}, Outcome.OK),
        (403, "<title>Just a moment...</title>", {}, Outcome.CHALLENGE),
        (200, "<script src='/cdn-cgi/challenge-platform/x.js'>", {}, Outcome.CHALLENGE),
        (403, "<h1>Sorry, you have been blocked</h1>", {}, Outcome.BLOCKED),
        (403, "plain forbidden", {}, Outcome.BLOCKED),
        (429, "slow", {"Retry-After": "12"}, Outcome.RATE_LIMITED),
        (404, "missing", {}, Outcome.NOT_FOUND),
        (502, "bad gateway", {}, Outcome.SERVER_ERROR),
        (None, "", {}, Outcome.NETWORK_ERROR),
    ],
)
def test_detector(status, html, headers, outcome):
    verdict = Detector(DetectionConfig()).classify(status, html, headers)
    assert verdict.outcome is outcome
    if outcome is Outcome.RATE_LIMITED:
        assert verdict.retry_after_s == 12


def test_parse_retry_after_http_date():
    assert parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 0.0
    assert parse_retry_after("nonsense") is None


# ------------------------------------------------------------------ storage


def test_normalize_url():
    assert normalize_url("HTTPS://Example.COM:443/a?b=1#frag") == "https://example.com/a?b=1"
    assert normalize_url("http://example.com") == "http://example.com/"
    assert normalize_url("http://example.com:8080/x") == "http://example.com:8080/x"


def test_store_queue_lifecycle(tmp_path):
    clock = FakeClock()
    store = Store(tmp_path / "s.db", clock=clock)
    assert store.add_jobs(["https://a.com/1", "https://A.com/1#x", "https://a.com/2"], "t") == 2
    assert store.add_job("https://a.com/1", "other-target")  # same URL, different target
    job = store.claim()
    assert job.url == "https://a.com/1"
    assert store.retry(job, delay_s=10, error="boom", max_retries=1)
    assert store.counts()["pending"] == 3
    # Not ready until its backoff elapses.
    claimed = [store.claim(), store.claim()]
    assert {j.url for j in claimed} == {"https://a.com/2", "https://a.com/1"}
    assert store.claim() is None
    clock.now += 11
    again = store.claim()
    assert again.id == job.id and again.attempts == 1
    assert not store.retry(again, 1, "boom", max_retries=1)  # out of retries
    assert store.counts()["failed"] == 1
    store.complete(claimed[0])
    store.release(claimed[1])
    assert store.reset_in_progress() == 0
    assert store.requeue_failed() == 1
    assert not store.is_drained()
    store.log_fetch(url="https://a.com/2", outcome="ok", identity="p@x", ts=clock.now - 10)
    assert store.fetch_times_by_identity(0) == {"p@x": [clock.now - 10]}
    store.close()


def test_jsonl_sink(tmp_path):
    sink = JsonlSink(tmp_path / "out")
    sink.write("t", [{"a": 1}, {"b": "é"}])
    sink.close()
    lines = (tmp_path / "out" / "t.jsonl").read_text(encoding="utf-8").splitlines()
    assert lines == ['{"a": 1}', '{"b": "é"}']


# --------------------------------------------------------------- extraction

HTML = """
<html><head><base href="https://shop.test/catalog/"></head><body>
  <div class="p"><h2> Widget  A </h2><a href="w-a">x</a><span class="tag">new</span><span class="tag">hot</span></div>
  <div class="p"><h2>Widget B</h2><a href="/abs/w-b">x</a></div>
  <div class="p"></div>
  <a class="next" href="?page=2">next</a>
  <a class="ext" href="https://other.test/x">ext</a>
  <a class="ext" href="javascript:void(0)">js</a>
</body></html>
"""


def _ctx(**target_kwargs) -> ExtractContext:
    target = TargetConfig.model_validate({"name": "t", **target_kwargs})
    return ExtractContext(url="https://shop.test/catalog/index.html", html=HTML, target=target)


def test_extract_items():
    ctx = _ctx(
        item_selector=".p",
        fields={"name": "h2", "url": {"selector": "a", "attr": "href"}, "tags": {"selector": ".tag", "multiple": True}},
    )
    items = extract_items(ctx)
    assert items == [
        {"name": "Widget A", "url": "https://shop.test/catalog/w-a", "tags": ["new", "hot"]},
        {"name": "Widget B", "url": "https://shop.test/abs/w-b", "tags": []},
    ]


def test_extract_links():
    ctx = _ctx(fields={"x": "h2"})
    assert extract_links(ctx, "a.next", same_domain=True, allowed_domains=[]) == ["https://shop.test/catalog/?page=2"]
    assert extract_links(ctx, "a.ext", same_domain=True, allowed_domains=[]) == []
    assert extract_links(ctx, "a.ext", same_domain=False, allowed_domains=[]) == ["https://other.test/x"]
    assert extract_links(ctx, "a.ext::attr(href)", same_domain=True, allowed_domains=["other.test"]) == ["https://other.test/x"]


# ------------------------------------------------------------------- robots


class _FakeResponse:
    def __init__(self, status: int, body: str):
        self.status = status
        self._body = body

    async def text(self) -> str:
        return self._body


class _FakeRequest:
    def __init__(self, responses: dict[str, _FakeResponse | Exception]):
        self.responses = responses
        self.calls: list[str] = []

    async def get(self, url: str, **kwargs):
        self.calls.append(url)
        result = self.responses[url]
        if isinstance(result, Exception):
            raise result
        return result


async def test_robots_cache():
    request = _FakeRequest(
        {
            "https://a.test/robots.txt": _FakeResponse(200, "User-agent: *\nDisallow: /private/\n"),
            "https://b.test/robots.txt": _FakeResponse(404, ""),
            "https://c.test/robots.txt": _FakeResponse(503, ""),
        }
    )
    robots = RobotsCache()
    assert await robots.allowed(request, "https://a.test/public/1")
    assert not await robots.allowed(request, "https://a.test/private/1")
    assert await robots.allowed(request, "https://b.test/anything")
    assert await robots.allowed(request, "https://c.test/x")  # 5xx: allowed, not cached
    assert await robots.allowed(request, "https://c.test/y")
    assert request.calls.count("https://a.test/robots.txt") == 1
    assert request.calls.count("https://c.test/robots.txt") == 2
