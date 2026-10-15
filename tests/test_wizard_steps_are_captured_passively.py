# -*- coding: utf-8 -*-
"""Шаги мастера креатива снимаются, а нажатия остаются прежними (ACL-1426, фаза A).

Живые прогоны 01.10 (Facebook_1443, 04c8c214d2ba и 0bd520418dbe): после
выбора файла в медиатеке робот проходит мастер Trim → Crop → Text → Image
generation → Enhancements → Translation кнопкой «вперёд», и ни одного снимка
этих шагов нет — страница редактора режется на 4 МБ раньше портала мастера.
Фаза A: пока обход видит мастер, каждый новый шаг снимается (блок мастера,
вычищенный), а в телеметрию уходит последовательность шагов с временем.

Главное здесь — запись пассивна: с ней и без неё обход жмёт те же кнопки в
том же порядке, а сбой записи в обход не пробивается. Офлайн-Chrome,
синтетический мастер в разметке Meta 25.09 (role=dialog, строка шагов
role=list с aria-current="step", заголовок тела шага aria-level=3).
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
from src.fb_ads_rpa import page_capture  # noqa: E402
from src.fb_ads_rpa import run_telemetry as rt  # noqa: E402
from src.fb_ads_rpa import wizard_capture as wc  # noqa: E402

STEPS = ["Creative setup", "Media", "Trim", "Crop", "Text", "Image generation",
         "Enhancements", "Translation"]
#: Подпись кнопки «вперёд» на каждом шаге (как у Meta с 16.09).
FORWARD = ["Next", "Next", "Skip and continue", "Skip and continue", "Skip and continue",
           "Skip and continue", "Skip and continue", "Done"]
#: Заголовок тела шага; у Crop — свой, как на скриншоте владельца 01.10.
HEADINGS = {"Crop": "Select media crop for placements"}

_HTML = """
<!doctype html><html><body style="margin:0">
<div id="ad"><div id="empty">Please specify the media to run with this ad.</div>
  <div id="filled" style="display:none">Edit media</div></div>
<div id="wiz" role="dialog" style="position:fixed;left:5vw;top:5vh;width:90vw;height:90vh;background:#fff">
  <div role="list" style="display:flex;gap:16px;padding:8px" id="row"></div>
  <div style="padding:16px">
    <div role="heading" aria-level="3" id="head"></div>
    <div id="crop" style="display:none">
      <div>Square <span role="radio" aria-checked="true" aria-label="Original">Original</span>
        <label><input type="checkbox" checked> Crop and expand (Meta AI)</label></div>
      <div>Vertical <span role="radio" aria-checked="true" aria-label="9:16">9:16</span>
        <label><input type="checkbox" checked> Crop and expand (Meta AI)</label>
        <button>Replace</button></div>
    </div>
    <button id="cancel">Cancel</button>
    <button id="fwd" onclick="advance()"></button>
  </div>
</div>
<script>
  const steps = %(steps)s, labels = %(labels)s, heads = %(heads)s;
  let at = %(start)d;
  window.clicks = [];
  function paint() {
    document.getElementById('row').innerHTML = steps.map((s, i) =>
      '<div role="listitem"><div role="button" ' + (i === at ? 'aria-current="step"' : '') +
      '><div>' + s + '</div></div></div>').join('');
    document.getElementById('head').textContent = heads[steps[at]] || steps[at];
    document.getElementById('crop').style.display = steps[at] === 'Crop' ? 'block' : 'none';
    document.getElementById('fwd').textContent = labels[at];
  }
  function advance() {
    window.clicks.push(document.getElementById('fwd').textContent);
    at += 1;
    if (at >= labels.length) {
      window.clicks.push('close');
      document.getElementById('wiz').remove();
      document.getElementById('empty').style.display = 'none';
      document.getElementById('filled').style.display = 'block';
      return;
    }
    paint();
  }
  paint();
