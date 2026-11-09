"""Browser automation engine selection (Playwright or the Patchright fork).

Patchright is a drop-in Playwright fork that additionally hides CDP
``Runtime.enable`` leaks and other automation artefacts at the driver level.
Both expose the same async API, but their exception classes differ, so the
rest of the code gets them from here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Engine:
    name: str
    async_playwright: Any
    Error: type[Exception]
    TimeoutError: type[Exception]


def load_engine(name: str) -> Engine:
    if name == "patchright":
        try:
            from patchright.async_api import Error, TimeoutError, async_playwright
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError(
                "browser.engine is 'patchright' but the package is not installed: "
                "pip install patchright && patchright install chromium"
            ) from exc
    else:
        from playwright.async_api import Error, TimeoutError, async_playwright
    return Engine(name=name, async_playwright=async_playwright, Error=Error, TimeoutError=TimeoutError)
