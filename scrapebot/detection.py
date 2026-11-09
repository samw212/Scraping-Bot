"""Classify a fetched page: success, block, challenge, rate limit, ..."""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone

from .config import DetectionConfig


class Outcome(str, enum.Enum):
    OK = "ok"
    CHALLENGE = "challenge"  # interstitial JS/captcha challenge (may self-resolve)
    BLOCKED = "blocked"  # hard block: identity/IP is burned for now
    RATE_LIMITED = "rate_limited"  # slow down, same identity may continue later
    NOT_FOUND = "not_found"  # permanent, do not retry
    SERVER_ERROR = "server_error"  # transient upstream error, retry
    NETWORK_ERROR = "network_error"  # timeout / proxy / DNS, retry

    @property
    def is_block(self) -> bool:
        return self in (Outcome.CHALLENGE, Outcome.BLOCKED)


@dataclass
class Verdict:
    outcome: Outcome
    reason: str = ""
    retry_after_s: float | None = None


def parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip()
    if re.fullmatch(r"\d+(\.\d+)?", value):
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


class Detector:
    def __init__(self, config: DetectionConfig):
        self.config = config
        self._challenge = [m.lower() for m in config.challenge_markers]
        self._block = [m.lower() for m in config.block_markers]

    def find_marker(self, html: str) -> tuple[Outcome, str] | None:
        text = html[: self.config.scan_chars].lower()
        for marker in self._block:
            if marker in text:
                return Outcome.BLOCKED, marker
        for marker in self._challenge:
            if marker in text:
                return Outcome.CHALLENGE, marker
        return None

    def classify(self, status: int | None, html: str, headers: dict[str, str] | None = None) -> Verdict:
        headers = {k.lower(): v for k, v in (headers or {}).items()}
        cfg = self.config
        if status is None:
            return Verdict(Outcome.NETWORK_ERROR, "no response")

        # Content markers win over status codes: anti-bot vendors serve
        # challenges with 403/429/503 and sometimes even 200.
        marker = self.find_marker(html)
        if marker is not None:
            outcome, text = marker
            return Verdict(outcome, f"marker {text!r} (HTTP {status})")

        if status in cfg.rate_limit_status:
            return Verdict(Outcome.RATE_LIMITED, f"HTTP {status}", parse_retry_after(headers.get("retry-after")))
        if status in cfg.block_status:
            return Verdict(Outcome.BLOCKED, f"HTTP {status}")
        if status in cfg.not_found_status:
            return Verdict(Outcome.NOT_FOUND, f"HTTP {status}")
        if status in cfg.retry_status or status >= 500:
            return Verdict(Outcome.SERVER_ERROR, f"HTTP {status}", parse_retry_after(headers.get("retry-after")))
        if status >= 400:
            return Verdict(Outcome.NOT_FOUND, f"HTTP {status}")
        return Verdict(Outcome.OK, f"HTTP {status}")
