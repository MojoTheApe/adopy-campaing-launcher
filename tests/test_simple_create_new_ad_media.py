# -*- coding: utf-8 -*-
"""Медиа нового объявления в новом редакторе (ACL-1412).

Живой прогон d1755a59e139 (30.09, Facebook_1443, объявление 2): мастер медиа
открылся, файл ушёл в медиатеку, а блок Media объявления робот больше не
увидел — раздел объявления был размонтирован (страница стояла на кампании и
группе). Проверка медиа 85 с читала страницу без «Please specify the media» и
без «Edit media», второй заход не нашёл «Set up creative», назначение —
раздела Destination.

Страница здесь — прокручиваемый контейнер с разделом «Ad», чей блок Media
монтируется, только когда контейнер стоит рядом с ним (как у Meta). Блоки —
живые вырезки: пустой «Set up creative» — `new_ad_media` из
`simple_create_more_ads_2026_09_30.html`, заполненный «Edit media» и окно
выбора медиа — `simple_create_new_ad_media_2026_09_30.html`. Синтетическое —
монтирование при прокрутке, меню «Set up creative» (Video ad / Image ad) и
появление окна выбора по «Video ad».
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import meta_dom as md  # noqa: E402
from src.fb_ads_rpa import simple_create as sc  # noqa: E402

_FIX = ROOT / "tests" / "fixtures" / "ads_manager"
_RAW_NEW = (_FIX / "simple_create_new_ad_media_2026_09_30.html").read_text(encoding="utf-8")
_RAW_MORE = (_FIX / "simple_create_more_ads_2026_09_30.html").read_text(encoding="utf-8")


def _fragments(raw: str) -> dict[str, str]:
    parts = re.split(r"<!-- fragment: (\w+) \|[^>]*-->\n", raw)
    return {parts[i]: parts[i + 1].strip() for i in range(1, len(parts), 2)}


FRAG = {**_fragments(_RAW_MORE), **_fragments(_RAW_NEW)}

SIMPLE_URL = ("https://adsmanager.facebook.com/adsmanager/manage/simple_create"
              "?act=1&selected_campaign_ids=2&selected_adset_ids=3&selected_ad_ids=4")
CLASSIC_URL = "https://adsmanager.facebook.com/adsmanager/manage/ads/edit/standalone?act=1"

_SCRIPT = """
<style>body { margin: 0; } #scroller { height: 700px; overflow-y: auto; }
  #picker-host > [role=dialog] { position: absolute; left: 40px; top: 40px; width: 900px;
    background: #fff; z-index: 5; }
  [role=menu] { background: #fff; }</style>
<script>
  window.clicks = [];
  window.mediaState = 'empty';
  const FR = %FRAGS%;
  const norm = (s) => (s || '').replace(/[\\s\\u200b]+/g, ' ').trim();
  document.addEventListener('click', (e) => {
    const t = e.target.closest('[role=button], [role=menuitem], button');
    if (!t) return;
    window.clicks.push(norm(t.getAttribute('aria-label')) || norm(t.textContent).slice(0, 40));
  }, true);
  // Виртуальная прокрутка: блок Media есть в DOM, только когда контейнер рядом.
  const mount = () => {
    const sc = document.getElementById('scroller');
    const host = document.getElementById('media-host');
    const r = host.getBoundingClientRect(), c = sc.getBoundingClientRect();
    const near = r.top < c.bottom + 300 && r.bottom > c.top - 300;
    if (near && !host.children.length)
      host.innerHTML = window.mediaState === 'filled' ? FR.media_filled : FR.new_ad_media;
    if (!near && host.children.length) host.innerHTML = '';
  };
  document.addEventListener('DOMContentLoaded', () => {
    document.getElementById('scroller').addEventListener('scroll', mount);
  });
  document.addEventListener('click', (e) => {
    const b = e.target.closest('[aria-haspopup=menu][role=button]');
    if (b && /Set up creative/.test(b.textContent) && !document.querySelector('[role=menu]')) {
      const m = document.createElement('div');
      m.setAttribute('role', 'menu');
      m.innerHTML = '<div role="menuitem">Image ad</div><div role="menuitem">Video ad</div>';
      b.parentElement.appendChild(m);
      return;
    }
    const it = e.target.closest('[role=menuitem]');
    if (it && /Video ad/.test(it.textContent)) {
      document.querySelector('[role=menu]').remove();
      document.getElementById('picker-host').innerHTML = FR.media_picker;
    }
  }, true);
</script>
"""


def _html() -> str:
    script = _SCRIPT.replace("%FRAGS%", json.dumps(FRAG))
    body = ('<div id="scroller"><div style="height:2600px">Campaign and ad set</div>'
            '<h2>Ad</h2><span data-surface="/am/editor:ad"><div>'
            '<input placeholder="Enter your ad name here..." value="Sample Ad 02"></div>'
            '<div id="media-host" style="min-height:200px"></div><div>Destination</div></span>'
            '<div style="height:2600px"></div></div><div id="picker-host"></div>')
    return "<!doctype html><html><head>" + script + "</head><body>" + body + "</body></html>"


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=os.environ.get("ACL_BROWSER_EXECUTABLE") or None)
        try:
            yield b
        finally:
            b.close()


@pytest.fixture
def env(browser, monkeypatch):
    rows: list[tuple] = []
    monkeypatch.setattr(md, "_sleep_h", lambda s, *a, **k: time.sleep(min(float(s), 0.05)))
    monkeypatch.setattr(md, "_human_jitter", lambda base, spread: 0.0)
    monkeypatch.setattr(md, "wait_editor_saved", lambda p, **k: None)
    monkeypatch.setattr(md, "_editor_is_busy_saving", lambda p: False)
    monkeypatch.setattr(md._telemetry, "record",
                        lambda step, ok=None, **kw: rows.append((step, ok, kw.get("note", ""), kw.get("diag"))))
    ctx = browser.new_context(viewport={"width": 1280, "height": 800})
    page = ctx.new_page()
    html = _html()
    page.route("**/*", lambda route: route.fulfill(status=200, content_type="text/html", body=html)
               if route.request.url.startswith("https://adsmanager.facebook.com/") else route.abort())

    def open_(url: str = SIMPLE_URL, media: str = "empty") -> None:
        page.goto(url)
        page.evaluate("(m) => { window.mediaState = m; }", media)

    try:
        yield {"page": page, "rows": rows, "open": open_}
    finally:
        ctx.close()


def _mounted(page) -> bool:
    return page.evaluate("!!document.querySelector('#imageVideoSelectorEditorComponent')")


def test_the_fixture_is_the_live_capture():
    """Вырезки — живые: пустой блок нового объявления, заполненный блок и окно
    выбора медиа; вычищены номера, адреса и имена файлов."""
    assert "Set up creative" in FRAG["new_ad_media"] and "Please specify the media" in FRAG["new_ad_media"]
    assert "Edit media" in FRAG["media_filled"] and "Remove media" in FRAG["media_filled"]
    assert "Please specify the media" not in FRAG["media_filled"]
    picker = FRAG["media_picker"]
    assert picker.startswith('<div aria-labelledby="syn_1" role="dialog"')
    assert "ad_image_video_selector_section/lib:creative-tool-asset-picker-upload-button" in picker
    for label in ("Media", "Trim", "Crop", "Text", "Creative generation", "Enhancements", "Translation",
                  "Search media", "Upload", "1 selected", "Cancel", "Next"):
        assert label in picker, label
    assert not re.search(r"\b\d{5,}\b|act=|https?://|js_\d|@|\d_[a-z]+_\d{4}", _RAW_NEW)


def test_an_unmounted_empty_block_is_brought_back(env):
    env["open"]()
    page = env["page"]
    assert not _mounted(page), "блок смонтирован сразу — тест ничего не проверил"
    got = sc.show_media_block(page)
    assert got["mounted"] and got["via"] in ("ad_heading", "scroll"), got
    assert got["state"] == "empty" and got["set_up_creative"] is True and got["picker"] is False
    assert page.evaluate("window.clicks") == [], "показ блока ничего не нажимает"


def test_the_filled_block_is_read_as_filled(env):
    env["open"](media="filled")
    got = sc.show_media_block(env["page"])
    assert got["mounted"] and got["state"] == "filled" and got["set_up_creative"] is False


def test_media_on_an_unmounted_block_is_seen(env):
    """d1755a59e139: 85 с «медиа нет» на странице, где блока Media не было в
    DOM. Теперь блок возвращается на экран, и медиа видно."""
    env["open"](media="filled")
    page = env["page"]
    assert not _mounted(page)
    t0 = time.time()
    assert md._verify_ad_media_present(page) is True
    assert time.time() - t0 < 30
    env["open"](media="filled")
    assert md._media_block_filled_now(page) is True


def test_the_classic_wizard_does_not_scroll_for_the_block(env, monkeypatch):
    """Прежний мастер — без изменений: блок не возвращается, и без него медиа
    не видно (так отказывал новый редактор до ACL-1412)."""
    env["open"](url=CLASSIC_URL, media="filled")
    shown: list = []
    monkeypatch.setattr(sc, "show_media_block", lambda p, **kw: shown.append(kw) or {})
    assert md._media_block_filled_now(env["page"]) is False
    assert shown == []


def test_the_new_ad_media_is_opened_through_its_set_up_creative(env):
    """Вход в медиа нового объявления — его собственный «Set up creative» в
    блоке Media (тот же путь, что у объявления 1), даже если блок был
    размонтирован; вход — в телеметрию."""
    env["open"]()
    page = env["page"]
    trigger = "//*[@role='button' or self::button][contains(normalize-space(.), 'Set up creative')]"
    item = "//*[@role='menuitem' or @role='option'][contains(normalize-space(.), 'Video ad')]"
    opened, tries = md._open_creative_wizard(page, trigger, item)
    assert opened, tries
    assert md._creative_wizard_open(page)
    assert page.evaluate("window.clicks") == ["Set up creative", "Video ad"]
    entry = [r for r in env["rows"] if r[0] == "ad_creative_upload"]
    assert entry and entry[0][1] is None and entry[0][2] == "media_entry:set_up_creative", env["rows"]
    assert entry[0][3]["via"] in ("ad_heading", "scroll")


def test_media_already_on_the_new_ad_is_named_as_the_entry(env):
    env["open"](media="filled")
    page = env["page"]
    trigger = "//*[@role='button' or self::button][contains(normalize-space(.), 'Set up creative')]"
    item = "//*[@role='menuitem' or @role='option'][contains(normalize-space(.), 'Video ad')]"
    opened, tries = md._open_creative_wizard(page, trigger, item, attempts=1)
    assert not opened and tries[0].get("media_on_ad") is True
    assert ("ad_creative_upload", None, "media_entry:media_on_ad") in [r[:3] for r in env["rows"]]
    assert page.evaluate("window.clicks") == []
