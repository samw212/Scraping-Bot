"""Stealth layer: launch flags, init scripts and per-page CDP overrides.

Three cooperating layers, each driven by the active profile:

1. **Launch flags** (:meth:`StealthKit.launch_args`) — removes
   ``navigator.webdriver`` natively, pins the UI language, forces WebRTC to
   stay behind the proxy, and keeps WebGL available on GPU-less hosts.
2. **playwright-stealth** — the community evasion bundle (``window.chrome``,
   plugins, permissions, codecs, ...). Modules that the profile layer handles
   better (UA, platform, client hints, hardwareConcurrency, WebGL vendor) are
   switched off so the two layers never fight.
3. **Profile layer** — ``fingerprint.js`` (hardware, screen, WebGL, canvas and
   audio noise) plus a CDP ``Emulation.setUserAgentOverride`` that sets the UA,
   ``navigator.platform``, ``Accept-Language`` and full UA client hints
   natively, so headers and JS APIs agree.
"""

from __future__ import annotations

import json
import logging
from importlib import resources
from typing import Any

from ..config import StealthConfig
from ..profiles import ResolvedProfile

log = logging.getLogger(__name__)

_FINGERPRINT_TEMPLATE: str | None = None
_PLACEHOLDER = "const OPTS = __SCRAPEBOT_OPTS__;"


def _fingerprint_template() -> str:
    global _FINGERPRINT_TEMPLATE
    if _FINGERPRINT_TEMPLATE is None:
        _FINGERPRINT_TEMPLATE = (resources.files(__package__) / "fingerprint.js").read_text("utf-8")
    return _FINGERPRINT_TEMPLATE


def build_fingerprint_script(options: dict[str, Any]) -> str:
    # json.dumps output is a valid JS literal; escape "</" defensively.
    payload = json.dumps(options, separators=(",", ":")).replace("</", "<\\/")
    template = _fingerprint_template()
    if _PLACEHOLDER not in template:  # pragma: no cover - packaging error
        raise RuntimeError("fingerprint.js is missing its options placeholder")
    return template.replace(_PLACEHOLDER, f"const OPTS = {payload};", 1)


class StealthKit:
    """Applies all stealth layers for one resolved profile."""

    # playwright-stealth modules superseded by the profile layer.
    _SUPERSEDED_WITH_CLIENT_HINTS = ("navigator_user_agent", "navigator_user_agent_data", "navigator_platform", "sec_ch_ua")

    # playwright-stealth shims that only make sense for the legacy headless
    # shell. In new-headless / headful Chromium the native values are already
    # consistent, and the shims make them *worse* (e.g. the permissions shim
    # reports state "default", which real Chrome never returns).
    _HEADLESS_SHELL_ONLY = ("navigator_plugins", "navigator_permissions")

    def __init__(self, config: StealthConfig, profile: ResolvedProfile, *, headless_shell: bool = False):
        self.config = config
        self.profile = profile
        self.headless_shell = headless_shell

    # ------------------------------------------------------------- launch
    def launch_args(self) -> list[str]:
        cfg = self.profile.config
        args = [
            "--disable-blink-features=AutomationControlled",
            f"--lang={cfg.locale}",
            f"--accept-lang={','.join(cfg.languages)}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-search-engine-choice-screen",
            # Without a GPU, recent Chromium refuses the SwiftShader fallback and
            # WebGL disappears entirely — a much stronger signal than a spoofed renderer.
            "--enable-unsafe-swiftshader",
            f"--window-size={cfg.screen.width},{cfg.screen.height}",
        ]
        if self.config.webrtc_ip_policy:
            args += [
                f"--force-webrtc-ip-handling-policy={self.config.webrtc_ip_policy}",
                f"--webrtc-ip-handling-policy={self.config.webrtc_ip_policy}",
            ]
        return args

    # ------------------------------------------------------------ context
    def playwright_stealth(self) -> Any | None:
        if not self.config.playwright_stealth:
            return None
        try:
            from playwright_stealth import Stealth
        except ImportError:  # pragma: no cover - dependency is declared
            log.warning("playwright-stealth is not installed; skipping that layer")
            return None
        cfg = self.profile.config
        options: dict[str, Any] = {
            "navigator_languages_override": tuple(cfg.languages),
            "navigator_platform_override": cfg.platform,
            "navigator_user_agent_override": self.profile.user_agent,
            # Hard-coded to 4 upstream; the profile layer sets the real value.
            "navigator_hardware_concurrency": False,
            # The profile layer patches WebGL more completely.
            "webgl_vendor": False,
        }
        if self.config.client_hints:
            for key in self._SUPERSEDED_WITH_CLIENT_HINTS:
                options[key] = False
            # Explicit None: upstream defaults the platform override to "Win32".
            options["navigator_user_agent_override"] = None
            options["navigator_platform_override"] = None
        if not self.headless_shell:
            for key in self._HEADLESS_SHELL_ONLY:
                options[key] = False
        options.update(self.config.playwright_stealth_options)
        return Stealth(**options)

    def fingerprint_script(self) -> str:
        cfg = self.config
        options = self.profile.fingerprint_options(
            hardware=cfg.hardware,
            screen=cfg.screen,
            webgl=cfg.webgl,
            canvas=cfg.canvas_noise,
            audio=cfg.audio_noise,
        )
        return build_fingerprint_script(options)

    async def apply_to_context(self, context: Any) -> None:
        stealth = self.playwright_stealth()
        if stealth is not None:
            await stealth.apply_stealth_async(context)
        await context.add_init_script(script=self.fingerprint_script())

    # --------------------------------------------------------------- page
    async def apply_to_page(self, context: Any, page: Any) -> Any | None:
        """Install native UA / client-hint overrides on ``page`` via CDP.

        Must run before the page's first navigation. Returns the CDP session,
        which must stay referenced: overrides are dropped when it detaches.
        """
        if not self.config.client_hints:
            return None
        try:
            session = await context.new_cdp_session(page)
        except Exception as exc:  # non-Chromium engines
            log.debug("CDP unavailable, client hints not applied: %s", exc)
            return None
        cfg = self.profile.config
        await session.send(
            "Emulation.setUserAgentOverride",
            {
                "userAgent": self.profile.user_agent,
                "acceptLanguage": ",".join(cfg.languages),
                "platform": cfg.platform,
                "userAgentMetadata": self.profile.ua_metadata(),
            },
        )
        return session
