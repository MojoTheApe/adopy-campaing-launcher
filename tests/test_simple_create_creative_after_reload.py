# -*- coding: utf-8 -*-
"""Креатив в новом редакторе после перезагрузки (ACL-1327, 97314905598e).

Первый живой прогон нового редактора (28.09, Facebook_1443): мастер креатива
в первый раз ОТКРЫЛСЯ той же кнопкой «Set up creative», что и в прежнем
мастере (в снимке 02 она на месте: `role=button aria-haspopup=menu` в
`/am/editor:ad/ad_image_video_selector_section` под заголовком «Ad creative»,
карточка «Show your offerings» стоит выше, в `ad_creative_source_section`, и
кнопку не накрывает). Медиатека ответила `not_in_library`, загрузка висела
на `progress: 0` три раза, и шаг перезагрузил страницу. Дальше
`wizard_not_opened`: попытка 0 нашла кнопку и нажала — `click_target_covered`
(y=913, под футером нового редактора, где Publish), попытки 1–3 кнопку не
нашли вовсе. Прежний мастер после перезагрузки открывается прямо на
объявлении, новый — сверху страницы, с недорисованными разделами.

Здесь проверяется:

* после перезагрузки в новом редакторе шаг сначала возвращается к разделу
  «Ad» (`simple_create.back_to_level`), в прежнем — нет;
* в новом редакторе накрытую кнопку не жмут (клик ушёл бы в футер с
  Publish); когда страница дорисована и кнопку видно — мастер открывается;
* пропавшую кнопку ищут, сперва дойдя до раздела «Ad».
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import meta_dom as md  # noqa: E402
from src.fb_ads_rpa import simple_create as sc  # noqa: E402

SIMPLE_URL = ("https://adsmanager.facebook.com/adsmanager/manage/simple_create"
              "?act=1&selected_campaign_ids=2&selected_adset_ids=3&selected_ad_ids=4")
CLASSIC_URL = "https://adsmanager.facebook.com/adsmanager/manage/ads/edit/standalone?act=1"

TRIGGER = ("//*[@role='button' or self::button]"
           "[contains(normalize-space(.), 'Set up creative')]")
ITEM = "//*[@role='menuitem' or @role='option'][contains(normalize-space(.), 'Video ad')]"


# ── перезагрузка: сначала обратно к разделу «Ad» ──────────────────────────

class _FakePage:
    def __init__(self, url: str) -> None:
        self.url = url


def _retry_calls(monkeypatch, tmp_path, url: str) -> list:
    calls: list = []
    monkeypatch.setattr(md, "_sleep_h", lambda *a, **k: None)
    monkeypatch.setattr(md._telemetry, "record", lambda *a, **k: None)

    def upload(page, *a, **k):
        calls.append("upload")
        return False

    def reload(page, edit_url=""):
        calls.append("reload")

    def level(page, lvl, **k):
        calls.append(("level", lvl))
        return {"ok": True}

    monkeypatch.setattr(md, "upload_ad_creative", upload)
    monkeypatch.setattr(md, "media_refused_on_ad", lambda: False)
    monkeypatch.setattr(md, "_close_creative_setup_modal", lambda page: False)
    monkeypatch.setattr(md, "_verify_ad_media_present", lambda page: False)
    monkeypatch.setattr(md, "_upload_stalled", lambda page, **k: True)
    monkeypatch.setattr(md, "_upload_retry_can_reload", lambda page, edit_url="": True)
    monkeypatch.setattr(md, "_reload_editor", reload)
    monkeypatch.setattr(md, "_close_onboarding_tour", lambda page, where="": "")
    monkeypatch.setattr(sc, "scroll_to_level", level)
    clip = tmp_path / f"clip_{time.time_ns()}.mp4"
    clip.write_bytes(b"0")
    assert md.upload_ad_creative_with_retry(_FakePage(url), "Video", str(clip), edit_url=url) is False
    return calls


def test_after_a_reload_the_single_screen_editor_goes_back_to_the_ad_section(monkeypatch, tmp_path):
    calls = _retry_calls(monkeypatch, tmp_path, SIMPLE_URL)
    assert calls == ["upload", "reload", ("level", "ad"), "upload"]


def test_the_classic_editor_reload_is_unchanged(monkeypatch, tmp_path):
    calls = _retry_calls(monkeypatch, tmp_path, CLASSIC_URL)
    assert calls == ["upload", "reload", "upload"]


# ── накрытая кнопка не жмётся ─────────────────────────────────────────────

# Страница только что перезагружена: над кнопкой длинная колонка, под ней
# ничего ещё не дорисовано — прокрутить кнопку в середину нельзя, и она
# стоит внизу окна, под футером (как y=913 в 97314905598e). В футере —
# Publish во всю ширину: любое нажатие туда — это Publish.
_PAGE = """
<!doctype html><html><body style="margin:0">
<div id="top" style="height:2000px"><span>Campaign</span></div>
<div id="trigger" role="button" tabindex="0" aria-haspopup="menu"
     style="width:160px; height:32px"><span>Set up creative</span></div>
