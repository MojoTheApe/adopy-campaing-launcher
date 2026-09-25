# -*- coding: utf-8 -*-
"""Мастер креатива открывается надёжно, а каждый отказ медиа назван (ACL-1313).

Прогон 25.09 GT FX (5b223701b271, ad3,
rpa_screenshots/5b223701b271/20260925_071100_recorded_fail_ad3_creative_upload.png):
мастер креатива так и не открылся. На экране — «Verifying your edits…» в
футере, крутящееся превью, карточки «Show your offerings» и «Terms of Service
Not Accepted»; кнопка «Set up creative» была и была доступна; четыре попытки
за ~15 с. В телеметрии при этом одна строка `same_file_already_uploaded_this_run`
— почему не открылся мастер, не записано нигде: путь «известного файла»
глотал исключение, а ранние `return False` в reuse и `if not opened` молчали.

Здесь проверяется:

* открытие ждёт автосейв и прокручивает к кнопке (офлайн Chromium);
* если клик по координатам меню не открыл — второй клик через CDP по рамке
  элемента;
* каждый молчаливый выход пишет строку телеметрии с точной причиной;
* мастер на шаге Text после нашего выбора в медиатеке — это «прикреплено»,
  а не повод отступать.
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

TRIGGER = ("//*[@role='button' or self::button]"
           "[contains(normalize-space(.), 'Set up creative')]")
ITEM = "//*[@role='menuitem' or @role='option'][contains(normalize-space(.), 'Video ad')]"

# Редактор: прокручиваемая колонка, кнопка далеко внизу, футер автосейва.
# Пока футер «Verifying your edits…» на экране, клик по кнопке уходит в
# перерисовку — как на живом снимке 5b223701b271.
_EDITOR = """
<div id="scroller" style="height:600px; overflow:auto">
  <div style="height:1800px">
    <div><span>Show your offerings in more engaging ads</span></div>
    <div><span>Terms of Service Not Accepted</span></div>
  </div>
  <div id="trigger" role="button" tabindex="0" aria-haspopup="menu"
       style="width:160px; height:32px"><span><div><div>Set up creative</div></div></span></div>
  <div style="height:1800px"></div>
</div>
<div id="footer"><span>Verifying your edits…</span></div>
<script>
  window.busy = true;
  window.triggerClicks = 0;
  setTimeout(() => { window.busy = false; document.getElementById('footer').remove(); }, BUSY_MS);
  document.getElementById('trigger').addEventListener('click', () => {
    window.triggerClicks++;
    if (window.busy) return;
    if (document.getElementById('menu')) return;
    const m = document.createElement('div');
    m.id = 'menu'; m.setAttribute('role', 'menu');
    m.style = 'position:fixed; top:100px; left:400px';
    m.innerHTML = '<div role="menuitem" style="width:120px;height:30px">Image ad</div>'
                + '<div id="video" role="menuitem" style="width:120px;height:30px">Video ad</div>';
    document.body.appendChild(m);
    document.getElementById('video').addEventListener('click', () => {
      m.remove();
      const w = document.createElement('div');
      w.id = 'wizard'; w.style = 'position:fixed; top:50px; left:50px; background:#fff';
      w.innerHTML = '<span>Creative setup</span> <span>Media</span> <span>Text</span>'
                  + '<div role="button">Cancel</div><div role="button">Next</div>';
      document.body.appendChild(w);
    });
  });
