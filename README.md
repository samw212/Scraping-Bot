# Scraping-Bot (`scrapebot`)

An orchestrated fleet of stealth Playwright (Python) scraping bots for sites
with aggressive bot detection. Each bot drives a Chromium session whose
fingerprint comes from a **browser profile in a config file**, whose traffic
exits through a **Gluetun VPN container**, and whose pace is bounded by
**fleet-wide scrape limits**.

```
                         ┌────────────────────── Orchestrator ──────────────────────┐
 config.yaml ──────────▶ │ SQLite job queue · scrape limits · circuit breaker        │
 profiles.yaml           │ identity manager (profile × proxy) · proxy health/rotation│
                         └──────┬──────────────────┬──────────────────┬──────────────┘
                                │                  │                  │
                           worker-0           worker-1           worker-2      (one browser each)
                      Chromium + stealth  Chromium + stealth  Chromium + stealth
                                │                  │                  │
                         gluetun-1:8888     gluetun-2:8888     gluetun-3:8888   (HTTP proxy)
                         VPN exit (US)      VPN exit (CA)      VPN exit (UK)
                                └───── control API :8000: exit IP, geo, IP rotation ─────┘
```

## Features

| Requirement | How it is implemented |
|---|---|
| **Orchestration** | `Orchestrator` runs N async `Worker` bots over a persistent SQLite queue (dedup, retries with exponential backoff, crash-safe resume), graceful SIGINT/SIGTERM shutdown, periodic stats + `data/stats.json`. |
| **Scrape limits** | Per-domain token bucket + concurrency cap · fleet `max_pages_total` / per hour / per day (persisted across restarts) · per-identity pages/hour · pages per browser session · random per-worker delays · robots.txt compliance (fetched through the proxy) · `Retry-After`-aware domain pauses on 429 · **circuit breaker** that pauses the whole fleet when the block ratio spikes. |
| **Stealth patch** | `playwright-stealth` evasion bundle as the base layer, with the modules that a profile handles better switched off so the layers never conflict. Shims that only help the legacy headless shell are disabled automatically in new-headless/headful mode, where they would *create* tells. |
| **WebGL** | Profile-driven `UNMASKED_VENDOR/RENDERER` and arbitrary `getParameter` overrides (e.g. `MAX_TEXTURE_SIZE`); `--enable-unsafe-swiftshader` keeps WebGL present on GPU-less servers. |
| **Browser profile from config** | `config/profiles.yaml`: UA (templated to the real Chrome version), platform, full UA Client Hints, locale/languages, timezone (`auto` = follow the VPN exit IP), viewport/screen/DPR, hardwareConcurrency, deviceMemory, touch points, WebGL, extra headers. Weighted selection. |
| **Proxies through Gluetun** | Each Gluetun container's built-in HTTP proxy is one exit IP. The control API supplies exit IP/country/timezone and **rotates the IP** (VPN restart) when an identity is blocked or every N pages. Health checks, failure accounting, API-key auth. Static proxies (e.g. a residential gateway) also supported. |
| **Extras** | Canvas + audio noise (deterministic per identity), WebRTC IP-leak prevention, native client hints via CDP, human-like Bézier mouse paths / wheel scrolling / dwell, warm-up visits, JS-challenge wait, block/challenge detection for Cloudflare, Akamai, Imperva, DataDome, PerimeterX, Kasada, per-identity cookie jars, optional Patchright engine. |

## Quick start (Docker)

```bash
cp .env.example .env                          # VPN provider credentials, GLUETUN_API_KEY, proxy password
cp config/config.example.yaml config/config.yaml
docker compose up -d gluetun-1 gluetun-2 gluetun-3
docker compose run --rm scraper proxies       # exit IPs, countries, timezones, health
docker compose run --rm scraper fingerprint   # what a website sees from one bot
docker compose up scraper                     # run the fleet
```

Results land in `data/output/<target>.jsonl`; run state in `data/state.db`.
`HEADFUL=1` (the default in compose) runs Chromium with a real virtual display
under Xvfb, which removes a whole class of headless-only differences.

Generate the control-server key with `docker run --rm qmcgaw/gluetun genkey`.
Gluetun's control server requires auth on all routes; compose passes the key
via `HTTP_CONTROL_SERVER_AUTH_DEFAULT_ROLE`. For per-route roles use
`gluetun/auth/config.toml.example`. scrapebot needs only
`GET /v1/publicip/ip`, `GET /v1/vpn/status` and `PUT /v1/vpn/status`.

To add capacity, copy a `gluetun-N` service in `docker-compose.yml` and add a
matching entry under `proxies.gluetun`. Some VPN providers limit simultaneous
connections per account/key. Give a container its own credentials by
overriding `WIREGUARD_PRIVATE_KEY` / `WIREGUARD_ADDRESSES` in that service.

## Local development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]" && playwright install chromium
scrapebot validate -c config/config.example.yaml
# Without Gluetun you must explicitly opt in to using this machine's IP:
scrapebot fingerprint -c config/config.example.yaml --set proxies.allow_direct=true --set "proxies.gluetun=[]"
scrapebot run -c config/config.yaml --workers 2 --max-pages 50
scrapebot status -c config/config.yaml [--requeue-failed]
scrapebot proxies -c config/config.yaml [--rotate vpn-1]
pytest                      # unit tests + real-Chromium stealth and end-to-end tests
```

Any config value can be overridden with `--set dotted.key=value` (YAML-parsed).
To use Gluetun from the host, publish its ports (see the commented `ports:`
line in compose) and set e.g. `GLUETUN_1_PROXY=http://127.0.0.1:8881`,
`GLUETUN_1_CONTROL=http://127.0.0.1:8001`.