<div id="rest"></div>
<div id="footer" style="position:fixed; left:0; right:0; bottom:0; height:90px; background:#eee">
  <div role="button" id="publish" style="width:100%; height:90px">Publish</div>
</div>
<script>
  window.publishClicks = 0;
  document.getElementById('publish').addEventListener('click', () => window.publishClicks++);
  document.getElementById('trigger').addEventListener('click', () => {
    if (document.getElementById('menu')) return;
    const m = document.createElement('div');
    m.id = 'menu'; m.setAttribute('role', 'menu');
    m.style = 'position:fixed; top:100px; left:400px';
    m.innerHTML = '<div id="video" role="menuitem" style="width:120px;height:30px">Video ad</div>';
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
  // Остальная страница дорисовывается через MOUNT_MS после первой
  // прокрутки (-1 — никогда).
  if (MOUNT_MS >= 0) addEventListener('scroll', () => setTimeout(() => {
    document.getElementById('rest').style.height = '2000px';
  }, MOUNT_MS), { once: true });
</script>
</body></html>
"""


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=os.environ.get("ACL_BROWSER_EXECUTABLE") or None)
        try:
            yield b
        finally:
            b.close()


@pytest.fixture
def page(browser, monkeypatch):
    from src.fb_ads_rpa import page_capture

    monkeypatch.setattr(md, "_sleep_h", lambda s, *a, **k: time.sleep(min(float(s), 1.0)))
    monkeypatch.setattr(md, "_human_jitter", lambda base, spread: 0.0)
    monkeypatch.setattr(md._telemetry, "record", lambda *a, **k: None)
    monkeypatch.setattr(page_capture, "capture", lambda *a, **k: None)
    ctx = browser.new_context(viewport={"width": 1280, "height": 800})
    p = ctx.new_page()
    try:
        yield p
    finally:
        ctx.close()


def _open(page, url: str, mount_ms: int) -> None:
    html = _PAGE.replace("MOUNT_MS", str(mount_ms))
    page.route("**/*", lambda route: route.fulfill(status=200, content_type="text/html", body=html)
               if route.request.url.startswith("https://adsmanager.facebook.com/") else route.abort())
    page.goto(url)


def test_a_covered_button_is_never_pressed_into_the_footer(page):
    _open(page, SIMPLE_URL, -1)
    opened, tries = md._open_creative_wizard(page, TRIGGER, ITEM, attempts=2)
    assert opened is False
    assert page.evaluate("window.publishClicks") == 0
    assert tries and all(t.get("covered") for t in tries), tries
    assert all(t["button"]["hit"] is False for t in tries)


def test_the_wizard_opens_once_the_page_has_drawn_below_the_button(page):
    _open(page, SIMPLE_URL, 1500)
    opened, tries = md._open_creative_wizard(page, TRIGGER, ITEM)
    assert opened is True, tries
    assert page.evaluate("window.publishClicks") == 0
    assert tries[0].get("covered") is True


def test_a_missing_button_is_looked_for_in_the_ad_section_first(page, monkeypatch):
    _open(page, SIMPLE_URL, 0)
    order: list = []
    real_seen = md._scroll_xpath_into_view
    state = {"n": 0}

    def seen(p, xp):
        state["n"] += 1
        return None if state["n"] == 1 else real_seen(p, xp)

    def level(p, lvl, **k):
        order.append(("level", lvl))
        return {"ok": True}

    def mounted(p, js, **k):
        order.append("mounted")
        return True

    monkeypatch.setattr(md, "_scroll_xpath_into_view", seen)
    monkeypatch.setattr(sc, "scroll_to_level", level)
    monkeypatch.setattr(md, "_scroll_until_mounted", mounted)
    opened, tries = md._open_creative_wizard(page, TRIGGER, ITEM)
    assert opened is True, tries
    # ACL-1412: до кнопки — блок Media (`show_media_block`: раздел «Ad», затем
    # доли прокрутки), и так перед первым заходом и в повторе; раздел — всегда первым.
    assert order[0] == ("level", "ad") and order[-1] == "mounted", order
    assert order.count(("level", "ad")) == 2, order
