"""robots.txt compliance.

robots.txt is fetched through the browser context's own request client, so the
request uses the session's proxy (never the host IP) and cookie jar. Results
are cached per origin for the lifetime of the run.

Status handling follows RFC 9309 loosely: 2xx is parsed, 4xx means "no
restrictions", and 5xx or network errors are treated as allowed for now but are
not cached, so the next URL on that origin tries again.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

log = logging.getLogger(__name__)


class RobotsCache:
    def __init__(self, user_agent: str = "*", timeout_s: float = 15.0):
        self.user_agent = user_agent
        self.timeout_ms = timeout_s * 1000
        self._parsers: dict[str, RobotFileParser] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    @staticmethod
    def origin(url: str) -> str:
        parts = urlsplit(url)
        return f"{parts.scheme}://{parts.netloc}"

    async def _load(self, request: Any, origin: str) -> RobotFileParser | None:
        parser = RobotFileParser()
        try:
            response = await request.get(f"{origin}/robots.txt", timeout=self.timeout_ms, fail_on_status_code=False)
            status = response.status
            body = await response.text() if 200 <= status < 300 else ""
        except Exception as exc:
            log.warning("robots.txt for %s unavailable (%s); allowing for now", origin, exc)
            return None
        if 200 <= status < 300:
            parser.parse(body.splitlines())
        elif 400 <= status < 500:
            parser.parse([])  # no robots.txt -> everything allowed
        else:
            log.warning("robots.txt for %s returned HTTP %d; allowing for now", origin, status)
            return None
        return parser

    async def allowed(self, request: Any, url: str) -> bool:
        origin = self.origin(url)
        parser = self._parsers.get(origin)
        if parser is None:
            lock = self._locks.setdefault(origin, asyncio.Lock())
            async with lock:
                parser = self._parsers.get(origin)
                if parser is None:
                    parser = await self._load(request, origin)
                    if parser is None:
                        return True
                    self._parsers[origin] = parser
        return parser.can_fetch(self.user_agent, url)
