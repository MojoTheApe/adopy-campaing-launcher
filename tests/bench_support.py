# -*- coding: utf-8 -*-
"""Shared helpers for the offline bench of Meta layouts (ACL-1433).

Every bench test loads a scrubbed fragment of a live capture into headless,
offline Chromium (all network requests aborted) and runs the robot's real step
function or JS reader on it. The catalogue of what is covered lives in
tests/bench/catalogue.json (rendered to tests/bench/CATALOGUE.md).

Usage in a test module:

    from tests.bench_support import FIXTURES, browser, offline_page, fragment  # noqa: F401
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FIXTURES = ROOT / "tests" / "fixtures" / "ads_manager"

_FRAGMENT_RE = re.compile(r"<!-- fragment: (\w+) \| source: [^>]*-->\n(.*?)(?=\n<!-- fragment: |\Z)", re.S)

#: Without Meta's CSS the wizard's step list would lie in a column; on screen it
#: is a row (every screenshot since 24.08). The robot's step reader decides
#: "row of step labels" by geometry, so the bench lays it out the same way.
WIZARD_ROW_CSS = (
    "<style>[role=dialog] [role=list]{display:flex; gap:18px}"
    " [role=dialog] [role=listitem]{display:flex; gap:4px}</style>"
)


def fragments(path: Path) -> dict[str, str]:
    """All `<!-- fragment: NAME | source: ... -->` blocks of a fixture file."""
    text = Path(path).read_text(encoding="utf-8")
    found = {m.group(1): m.group(2).strip() for m in _FRAGMENT_RE.finditer(text)}
    if not found:
        raise ValueError(f"{path.name}: no fragments")
    return found


def fragment(path: Path, name: str) -> str:
    return fragments(path)[name]


@pytest.fixture(scope="module")
def browser():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=os.environ.get("ACL_BROWSER_EXECUTABLE") or None)
        try:
            yield b
        finally:
            b.close()


@pytest.fixture
def offline_page(browser):
    ctx = browser.new_context(viewport={"width": 1600, "height": 1000})
    ctx.route("**/*", lambda route: route.abort())
    page = ctx.new_page()
    try:
        yield page
    finally:
        ctx.close()