</script>
"""


def _editor(busy_ms: int) -> str:
    return _EDITOR.replace("BUSY_MS", str(busy_ms))


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
    from src.fb_ads_rpa import page_capture

    monkeypatch.setattr(md, "_sleep_h", lambda *a, **k: None)
    monkeypatch.setattr(page_capture, "capture", lambda *a, **k: None)
    monkeypatch.setattr(run_telemetry, "TELEMETRY_PATH", tmp_path / "telemetry.jsonl")
    ctx = browser.new_context(viewport={"width": 1280, "height": 800})
    p = ctx.new_page()
    p.route("**/*", lambda route: route.abort())
    try:
        yield p
    finally:
        ctx.close()


def _rows(tmp_path, step=None):
    f = tmp_path / "telemetry.jsonl"
    if not f.exists():
        return []
    rows = [json.loads(line) for line in f.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [r for r in rows if step is None or r["step"] == step]


# ── открытие мастера ───────────────────────────────────────────────────────

def test_the_wizard_opens_after_the_autosave_clears(page):
    """Кнопка вне окна, автосейв ещё идёт ~3 с: ждём его и прокручиваем.

    Без ожидания все четыре попытки уходят в «Verifying your edits…» — ровно
    как 25.09 на GT FX.
    """
    page.set_content(_editor(3000))
    opened, tries = md._open_creative_wizard(page, TRIGGER, ITEM)
    assert opened is True, tries
    assert page.locator("#wizard").count() == 1
    # Открыто первой же попыткой — не перебором, пока автосейв не кончился.
    assert page.evaluate("window.triggerClicks") == 1


def test_between_attempts_the_busy_footer_is_waited_out(page, monkeypatch):
    """Автосейв вспыхнул уже после первого клика — ждём и между попытками."""
    page.set_content(_editor(0))
    page.wait_for_function("!document.getElementById('footer')")
    real = md._click_xpath
    state = {"n": 0}

    def eaten_first(p, xpath, **kw):
        if xpath == TRIGGER and state["n"] == 0:
            state["n"] += 1
            # Клик «съеден», и Meta тут же начала сохранять: футер на ~2,5 с.
            p.evaluate("""() => {
              window.busy = true;
              const f = document.createElement('div'); f.id = 'footer';
              f.innerHTML = '<span>Verifying your edits…</span>';
              document.body.appendChild(f);
              setTimeout(() => { window.busy = false; f.remove(); }, 2500);
            }""")
            return True
        return real(p, xpath, **kw)

    monkeypatch.setattr(md, "_click_xpath", eaten_first)
    # Второй клик — через рамку элемента; пусть и он «съедается», пока занято.
    real_trusted = md._click_trusted
    monkeypatch.setattr(md, "_click_trusted", lambda p, h, **kw: False)
    opened, tries = md._open_creative_wizard(page, TRIGGER, ITEM)
    monkeypatch.setattr(md, "_click_trusted", real_trusted)
    assert opened is True, tries
    # Первая попытка съедена, вторая — уже после того, как футер погас.
    assert tries[0]["try"] == 0 and tries[-1]["try"] >= 1
    assert tries[-1]["busy"] is False


def test_a_click_that_did_not_open_the_menu_is_repeated_through_cdp(page, monkeypatch):
    """Клик по координатам из XPath ушёл в никуда — CDP-клик по рамке элемента."""
    page.set_content(_editor(0))
    page.wait_for_function("!document.getElementById('footer')")
    real = md._click_xpath

    def trigger_lost(p, xpath, **kw):
        if xpath == TRIGGER:
            return True  # «клик отправлен», эффекта нет
        return real(p, xpath, **kw)

    monkeypatch.setattr(md, "_click_xpath", trigger_lost)
    opened, tries = md._open_creative_wizard(page, TRIGGER, ITEM)
    assert opened is True, tries
    assert tries[-1]["menu_by"] == "cdp_handle"


# ── каждый отказ назван ────────────────────────────────────────────────────

def test_a_wizard_that_never_opens_is_recorded(page, tmp_path, monkeypatch):
    page.set_content("<div><span>Ad setup</span></div>")
    monkeypatch.setattr(md, "_scroll_until_mounted", lambda *a, **k: False)
    assert md.upload_ad_creative(page, "Video", "C:/media/clip.mp4") is False
    row = _rows(tmp_path, "ad_creative_upload")[-1]
    assert row["ok"] is False and row["note"] == "wizard_not_opened"
    assert row["diag"]["file"] == "clip.mp4"
    assert row["diag"]["tries"], "дневник попыток пуст"


def test_a_wizard_without_the_upload_button_is_recorded(page, tmp_path, monkeypatch):
    page.set_content('<div id="wizard"><span>Creative setup</span> <span>Media</span> <span>Text</span>'
                     '<div role="button" aria-disabled="true">Next</div><div role="button">Cancel</div></div>')
    monkeypatch.setattr(md, "_scroll_until_mounted", lambda *a, **k: False)
    assert md.upload_ad_creative(page, "Video", "C:/media/clip.mp4") is False
    row = _rows(tmp_path, "ad_creative_upload")[-1]
    assert row["note"] == "upload_button_missing"


def test_an_exception_on_the_known_file_path_is_recorded_with_its_text(monkeypatch, tmp_path):
    monkeypatch.setattr(run_telemetry, "TELEMETRY_PATH", tmp_path / "telemetry.jsonl")
    monkeypatch.setattr(md, "upload_already_succeeded", lambda path: True)
    monkeypatch.setattr(md, "upload_already_failed", lambda path: False)
    monkeypatch.setattr(md, "_media_block_filled_now", lambda p: False)
    monkeypatch.setattr(md, "_wizard_past_media", lambda p: False)

    def boom(*a, **k):
        raise RuntimeError("Target page, context or browser has been closed")

    monkeypatch.setattr(md, "upload_ad_creative", boom)
    assert md.upload_ad_creative_with_retry(object(), "Video", "C:/media/clip.mp4") is False
    notes = [(r["note"], r.get("diag", {}).get("error")) for r in _rows(tmp_path, "ad_creative_upload")]
    assert ("exception:RuntimeError", "Target page, context or browser has been closed") in notes
    assert notes[-1][0] == "same_file_already_uploaded_this_run"


# ── reuse: выход после выбора плитки ──────────────────────────────────────

class _Tile:
    def __init__(self, grid, i):
        self.grid, self.i = grid, i


class _Grid:
    def __init__(self):
        self.selected: set[int] = set()

    def count(self):
        return 2

    def nth(self, i):
        return _Tile(self, i)


class _Button:
    def __init__(self, on_click):
        self.first = self
        self._on_click = on_click

    def count(self):
        return 1

    def is_disabled(self):
        return False

    def click(self, timeout=0):
        self._on_click()


class _Dialog:
    def __init__(self, on_next):
        self.first = self
        self._on_next = on_next

    def is_visible(self, timeout=0):
        return True

    def get_by_role(self, role, name=None):
        return _Button(self._on_next)


class _Root:
    def __init__(self, on_next):
        self._on_next = on_next

    def get_by_role(self, role, name=None):
        return _Dialog(self._on_next)


class _Page:
    url = "https://adsmanager.facebook.com/adsmanager/manage/ads/edit/standalone"


@pytest.fixture
def reuse(monkeypatch, tmp_path):
    from src.fb_ads_rpa import page_capture

    g = _Grid()
    state = {"at_text": False, "roots": [], "closed": 0}
    monkeypatch.setattr(run_telemetry, "TELEMETRY_PATH", tmp_path / "telemetry.jsonl")
    monkeypatch.setattr(page_capture, "capture", lambda *a, **k: None)
    monkeypatch.setattr(md, "_sleep_h", lambda *a, **k: None)
    monkeypatch.setattr(md, "_library_search", lambda page, filename: True)
    monkeypatch.setattr(md, "_library_tiles", lambda page, filename: g)
    monkeypatch.setattr(md, "_library_selected_count", lambda page: len(g.selected))
    monkeypatch.setattr(md, "_library_tile_selected", lambda tile: tile.i in tile.grid.selected)

    def click(page, tile, before=None):
        tile.grid.selected.add(tile.i)
        return ""

    monkeypatch.setattr(md, "_click_library_tile", click)
    monkeypatch.setattr(md, "_library_undo_selection",
                        lambda page, tiles, clicked, before: (g.selected.clear() or True))
    monkeypatch.setattr(md, "_tile_fingerprint", lambda tile: [])
    monkeypatch.setattr(md, "_tile_thumb_ids", lambda tile: [])
    monkeypatch.setattr(md, "_library_sight", lambda page, filename: {})
    monkeypatch.setattr(md, "_library_tile_shape", lambda page, filename: {})
    monkeypatch.setattr(md, "_verify_ad_media_present", lambda page: False)
    monkeypatch.setattr(md, "_media_block_filled_now", lambda page: False)
    monkeypatch.setattr(md, "_wizard_past_media", lambda page: state["at_text"])

    def roots(page):
        r = state["roots"]
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(md, "_roots", roots)

    def close(page):
        state["closed"] += 1
        return True

    monkeypatch.setattr(md, "_close_creative_setup_modal", close)
    state["grid"] = g
    return state


def _reuse_notes(tmp_path):
    return [(r["ok"], r.get("note")) for r in _rows(tmp_path, "ad_creative_reuse")]


def test_no_next_and_no_text_step_retreats_with_a_reason(reuse, tmp_path):
    assert md._try_reuse_library_media(_Page(), "clip.mp4") == md.REUSE_ABSENT
    assert (None, "next_not_pressed") in _reuse_notes(tmp_path)
    assert reuse["grid"].selected == set()


def test_no_next_but_the_wizard_is_on_text_is_attached(reuse, tmp_path):
    reuse["at_text"] = True
    assert md._try_reuse_library_media(_Page(), "clip.mp4") == md.REUSE_ATTACHED
    assert (True, "wizard_at_text_after_pick") in _reuse_notes(tmp_path)
    assert reuse["grid"].selected, "выбор не снимаем — он принят"


def test_next_to_text_is_attached_even_before_the_ad_page_shows_media(reuse, tmp_path):
    reuse["roots"] = [_Root(lambda: reuse.update(at_text=True))]
    assert md._try_reuse_library_media(_Page(), "clip.mp4") == md.REUSE_ATTACHED
    row = [r for r in _rows(tmp_path, "ad_creative_reuse") if r.get("note") == "wizard_at_text_after_pick"][-1]
    assert row["diag"]["via"] == "next_to_text"


def test_advanced_without_media_is_dirty_and_says_so(reuse, tmp_path):
    reuse["roots"] = [_Root(lambda: None)]
    assert md._try_reuse_library_media(_Page(), "clip.mp4") == md.REUSE_DIRTY
    assert (False, "dirty_after_advance") in _reuse_notes(tmp_path)


def test_an_exception_after_the_pick_is_named(reuse, tmp_path):
    reuse["roots"] = RuntimeError("Element is not attached to the DOM")
    assert md._try_reuse_library_media(_Page(), "clip.mp4") == md.REUSE_ABSENT
    row = [r for r in _rows(tmp_path, "ad_creative_reuse") if (r.get("note") or "").startswith("exception:")][-1]
    assert row["note"] == "exception:RuntimeError"
    assert "not attached" in row["diag"]["error"]


def test_an_exception_while_the_wizard_moved_to_text_is_attached(reuse, tmp_path):
    reuse["roots"] = RuntimeError("Element is not attached to the DOM")
    reuse["at_text"] = True
    assert md._try_reuse_library_media(_Page(), "clip.mp4") == md.REUSE_ATTACHED
