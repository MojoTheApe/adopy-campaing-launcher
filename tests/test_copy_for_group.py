# -*- coding: utf-8 -*-
"""Одна кнопка Copy for group: снимок проблемного состояния (ACL-1520 → ACL-1529).

Решение владельца 03.10 (ACL-1520): в группу поставщика вставляется одна
картинка. Отзыв 03.10 (ACL-1529, 9943 Account Disabled): полоса сверху
закрывала верх картинки, а сам снимок был мелким — страница занимала левый
верхний угол, остальное белое. Теперь в буфер уходит сам снимок: видимая
область без clip и scale, обрезанная по таблице и белым полям уже при чтении,
а у старых снимков пустые белые поля справа и снизу срезаются при копировании. Наружу ничего не уходит и нигде не
сохраняется.

Плюс п.5 ACL-1520: снимок берёт баннер Meta о состоянии кабинета, если тот
показан над таблицей, — проверка на подставной странице с баннером.

Живой кабинет здесь не открывается: страницы подставные, данные выдуманные.
"""
from __future__ import annotations

import base64
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


def _png_in_white(w: int, h: int, cw: int, ch: int, rgb: tuple[int, int, int]) -> bytes:
    """PNG w×h: цветной прямоугольник cw×ch в левом верхнем углу, остальное белое —
    как старый снимок из окна больше страницы (9943, 03.10)."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    full = b"\x00" + bytes(rgb) * cw + b"\xff\xff\xff" * (w - cw)
    blank = b"\x00" + b"\xff\xff\xff" * w
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(full * ch + blank * (h - ch))) + chunk(b"IEND", b""))


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


#: Разобрать PNG в браузере: размер, цвет углов и есть ли где-нибудь красный полосы.
_PIXELS = """async (blob) => {
  const bmp = await createImageBitmap(blob);
  const c = document.createElement('canvas'); c.width = bmp.width; c.height = bmp.height;
  const g = c.getContext('2d'); g.drawImage(bmp, 0, 0);
  const at = (x, y) => Array.from(g.getImageData(x, y, 1, 1).data.slice(0, 3));
  const d = g.getImageData(0, 0, bmp.width, bmp.height).data;
  let red = 0;
  for (let i = 0; i < d.length; i += 4) if (d[i] === 0xB3 && d[i+1] === 0x26 && d[i+2] === 0x1E) red++;
  return {w: bmp.width, h: bmp.height, corner: at(2, 2), bottom: at(bmp.width - 1, bmp.height - 1), red};
}"""


def _decode(page, js_blob: str) -> dict:
    return page.evaluate(f"(async () => ({_PIXELS})({js_blob}))()")


# ── картинка в буфере обмена ─────────────────────────────────────────────

def test_the_button_copies_the_screenshot_itself_without_a_band(browser):
    ctx, page, sent = _page(browser, clipboard="ok")
    try:
        page.click(".amap-copy-group")
        page.wait_for_function("window.__copied.length > 0")
        # Сразу: через 2 с надпись возвращается, а разбор PNG под нагрузкой дольше.
        page.wait_for_function("document.querySelector('.amap-copy-group').textContent === 'Copied'")
        assert page.evaluate("window.__copied.map(c => [c.type, c.blob.type])") == [["image/png", "image/png"]]
        px = _decode(page, "window.__copied[0].blob")
        assert (px["w"], px["h"]) == (W, H), "в буфере не снимок как есть"
        assert px["corner"] == list(GREEN), "верх картинки закрыт полосой"
        assert px["bottom"] == list(GREEN)
        assert px["red"] == 0, "красная полоса ACL-1520 всё ещё рисуется"
        assert page.evaluate("window.__texts") == [], "на картинке больше ничего не пишется"
        assert len(ctx.pages) == 1, "при удачном копировании вкладка не открывается"
    finally:
        ctx.close()


def test_white_margins_of_an_old_screenshot_are_cropped(browser):
    """Старый снимок 9943: страница в левом верхнем углу, справа и снизу белое."""
    global PNG
    old, PNG = PNG, _png_in_white(2000, 1100, 1000, 560, GREEN)
    try:
        ctx, page, _sent = _page(browser, clipboard="ok")
        page.click(".amap-copy-group")
        page.wait_for_function("window.__copied.length > 0")
        px = _decode(page, "window.__copied[0].blob")
        assert 1000 <= px["w"] <= 1020 and 560 <= px["h"] <= 580, px
        assert px["corner"] == list(GREEN) and px["red"] == 0
        ctx.close()
    finally:
        PNG = old


def test_a_narrow_screenshot_is_not_padded_to_960(browser):
    """Полосы нет — и холста шире снимка тоже нет."""
    global PNG
    old, PNG = PNG, _png(100, 50, GREEN)
    try:
        ctx, page, _sent = _page(browser, clipboard="ok")
        page.click(".amap-copy-group")
        page.wait_for_function("window.__copied.length > 0")
        px = _decode(page, "window.__copied[0].blob")
        assert (px["w"], px["h"]) == (100, 50)
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
def test_a_refused_clipboard_opens_the_image_in_a_new_tab(browser, clipboard):
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
        assert size == [W, H], "во вкладке — не снимок"
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


# ── снимок: баннер, выбор кабинета, таблица; масштаб 2; обрезка ──────────
#
# Снимок (ACL-1430) снимается сразу после чтения состояния, до прокрутки к
# итогам. Сверено 03.10 на настоящих снимках внутри профиля: у 6540
# (restricted) красный баннер «We restricted your ad account» стоит над
# таблицей и в снимок попадает; у Doc20 (disabled) баннера нет, «Account
# disabled» — в колонке Delivery.
#
# ACL-1529: снимок — видимая область страницы голым Page.captureScreenshot
# (без clip и scale — экран профиля не трогаем), снизу обрезанная в Python по
# последнему тексту таблицы, белые поля срезаны. Верх (выбор кабинета с номером, баннер,
# заголовок таблицы) не режется; все строки и колонка Delivery — в кадре.
# Подставная страница похожа на Ads Manager: верхняя полоса с выбором
# кабинета, баннер, таблица, левая колонка навигации во всю высоту и пустое
# место под таблицей.

_BAR_RGB = (240, 244, 255)
_BANNER_RGB = (160, 16, 32)
_HEAD_RGB = (210, 220, 235)
_TABLE_RGB = (230, 240, 250)
_LAST_RGB = (250, 235, 200)
_NAV_RGB = (40, 44, 52)
_FIXTURE = f"""<!doctype html><html><body style="margin:0;font:14px Arial">
<div style="position:fixed;left:0;top:0;bottom:0;width:48px;background:rgb{_NAV_RGB}"></div>
<div style="margin-left:48px">
<div id="bar" style="height:40px;background:rgb{_BAR_RGB}">Campaigns
  <span id="selector" role="button">111122223333444 (111122223333444)</span></div>
