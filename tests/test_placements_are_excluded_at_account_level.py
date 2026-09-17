# -*- coding: utf-8 -*-
"""Площадки исключаются в Account controls, когда у ad set ручного выбора нет (16.09).

Живое чтение Doc21, 22:35: карточка Placements — «Advantage+ on», «Placement
value rules» и строка «Account controls · Excluded placements: None»;
раскрывается в «Edit placement controls»; окно «Placement controls for this
ad account» — переключатель «My business can only advertise on specific
placements», под ним группы «Apps and sites» и «Feeds» с четырьмя
галочками, внизу Cancel / Review changes. Страница ниже повторяет эту форму.
Шаг подтверждения после Review changes вживую ещё не видели — здесь он
«Confirm», и шаг сохраняет снимок страницы, чтобы следующий прогон показал
настоящий.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import meta_dom as md  # noqa: E402
from src.fb_ads_rpa import run_telemetry  # noqa: E402

_PAGE = """
<!doctype html><html><body>
<div id="card">
  <div role="heading">Placements</div><div>Advantage+ on</div>
  <div role="button" aria-expanded="true">Placement value rules</div>
  <div role="button" aria-expanded="false" id="acrow" onclick="expand()">Account controls<div><span id="summary"><b>Excluded placements:</b> %(summary)s</span></div></div>
  <div id="acbody" style="display:none">Account controls let you limit where your ads appear.
    <div role="button" onclick="openDlg()">Edit placement controls</div></div>
</div>
<div role="dialog" id="dlg" style="display:none">
  <div>Placement controls for this ad account</div>
  <label>My business can only advertise on specific placements
    <input type="checkbox" role="switch" aria-label="My business can only advertise on specific placements" id="sw" %(sw)s onchange="document.getElementById('list').style.display=this.checked?'block':'none'"></label>
  <div id="list" style="display:%(list)s">
    <div role="button" aria-expanded="true">Apps and sites</div>
    <div><div><span>Audience Network native, banner and interstitial</span></div><input type="checkbox" %(c)s></div>
    <div><div><span>Audience Network rewarded videos</span></div><input type="checkbox" %(c)s></div>
    <div role="button" aria-expanded="true">Feeds</div>
    <div><div><span>Facebook Marketplace</span></div><input type="checkbox" %(c)s></div>
    <div><div><span>Facebook right column</span></div><input type="checkbox" %(c)s></div>
  </div>
  <div role="button" onclick="window.log.push('cancel');hide('dlg')">Cancel</div>
  <div role="button" onclick="review()">Review changes</div>
</div>
<div id="toast" style="display:none;position:fixed;right:0;bottom:0">
  <div><span>Account controls updating</span><div role="button" aria-label="Close" onclick="window.log.push('toast_closed');hide('toast')"><svg width="10" height="10"></svg></div></div>
  <div>Your account controls have been saved.</div>
</div>
<div role="dialog" id="rev" style="display:none">
  <div>Review changes</div>
  <div role="button" onclick="confirmIt()">Confirm</div>
</div>
<script>
  window.log = [];
  function hide(id) { document.getElementById(id).style.display = 'none'; }
  function expand() { document.getElementById('acrow').setAttribute('aria-expanded','true'); document.getElementById('acbody').style.display='block'; }
  function openDlg() { window.log.push('open'); document.getElementById('dlg').style.display='block'; }
  function review() { window.log.push('review'); hide('dlg'); document.getElementById('rev').style.display='block'; }
  function confirmIt() {
    window.log.push('confirm'); hide('rev'); document.getElementById('toast').style.display = 'block';
    const off = [...document.querySelectorAll('#list input')].filter(i => !i.checked).length;
    document.getElementById('summary').innerHTML = '<b>Excluded placements:</b> ' + (off ? [...document.querySelectorAll('#list span')].map(x => x.textContent).join(', ') : 'None');
  }
</script>
</body></html>
"""


def _html(*, done: bool = False) -> str:
    return _PAGE % {
        "summary": ", ".join(md._ACCOUNT_EXCLUDED_PLACEMENTS) if done else "None",
        "sw": "checked" if done else "",
        "list": "block" if done else "none",
        "c": "" if done else "checked",
    }


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=os.environ.get("ACL_BROWSER_EXECUTABLE") or None)
        try:
            yield b
        finally:
            b.close()


@pytest.fixture
def page(browser, monkeypatch, tmp_path):
    monkeypatch.setattr(md, "_sleep_h", lambda s: None)
    monkeypatch.setattr(md, "_human_jitter", lambda base, spread: 0.0)
    monkeypatch.setattr(run_telemetry, "TELEMETRY_PATH", tmp_path / "telemetry.jsonl")
    import src.fb_ads_rpa.page_capture as pc
    monkeypatch.setattr(pc, "capture", lambda *a, **k: "")
    ctx = browser.new_context(viewport={"width": 1280, "height": 800})
    p = ctx.new_page()
    try:
        yield p
    finally:
        ctx.close()


def _rows(tmp_path):
    f = tmp_path / "telemetry.jsonl"
    return [json.loads(l) for l in f.read_text(encoding="utf-8").splitlines() if l.strip()] if f.exists() else []


def test_the_four_placements_are_excluded_and_confirmed(page, tmp_path):
    page.set_content(_html())
    report: dict = {}
    assert md.ensure_account_placement_exclusions(page, report) is True
    assert page.evaluate("window.log") == ["open", "review", "confirm", "toast_closed"]
    assert not page.is_visible("#toast")
    assert page.evaluate("[...document.querySelectorAll('#list input')].every(i => !i.checked)")
    assert report["account_exclusions"] == "excluded"
    assert md._excluded_placements_summary(page) == ", ".join(md._ACCOUNT_EXCLUDED_PLACEMENTS)
    row = [r for r in _rows(tmp_path) if r["step"] == "account_placement_exclusions"][-1]
    assert row["ok"] is True and sorted(row["diag"]["changed"]) == sorted(md._ACCOUNT_EXCLUDED_PLACEMENTS)


def test_already_excluded_changes_nothing_and_cancels(page):
    page.set_content(_html(done=True))
    report: dict = {}
    assert md.ensure_account_placement_exclusions(page, report) is True
    assert page.evaluate("window.log") == ["open", "cancel"]
    assert report["account_exclusions"] == "already_excluded"


def test_a_missing_placement_fails_and_leaves_nothing_saved(page):
    page.set_content(_html().replace("Facebook right column", "Something new"))
    report: dict = {}
    assert md.ensure_account_placement_exclusions(page, report) is False
    assert "review" not in page.evaluate("window.log")
    assert report["account_exclusions"] == "placements_not_unchecked"
    assert report["missing"] == ["Facebook right column"]


def test_the_card_with_only_account_controls_is_recognised(page):
    page.set_content(_html())
    assert md._placements_exclusion_removed(page) is True


def test_a_row_playwright_calls_invisible_is_still_opened(page):
    """Прогон 0054550ac608: `row.click` — TimeoutError на обоих аккаунтах."""
    page.set_content(_html().replace('id="acrow"', 'id="acrow" style="height:0;overflow:hidden"'))
    assert md.ensure_account_placement_exclusions(page, {}) is True
    assert page.evaluate("window.log")[:3] == ["open", "review", "confirm"]
