"""ACL-2509: connecting an account in the builder drawer keeps the country.

Connect reloads the page; the drawer used to rebuild with the first country of
the list and no account. The behaviour is checked by running the template's own
code (`acl2509_pick_harness.cjs`); the wiring is checked on the template text.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TPL = ROOT / "src" / "web" / "templates" / "report.html"


def test_the_page_code_keeps_country_and_selects_new_account():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    r = subprocess.run(
        [node, str(ROOT / "tests" / "acl2509_pick_harness.cjs"), str(TPL)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert r.returncode == 0, r.stderr + r.stdout


def _src() -> str:
    return TPL.read_text(encoding="utf-8")


def test_connect_remembers_the_country_before_the_form_runs():
    s = _src()
    i = s.index("act === 'connect' || act === 'connect-picked'")
    branch = s[i:i + 1500]
    assert "rememberPick(country)" in branch
    assert "forgetPick()" in branch, "closing the form must drop the parked pick"


def test_the_drawer_applies_the_parked_pick_when_it_paints():
    s = _src()
    i = s.index("function paintFoot()")
    body = s[i:i + 1800]
    assert re.search(r"takePick\(\)", body)
    assert "keep = afterConnect.country" in body
    assert "keepAccount = afterConnect.account" in body
