"""Human-like interaction: curved mouse paths, wheel scrolling, dwell time.

Behavioral bot detection scores how a session *moves*, not just what it
looks like: perfectly straight, instant mouse jumps and zero scroll activity
are strong signals. These helpers add organic noise cheaply.
"""

from __future__ import annotations

import asyncio
import math
import random
from typing import Any

from .config import BehaviorConfig

Point = tuple[float, float]


def _ease_in_out(t: float) -> float:
    return 0.5 - 0.5 * math.cos(math.pi * t)


def bezier_path(start: Point, end: Point, rng: random.Random, steps: int | None = None) -> list[Point]:
    """Cubic Bézier from ``start`` to ``end`` with randomized control points."""
    (x0, y0), (x3, y3) = start, end
    distance = math.hypot(x3 - x0, y3 - y0)
    steps = steps or max(8, min(60, int(distance / 12)))
    spread = max(20.0, distance * 0.3)
    x1 = x0 + (x3 - x0) * rng.uniform(0.2, 0.4) + rng.uniform(-spread, spread)
    y1 = y0 + (y3 - y0) * rng.uniform(0.2, 0.4) + rng.uniform(-spread, spread)
    x2 = x0 + (x3 - x0) * rng.uniform(0.6, 0.8) + rng.uniform(-spread, spread)
    y2 = y0 + (y3 - y0) * rng.uniform(0.6, 0.8) + rng.uniform(-spread, spread)
    points = []
    for i in range(1, steps + 1):
        t = _ease_in_out(i / steps)
        mt = 1 - t
        x = mt**3 * x0 + 3 * mt**2 * t * x1 + 3 * mt * t**2 * x2 + t**3 * x3
        y = mt**3 * y0 + 3 * mt**2 * t * y1 + 3 * mt * t**2 * y2 + t**3 * y3
        # Sub-pixel hand tremor.
        points.append((x + rng.gauss(0, 0.6), y + rng.gauss(0, 0.6)))
    points[-1] = end
    return points


class Humanizer:
    def __init__(self, config: BehaviorConfig, rng: random.Random, viewport: tuple[int, int]):
        self.config = config
        self.rng = rng
        self.width, self.height = viewport
        self.position: Point = (rng.uniform(0.3, 0.7) * self.width, rng.uniform(0.3, 0.7) * self.height)

    def _clamp(self, point: Point) -> Point:
        return (min(max(point[0], 1), self.width - 2), min(max(point[1], 1), self.height - 2))

    async def move_to(self, page: Any, target: Point) -> None:
        for x, y in bezier_path(self.position, target, self.rng):
            await page.mouse.move(*self._clamp((x, y)))
            await asyncio.sleep(self.rng.uniform(0.004, 0.018))
        self.position = target

    async def scroll(self, page: Any) -> None:
        direction = -1 if self.rng.random() < 0.2 else 1
        total = direction * self.rng.uniform(250, 900)
        ticks = self.rng.randint(3, 8)
        for _ in range(ticks):
            await page.mouse.wheel(0, total / ticks)
            await asyncio.sleep(self.rng.uniform(0.03, 0.12))

    async def perform(self, page: Any) -> None:
        """One burst of idle-looking activity on the current page."""
        cfg = self.config
        if not cfg.humanize:
            return
        rng = self.rng
        actions = ["move"] * rng.randint(*cfg.mouse_moves) + ["scroll"] * rng.randint(*cfg.scrolls)
        rng.shuffle(actions)
        for action in actions:
            if action == "move":
                target = (rng.uniform(0.05, 0.95) * self.width, rng.uniform(0.08, 0.92) * self.height)
                await self.move_to(page, target)
            else:
                await self.scroll(page)
            await asyncio.sleep(rng.uniform(0.25, 1.2))
        await asyncio.sleep(rng.uniform(*cfg.dwell_s))
