# -*- coding: utf-8 -*-
"""The wizard's Media step while Meta's media picker loads, and once it has (ACL-1559).

Bench. `wizard_media_picker_2026_10_04.html` holds two scrubbed wizard dialogs
of the same Media-first step row (Media · Trim · Crop · Text · Creative
generation · Enhancements · Translation), both with "Media" as the current step:

- `loading` (2f140fa687fa/00_ad_creative_media_step, Facebook_1443, 04.10
  11:02): the picker area holds only three `role=progressbar aria-busy=true`
  "Loading..." bars; no Upload, no tabs, no "Search media", no grid; footer
  Next `aria-disabled`, Cancel enabled. The robot clicked the current Media
  step twice, probed the disabled Next four times and gave up with the
  generic `upload_button_missing`.
- `loaded` (232d398471bd/00_wizard_media, 04.10 07:36): the same step with the
  picker loaded - tabs Account / Video URL / Page, "Search media", "Filters 1",
  Upload (`role=button`, data-surface `...creative-tool-asset-picker-upload-button`),
  the drop zone and the library grid (`aria-label="grid"`, two tiles).

The robot must not click the current Media step nor press Next there; it
reads the picker (read-only) until it loads, then goes on to the library /
Upload; if it never loads, the reason is `media_picker_loading`.
"""
from __future__ import annotations

import json

import pytest

from src.fb_ads_rpa import meta_dom as md
from src.fb_ads_rpa import run_telemetry as rt
from tests.bench_support import FIXTURES, WIZARD_ROW_CSS, browser, fragment, offline_page  # noqa: F401

FIXTURE = FIXTURES / "wizard_media_picker_2026_10_04.html"
ROW = ["Media", "Trim", "Crop", "Text", "Creative generation", "Enhancements", "Translation"]


def _load(page, name: str) -> None:
    page.set_content(WIZARD_ROW_CSS + fragment(FIXTURE, name))


def _picker(page) -> dict:
    return page.evaluate(md._WIZARD_MEDIA_PICKER_JS)


# ── читатели на настоящей разметке ────────────────────────────────────────

@pytest.mark.parametrize("name", ["loading", "loaded"])
def test_media_is_the_current_step(offline_page, name):
    _load(offline_page, name)
    step = md._wizard_media_step(offline_page)
    assert step and step["found"] is True, step
    assert step["steps"] == ROW
    assert step["current"] == "Media"
    assert md._media_step_behind(step) is False


def test_the_loading_picker_reads_as_loading(offline_page):
    _load(offline_page, "loading")
    got = _picker(offline_page)
    assert got["root"] is True
    assert got["loading"] is True and got["skeletons"] == 3, got
    assert got["upload"] is False and got["search"] is False and got["tiles"] == 0, got


def test_the_loaded_picker_reads_as_loaded(offline_page):
    _load(offline_page, "loaded")
    got = _picker(offline_page)
    assert got["root"] is True
    assert got["loading"] is False and got["skeletons"] == 0, got
    assert got["upload"] is True and got["search"] is True, got
    assert got["tiles"] == 2, got


def test_exactly_one_upload_inside_the_wizard_dialog(offline_page):
    """The robot's own lookup finds Upload once, and it is inside the wizard root."""
    _load(offline_page, "loaded")
    assert offline_page.get_by_role("button", name="Upload").count() == 1
    inside = offline_page.evaluate(
        "(() => { const root = (" + md._WIZARD_ROOT_JS + ")();"
        " return root ? [...root.querySelectorAll('[role=button]')]"
        ".filter(b => b.textContent.trim() === 'Upload').length : -1; })()")
    assert inside == 1


def test_negative_control_without_the_bars_nothing_is_loading(offline_page):
    """The reader keys on the progressbars, not on the missing Upload."""
    _load(offline_page, "loading")
    offline_page.evaluate("() => document.querySelectorAll('[role=progressbar]').forEach(e => e.remove())")
    got = _picker(offline_page)
    assert got["loading"] is False and got["skeletons"] == 0, got


def test_upload_by_its_data_surface_alone_is_seen(offline_page):
    """If Meta drops the visible label, the data-surface still says it is Upload."""
    _load(offline_page, "loaded")
    offline_page.evaluate("""() => {
      const b = document.querySelector('[data-surface$="creative-tool-asset-picker-upload-button"]');
      b.querySelectorAll('div').forEach(d => { if (d.textContent.trim() === 'Upload') d.textContent = ''; });
    }""")
    assert _picker(offline_page)["upload"] is True


# ── шаг креатива на настоящей разметке ────────────────────────────────────