</script>
</body></html>
"""


def _open(page, start=2):
    page.set_content(_HTML % {"steps": json.dumps(STEPS), "labels": json.dumps(FORWARD),
                              "heads": json.dumps(HEADINGS), "start": start})


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
    monkeypatch.setattr(md, "_sleep_h", lambda *a, **k: None)
    monkeypatch.setattr(md, "_WIZARD_STEP_SETTLE_S", 0.3)
    monkeypatch.setattr(md, "_WIZARD_CLOSE_POLL_S", 0.05)
    monkeypatch.setattr(rt, "TELEMETRY_PATH", tmp_path / "telemetry.jsonl")
    monkeypatch.setattr(page_capture, "_root", lambda: tmp_path / "rpa_pages")
    monkeypatch.delenv(wc.SETTING, raising=False)
    page_capture._taken.clear()
    page_capture._quota_taken.clear()
    rt.set_current_run("run-1426", "CAMP")
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    p = ctx.new_page()
    p.route("**/*", lambda route: route.abort())
    try:
        yield p
    finally:
        ctx.close()
        rt.clear_current_run()
        page_capture._taken.clear()
        page_capture._quota_taken.clear()


def _rows(tmp_path, step="ad_creative_wizard"):
    f = tmp_path / "telemetry.jsonl"
    if not f.exists():
        return []
    rows = [json.loads(line) for line in f.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [r for r in rows if r.get("step") == step]


def _captures(tmp_path):
    root = tmp_path / "rpa_pages" / "run-1426"
    return sorted(d.name for d in root.iterdir()) if root.is_dir() else []


def _walk(page, origin="library_pick"):
    with wc.origin(origin, "4_es_2403_post.mp4"):
        return md._walk_creative_wizard(page)


# ── пассивность ───────────────────────────────────────────────────────────

def test_the_same_presses_with_capture_on_and_off(page, monkeypatch, tmp_path):
    _open(page)
    assert _walk(page) is True
    with_capture = page.evaluate("window.clicks")
    assert _captures(tmp_path), "capture on: nothing was saved"

    monkeypatch.setenv(wc.SETTING, "0")
    page_capture._quota_taken.clear()
    _open(page)
    assert _walk(page) is True
    without = page.evaluate("window.clicks")

    assert with_capture == without
    assert with_capture == ["Skip and continue"] * 5 + ["Done", "close"]


def test_a_broken_capture_never_reaches_the_walk(page, monkeypatch, tmp_path):
    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(page_capture, "capture", boom)
    _open(page)
    assert _walk(page) is True
    assert page.evaluate("window.clicks")[-1] == "close"

    monkeypatch.setattr(wc, "read_state", boom)
    monkeypatch.setattr(rt, "record", boom)
    _open(page)
    assert _walk(page) is True
    assert page.evaluate("window.clicks") == ["Skip and continue"] * 5 + ["Done", "close"]


def test_the_trail_presses_nothing(page):
    """Чтение шага не меняет ни шаг, ни отметки: та же разметка до и после."""
    _open(page, start=3)
    before = page.evaluate("document.getElementById('wiz').outerHTML")
    state = wc.read_state(page)
    assert page.evaluate("document.getElementById('wiz').outerHTML") == before
    assert page.evaluate("window.clicks") == []
    assert state["current"] == "Crop"
    assert "Select media crop for placements" in state["headings"]
    labels = [b["label"] for b in state["buttons"]]
    assert "Replace" in labels and "Skip and continue" in labels
    assert [c["checked"] for c in state["checks"] if "Meta AI" in c["label"]] == [True, True]


# ── что записывается ──────────────────────────────────────────────────────

def test_each_step_is_captured_once_with_its_name(page, tmp_path):
    _open(page)
    assert _walk(page) is True
    names = _captures(tmp_path)
    assert [n.split("_", 1)[1] for n in names] == [
        "wizard_trim", "wizard_crop", "wizard_text", "wizard_image_generation",
        "wizard_enhancements", "wizard_translation"]
    crop = tmp_path / "rpa_pages" / "run-1426" / names[1]
    block = (crop / "block_creative_wizard.html").read_text(encoding="utf-8")
    assert "Select media crop for placements" in block and "Crop and expand (Meta AI)" in block
    context = json.loads((crop / "context.json").read_text(encoding="utf-8"))
    assert context["diag"]["origin"] == "library_pick"
    assert context["diag"]["state"]["current"] == "Crop"
    # Блок нашёлся — страница целиком (4 МБ у Meta) не пишется.
    assert not (crop / "page.html").exists()
    # Снимок шага мастера — не отказ: раздел для Sentry не запоминается.
    assert not any(k[0] == "run-1426" for k in page_capture._sections)


def test_the_step_sequence_and_timings_go_to_telemetry(page, tmp_path):
    _open(page)
    assert _walk(page) is True
    rows = _rows(tmp_path)
    assert len(rows) == 1
    row = rows[0]
    assert row["note"] == "library_pick:closed"
    diag = row["diag"]
    assert diag["sequence"] == ["Trim", "Crop", "Text", "Image generation", "Enhancements",
                                "Translation"]
    assert diag["steps"][1]["pressed"] == ["Skip and continue"]
    assert diag["steps"][-1]["pressed"] == ["Done"]
    assert all(s["s"] >= 0 for s in diag["steps"])
    assert diag["captures"]["wizard_crop"] is True


def test_upload_origin_is_captured_too(page, tmp_path):
    _open(page)
    assert _walk(page, origin="upload") is True
    assert _rows(tmp_path)[0]["note"] == "upload:closed"
    assert any(n.endswith("wizard_crop") for n in _captures(tmp_path))


def test_other_closes_are_logged_but_not_captured(page, tmp_path):
    """Закрытие мастера при откате/повторе — не «после выбора/загрузки»."""
    _open(page)
    assert md._walk_creative_wizard(page) is True
    assert _captures(tmp_path) == []
    assert _rows(tmp_path)[0]["note"] == "other:closed"


def test_the_run_cap_holds_and_failures_keep_their_own_limit(page, monkeypatch, tmp_path):
    monkeypatch.setattr(wc, "MAX_PER_RUN", 4)
    _open(page)
    _walk(page)
    _open(page)
    _walk(page)
    assert len(_captures(tmp_path)) == 4
    # Лимит отказов цел: снимок отказа берётся и ложится следующим номером.
    saved = page_capture.capture(page, step="ad2_creative_upload")
    assert saved and Path(saved).name == "04_ad2_creative_upload"
    assert page_capture._taken["run-1426"] == 1


def test_no_wizard_no_row(page, tmp_path):
    page.set_content("<div>Edit media</div>")
    assert _walk(page) is True
    assert _rows(tmp_path) == []
    assert _captures(tmp_path) == []


def test_step_labels():
    assert wc.step_label("Image generation") == "wizard_image_generation"
    assert wc.step_label("Crop") == "wizard_crop"
    assert wc.step_label("") == "wizard_unknown"
    assert wc.step_label(None) == "wizard_unknown"
