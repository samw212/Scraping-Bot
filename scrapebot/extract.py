"""Item and link extraction.

Extraction runs on the serialized DOM (``page.content()``) in Python using
parsel, never as JavaScript inside the page. Anti-bot scripts can observe
``querySelectorAll`` storms and other DOM probing from injected code; parsing
off-page is invisible to them.

Custom extractors: set ``extractor: "package.module:function"`` on a target.
The function receives an :class:`ExtractContext` and may be sync or async. It
returns a list of item dicts, a single dict, or None.
"""

from __future__ import annotations

import importlib
import inspect
import re
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urljoin, urlsplit

from parsel import Selector

from .config import FieldSpec, TargetConfig

_WS = re.compile(r"\s+")
_URL_ATTRS = {"href", "src", "action", "data-src", "data-href"}


@dataclass
class ExtractContext:
    url: str
    html: str
    target: TargetConfig
    page: Any = None  # live Playwright page, for extractors that need interaction
    job: Any = None
    selector: Selector = field(init=False)

    def __post_init__(self) -> None:
        self.selector = Selector(text=self.html, base_url=self.url)

    @property
    def base_url(self) -> str:
        base = self.selector.css("base::attr(href)").get()
        return urljoin(self.url, base) if base else self.url


def _read(node: Selector, spec: FieldSpec, base_url: str) -> str | None:
    if spec.attr == "text" or spec.attr == "inner_text":
        value = node.xpath("string()").get()
        return _WS.sub(" ", value).strip() if value is not None else None
    if spec.attr == "html":
        return node.get()
    value = node.attrib.get(spec.attr)
    if value is None:
        return None
    value = value.strip()
    if spec.absolute and spec.attr in _URL_ATTRS:
        value = urljoin(base_url, value)
    return value


def _extract_fields(root: Selector, fields: dict[str, FieldSpec], base_url: str) -> dict[str, Any]:
    item: dict[str, Any] = {}
    for name, spec in fields.items():
        nodes = root.css(spec.selector) if spec.selector else [root]
        if spec.multiple:
            item[name] = [v for v in (_read(n, spec, base_url) for n in nodes) if v is not None]
        else:
            item[name] = _read(nodes[0], spec, base_url) if nodes else None
    return item


def extract_items(ctx: ExtractContext) -> list[dict[str, Any]]:
    target = ctx.target
    if not target.fields:
        return []
    base = ctx.base_url
    if target.item_selector:
        roots = ctx.selector.css(target.item_selector)
        items = [_extract_fields(r, target.fields, base) for r in roots]
    else:
        items = [_extract_fields(ctx.selector, target.fields, base)]
    # Drop items where every field came back empty.
    return [i for i in items if any(v not in (None, "", []) for v in i.values())]


def extract_links(ctx: ExtractContext, selector: str, *, same_domain: bool, allowed_domains: list[str], limit: int | None = None) -> list[str]:
    base = ctx.base_url
    origin_host = urlsplit(ctx.url).hostname or ""
    links: list[str] = []
    seen: set[str] = set()
    for node in ctx.selector.css(selector):
        if isinstance(node.root, str):
            # Selector already points at a value, e.g. "a::attr(href)".
            href = node.root
        else:
            href = node.attrib.get("href")
        if not href:
            continue
        href = href.strip()
        if href.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        url = urljoin(base, href).split("#", 1)[0]
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https"):
            continue
        host = parts.hostname or ""
        if same_domain and not _host_allowed(host, [origin_host, *allowed_domains]):
            continue
        if not same_domain and allowed_domains and not _host_allowed(host, allowed_domains):
            continue
        if url not in seen:
            seen.add(url)
            links.append(url)
        if limit and len(links) >= limit:
            break
    return links


def _host_allowed(host: str, allowed: list[str]) -> bool:
    return any(host == a or host.endswith("." + a) for a in allowed if a)


_CUSTOM_CACHE: dict[str, Callable[..., Any]] = {}


def load_extractor(path: str) -> Callable[..., Any]:
    if path not in _CUSTOM_CACHE:
        module_name, _, attr = path.partition(":")
        if not module_name or not attr:
            raise ValueError(f"extractor must look like 'package.module:function', got {path!r}")
        module = importlib.import_module(module_name)
        _CUSTOM_CACHE[path] = getattr(module, attr)
    return _CUSTOM_CACHE[path]


async def run_extractors(ctx: ExtractContext) -> list[dict[str, Any]]:
    items = extract_items(ctx)
    if ctx.target.extractor:
        fn = load_extractor(ctx.target.extractor)
        result = fn(ctx)
        if inspect.isawaitable(result):
            result = await result
        if isinstance(result, dict):
            items.append(result)
        elif result:
            items.extend(result)
    return items
