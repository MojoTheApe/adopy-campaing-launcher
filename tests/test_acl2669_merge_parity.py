# -*- coding: utf-8 -*-
"""ACL-2669: `cbKeep.merge` (JS) и `merge_keep` (Python) обязаны давать одно и то же.

JS берётся живьём из report.html и исполняется в Chromium через Playwright
(`page.evaluate`), как в соседних браузерных тестах. Таблица случаев гоняется
через обе реализации; сравнивается каноническая JSON-запись результата.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.web import builder_drafts as bd  # noqa: E402

pytest.importorskip("playwright.sync_api")

# фикстуры браузера и страницы — из соседнего файла
from test_acl2669_drafts_never_shrink_browser import _local_dir, browser, make  # noqa: E402,F401


def camp(cid=None, **kw):
    c = {"fields": {"name": cid or "legacy"}, "shown": {}, "ads": 1, **kw}
    if cid:
        c["id"] = cid
    return c


def form(touched, *campaigns, tracking="t"):
    return {"touched": touched, "tracking": tracking, "campaigns": list(campaigns)}


def doc(forms, removed=None, v=4):
    d = {"v": v, "forms": forms}
    if removed is not None:
        d["removed"] = removed
    return d


CASES = {
    "no form key in incoming": (
        doc({"A": form(2, camp("a1")), "B": form(3, camp("b1"))}), doc({"A": form(2, camp("a1"))})),
    "incoming misses a campaign": (
        doc({"A": form(5, camp("a1"), camp("a2"))}), doc({"A": form(9, camp("a1"))})),
    "newer touched wins per key": (
        doc({"A": form(10, camp("a1", ads=1), tracking="old"), "B": form(50, camp("b1", ads=1))}),
        doc({"A": form(20, camp("a1", ads=2), tracking="new"), "B": form(5, camp("b1", ads=9))})),
    "touched tie goes to incoming": (
        doc({"A": form(10, camp("a1", ads=1))}), doc({"A": form(10, camp("a1", ads=2))})),
    "older incoming adds a unique campaign": (
        doc({"A": form(20, camp("a1"))}), doc({"A": form(10, camp("a1"), camp("a2"))})),
    "tombstone blocks the older copy": (
        doc({"A": form(20, camp("a1"))}, removed={"A#a2": 15}), doc({"A": form(10, camp("a1"), camp("a2"))})),
    "later edit beats the tombstone": (
        doc({"A": form(30, camp("a1"), camp("a2"))}, removed={"A#a2": 25}), doc({})),
    "tombstone newer than the newest copy drops the campaign": (
        doc({"A": form(20, camp("a1"), camp("a2"))}, removed={"A#a2": 25}), doc({})),
    "form tombstone drops the key": (
        doc({"A": form(20, camp("a1"))}, removed={"A": 25}), doc({})),
    "form tombstone older than the edit keeps it": (
        doc({"A": form(20, camp("a1"))}, removed={"A": 10}), doc({"A": form(25, camp("a1"))})),
    "empty result drops the key": (
        doc({"A": form(20, camp("a1"))}, removed={"A#a1": 30}), doc({"A": form(10, camp("a1"))})),
    "idless stored and idless incoming": (
        doc({"A": form(20, camp(), camp())}), doc({"A": form(30, camp())})),
    "idless older copy gets legacy ids": (
        doc({"A": form(20, camp(), camp())}), doc({"A": form(10, camp(), camp(), camp())})),
    "idless stored and legacy ids incoming": (
        doc({"A": form(10, camp(), camp())}), doc({"A": form(20, camp("legacy-0"), camp("legacy-1"))})),
    "legacy ids stored and idless incoming": (
        doc({"A": form(20, camp("legacy-0"), camp("legacy-1"))}), doc({"A": form(30, camp(), camp())})),
    "idless winner plus id in older": (
        doc({"A": form(10, camp("a1"))}), doc({"A": form(30, camp())})),
    "removed is a union with max, v is max": (
        doc({}, removed={"A": 1, "B": 9}, v=3), doc({}, removed={"A": 5, "C": 2}, v=4)),
    "campaign tombstone equals older.touched blocks it": (
        doc({"A": form(20, camp("a1"))}, removed={"A#a2": 10}), doc({"A": form(10, camp("a1"), camp("a2"))})),
    "campaign tombstone equals newer.touched keeps it": (
        doc({"A": form(20, camp("a1"), camp("a2"))}, removed={"A#a2": 20}), doc({})),
    "form tombstone equals result touched drops the form": (
        doc({"A": form(20, camp("a1"))}, removed={"A": 20}), doc({})),
    "non-dict and non-string-id campaigns in older": (
        doc({"A": form(20, camp("a1"))}),
        doc({"A": form(10, camp("a1"), "junk", {"id": 5, "fields": {}}, None, camp("a2"))})),
    "one side has no forms": (
        {"v": 4}, doc({"A": form(1, camp("a1"))})),
}


@pytest.mark.parametrize("name", list(CASES))
def test_js_and_python_merge_give_identical_results(make, name):
    p = make().open()
    cur, inc = CASES[name]
    js = p.evaluate("([c, i]) => cbKeep.merge(c, i)", [cur, inc])
    py = bd.merge_keep(json.loads(json.dumps(cur)), json.loads(json.dumps(inc)))
    canon = lambda d: json.dumps(d, sort_keys=True, ensure_ascii=False)
    assert canon(js) == canon(py), name
