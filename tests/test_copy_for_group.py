# -*- coding: utf-8 -*-
"""Одна кнопка Copy for group: строка и снимок одной картинкой (ACL-1520).

Решение владельца 03.10: в группу поставщика вставляется одна картинка —
сверху полоса «<имя> — Account <Status> · <время чтения>», под ней снимок
проблемного состояния (ACL-1430). Картинка собирается в браузере на canvas из
сохранённого PNG; наружу ничего не уходит и нигде не сохраняется.

Плюс п.5 Issue: снимок берёт баннер Meta о состоянии кабинета, если тот
показан над таблицей, — проверка на подставной странице с баннером.

Живой кабинет здесь не открывается: страницы подставные, данные выдуманные.
"""
from __future__ import annotations

import os
import re
import struct
import sys
import zlib
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.analysis_agent import account_spend as sp  # noqa: E402
from tests.profile_lease_fakes import fake_profile

TPL = ROOT / "src" / "web" / "templates"
SHOT = "status_Doc20__20261003-100001"
HEAD = "FacebookDoc20 — Account Disabled · 2026-10-03 10:00:00"
W, H = 1200, 300
GREEN = (0, 170, 0)
BAND_RED = (0xB3, 0x26, 0x1E)


def _png(w: int, h: int, rgb: tuple[int, int, int]) -> bytes:
    """Однотонный PNG без Pillow: снимок-заглушка с узнаваемым цветом."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    row = b"\x00" + bytes(rgb) * w
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(row * h)) + chunk(b"IEND", b""))


PNG = _png(W, H, GREEN)


def _block() -> str:
    src = (TPL / "report.html").read_text(encoding="utf-8")
    m = re.search(r"/\* ACL-1520 copy-group \{ \*/(.*?)/\* \} ACL-1520 copy-group \*/", src, re.S)
    assert m, "блок Copy for group в report.html не найден"
    return m.group(1)


@pytest.fixture(scope="module")
def browser():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=os.environ.get("ACL_BROWSER_EXECUTABLE") or None)
        try:
            yield b
        finally:
            b.close()


#: Буфер обмена в тестах подменён: настоящий в headless недоступен, а главное —
#: так видно ровно то, что кнопка в него положила.
_CLIPBOARD = {
    "ok": """
      window.__copied = [];
      window.ClipboardItem = function (items) { this.items = items; this.types = Object.keys(items); };
      Object.defineProperty(navigator, 'clipboard', {value: {write: async function (list) {
        for (const item of list) for (const t of item.types) {
          const blob = await item.items[t];
          window.__copied.push({type: t, blob: blob});
        }
      }}});""",
    "refuse": """
      window.ClipboardItem = function (items) { this.items = items; this.types = Object.keys(items); };
      Object.defineProperty(navigator, 'clipboard', {value: {write: async function () {
        throw new DOMException('Write permission denied.', 'NotAllowedError');
      }}});""",
    "missing": "delete window.ClipboardItem;",
}

#: Что рисуется текстом на canvas — из этого видно строку в полосе.
_SPY_TEXT = """
  window.__texts = [];
  const _fill = CanvasRenderingContext2D.prototype.fillText;
  CanvasRenderingContext2D.prototype.fillText = function (t, x, y) { window.__texts.push(String(t)); return _fill.apply(this, arguments); };
