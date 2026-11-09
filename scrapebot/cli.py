"""Command-line interface: ``scrapebot <command> -c config.yaml``."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
import sys
import tempfile
from pathlib import Path
from typing import Any

import yaml

from .config import Config, ConfigError, load_config

log = logging.getLogger("scrapebot")


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(level: str, as_json: bool = False) -> None:
    handler = logging.StreamHandler(sys.stderr)
    if as_json:
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    for noisy in ("httpx", "httpcore", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _parse_overrides(pairs: list[str]) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep:
            raise ConfigError(f"--set expects key=value, got {pair!r}")
        overrides[key.strip()] = yaml.safe_load(value)
    return overrides


def _load(args: argparse.Namespace, extra: dict[str, Any] | None = None) -> Config:
    overrides = _parse_overrides(args.set or [])
    overrides.update(extra or {})
    return load_config(args.config, overrides)


# ----------------------------------------------------------------- commands


def cmd_run(args: argparse.Namespace) -> int:
    extra: dict[str, Any] = {}
    if args.workers:
        extra["orchestrator.workers"] = args.workers
    if args.max_pages:
        extra["limits.max_pages_total"] = args.max_pages
    if args.headful:
        extra["browser.headless"] = False
    config = _load(args, extra)
    setup_logging(config.run.log_level, config.run.log_json)
    from .orchestrator import Orchestrator

    summary = asyncio.run(Orchestrator(config).run())
    print(json.dumps({k: summary[k] for k in ("outcomes", "items_written", "queue", "stop_reason", "elapsed_s")}, indent=2))
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    from .profiles import BrowserVersion, resolve_profile

    config = _load(args)
    version = BrowserVersion.parse(config.browser.version if config.browser.version != "auto" else "130.0.0.0")
    print(f"config OK: {args.config}")
    print(f"  workers={config.orchestrator.workers} engine={config.browser.engine} headless={config.browser.headless}")
    print(f"  proxies: gluetun={[p.name for p in config.proxies.gluetun]} static={[p.name for p in config.proxies.static]} direct={config.proxies.allow_direct}")
    print(f"  targets: {[t.name for t in config.targets]}")
    print("  profiles (UA shown for Chrome %s%s):" % (version.full, " — real version is detected at runtime" if config.browser.version == "auto" else ""))
    for profile in config.profiles:
        resolved = resolve_profile(profile, version, timezone=None, identity_key="validate")
        webgl = profile.webgl.renderer if profile.webgl else "(native)"
        print(f"    - {profile.name} (weight {profile.weight}): {resolved.user_agent}")
        print(f"      tz={profile.timezone} locale={profile.locale} viewport={profile.viewport.width}x{profile.viewport.height} webgl={webgl}")
    limits = config.limits
    print(
        f"  limits: total={limits.max_pages_total} hour={limits.max_pages_per_hour} day={limits.max_pages_per_day} "
        f"per-domain={limits.per_domain.requests_per_minute}/min (burst {limits.per_domain.burst}, "
        f"concurrency {limits.per_domain.max_concurrency}) per-identity/hour={limits.per_identity_pages_per_hour}"
    )
    return 0


async def _proxies(config: Config, rotate: str | None) -> int:
    from .proxy import ProxyPool

    pool = ProxyPool(config.proxies)
    try:
        if rotate:
            state = pool.get(rotate)
            if state.gluetun is None:
                print(f"proxy {rotate!r} has no Gluetun control_url; cannot rotate", file=sys.stderr)
                return 2
            ok = await pool.rotate(state, "manual")
            print(json.dumps(state.snapshot(), indent=2))
            return 0 if ok else 1
        await pool.check_all()
        rows = [s.snapshot() for s in pool.states]
        print(json.dumps(rows, indent=2))
        return 0 if all(r["healthy"] for r in rows) else 1
    finally:
        await pool.aclose()


def cmd_proxies(args: argparse.Namespace) -> int:
    config = _load(args)
    setup_logging(config.run.log_level)
    return asyncio.run(_proxies(config, args.rotate))


async def _fingerprint(config: Config, args: argparse.Namespace) -> int:
    from .browser import BrowserSession, detect_browser_version
    from .detection import Detector
    from .diagnostics import open_probe_page, probe
    from .engine import load_engine
    from .identity import Identity
    from .limits import RollingCounter
    from .profiles import resolve_profile
    from .proxy import ProxyPool

    profiles = {p.name: p for p in config.profiles}
    if args.profile and args.profile not in profiles:
        print(f"unknown profile {args.profile!r}; available: {sorted(profiles)}", file=sys.stderr)
        return 2
    profile = profiles[args.profile] if args.profile else config.profiles[0]
    pool = ProxyPool(config.proxies)
    try:
        await pool.check_all()
        if args.proxy:
            proxy = pool.get(args.proxy)
        else:
            healthy = pool.healthy()
            if not healthy:
                print("no healthy proxy available", file=sys.stderr)
                return 1
            proxy = healthy[0]
        engine = load_engine(config.browser.engine)
        async with engine.async_playwright() as playwright:
            version = await detect_browser_version(engine, playwright, config)
            with tempfile.TemporaryDirectory() as tmp:
                identity = Identity(profile=profile, proxy=proxy, storage_dir=Path(tmp), hourly=RollingCounter(3600, None))
                resolved = resolve_profile(profile, version, timezone=proxy.timezone, identity_key=identity.key)
                session = BrowserSession(engine, playwright, config, identity, resolved, Detector(config.detection), random.Random())
                await session.start()
                try:
                    if args.url:
                        await session.page.goto(args.url, wait_until="load")
                        await asyncio.sleep(args.wait)
                    else:
                        await open_probe_page(session.page)
                    report = {
                        "profile": profile.name,
                        "proxy": proxy.snapshot(),
                        "browser_version": version.full,
                        "seen_by_page": await probe(session.page),
                    }
                    print(json.dumps(report, indent=2, ensure_ascii=False))
                    if args.screenshot:
                        await session.page.screenshot(path=args.screenshot, full_page=True)
                        print(f"screenshot saved to {args.screenshot}", file=sys.stderr)
                finally:
                    await session.close(save_storage=False)
    finally:
        await pool.aclose()
    return 0


def cmd_fingerprint(args: argparse.Namespace) -> int:
    extra: dict[str, Any] = {"browser.persist_storage": False}
    if args.headful:
        extra["browser.headless"] = False
    config = _load(args, extra)
    setup_logging(config.run.log_level)
    return asyncio.run(_fingerprint(config, args))


def cmd_status(args: argparse.Namespace) -> int:
    from .storage import Store

    config = _load(args)
    db = config.run.data_dir / "state.db"
    if not db.exists():
        print(f"no state database at {db} (nothing has run yet)")
        return 0
    store = Store(db)
    try:
        if args.requeue_failed:
            print(f"requeued {store.requeue_failed()} failed jobs")
        print(json.dumps(store.counts(), indent=2))
    finally:
        store.close()
    stats = config.run.data_dir / "stats.json"
    if stats.exists():
        print(f"last run stats: {stats}")
    return 0


# --------------------------------------------------------------------- main


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="scrapebot", description="Orchestrated stealth scraping fleet (Playwright + Gluetun).")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("-c", "--config", default="config/config.yaml", help="path to the YAML config")
        p.add_argument("--set", action="append", metavar="KEY=VALUE", help="override a config value, e.g. --set limits.max_pages_total=50")

    p = sub.add_parser("run", help="run the scraping fleet")
    common(p)
    p.add_argument("--workers", type=int, help="number of concurrent worker bots")
    p.add_argument("--max-pages", type=int, help="stop after this many page loads")
    p.add_argument("--headful", action="store_true", help="show browser windows (use Xvfb on servers)")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("validate", help="validate the config and print resolved profiles")
    common(p)
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("proxies", help="check Gluetun proxies (exit IP, geo, health) or rotate one")
    common(p)
    p.add_argument("--rotate", metavar="NAME", help="restart this Gluetun VPN to get a new exit IP")
    p.set_defaults(func=cmd_proxies)

    p = sub.add_parser("fingerprint", help="launch one stealth session and print what a site sees")
    common(p)
    p.add_argument("--profile", help="profile name (default: first)")
    p.add_argument("--proxy", help="proxy name (default: first healthy)")
    p.add_argument("--url", help="visit a fingerprinting test site instead of the offline probe page")
    p.add_argument("--wait", type=float, default=5.0, help="seconds to wait on --url before probing")
    p.add_argument("--screenshot", help="save a full-page screenshot to this path")
    p.add_argument("--headful", action="store_true")
    p.set_defaults(func=cmd_fingerprint)

    p = sub.add_parser("status", help="show queue status from the state database")
    common(p)
    p.add_argument("--requeue-failed", action="store_true", help="reset failed jobs to pending")
    p.set_defaults(func=cmd_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