## How a page is fetched

1. A worker claims a job; it waits on the circuit breaker and fleet budgets.
2. It reuses or creates a **session**: the identity manager leases a
   `(profile, proxy)` identity that is not cooling down, not over its hourly
   budget, and on a healthy proxy with spare capacity. Chromium launches through
   that proxy with the profile's flags, context options, stealth scripts, CDP
   client-hint overrides and the identity's saved cookies. Warm-up URLs are
   visited first.
3. Inside a per-domain slot (rate + concurrency) it navigates, classifies the
   response (content markers first, then status), waits out JS challenges,
   checks `wait_for`, then performs human-like activity.
4. Extraction runs **off-page** on the rendered HTML with parsel. No extraction
   JavaScript ever runs where the site's scripts could observe it.
5. Outcome handling:
   - **ok**: save items and enqueue pagination/follow links.
   - **blocked/challenge**: clear the cookie jar, rotate the Gluetun exit IP,
     bench the identity, and retry the job on another identity.
   - **429**: pause the domain (honouring `Retry-After`).
   - **network error**: count against the proxy's health.
   - **404**: fail the job without retrying.

## Stealth layers

| Layer | Covers |
|---|---|
| Launch flags | `navigator.webdriver` removed natively (`AutomationControlled`), `--enable-automation` dropped, UI language, WebRTC `disable_non_proxied_udp`, SwiftShader WebGL. New headless mode (`channel: chromium`) by default. |
| Context options | UA, locale, timezone, viewport, screen, DPR, touch, color scheme. These are native, so they also apply inside iframes. |
| CDP `Emulation.setUserAgentOverride` | `navigator.platform`, `Accept-Language` (`en-US,en;q=0.9`), `Sec-CH-UA*` headers and `navigator.userAgentData` incl. high-entropy values, with Chrome's exact GREASE brand algorithm. JS values and HTTP headers agree. |
| playwright-stealth | `window.chrome`, codecs, iframe, error-stack and similar evasions. |
| `fingerprint.js` | hardwareConcurrency/deviceMemory/maxTouchPoints, screen.avail*/colorDepth, outerWidth/outerHeight, WebGL, canvas noise (edge pixels only, so solid-fill probes stay clean), audio noise. Every override reports `[native code]`, keeps native name/length, preserves "Illegal invocation" errors and leaves no globals behind. |

Verify any profile with `scrapebot fingerprint --profile NAME`. It prints what a
page sees, from an offline secure-context probe page. Add
`--url <fingerprint test site> --screenshot fp.png` to check against an
external tester.

### Known limits (be realistic)

- **Fonts, emoji rendering and the WebGL *image* hash come from the real
  host.** A Windows profile on a Linux container can be told apart by font
  enumeration and rendering. The Linux profile is weighted highest for this
  reason. Run Windows/macOS profiles on matching hosts if that matters.
- Init scripts do not run inside **Web Workers**. Inside a worker,
  `navigator.platform`/`hardwareConcurrency` show the real values. The UA and
  brands are correct there because they are set natively.
- TLS/HTTP2 fingerprints are those of real Chromium, which is good. Proxy
  hops through Gluetun do not change them.
- CAPTCHAs are **not** solved. Interactive challenges are detected, and the
  identity is retired and the exit IP rotated. Treat a rising block rate as a
  signal to slow down; the circuit breaker does this automatically.
- `browser.engine: patchright` (`pip install patchright`) additionally hides
  driver-level CDP leaks such as `Runtime.enable` that no JS patch can.

## Custom extraction

```yaml
targets:
  - name: products
    extractor: "examples.custom_extractor:extract"   # sync or async fn(ctx) -> list[dict] | dict
```

`ctx` provides `url`, `html`, `selector` (parsel), `page` (live, use sparingly),
`target` and `job`. See `examples/custom_extractor.py`.

## Project layout

```
scrapebot/
  orchestrator.py   fleet runner: queue, budgets, breaker, stats, shutdown
  worker.py         bot loop: sessions, fetch, outcome handling, link discovery
  browser.py        one Chromium session per identity
  stealth/          launch flags, playwright-stealth config, fingerprint.js, CDP client hints
  profiles.py       profile resolution, UA templating, GREASE brands, client-hint metadata
  proxy.py          Gluetun control client, proxy pool, health, IP rotation
  identity.py       (profile × proxy) identities, leasing, cooldowns, cookie jars
  limits.py         token bucket, rolling windows, domain limiter, circuit breaker
  detection.py      ok / challenge / blocked / rate-limited / ... classification
  extract.py        off-page extraction (parsel) + custom extractor hook
  human.py          Bézier mouse movement, wheel scrolling, dwell
  storage.py        SQLite queue + fetch log, JSONL output
  diagnostics.py    fingerprint probe used by `scrapebot fingerprint` and tests
config/             config.example.yaml, profiles.yaml
docker-compose.yml  3 × Gluetun + scraper; Dockerfile uses the Playwright image (+Xvfb)
```

## Responsible use

Use this only on sites whose terms permit automated access, or where you have
permission. Respect `robots.txt` where it applies and keep the limits polite. Do
not collect personal data without a lawful basis. The defaults are deliberately
conservative: 12 requests per minute per domain, one session per exit IP, and an
automatic fleet pause when blocks spike.