"""


def _page(browser, *, clipboard: str, shot_status: int = 200, button: str | None = None):
    """Карта с одной кнопкой. Каждый запрос записывается; чужие обрываются."""
    ctx = browser.new_context()
    sent: list[tuple[str, str]] = []
    btn = button or (f'<button type="button" class="amap-btn amap-copy-group" data-shot="{SHOT}" '
                     f'data-head="{HEAD}" onclick="amapCopyGroup(this)">Copy for group</button>')

    def serve(route):
        req = route.request
        sent.append((req.method, req.url))
        if req.url.endswith("/map"):
            route.fulfill(content_type="text/html; charset=utf-8", body=(
                '<!doctype html><html><body><div class="amap-status"><span class="amap-st bad">disabled</span>'
                f'<div class="amap-st-report">{btn}</div></div><script>{_block()}</script></body></html>'))
        elif req.url.endswith(f"/account-spend/status-shot/{SHOT}"):
            if shot_status == 200:
                route.fulfill(content_type="image/png", body=PNG)
            else:
                route.fulfill(status=shot_status, body="")
        elif req.url.endswith(f"/account-spend/status-shot/{SHOT}/view"):
            route.fulfill(content_type="text/html", body="<p>Right-click → Copy image</p>")
        else:
            route.fulfill(status=404, body="")

    ctx.route("http://acl.test/**", serve)
    ctx.route(re.compile(r"^(?!http://acl\.test/|about:|blob:).*"), lambda r: (sent.append(("?", r.request.url)), r.abort()))
    ctx.add_init_script(_CLIPBOARD[clipboard] + _SPY_TEXT)
    page = ctx.new_page()
    page.goto("http://acl.test/map")
    return ctx, page, sent


#: Разобрать PNG в браузере: размер и цвет нескольких точек.
_PIXELS = """async (blob) => {
  const bmp = await createImageBitmap(blob);
  const c = document.createElement('canvas'); c.width = bmp.width; c.height = bmp.height;
  const g = c.getContext('2d'); g.drawImage(bmp, 0, 0);
  const at = (x, y) => Array.from(g.getImageData(x, y, 1, 1).data.slice(0, 3));
  // полоса — сверху, пока цвет не станет цветом снимка
  let band = 0; while (band < bmp.height && at(2, band).join() !== '0,170,0') band++;
  let white = 0;
  const row = g.getImageData(0, 0, bmp.width, band).data;
  for (let i = 0; i < row.length; i += 4) if (row[i] > 240 && row[i+1] > 240 && row[i+2] > 240) white++;
  return {w: bmp.width, h: bmp.height, band, corner: at(2, 2), white,
          below: at(10, band + 10), bottom: at(W - 1, bmp.height - 1)};
}""".replace("W - 1", str(W - 1))


def _decode(page, js_blob: str) -> dict:
    return page.evaluate(f"(async () => ({_PIXELS})({js_blob}))()")


# ── картинка в буфере обмена ─────────────────────────────────────────────

def test_the_button_copies_one_png_with_the_header_above_the_screenshot(browser):
    ctx, page, sent = _page(browser, clipboard="ok")
    try:
        page.click(".amap-copy-group")
        page.wait_for_function("window.__copied.length > 0")
        assert page.evaluate("window.__copied.map(c => [c.type, c.blob.type])") == [["image/png", "image/png"]]
        px = _decode(page, "window.__copied[0].blob")
        assert px["w"] == W, "ширина — ширина снимка"
        assert px["band"] >= 60, "полоса со строкой слишком узкая или её нет"
        assert px["h"] == H + px["band"], "под полосой — весь снимок, ни строкой меньше"
        assert px["corner"] == list(BAND_RED), "полоса не того цвета"
        assert px["white"] > 500, "в полосе нет текста"
        assert px["below"] == list(GREEN) and px["bottom"] == list(GREEN), "под полосой не снимок"
        assert page.evaluate("window.__texts") == [HEAD]
        assert page.text_content(".amap-copy-group") == "Copied"
        assert len(ctx.pages) == 1, "при удачном копировании вкладка не открывается"
    finally:
        ctx.close()


def test_a_narrow_screenshot_gets_a_readable_band(browser):
    """Узкий снимок не сжимает строку: холст не уже 960 точек."""
    global PNG
    old, PNG = PNG, _png(100, 50, GREEN)
    try:
        ctx, page, _sent = _page(browser, clipboard="ok")
        page.click(".amap-copy-group")
        page.wait_for_function("window.__copied.length > 0")
        px = page.evaluate("""async () => { const b = await createImageBitmap(window.__copied[0].blob);
                               return [b.width, b.height]; }""")
        assert px[0] == 960 and px[1] > 50
        ctx.close()
    finally:
        PNG = old


# ── без снимка ───────────────────────────────────────────────────────────

def test_without_a_shot_the_button_is_disabled_and_does_nothing(browser):
    btn = ('<button type="button" class="amap-btn amap-copy-group" disabled '
           'title="Screenshots are switched off for safety">Copy for group</button>')
    ctx, page, sent = _page(browser, clipboard="ok", button=btn)
    try:
        b = page.locator(".amap-copy-group")
        assert b.is_disabled()
        assert b.get_attribute("title") == "Screenshots are switched off for safety"
        b.click(force=True)
        page.wait_for_timeout(300)
        assert page.evaluate("window.__copied") == []
        assert [u for _m, u in sent if "status-shot" in u] == []
    finally:
        ctx.close()


# ── запасной путь: браузер не дал буфер ──────────────────────────────────

@pytest.mark.parametrize("clipboard", ["refuse", "missing"])
def test_a_refused_clipboard_opens_the_composed_image_in_a_new_tab(browser, clipboard):
    ctx, page, _sent = _page(browser, clipboard=clipboard)
    try:
        with ctx.expect_page() as new:
            page.click(".amap-copy-group")
        tab = new.value
        tab.wait_for_function("document.images.length === 1 && document.images[0].complete "
                              "&& document.images[0].naturalWidth > 0")
        assert "Right-click → Copy image" in tab.inner_text("body")
        assert tab.title() == HEAD
        size = tab.evaluate("[document.images[0].naturalWidth, document.images[0].naturalHeight]")
        assert size[0] == W and size[1] > H, "во вкладке — не собранная картинка, а голый снимок"
        assert page.text_content(".amap-copy-group") == "Copy for group"
    finally:
        ctx.close()


def test_a_screenshot_that_will_not_load_falls_back_to_the_view_page(browser):
    ctx, page, _sent = _page(browser, clipboard="missing", shot_status=404)
    try:
        with ctx.expect_page() as new:
            page.click(".amap-copy-group")
        tab = new.value
        tab.wait_for_load_state()
        assert tab.url == f"http://acl.test/account-spend/status-shot/{SHOT}/view"
    finally:
        ctx.close()


# ── ничего не уходит и не сохраняется ────────────────────────────────────

@pytest.mark.parametrize("clipboard", ["ok", "refuse"])
def test_nothing_is_sent_or_stored(browser, clipboard):
    ctx, page, sent = _page(browser, clipboard=clipboard)
    try:
        if clipboard == "ok":
            page.click(".amap-copy-group")
            page.wait_for_function("window.__copied.length > 0")
        else:
            with ctx.expect_page() as new:
                page.click(".amap-copy-group")
            new.value.wait_for_function("document.images.length === 1 && document.images[0].complete")
        page.wait_for_timeout(300)
        assert sent == [("GET", "http://acl.test/map"), ("GET", f"http://acl.test/account-spend/status-shot/{SHOT}")], \
            f"кроме самого снимка что-то ушло в сеть: {sent}"
        for p in ctx.pages:
            if p.url.startswith("http"):
                assert p.evaluate("localStorage.length + sessionStorage.length") == 0
        assert ctx.cookies() == []
    finally:
        ctx.close()


# ── п.5: баннер Meta попадает в снимок ───────────────────────────────────
#
# Снимок (ACL-1430) — видимая часть окна (`full_page=False`), без обрезки по
# таблице, и снимается сразу после чтения состояния, до прокрутки к итогам.
# Сверено 03.10 на настоящих снимках внутри профиля: у 6540 (restricted)
# красный баннер «We restricted your ad account» стоит над таблицей и в снимок
# попадает; у Doc20 (disabled) баннера нет, «Account disabled» — в колонке
# Delivery. Тест держит оба условия: баннер над таблицей — в кадре, и снимок
# раньше прокрутки.

_BANNER_RGB = (160, 16, 32)
_TABLE_RGB = (230, 240, 250)
_FIXTURE = f"""<!doctype html><html><body style="margin:0;font:14px Arial">
<div id="banner" style="height:52px;background:rgb{_BANNER_RGB};color:#fff;padding:6px 16px">
  <b>We restricted your ad account</b><br>Your ads won't run until you authenticate your account.</div>
