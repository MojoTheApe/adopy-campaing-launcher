# -*- coding: utf-8 -*-
"""The bench catalogue stays true to the files it names (ACL-1433).

`tests/bench/catalogue.json` lists every Meta layout the robot has met, per
robot step, with its fixture, test and expected result. These checks keep it
honest: every fixture in the bench is catalogued, every catalogued fixture,
fragment and test exists, an `xfail` row has an xfail in its test, and the
rendered CATALOGUE.md is current.
"""
from __future__ import annotations

import re

from scripts import bench_catalogue
from tests.bench_support import FIXTURES, ROOT, fragments

CAT = bench_catalogue.load()
LAYOUTS = [(s["step"], lay) for s in CAT["steps"] for lay in s["layouts"]]


def _names(field) -> list[str]:
    return [x.strip() for x in str(field or "").split(",") if x.strip()]


def test_every_fixture_in_the_bench_is_catalogued():
    named = {lay["fixture"] for _, lay in LAYOUTS if lay.get("fixture")}
    on_disk = {p.name for p in FIXTURES.glob("*.html")}
    assert on_disk - named == set(), "fixtures missing from tests/bench/catalogue.json"


def test_every_catalogued_file_exists():
    for step, lay in LAYOUTS:
        assert (ROOT / lay["test"]).exists(), (step, lay["test"])
        if lay.get("fixture"):
            assert (FIXTURES / lay["fixture"]).exists(), (step, lay["fixture"])


def test_every_catalogued_fragment_exists():
    for step, lay in LAYOUTS:
        if not lay.get("fixture") or not lay.get("fragment"):
            continue
        have = fragments(FIXTURES / lay["fixture"])
        for name in _names(lay["fragment"]):
            assert name in have, (step, lay["fixture"], name)


def test_each_test_reads_its_fixture():
    for step, lay in LAYOUTS:
        if lay.get("fixture"):
            text = (ROOT / lay["test"]).read_text(encoding="utf-8")
            assert lay["fixture"] in text, (step, lay["test"], lay["fixture"])


def test_status_is_known_and_xfail_rows_are_xfail_tests():
    for step, lay in LAYOUTS:
        assert lay["status"] in ("pass", "xfail", "reference"), (step, lay["status"])
        if lay["status"] == "xfail":
            assert lay.get("bug"), (step, lay["layout"])
            assert re.search(r"xfail\(strict=True", (ROOT / lay["test"]).read_text(encoding="utf-8"))


def test_every_step_has_a_fixture():
    for s in CAT["steps"]:
        assert any(lay.get("fixture") for lay in s["layouts"]), s["step"]


def test_the_markdown_is_current():
    md = (ROOT / "tests" / "bench" / "CATALOGUE.md").read_text(encoding="utf-8")
    assert md == bench_catalogue.render(CAT), "run: python scripts/bench_catalogue.py --write"