<div id="banner" style="height:52px;background:rgb{_BANNER_RGB};color:#fff;padding:6px 16px">
  <b>We restricted your ad account</b><br>Your ads will not run until you authenticate your account.</div>
<table id="grid" style="width:100%;border-collapse:collapse;background:rgb{_TABLE_RGB}">
  <tr style="height:40px;background:rgb{_HEAD_RGB}"><th>Campaign</th><th>Delivery</th><th>Amount spent</th></tr>
  <tr style="height:36px"><td>Example campaign 1</td><td>Account disabled</td><td>$0.00</td></tr>
  <tr style="height:36px"><td>Example campaign 2</td><td>Account disabled</td><td>$0.00</td></tr>
  <tr id="last" style="height:36px;background:rgb{_LAST_RGB}"><td>Results from 3 campaigns</td><td></td><td>$0.00</td></tr>
</table><div style="height:3000px"></div></div></body></html>"""
VW, VH = 1280, 720


@pytest.fixture
def local(tmp_path, monkeypatch):
    monkeypatch.setattr(sp.paths, "get_local_config_dir", lambda: str(tmp_path))
    return tmp_path


def _probe(ctx, png: bytes, points: dict) -> dict:
    """Размер PNG и цвет точек — разбор в браузере, без Pillow."""
    probe = ctx.new_page()
    probe.set_content("<body></body>")
    return probe.evaluate("""async ([b64, points]) => {
        const bytes = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
        const bmp = await createImageBitmap(new Blob([bytes], {type: 'image/png'}));
        const c = document.createElement('canvas'); c.width = bmp.width; c.height = bmp.height;
        const g = c.getContext('2d'); g.drawImage(bmp, 0, 0);
        const out = {w: bmp.width, h: bmp.height};
        for (const [k, [x, y]] of Object.entries(points))
          out[k] = Array.from(g.getImageData(x, y, 1, 1).data.slice(0, 3));
        return out;
    }""", [base64.b64encode(png).decode(), points])


#: Что не должно меняться от снимка: размеры, DPR, экран и медиазапросы
#: разрешения — отпечаток экрана профиля Multilogin (ревью PR #530, B2).
_SCREEN_PROBE = ("[innerWidth, innerHeight, devicePixelRatio, scrollY, screen.width, screen.height,"
                 " screen.availWidth, screen.availHeight,"
                 " matchMedia('(min-resolution: 1.5dppx)').matches, matchMedia('(min-resolution: 2dppx)').matches]")


@pytest.fixture
def telemetry(monkeypatch):
    """Записи `run_telemetry.record` о снимке: какой путь сработал."""
    from src.fb_ads_rpa import run_telemetry

    got: list[tuple[str, object, dict]] = []
    monkeypatch.setattr(run_telemetry, "record",
                        lambda step, ok=None, **kw: got.append((step, ok, kw.get("diag") or {})))
    return got


def _paths(telemetry) -> list[tuple[str, str]]:
    return [(d.get("path"), d.get("reason", "")) for step, _ok, d in telemetry if step == "status_shot"]


def _shoot_fixture(browser, html: str = _FIXTURE, scroll: int = 0, dpr: float = 1):
    ctx = browser.new_context(viewport={"width": VW, "height": VH}, device_scale_factor=dpr)
    page = ctx.new_page()
    page.set_content(html)
    if scroll:
        page.evaluate(f"window.scrollTo(0, {scroll})")
    before = page.evaluate(_SCREEN_PROBE)
    shot_id, _at = sp._status_shot(page, "FacebookExample - Owner")
    assert shot_id
    after = page.evaluate(_SCREEN_PROBE)
    assert after == before, "снимок изменил экран, масштаб или прокрутку страницы"
    return ctx, page, sp.status_shot_path(shot_id).read_bytes()


def test_the_capture_is_the_view_cropped_below_the_table(browser, local, telemetry):
    ctx, page, png = _shoot_fixture(browser)
    try:
        last = page.evaluate("document.getElementById('last').getBoundingClientRect().bottom")
        got = _probe(ctx, png, {})
        assert got["w"] == VW, "по ширине — не вся видимая область"
        assert last <= got["h"] <= last + sp._SHOT_PAD + 2, \
            f"низ не обрезан по таблице: {got['h']} при последней строке на {last}"
        assert got["h"] < VH, "пустое место под таблицей осталось в кадре"
        assert _paths(telemetry) == [("full", "")]
    finally:
        ctx.close()


@pytest.fixture(scope="module")
def hidpi(browser):
    """Хост с настоящим масштабом экрана (Windows 150 %, 200 %…), headless.

    Не `device_scale_factor` контекста: это подмена метрик через CDP, а
    снимок без clip под подменой приходит в пикселях хоста, не в DPR страницы.
    """
    browsers: dict[float, object] = {}

    def launch(dsf: float):
        if dsf not in browsers:
            browsers[dsf] = browser.browser_type.launch(
                executable_path=os.environ.get("ACL_BROWSER_EXECUTABLE") or None,
                args=[f"--force-device-scale-factor={dsf}", f"--window-size={VW},{VH}"])
        return browsers[dsf]
    try:
        yield launch
    finally:
        for b in browsers.values():
            b.close()


def _shoot_hidpi(hidpi, dsf: float, override: dict | None = None):
    ctx = hidpi(dsf).new_context(no_viewport=True)
    page = ctx.new_page()
    page.set_content(_FIXTURE)
    if override:
        ctx.new_cdp_session(page).send("Emulation.setDeviceMetricsOverride", dict(
            width=0, height=0, mobile=False, **override))
    before = page.evaluate(_SCREEN_PROBE)
    shot_id, _at = sp._status_shot(page, "FacebookExample - Owner")
    assert shot_id
    assert page.evaluate(_SCREEN_PROBE) == before, "снимок изменил экран или масштаб страницы"
    css = page.evaluate("[innerWidth, document.getElementById('last').getBoundingClientRect()]")
    return ctx, css[0], css[1], sp.status_shot_path(shot_id).read_bytes()


def test_the_crop_follows_the_screen_scale(hidpi, local):
    """Хост 200 %: снимок в пикселях устройства, рамка CSS px ×2."""
    ctx, vw, last, png = _shoot_hidpi(hidpi, 2)
    try:
        got = _probe(ctx, png, {"last": (vw * 2 - 40, int(last["bottom"] * 2) - 10)})
        assert got["w"] == vw * 2
        assert last["bottom"] * 2 <= got["h"] <= (last["bottom"] + sp._SHOT_PAD + 2) * 2
        assert got["last"] == list(_LAST_RGB)
    finally:
        ctx.close()


def test_a_profile_dpr_override_does_not_halve_the_crop(hidpi, local):
    """Как в профиле 9943: хост 150 %, профиль держит DPR 0.75 и экран 1280×800.
    Снимок — 1.5 px на CSS px, а не 0.75: кадр не режется пополам."""
    ctx, vw, last, png = _shoot_hidpi(hidpi, 1.5, {"deviceScaleFactor": 0.75, "screenWidth": 1280,
                                                   "screenHeight": 800})
    try:
        got = _probe(ctx, png, {"bar": (int(vw * 1.5) - 20, 4),
                                "last": (int(vw * 1.5) - 20, int(last["bottom"] * 1.5) - 8)})
        assert got["w"] == round(vw * 1.5), "ширина кадра посчитана по подменённому DPR"
        assert got["bar"] == list(_BAR_RGB) and got["last"] == list(_LAST_RGB)
        assert got["h"] <= (last["bottom"] + sp._SHOT_PAD + 2) * 1.5, "низ не обрезан по таблице"
    finally:
        ctx.close()


def test_a_wide_capture_is_capped(hidpi, local):
    """Хост 300 %: 1280 CSS px — 3840 px; храним не шире `STATUS_SHOT_MAX_WIDTH` (ревью N1)."""
    ctx, _vw, _last, png = _shoot_hidpi(hidpi, 3)
    try:
        got = _probe(ctx, png, {"bar": (sp.STATUS_SHOT_MAX_WIDTH - 10, 4)})
        assert got["w"] == sp.STATUS_SHOT_MAX_WIDTH
        assert got["bar"] == list(_BAR_RGB)
    finally:
        ctx.close()


def test_the_capture_keeps_the_selector_banner_header_and_all_rows(browser, local):
    ctx, page, png = _shoot_fixture(browser)
    try:
        box = page.evaluate("""() => Object.fromEntries(['selector', 'banner', 'last'].map(id => {
            const r = document.getElementById(id).getBoundingClientRect();
            return [id, [r.right, r.top + r.height / 2]]; }))""")
        head = page.evaluate("(() => { const r = document.querySelector('#grid tr').getBoundingClientRect();"
                             " return [r.right - 20, r.top + 4]; })()")
        delivery = page.evaluate("(() => { const r = document.querySelectorAll('#grid tr')[1].children[1]"
                                 ".getBoundingClientRect(); return [r.right - 4, r.top + 4]; })()")
        pt = {k: (int(x), int(y)) for k, (x, y) in
              dict(bar=(VW - 20, 4), banner=(VW - 20, box["banner"][1]), head=head,
                   delivery=delivery, last=(VW - 20, box["last"][1]), nav=(10, 10)).items()}
        got = _probe(ctx, png, pt)
        assert got["bar"] == list(_BAR_RGB), "верхняя полоса с выбором кабинета срезана"
        assert got["banner"] == list(_BANNER_RGB), "баннер над таблицей не попал в снимок"
        assert got["head"] == list(_HEAD_RGB), "заголовок таблицы срезан"
        assert got["delivery"] == list(_TABLE_RGB), "колонка Delivery срезана"
        assert got["last"] == list(_LAST_RGB), "последняя строка таблицы срезана"
        assert got["nav"] == list(_NAV_RGB)
        assert box["selector"][0] < got["w"] and box["selector"][1] < got["h"], \
            "выбор кабинета с номером не целиком в кадре"
    finally:
        ctx.close()


def test_the_capture_follows_the_scrolled_page(browser, local):
    """Рамка — то, что на экране, а не верх документа."""
    ctx, _page, png = _shoot_fixture(browser, scroll=40)
    try:
        got = _probe(ctx, png, {"top": (VW - 40, 4)})
        assert got["top"] == list(_BANNER_RGB), "снят верх документа, а не видимая часть"
    finally:
        ctx.close()


def test_without_a_table_the_whole_view_is_kept(browser, local):
    """У отключённого кабинета таблицы может не быть — тогда весь экран."""
    ctx, _page, png = _shoot_fixture(
        browser, f'<body style="margin:0;background:#888"><div style="height:60px;background:rgb{_BANNER_RGB}">'
                 'Your ad account is disabled</div></body>')
    try:
        got = _probe(ctx, png, {"banner": (200, 20)})
        assert (got["w"], got["h"]) == (VW, VH)
        assert got["banner"] == list(_BANNER_RGB)
    finally:
        ctx.close()


def test_white_margins_are_trimmed_when_the_shot_is_saved():
    """Окно больше страницы (9943): справа и снизу белое — срезается уже при чтении."""
    import io

    from PIL import Image

    png = _png_in_white(2000, 1100, 1000, 560, GREEN)
    out = Image.open(io.BytesIO(sp._crop_shot(png, {"width": 2000, "height": 1100, "dpr": 1})))
    pad = round(2000 / 200)
    assert out.size == (1000 + pad, 560 + pad)
    assert out.getpixel((2, 2)) == GREEN
    # Белый снимок целиком не схлопывается.
    blank = _png(300, 200, (255, 255, 255))
    assert Image.open(io.BytesIO(sp._crop_shot(blank, {"width": 300, "height": 200, "dpr": 1}))).size == (300, 200)


# ── экран профиля не трогаем (ревью PR #530, B2) ─────────────────────────

#: Подмены, которые держит чужая сессия: экран и DPR (отпечаток профиля) и
#: размер области (замер ACL-1532 на 9943 03.10: `page.screenshot` снял её —
#: страница из 1919 стала 3837 px и так и осталась). Один размер области
#: headless Chromium 149 при `page.screenshot` не сбрасывает (живой сброс тут
#: не воспроизводится), поэтому он идёт вместе с DPR и экраном — так
#: контроль в конце видит сброс, и проверка не пустая.
_FOREIGN_OVERRIDES = {
    "screen_and_dpr": ({"width": 0, "height": 0, "deviceScaleFactor": 1.5, "mobile": False,
                        "screenWidth": 1920, "screenHeight": 1080},
                       lambda v: v[2] == 1.5 and v[4:6] == [1920, 1080] and v[8] is True),
    "viewport_size": ({"width": 640, "height": 400, "deviceScaleFactor": 1.5, "mobile": False,
                       "screenWidth": 1280, "screenHeight": 800},
                      lambda v: v[0:2] == [640, 400] and v[2] == 1.5 and v[4:6] == [1280, 800]),
}


@pytest.mark.parametrize("override", list(_FOREIGN_OVERRIDES))
def test_a_foreign_device_metrics_override_survives_the_capture(browser, local, telemetry, override):
    """Подмену метрик держит чужая CDP-сессия (как профиль Multilogin) — после
    снимка размеры, экран, DPR и медиазапросы разрешения те же. Контроль в
    конце: прежний `page.screenshot` эту же подмену сбрасывает, т.е. проверка
    не пустая."""
    params, held = _FOREIGN_OVERRIDES[override]
    ctx = browser.new_context(no_viewport=True)
    try:
        page = ctx.new_page()
        page.set_content(_FIXTURE)
        holder = ctx.new_cdp_session(page)
        holder.send("Emulation.setDeviceMetricsOverride", params)
        before = page.evaluate(_SCREEN_PROBE)
        assert held(before), before
        for _ in range(3):
            shot_id, _at = sp._status_shot(page, "FacebookExample - Owner")
            assert shot_id
            assert page.evaluate(_SCREEN_PROBE) == before, "снимок сбросил подмену экрана профиля"
        assert _paths(telemetry) == [("full", "")] * 3

        page.screenshot(full_page=False)
        assert page.evaluate(_SCREEN_PROBE) != before, "контроль: page.screenshot должен был сбросить подмену"
    finally:
        ctx.close()


# ── не виснуть: скрытая страница, тайм-аут, отказ (ревью PR #530, B1) ────

def _spy_session(ctx, monkeypatch, send=None):
    """Настоящая CDP-сессия, у которой подменён `send` реализации; считает detach."""
    seen = {"send": [], "detach": 0}
    real = ctx.new_cdp_session

    def make(page):
        s = real(page)
        impl = s._impl_obj
        orig_send, orig_detach = impl.send, impl.detach

        async def spy_send(method, params=None):
            seen["send"].append(method)
            if send is not None:
                return await send(method, params)
            return await orig_send(method, params)

        async def spy_detach():
            seen["detach"] += 1
            return await orig_detach()

        impl.send, impl.detach = spy_send, spy_detach
        return s

    monkeypatch.setattr(ctx, "new_cdp_session", make)
    return seen


def test_a_capture_that_never_answers_is_abandoned(browser, local, telemetry, monkeypatch):
    import asyncio
    import time

    async def never(_m, _p):
        await asyncio.sleep(3600)

    monkeypatch.setattr(sp, "_SHOT_DEADLINE_S", 1.0)
    ctx = browser.new_context(viewport={"width": VW, "height": VH})
    try:
        page = ctx.new_page()
        page.set_content(_FIXTURE)
        seen = _spy_session(ctx, monkeypatch, send=never)
        t0 = time.monotonic()
        assert sp._status_shot(page, "FacebookExample - Owner") == ("", "")
        assert time.monotonic() - t0 < 6, "снимок ждал дольше срока"
        assert seen["send"] == ["Page.captureScreenshot"] and seen["detach"] == 1
        assert _paths(telemetry) == [("none", "timeout")]
        assert not list((local / "spend_shots").glob("*.png"))
        assert page.evaluate("1 + 1") == 2, "страница после брошенного снимка не отвечает"
    finally:
        ctx.close()


def test_a_capture_that_fails_leaves_no_shot_and_detaches(browser, local, telemetry, monkeypatch):
    async def boom(_m, _p):
        raise RuntimeError("Target closed")

    ctx = browser.new_context(viewport={"width": VW, "height": VH})
    try:
        page = ctx.new_page()
        page.set_content(_FIXTURE)
        seen = _spy_session(ctx, monkeypatch, send=boom)
        assert sp._status_shot(page, "FacebookExample - Owner") == ("", "")
        assert seen["detach"] == 1, "CDP-сессия не отцеплена после отказа"
        assert _paths(telemetry) == [("none", "error")]
    finally:
        ctx.close()


def test_a_hidden_page_is_not_captured(browser, local, telemetry, monkeypatch):
    """Свёрнутое окно: кадра может не быть никогда — даже не пробуем."""
    ctx = browser.new_context(viewport={"width": VW, "height": VH})
    try:
        ctx.add_init_script("Object.defineProperty(document, 'visibilityState', {get: () => 'hidden'});")
        page = ctx.new_page()
        page.set_content(_FIXTURE)
        seen = _spy_session(ctx, monkeypatch)
        assert sp._status_shot(page, "FacebookExample - Owner") == ("", "")
        assert seen["send"] == [], "у скрытой страницы снимок всё равно запрошен"
        assert _paths(telemetry) == [("none", "hidden")]
    finally:
        ctx.close()


def _fake_page(session):
    def no_plain(**_k):
        pytest.fail("после отказа CDP старый page.screenshot не зовётся")

    return NS(context=NS(new_cdp_session=lambda _p: session),
              evaluate=lambda js, arg=None: {"visible": True, "dpr": 1, "width": 100, "height": 50},
              screenshot=no_plain)


def test_a_blocking_send_outside_playwright_is_bounded_too(local, telemetry, monkeypatch):
    import threading
    import time

    gate = threading.Event()
    detached = []
    session = NS(send=lambda *a: gate.wait(30), detach=lambda: detached.append(1))
    monkeypatch.setattr(sp, "_SHOT_DEADLINE_S", 0.5)
    t0 = time.monotonic()
    try:
        assert sp._status_shot(_fake_page(session), "FacebookExample - Owner") == ("", "")
        assert time.monotonic() - t0 < 5
        assert detached == [1]
        assert _paths(telemetry) == [("none", "timeout")]
    finally:
        gate.set()


def test_detach_runs_even_when_send_raises(local, telemetry):
    """Ревью N2: `session.detach()` — в `finally`."""
    detached = []

    def boom(*_a):
        raise RuntimeError("Protocol error")

    session = NS(send=boom, detach=lambda: detached.append(1))
    assert sp._status_shot(_fake_page(session), "FacebookExample - Owner") == ("", "")
    assert detached == [1]
    assert _paths(telemetry) == [("none", "error")]


def test_without_cdp_the_plain_screenshot_is_taken(local, telemetry):
    """Не Chromium (CDP нет вовсе) — прежний `page.screenshot`."""
    shots = []
    page = NS(context=NS(),
              screenshot=lambda path, full_page=False: (shots.append(full_page), Path(path).write_bytes(PNG)))
    shot_id, _at = sp._status_shot(page, "FacebookExample - Owner")
    assert shot_id and shots == [False]
    assert sp.status_shot_path(shot_id).read_bytes() == PNG
    assert _paths(telemetry) == [("fallback", "")]


def test_a_failed_capture_is_reported_once_per_account_and_scrubbed(local, telemetry, monkeypatch):
    import contextlib

    from src import acl_sentry

    sent: list[dict] = []

    class Scope:
        def __init__(self):
            self.tags, self.ctx, self.fingerprint = {}, {}, None

        def set_tag(self, k, v):
            self.tags[k] = v

        def set_context(self, k, v):
            self.ctx[k] = v

    current: list[Scope] = []

    @contextlib.contextmanager
    def new_scope():
        current.append(Scope())
        yield current[-1]

    def capture_message(msg, level=None):
        s = current[-1]
        sent.append({"fp": s.fingerprint, "tags": s.tags, "ctx": s.ctx, "msg": msg, "level": level})

    monkeypatch.setitem(sys.modules, "sentry_sdk", NS(new_scope=new_scope, capture_message=capture_message))
    monkeypatch.setattr(acl_sentry, "init", lambda: True)
    monkeypatch.setattr(sp, "_shot_warned", {})

    def boom(*_a):
        raise RuntimeError("failed at https://adsmanager.facebook.com/adsmanager?act=123456789 token EAA"
                           + "x" * 30 + " act_987654321")

    session = NS(send=boom, detach=lambda: None)
    for _ in range(3):
        sp._status_shot(_fake_page(session), "FacebookExample - Owner")
    sp._status_shot(_fake_page(session), "FacebookOther - Owner")
    assert len(sent) == 2, "предупреждение не ограничено одним на кабинет"
    first = sent[0]
    assert first["level"] == "warning" and first["msg"] == "status_shot: error"
    assert first["tags"]["account"] == "FacebookExample", "в теге имя персоны"
    detail = first["ctx"]["status_shot"]["detail"]
    for secret in ("adsmanager.facebook.com", "123456789", "987654321", "EAAx"):
        assert secret not in detail
    assert len(_paths(telemetry)) == 4


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