@pytest.fixture
def step(offline_page, monkeypatch, tmp_path):
    """`upload_ad_creative` up to the library: the wizard comes from the fixture."""
    from src.fb_ads_rpa import page_capture

    state = {"html": "", "reuse": [], "clicks": [], "xpath": []}
    monkeypatch.setattr(md, "_sleep_h", lambda *a, **k: None)
    monkeypatch.setattr(md, "MEDIA_PICKER_LOAD_S", 1.5)
    monkeypatch.setattr(md, "_MEDIA_PICKER_POLL_S", 0.05)
    monkeypatch.setattr(rt, "TELEMETRY_PATH", tmp_path / "telemetry.jsonl")
    monkeypatch.setattr(page_capture, "_root", lambda: tmp_path / "rpa_pages")
    page_capture._taken.clear()
    md._upload_why_tls.wizard_captured = False
    md._set_upload_why(None)
    rt.set_current_run("run-1559", "CAMP")

    def open_wizard(p, trigger, item, **kw):
        p.evaluate("(h) => { const d = document.createElement('div'); d.innerHTML = h;"
                   " document.body.appendChild(d);"
                   " d.querySelectorAll('script').forEach(s => { const n = document.createElement('script');"
                   " n.textContent = s.textContent; s.replaceWith(n); }); }", state["html"])
        return True, []

    def reuse(p, path):
        state["reuse"].append(p.get_by_role("button", name="Upload").count())
        return True

    real_click = md._cdp_mouse_click
    monkeypatch.setattr(md, "_cdp_mouse_click",
                        lambda p, x, y, *a, **k: state["clicks"].append((x, y)) or real_click(p, x, y, *a, **k))
    real_xpath = md._click_xpath
    monkeypatch.setattr(md, "_click_xpath",
                        lambda p, xp, *a, **k: state["xpath"].append(xp) or real_xpath(p, xp, *a, **k))
    monkeypatch.setattr(md, "_open_creative_wizard", open_wizard)
    monkeypatch.setattr(md, "_scroll_until_mounted", lambda *a, **k: None)
    monkeypatch.setattr(md, "reuse_before_upload", reuse)
    monkeypatch.setattr(md, "_close_creative_setup_modal", lambda p: True)
    offline_page.set_content("<div id='ad-page'>Set up creative</div>")
    return state


def _rows(tmp_path, step="ad_creative_upload"):
    f = tmp_path / "telemetry.jsonl"
    if not f.exists():
        return []
    return [r for r in (json.loads(x) for x in f.read_text(encoding="utf-8").splitlines() if x.strip())
            if r["step"] == step]


def test_a_picker_that_never_loads_is_named_without_clicks(offline_page, step, tmp_path):
    step["html"] = WIZARD_ROW_CSS + fragment(FIXTURE, "loading")
    assert md.upload_ad_creative(offline_page, "Video", "C:/media/1.mp4") is False
    assert step["clicks"] == [], "clicked the current Media step"
    assert step["xpath"] == [], "pressed Next on the Media step"
    assert step["reuse"] == []
    assert md.last_upload_why() == "media_picker_loading"
    row = _rows(tmp_path)[-1]
    assert row["ok"] is False and row["note"] == "media_picker_loading", row
    assert row["diag"]["skeletons"] == 3
    assert row["diag"]["waited_s"] >= 1.4
    assert row["diag"]["picker"]["loading"] is True
    assert row["diag"]["clicks"] == {"media": 0, "next": 0}


def test_a_picker_that_loads_during_the_wait_goes_on_to_the_library(offline_page, step, tmp_path,
                                                                    monkeypatch):
    monkeypatch.setattr(md, "MEDIA_PICKER_LOAD_S", 10.0)
    loaded = json.dumps(WIZARD_ROW_CSS + fragment(FIXTURE, "loaded"))
    step["html"] = WIZARD_ROW_CSS + fragment(FIXTURE, "loading") + (
        "<script>setTimeout(() => { const d = document.querySelector('[role=dialog]');"
        f" const w = document.createElement('div'); w.innerHTML = {loaded};"
        " d.replaceWith(...w.childNodes); }, 600);</script>")
    assert md.upload_ad_creative(offline_page, "Video", "C:/media/1.mp4") is True
    assert step["reuse"] == [1], "did not reach the library with Upload on screen"
    assert step["clicks"] == [] and step["xpath"] == []
    picker = [r for r in _rows(tmp_path) if (r.get("note") or "").startswith("media_picker:")]
    assert picker and picker[-1]["note"] == "media_picker:ready", _rows(tmp_path)
    assert picker[-1]["diag"]["upload"] is True and picker[-1]["diag"]["tiles"] == 2
    assert 0.4 <= picker[-1]["diag"]["waited_s"] < 10


def test_a_loaded_picker_goes_straight_to_the_library(offline_page, step):
    step["html"] = WIZARD_ROW_CSS + fragment(FIXTURE, "loaded")
    assert md.upload_ad_creative(offline_page, "Video", "C:/media/1.mp4") is True
    assert step["reuse"] == [1]
    assert step["clicks"] == [] and step["xpath"] == []
