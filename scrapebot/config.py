"""Configuration models and YAML loader.

Every tunable of the system lives in one YAML file (plus an optional
separate profiles file). Values may reference environment variables with
``${VAR}`` or ``${VAR:-default}`` so secrets (VPN keys, Gluetun API keys)
never have to be committed.

Relative paths:
  * ``profiles_file`` and ``targets[].url_file`` resolve against the directory
    of the config file (they are config inputs).
  * ``run.data_dir`` and ``output.dir`` resolve against the current working
    directory (they are runtime locations).
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ConfigError(ValueError):
    """Raised for invalid or unloadable configuration."""


_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def interpolate_env(value: Any) -> Any:
    """Recursively substitute ``${VAR}`` / ``${VAR:-default}`` in strings."""
    if isinstance(value, str):

        def repl(match: re.Match[str]) -> str:
            name, default = match.group(1), match.group(2)
            env_value = os.environ.get(name)
            if env_value is None or env_value == "":
                if default is None:
                    raise ConfigError(
                        f"environment variable ${{{name}}} is not set "
                        f"(use ${{{name}:-}} to make it optional)"
                    )
                return default
            return env_value

        return _ENV_PATTERN.sub(repl, value)
    if isinstance(value, dict):
        return {key: interpolate_env(item) for key, item in value.items()}
    if isinstance(value, list):
        return [interpolate_env(item) for item in value]
    return value


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


def _blank_to_none(value: Any) -> Any:
    if isinstance(value, str) and value.strip() == "":
        return None
    return value


def _check_range(value: tuple[float, float], name: str) -> tuple[float, float]:
    low, high = value
    if low < 0 or high < low:
        raise ValueError(f"{name} must be [low, high] with 0 <= low <= high")
    return value


# --------------------------------------------------------------------------- run


class RunConfig(_Model):
    name: str = "scrapebot"
    data_dir: Path = Path("./data")
    log_level: str = "INFO"
    log_json: bool = False
    stats_interval_s: float = Field(30.0, gt=0)
    seed: int | None = None


# ----------------------------------------------------------------------- browser


class BrowserConfig(_Model):
    engine: Literal["playwright", "patchright"] = "playwright"
    # "chromium" = Chromium's new headless mode (a real browser build, native
    # plugins/permissions). None with headless=true falls back to the legacy
    # "headless shell", which is much easier to fingerprint.
    channel: str | None = "chromium"
    executable_path: str | None = None
    headless: bool = True
    # Full Chrome version used to template user agents / client hints.
    # "auto" detects it from the launched browser so the UA always matches
    # the real engine (a mismatch is a classic detection signal).
    version: str = "auto"
    args: list[str] = Field(default_factory=list)
    ignore_default_args: list[str] = Field(default_factory=lambda: ["--enable-automation"])
    navigation_timeout_s: float = Field(45.0, gt=0)
    wait_until: Literal["commit", "domcontentloaded", "load", "networkidle"] = "domcontentloaded"
    block_resources: list[Literal["image", "media", "font", "stylesheet", "other"]] = Field(
        default_factory=list
    )
    persist_storage: bool = True
    clear_storage_on_block: bool = True

    _blank = field_validator("channel", "executable_path", mode="before")(_blank_to_none)

    @property
    def uses_headless_shell(self) -> bool:
        return self.headless and self.channel is None and self.executable_path is None


class StealthConfig(_Model):
    # Base layer: the playwright-stealth evasion bundle.
    playwright_stealth: bool = True
    # Extra kwargs passed straight to playwright_stealth.Stealth(...), e.g.
    # {"chrome_runtime": true, "iframe_content_window": false}.
    playwright_stealth_options: dict[str, Any] = Field(default_factory=dict)
    # Profile-driven layer (scrapebot/stealth/fingerprint.js + CDP).
    client_hints: bool = True
    webgl: bool = True
    hardware: bool = True
    screen: bool = True
    canvas_noise: bool = True
    audio_noise: bool = True
    # Chromium WebRTC IP handling policy. "disable_non_proxied_udp" prevents
    # WebRTC from leaking the host's real IP around the proxy.
    webrtc_ip_policy: (
        Literal[
            "disable_non_proxied_udp",
            "default_public_interface_only",
            "default_public_and_private_interfaces",
        ]
        | None
    ) = "disable_non_proxied_udp"


# ---------------------------------------------------------------------- profiles


class Size(_Model):
    width: int = Field(gt=0)
    height: int = Field(gt=0)


class ScreenSpec(_Model):
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    avail_width: int | None = None
    avail_height: int | None = None
    color_depth: int = 24
    # Height of the browser UI (tabs + address bar). Used to derive
    # window.outerHeight, which equals innerHeight in headless mode otherwise.
    window_chrome_height: int = 85


class WebGLSpec(_Model):
    vendor: str
    renderer: str
    # Optional extra getParameter() overrides keyed by GLenum, e.g.
    # {3379: 16384} for MAX_TEXTURE_SIZE. Lists become typed arrays when the
    # native value is a typed array.
    parameters: dict[int, Any] = Field(default_factory=dict)


class ProfileConfig(_Model):
    name: str
    weight: float = Field(1.0, gt=0)
    # May contain {chrome_major} and {chrome_version} placeholders.
    user_agent: str
    platform: str = "Win32"  # navigator.platform
    ua_platform: str = "Windows"  # Sec-CH-UA-Platform
    ua_platform_version: str = "15.0.0"
    architecture: str = "x86"
    bitness: str = "64"
    wow64: bool = False
    model: str = ""
    mobile: bool = False
    brand: str = "Google Chrome"
    locale: str = "en-US"
    languages: list[str] = Field(default_factory=lambda: ["en-US", "en"])
    # IANA zone, or "auto" to follow the proxy exit IP reported by Gluetun.
    timezone: str = "auto"
    fallback_timezone: str = "America/New_York"
    viewport: Size
    screen: ScreenSpec
    device_scale_factor: float = Field(1.0, gt=0)
    hardware_concurrency: int = Field(8, gt=0)
    device_memory: float = Field(8, gt=0)
    max_touch_points: int = Field(0, ge=0)
    color_scheme: Literal["light", "dark", "no-preference"] = "light"
    webgl: WebGLSpec | None = None
    extra_headers: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_geometry(self) -> ProfileConfig:
        if self.viewport.width > self.screen.width or self.viewport.height > self.screen.height:
            raise ValueError(f"profile {self.name!r}: viewport is larger than the screen")
        if not self.languages:
            raise ValueError(f"profile {self.name!r}: languages must not be empty")
        return self


# ----------------------------------------------------------------------- proxies


class GluetunEndpoint(_Model):
    name: str
    # Gluetun's built-in HTTP proxy (HTTPPROXY=on), e.g. http://gluetun-1:8888
    proxy_url: str
    username: str | None = None
    password: str | None = None
    # Gluetun control server, e.g. http://gluetun-1:8000. Without it the
    # endpoint still works as a proxy but cannot rotate IPs or report geo.
    control_url: str | None = None
    api_key: str | None = None
    control_username: str | None = None
    control_password: str | None = None

    _blank = field_validator(
        "username", "password", "control_url", "api_key", "control_username", "control_password",
        mode="before",
    )(_blank_to_none)


class StaticProxy(_Model):
    name: str
    url: str
    username: str | None = None
    password: str | None = None
    timezone: str | None = None

    _blank = field_validator("username", "password", "timezone", mode="before")(_blank_to_none)


class ProxiesConfig(_Model):
    gluetun: list[GluetunEndpoint] = Field(default_factory=list)
    static: list[StaticProxy] = Field(default_factory=list)
    # Safety switch: refuse to run from the host's own IP unless explicitly allowed.
    allow_direct: bool = False
    max_sessions_per_proxy: int = Field(1, ge=1)
    rotate_on_block: bool = True
    rotate_every_pages: int = Field(0, ge=0)  # 0 = only on block
    min_rotation_interval_s: float = Field(60.0, ge=0)
    rotation_timeout_s: float = Field(120.0, gt=0)
    health_check_interval_s: float = Field(120.0, gt=0)
    unhealthy_after_failures: int = Field(3, ge=1)
    request_timeout_s: float = Field(10.0, gt=0)

    @model_validator(mode="after")
    def _unique_names(self) -> ProxiesConfig:
        names = [p.name for p in self.gluetun] + [p.name for p in self.static]
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            raise ValueError(f"duplicate proxy names: {sorted(dupes)}")
        return self


# ------------------------------------------------------------------------ limits


class RateSpec(_Model):
    requests_per_minute: float = Field(10.0, gt=0)
    burst: int = Field(2, ge=1)
    max_concurrency: int = Field(2, ge=1)


class CircuitBreakerConfig(_Model):
    window: int = Field(20, ge=1)
    min_samples: int = Field(8, ge=1)
    block_ratio: float = Field(0.5, gt=0, le=1)
    pause_s: float = Field(600.0, ge=0)


class LimitsConfig(_Model):
    max_pages_total: int | None = Field(None, ge=1)  # per run
    max_pages_per_hour: int | None = Field(None, ge=1)  # fleet-wide, survives restarts
    max_pages_per_day: int | None = Field(None, ge=1)  # fleet-wide, survives restarts
    per_domain: RateSpec = Field(default_factory=RateSpec)
    domain_overrides: dict[str, RateSpec] = Field(default_factory=dict)
    per_identity_pages_per_hour: int | None = Field(60, ge=1)
    pages_per_session: int = Field(30, ge=1)  # recycle browser after N pages
    delay_s: tuple[float, float] = (2.0, 6.0)  # per-worker pause between pages
    max_retries: int = Field(3, ge=0)
    retry_backoff_s: float = Field(30.0, ge=0)
    identity_cooldown_s: float = Field(900.0, ge=0)
    rate_limit_pause_s: float = Field(120.0, ge=0)
    respect_robots_txt: bool = True
    robots_user_agent: str = "*"  # token matched against robots.txt groups
    circuit_breaker: CircuitBreakerConfig = Field(default_factory=CircuitBreakerConfig)

    @field_validator("delay_s")
    @classmethod
    def _delay(cls, value: tuple[float, float]) -> tuple[float, float]:
        return _check_range(value, "delay_s")


# --------------------------------------------------------------------- detection

DEFAULT_CHALLENGE_MARKERS = [
    "<title>Just a moment...</title>",
    "challenge-platform",
    "cf-chl-",
    "Checking your browser",
    "_Incapsula_Resource",
    "captcha-delivery.com",
    "px-captcha",
    "/_px/",
    "Pardon Our Interruption",
    "ak_bmsc_challenge",
]

DEFAULT_BLOCK_MARKERS = [
    "Sorry, you have been blocked",
    "You have been blocked",
    "<title>Access Denied</title>",
    "Request unsuccessful. Incapsula incident",
    "unusual traffic from your computer network",
    "Access to this page has been denied",
]


class DetectionConfig(_Model):
    block_status: list[int] = Field(default_factory=lambda: [401, 403, 407, 451])
    rate_limit_status: list[int] = Field(default_factory=lambda: [429])
    retry_status: list[int] = Field(default_factory=lambda: [500, 502, 503, 504, 520, 522, 524])
    not_found_status: list[int] = Field(default_factory=lambda: [404, 410])
    challenge_markers: list[str] = Field(default_factory=lambda: list(DEFAULT_CHALLENGE_MARKERS))
    block_markers: list[str] = Field(default_factory=lambda: list(DEFAULT_BLOCK_MARKERS))
    # How long to let an interstitial JS challenge resolve itself before
    # treating the page as blocked.
    challenge_wait_s: float = Field(20.0, ge=0)
    # Only scan the first N characters of the HTML for markers.
    scan_chars: int = Field(200_000, gt=0)


# ---------------------------------------------------------------------- behavior


class BehaviorConfig(_Model):
    humanize: bool = True
    mouse_moves: tuple[int, int] = (2, 5)
    scrolls: tuple[int, int] = (1, 4)
    dwell_s: tuple[float, float] = (1.5, 4.0)
    # Visited at the start of every browser session (e.g. the homepage) so the
    # first deep-link request does not arrive "cold".
    warmup_urls: list[str] = Field(default_factory=list)

    @field_validator("dwell_s")
    @classmethod
    def _dwell(cls, value: tuple[float, float]) -> tuple[float, float]:
        return _check_range(value, "dwell_s")

    @field_validator("mouse_moves", "scrolls")
    @classmethod
    def _int_range(cls, value: tuple[int, int]) -> tuple[int, int]:
        _check_range((float(value[0]), float(value[1])), "range")
        return value


# ----------------------------------------------------------------------- targets


class FieldSpec(_Model):
    selector: str = ""  # relative to the item; empty = the item element itself
    attr: str = "text"  # text | html | inner_text | <any attribute>
    multiple: bool = False
    absolute: bool = True  # resolve href/src to absolute URLs


class PaginationSpec(_Model):
    selector: str
    max_pages: int = Field(10, ge=1)


class FollowSpec(_Model):
    selector: str
    target: str | None = None  # target that processes followed pages (default: same)
    max_depth: int = Field(1, ge=1)
    same_domain: bool = True
    limit_per_page: int | None = Field(None, ge=1)


class TargetConfig(_Model):
    name: str
    start_urls: list[str] = Field(default_factory=list)
    url_file: Path | None = None
    wait_for: str | None = None
    wait_for_timeout_s: float = Field(15.0, gt=0)
    item_selector: str | None = None
    fields: dict[str, FieldSpec] = Field(default_factory=dict)
    extractor: str | None = None  # "package.module:function"
    next_page: PaginationSpec | None = None
    follow: FollowSpec | None = None
    allowed_domains: list[str] = Field(default_factory=list)

    @field_validator("fields", mode="before")
    @classmethod
    def _shorthand_fields(cls, value: Any) -> Any:
        # Allow `title: "h2"` as shorthand for `title: {selector: "h2"}`.
        if isinstance(value, dict):
            return {k: ({"selector": v} if isinstance(v, str) else v) for k, v in value.items()}
        return value


# ------------------------------------------------------------------------ output


class OutputConfig(_Model):
    dir: Path | None = None  # default: <data_dir>/output
    include_metadata: bool = True


class OrchestratorConfig(_Model):
    workers: int = Field(2, ge=1)
    idle_poll_s: float = Field(2.0, gt=0)
    shutdown_grace_s: float = Field(30.0, ge=0)


# -------------------------------------------------------------------------- root


class Config(_Model):
    run: RunConfig = Field(default_factory=RunConfig)
    browser: BrowserConfig = Field(default_factory=BrowserConfig)
    stealth: StealthConfig = Field(default_factory=StealthConfig)
    profiles: list[ProfileConfig] = Field(default_factory=list)
    proxies: ProxiesConfig = Field(default_factory=ProxiesConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    detection: DetectionConfig = Field(default_factory=DetectionConfig)
    behavior: BehaviorConfig = Field(default_factory=BehaviorConfig)
    orchestrator: OrchestratorConfig = Field(default_factory=OrchestratorConfig)
    targets: list[TargetConfig] = Field(default_factory=list)
    output: OutputConfig = Field(default_factory=OutputConfig)

    @model_validator(mode="after")
    def _cross_checks(self) -> Config:
        if not self.profiles:
            raise ValueError("at least one browser profile is required (profiles / profiles_file)")
        names = [p.name for p in self.profiles]
        if len(set(names)) != len(names):
            raise ValueError("profile names must be unique")
        target_names = {t.name for t in self.targets}
        if len(target_names) != len(self.targets):
            raise ValueError("target names must be unique")
        for target in self.targets:
            if target.follow and target.follow.target and target.follow.target not in target_names:
                raise ValueError(
                    f"target {target.name!r} follows into unknown target {target.follow.target!r}"
                )
            if not target.fields and not target.extractor and not target.follow and not target.next_page:
                raise ValueError(f"target {target.name!r} has nothing to do (no fields/extractor/follow)")
        if not (self.proxies.gluetun or self.proxies.static or self.proxies.allow_direct):
            raise ValueError(
                "no proxies configured; add proxies.gluetun / proxies.static, or set "
                "proxies.allow_direct: true to deliberately scrape from this machine's IP"
            )
        return self

    @property
    def output_dir(self) -> Path:
        return self.output.dir or (self.run.data_dir / "output")

    def target(self, name: str) -> TargetConfig:
        for target in self.targets:
            if target.name == name:
                return target
        raise KeyError(name)


def _read_yaml(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return yaml.safe_load(handle)
    except FileNotFoundError as exc:
        raise ConfigError(f"config file not found: {path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc


def load_config(path: str | Path, overrides: dict[str, Any] | None = None) -> Config:
    """Load, interpolate and validate a config file."""
    path = Path(path).resolve()
    raw = _read_yaml(path) or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: top level must be a mapping")
    raw = interpolate_env(raw)
    base_dir = path.parent

    profiles_file = raw.pop("profiles_file", None)
    if profiles_file:
        profiles_path = (base_dir / profiles_file).resolve()
        loaded = interpolate_env(_read_yaml(profiles_path) or {})
        extra = loaded.get("profiles", loaded) if isinstance(loaded, dict) else loaded
        if not isinstance(extra, list):
            raise ConfigError(f"{profiles_path}: expected a list of profiles")
        raw["profiles"] = list(raw.get("profiles") or []) + extra

    for target in raw.get("targets") or []:
        if isinstance(target, dict) and target.get("url_file"):
            target["url_file"] = str((base_dir / target["url_file"]).resolve())

    for dotted, value in (overrides or {}).items():
        node = raw
        *parents, leaf = dotted.split(".")
        for key in parents:
            node = node.setdefault(key, {})
        node[leaf] = value

    try:
        return Config.model_validate(raw)
    except ValueError as exc:
        raise ConfigError(f"invalid configuration in {path}:\n{exc}") from exc
