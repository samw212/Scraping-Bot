"""Example custom extractor.

Enable on a target with:  extractor: "examples.custom_extractor:extract"

Receives an ExtractContext:
  ctx.url       final page URL
  ctx.html      rendered HTML
  ctx.selector  parsel Selector over ctx.html (CSS + XPath)
  ctx.page      the live Playwright page (only if you must interact; every
                interaction is visible to the site's scripts)
  ctx.target    the TargetConfig, ctx.job the queue job

Return a list of dicts, a single dict, or None. May be sync or async.
"""

from __future__ import annotations

import json
from typing import Any


def extract(ctx: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    # Many sites embed structured data; it is more stable than CSS selectors.
    for raw in ctx.selector.css('script[type="application/ld+json"]::text').getall():
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        for entry in data if isinstance(data, list) else [data]:
            if isinstance(entry, dict) and entry.get("@type") in {"Product", "Person", "Article"}:
                items.append({"ld_type": entry["@type"], "ld": entry})
    return items
