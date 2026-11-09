"""Browser fingerprint profiles.

A :class:`ResolvedProfile` is a :class:`~scrapebot.config.ProfileConfig`
bound to a concrete browser version and timezone. It produces everything the
browser layer needs: the user agent, the User-Agent Client Hints metadata
(``Sec-CH-UA*`` headers and ``navigator.userAgentData``), the
``Accept-Language`` value and the options for the injected fingerprint script.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

from .config import ProfileConfig

# Chromium's GREASE algorithm (components/embedder_support/user_agent_utils.cc).
# Real Chrome derives the fake brand, its version and the brand order from the
# major version, so a hard-coded or random brand list is itself a signal.
_GREASE_CHARS = [" ", "(", ":", "-", ".", "/", ")", ";", "=", "?", "_"]
_GREASE_VERSIONS = ["8", "99", "24"]
_BRAND_ORDERS = [(0, 1, 2), (0, 2, 1), (1, 0, 2), (1, 2, 0), (2, 0, 1), (2, 1, 0)]


@dataclass(frozen=True)
class BrowserVersion:
    full: str

    @property
    def major(self) -> int:
        return int(self.full.split(".")[0])

    @classmethod
    def parse(cls, value: str) -> BrowserVersion:
        match = re.search(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:\.(\d+))?", value)
        if not match:
            raise ValueError(f"cannot parse browser version from {value!r}")
        parts = [p or "0" for p in match.groups()]
        return cls(".".join(parts))


def brand_version_list(major: int, brand: str, full_version: str | None = None) -> list[dict[str, str]]:
    """Return the brand list exactly as Chrome ``major`` would report it.

    With ``full_version`` the list mirrors ``fullVersionList`` (Sec-CH-UA-Full-Version-List),
    otherwise ``brands`` (Sec-CH-UA).
    """
    seed = major
    grease_brand = f"Not{_GREASE_CHARS[seed % len(_GREASE_CHARS)]}A{_GREASE_CHARS[(seed + 1) % len(_GREASE_CHARS)]}Brand"
    grease_version = _GREASE_VERSIONS[seed % len(_GREASE_VERSIONS)]
    if full_version is not None:
        grease_version = f"{grease_version}.0.0.0"
        real_version = full_version
    else:
        real_version = str(major)
    order = _BRAND_ORDERS[seed % len(_BRAND_ORDERS)]
    slots: list[dict[str, str]] = [{}, {}, {}]
    slots[order[0]] = {"brand": grease_brand, "version": grease_version}
    slots[order[1]] = {"brand": "Chromium", "version": real_version}
    slots[order[2]] = {"brand": brand, "version": real_version}
    return slots


def accept_language(languages: list[str]) -> str:
    """Build an Accept-Language value the way Chrome does (q decreasing by 0.1)."""
    parts = []
    for index, lang in enumerate(languages):
        if index == 0:
            parts.append(lang)
        else:
            q = max(0.1, round(1.0 - 0.1 * index, 1))
            parts.append(f"{lang};q={q:g}")
    return ",".join(parts)


def stable_seed(*parts: str) -> int:
    digest = hashlib.sha256("|".join(parts).encode()).digest()
    return int.from_bytes(digest[:4], "big")


@dataclass
class ResolvedProfile:
    config: ProfileConfig
    version: BrowserVersion
    timezone: str
    # Deterministic per identity, so canvas/audio noise is stable for one
    # "user" across pages and sessions but differs between identities.
    noise_seed: int
    user_agent: str = field(init=False)

    def __post_init__(self) -> None:
        self.user_agent = self.config.user_agent.format(
            chrome_major=self.version.major, chrome_version=self.version.full
        )

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def accept_language(self) -> str:
        return accept_language(self.config.languages)

    def ua_metadata(self) -> dict[str, Any]:
        """CDP ``Emulation.UserAgentMetadata`` for ``Emulation.setUserAgentOverride``."""
        cfg = self.config
        return {
            "brands": brand_version_list(self.version.major, cfg.brand),
            "fullVersionList": brand_version_list(self.version.major, cfg.brand, self.version.full),
            "fullVersion": self.version.full,
            "platform": cfg.ua_platform,
            "platformVersion": cfg.ua_platform_version,
            "architecture": cfg.architecture,
            "model": cfg.model,
            "mobile": cfg.mobile,
            "bitness": cfg.bitness,
            "wow64": cfg.wow64,
        }

    def context_options(self) -> dict[str, Any]:
        """Keyword arguments for ``browser.new_context`` derived from the profile."""
        cfg = self.config
        options: dict[str, Any] = {
            "user_agent": self.user_agent,
            "locale": cfg.locale,
            "timezone_id": self.timezone,
            "viewport": {"width": cfg.viewport.width, "height": cfg.viewport.height},
            "screen": {"width": cfg.screen.width, "height": cfg.screen.height},
            "device_scale_factor": cfg.device_scale_factor,
            "is_mobile": cfg.mobile,
            "has_touch": cfg.max_touch_points > 0,
            "color_scheme": cfg.color_scheme,
        }
        # Accept-Language is set natively through the CDP UA override (see
        # stealth.StealthKit.apply_to_page). Injecting it as an "extra" header
        # would change the header order, which is itself fingerprinted.
        if cfg.extra_headers:
            options["extra_http_headers"] = dict(cfg.extra_headers)
        return options

    def fingerprint_options(self, *, hardware: bool, screen: bool, webgl: bool, canvas: bool, audio: bool) -> dict[str, Any]:
        """Options consumed by ``stealth/fingerprint.js``."""
        cfg = self.config
        sc = cfg.screen
        opts: dict[str, Any] = {"seed": self.noise_seed, "canvas": canvas, "audio": audio}
        if hardware:
            opts["hardware"] = {
                "hardwareConcurrency": cfg.hardware_concurrency,
                "deviceMemory": cfg.device_memory,
                "maxTouchPoints": cfg.max_touch_points,
            }
        if screen:
            avail_w = sc.avail_width if sc.avail_width is not None else sc.width
            avail_h = sc.avail_height if sc.avail_height is not None else sc.height - 40
            opts["screen"] = {
                "width": sc.width,
                "height": sc.height,
                "availWidth": avail_w,
                "availHeight": avail_h,
                "availLeft": 0,
                "availTop": 0,
                "colorDepth": sc.color_depth,
                "pixelDepth": sc.color_depth,
                "chromeHeight": sc.window_chrome_height,
            }
        if webgl and cfg.webgl is not None:
            opts["webgl"] = {
                "vendor": cfg.webgl.vendor,
                "renderer": cfg.webgl.renderer,
                "parameters": {str(k): v for k, v in cfg.webgl.parameters.items()},
            }
        return opts


def resolve_profile(config: ProfileConfig, version: BrowserVersion, *, timezone: str | None, identity_key: str) -> ResolvedProfile:
    """Bind a profile to a browser version and (proxy-derived) timezone."""
    if config.timezone != "auto":
        tz = config.timezone
    else:
        tz = timezone or config.fallback_timezone
    return ResolvedProfile(
        config=config,
        version=version,
        timezone=tz,
        noise_seed=stable_seed(config.name, identity_key),
    )