<div style="height:48px;background:#f4f4f4">Campaigns</div>
<table style="width:100%;background:rgb{_TABLE_RGB}">
  <tr><th>Campaign</th><th>Delivery</th><th>Amount spent</th></tr>
  <tr><td>Example campaign</td><td>Account disabled</td><td>$0.00</td></tr>
</table><div style="height:3000px"></div></body></html>"""


@pytest.fixture
def local(tmp_path, monkeypatch):
    monkeypatch.setattr(sp.paths, "get_local_config_dir", lambda: str(tmp_path))
    return tmp_path


def test_the_status_capture_includes_the_account_banner(browser, local):
    ctx = browser.new_context(viewport={"width": 1280, "height": 720})
    try:
        page = ctx.new_page()
        page.set_content(_FIXTURE)
        shot_id, _at = sp._status_shot(page, "FacebookExample - Owner")
        assert shot_id
        png = sp.status_shot_path(shot_id).read_bytes()
        probe = ctx.new_page()
        probe.set_content("<body></body>")
        got = probe.evaluate("""async (b64) => {
            const bytes = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
            const bmp = await createImageBitmap(new Blob([bytes], {type: 'image/png'}));
            const c = document.createElement('canvas'); c.width = bmp.width; c.height = bmp.height;
            const g = c.getContext('2d'); g.drawImage(bmp, 0, 0);
            const at = (x, y) => Array.from(g.getImageData(x, y, 1, 1).data.slice(0, 3));
            return {top: at(1200, 2), banner: at(1200, 40), table: at(1200, 140)};
        }""", __import__("base64").b64encode(png).decode())
        assert got["top"] == list(_BANNER_RGB) and got["banner"] == list(_BANNER_RGB), \
            "баннер над таблицей не попал в снимок"
        assert got["table"] == list(_TABLE_RGB), "под баннером — не таблица"
    finally:
        ctx.close()


def test_the_status_shot_is_taken_before_scrolling_to_the_totals(local, monkeypatch):
    """Прокрутка к итогам (`_read_total`) увела бы баннер из кадра."""
    monkeypatch.setenv("ACL_STATUS_SHOT", "1")  # ACL-1535: по умолчанию снимка нет
    order: list[str] = []
    page = NS(url="https://adsmanager.facebook.com/?act=111", goto=lambda *a, **k: None, close=lambda: None,
              screenshot=lambda path, full_page=False: (order.append("shot"), Path(path).write_bytes(PNG)))
    monkeypatch.setattr(sp, "_entry_for", lambda account: NS(ad_account_id="111", profile_id=fake_profile(account)))
    monkeypatch.setattr(sp, "_attach_only", lambda *a: NS(page=page, close=lambda **k: None))
    monkeypatch.setattr(sp, "_wait_for_grid", lambda _: True)
    monkeypatch.setattr(sp, "_read_status", lambda _: ("restricted", "We restricted your ad account"))
    monkeypatch.setattr(sp, "_shown_period", lambda _: "2026-10-01")
    monkeypatch.setattr(sp, "_read_total", lambda _: (order.append("scroll"), (7.0, "test"))[1])
    monkeypatch.setattr(sp, "_read_tree", lambda *a, **k: (order.append("tree"), ([], "", ""))[1])
    monkeypatch.setattr(sp, "_store_path", lambda: local / "account_spend.json")
    out = sp.read_via_rpa("FacebookExample - Owner", "2026-10-01", "2026-10-01")
    assert out.status == "restricted" and out.status_shot
    assert order and order[0] == "shot", order


# ── ACL-1652 (owner, 07.10): the button and its hint are hidden for now ────────

def _status_cell(show=None):
    from src.web.app import templates

    country = {"country": "Kuwait", "key": "KW", "virtual": False,
               "rows": [{"account": "FacebookDoc20 - x", "has_data": False, "ready": True, "in_workbook": True, "to_do": "",
                         "status": {"status": "disabled", "status_text": "Account disabled", "at": "10:00",
                                    "at_short": "10:00", "shot_id": "s1", "shot_at": "10:00", "stale": False}}]}
    kw = {} if show is None else {"SHOW_COPY_FOR_GROUP": show}
    html = templates.env.get_template("_account_map_country.html").render(
        mc=country, mlx={}, amap={"countries": [country], "available": [], "all_accounts": [], "live": {}, "splits": {},
                                  "fallback": {}, "dates": [], "date_from": "", "date_to": "", "span": "", "notes": {}}, **kw)
    cell = html[html.index('<td class="amap-status">'):]
    return cell[: cell.index("</td>")]


def test_copy_for_group_is_hidden_by_default():
    from src.web import app

    assert app.SHOW_COPY_FOR_GROUP is False
    cell = _status_cell()
    assert "amap-copy-group" not in cell and "Copy for group" not in cell
    assert "amap-st-hint" not in cell and "provider" not in cell
    assert 'class="amap-st bad"' in cell, "статус остаётся"


def test_the_flag_brings_the_button_and_its_hint_back():
    cell = _status_cell(show=True)
    assert 'class="amap-btn amap-copy-group"' in cell and 'data-shot="s1"' in cell
    assert "Copy for group — paste it into the provider's group" in cell
