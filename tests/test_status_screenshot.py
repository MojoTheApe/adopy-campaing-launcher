# -*- coding: utf-8 -*-
"""Проблемное состояние кабинета приходит со снимком и кнопкой Copy screenshot (ACL-1430).

01.10 владелец попросил доказательство для поддержки: Doc20 disabled, 6540
restricted. До этой правки снимок страницы оставался только при неудачном
чтении, путём к файлу в подсказке. Теперь чтение, нашедшее что угодно, кроме
`clear`, снимает видимую часть Ads Manager — внутри того же профиля Multilogin,
другой страницы у чтения нет, — а карта даёт кнопку, которая кладёт картинку
в буфер обмена.

Живой кабинет здесь не открывается: страница подставная.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.analysis_agent import account_spend as sp  # noqa: E402

DAY = "2026-09-30"
ACC = "FacebookDoc20 - Example"
PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\rIDATx\x9cc\xf8\xcf\xc0\xf0\x1f\x00\x05\x00\x01\xff\xa5\xf6E@\x00\x00\x00\x00IEND\xaeB`\x82"
)


@pytest.fixture
def local(tmp_path, monkeypatch):
    """Хранилище и папка снимков — во временной папке, не в %LOCALAPPDATA%."""
    monkeypatch.setattr(sp.paths, "get_local_config_dir", lambda: str(tmp_path))
    monkeypatch.setattr(sp, "_store_path", lambda: tmp_path / "account_spend.json")
    return tmp_path


def _read(monkeypatch, status, *, grid=True):
    shots = []

    def screenshot(path, full_page=False):
        assert full_page is False, "снимок — видимая часть, баннер и колонка Delivery"
        shots.append(path)
        Path(path).write_bytes(PNG)

    page = NS(url="https://adsmanager.facebook.com/?act=111", goto=lambda *a, **k: None,
              close=lambda: None, screenshot=screenshot)
    monkeypatch.setattr(sp, "_entry_for", lambda _: NS(ad_account_id="111"))
    monkeypatch.setattr(sp, "_attach_only", lambda *a: NS(page=page, close=lambda **k: None))
    monkeypatch.setattr(sp, "_wait_for_grid", lambda _: grid)
    monkeypatch.setattr(sp, "_read_status", lambda _: status)
    monkeypatch.setattr(sp, "_shown_period", lambda _: DAY)
    monkeypatch.setattr(sp, "_read_total", lambda _: (7.0, "test"))
    monkeypatch.setattr(sp, "_read_tree", lambda *a, **k: ([], "", ""))
    return sp.read_via_rpa(ACC, DAY, DAY), shots


# ── снимок ───────────────────────────────────────────────────────────────

def test_a_disabled_read_saves_a_screenshot(local, monkeypatch):
    out, shots = _read(monkeypatch, ("disabled", "Account disabled"))
    assert out.state == "ok" and out.status == "disabled"
    assert len(shots) == 1
    assert out.status_shot and out.status_shot_at
    assert sp.status_shot_path(out.status_shot) == Path(shots[0]).resolve()
    assert Path(shots[0]).parent == local / "spend_shots"
    assert out.shot == "", "снимок неудачи — другое поле, у удачного чтения его нет"


def test_the_status_entry_keeps_the_shot(local, monkeypatch):
    out, _ = _read(monkeypatch, ("restricted", "Verification required to advertise"))
    sp._put(out)
    got = sp.get_status(ACC)
    assert got["status"] == "restricted"
    assert got["shot_id"] == out.status_shot
    assert got["shot_at"] == out.status_shot_at


def test_a_clear_read_takes_no_screenshot(local, monkeypatch):
    out, shots = _read(monkeypatch, ("clear", ""))
    assert out.state == "ok" and shots == []
    assert out.status_shot == ""
    sp._put(out)
    assert sp.get_status(ACC)["shot_id"] == ""


def test_a_clear_read_replaces_the_old_problem_shot_on_the_map(local, monkeypatch):
    bad, _ = _read(monkeypatch, ("disabled", "Account disabled"))
    sp._put(bad)
    good, _ = _read(monkeypatch, ("clear", ""))
    sp._put(good)
    assert sp.get_status(ACC)["shot_id"] == "", "кнопка у «no warning» доказывала бы прошлое"


def test_a_problem_seen_on_a_stuck_page_is_shot_too(local, monkeypatch):
    out, shots = _read(monkeypatch, ("disabled", "Account disabled"), grid=False)
    assert out.state == "failed"
    assert out.status_shot, "у отключённого кабинета таблицы может не быть — снимок тогда и нужен"
    assert out.shot, "снимок неудачи остаётся как был"


def test_a_failed_screenshot_does_not_break_the_read(local, monkeypatch):
    def boom(**_k):
        raise RuntimeError("page closed")
    page = NS(url="https://adsmanager.facebook.com/?act=111", goto=lambda *a, **k: None,
              close=lambda: None, screenshot=boom)
    monkeypatch.setattr(sp, "_entry_for", lambda _: NS(ad_account_id="111"))
    monkeypatch.setattr(sp, "_attach_only", lambda *a: NS(page=page, close=lambda **k: None))
    monkeypatch.setattr(sp, "_wait_for_grid", lambda _: True)
    monkeypatch.setattr(sp, "_read_status", lambda _: ("disabled", "Account disabled"))
    monkeypatch.setattr(sp, "_shown_period", lambda _: DAY)
    monkeypatch.setattr(sp, "_read_total", lambda _: (7.0, "test"))
    monkeypatch.setattr(sp, "_read_tree", lambda *a, **k: ([], "", ""))
    out = sp.read_via_rpa(ACC, DAY, DAY)
    assert out.state == "ok" and out.status == "disabled"
    assert out.status_shot == ""


def test_only_the_last_few_shots_per_account_are_kept(local, monkeypatch):
    clock = iter(range(100))
    monkeypatch.setattr(sp, "_shot_stamp", lambda: f"20261001-1200{next(clock):02d}")
    page = NS(screenshot=lambda path, full_page=False: Path(path).write_bytes(PNG))
    ids = [sp._status_shot(page, ACC)[0] for _ in range(sp.STATUS_SHOTS_KEEP + 3)]
    other = sp._status_shot(page, "FacebookDoc2")[0]
    left = sorted(p.stem for p in (local / "spend_shots").glob("*.png"))
    assert left == sorted(ids[-sp.STATUS_SHOTS_KEEP:] + [other]), \
        "чужой аккаунт с похожим именем не чистится вместе с этим"


# ── отдача снимка: только по id и только из папки ────────────────────────

@pytest.mark.parametrize("bad", [
    "", "..", "../account_spend", "..\\account_spend", "status_x/../../account_spend",
    "account_spend", "status_..", "status_a.png", "C:\\Windows\\win", "status_" + "a" * 300,
])
def test_a_shot_id_cannot_leave_the_folder(local, bad):
    (local / "account_spend.json").write_text("{}", encoding="utf-8")
    assert sp.status_shot_path(bad) is None


def test_an_unknown_shot_id_is_none(local):
    assert sp.status_shot_path("status_Nobody__20261001-120000") is None


class _Client:
    """GET через httpx.ASGITransport — как остальные тесты дешборда."""

    def __init__(self, base_url="http://127.0.0.1"):
        self.base_url = base_url

    def get(self, url):
        import asyncio

        import httpx

        from src.web.app import app

        async def go():
            transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
            async with httpx.AsyncClient(transport=transport, base_url=self.base_url) as c:
                return await c.get(url)

        return asyncio.run(go())


@pytest.fixture
def client(local):
    return _Client()


def test_the_endpoint_serves_a_shot_by_its_id(local, client):
    page = NS(screenshot=lambda path, full_page=False: Path(path).write_bytes(PNG))
    shot_id, _at = sp._status_shot(page, ACC)
    r = client.get(f"/account-spend/status-shot/{shot_id}")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.content == PNG


@pytest.mark.parametrize("bad", [
    "status_Nobody__20261001-120000",
    "..%2Faccount_spend.json",
    "..%5Caccount_spend.json",
    "%2E%2E",
    "status_x%2F..%2F..%2Faccount_spend",
])
def test_the_endpoint_refuses_unknown_ids_and_paths(local, client, bad):
    (local / "account_spend.json").write_text('{"secret": 1}', encoding="utf-8")
    r = client.get(f"/account-spend/status-shot/{bad}")
    assert r.status_code == 404
    assert b"secret" not in r.content


def test_the_fallback_page_shows_the_image_and_how_to_copy_it(local, client):
    page = NS(screenshot=lambda path, full_page=False: Path(path).write_bytes(PNG))
    shot_id, _at = sp._status_shot(page, ACC)
    r = client.get(f"/account-spend/status-shot/{shot_id}/view")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert "Right-click → Copy image" in r.text
    assert f'src="/account-spend/status-shot/{shot_id}"' in r.text
    assert client.get("/account-spend/status-shot/status_Nobody__20261001-120000/view").status_code == 404


def test_the_endpoint_is_local_only(local):
    r = _Client("http://evil.example").get("/account-spend/status-shot/status_x__20261001-120000")
    assert r.status_code == 400


# ── карта ────────────────────────────────────────────────────────────────

def _render(status: dict) -> str:
    from jinja2 import Environment, FileSystemLoader

    from src.analysis_agent.money_format import fmt_currency

    env = Environment(loader=FileSystemLoader(str(ROOT / "src" / "web" / "templates")), autoescape=True)
    env.filters["fmt_currency"] = fmt_currency
    env.filters["fmt_usd"] = str
    country = {"country": "Kuwait", "key": "KW", "virtual": False,
               "rows": [{"account": ACC, "has_data": False, "ready": True,
                         "in_workbook": True, "status": status, "to_do": ""}]}
    html = env.get_template("_account_map_country.html").render(
        mc=country, mlx={},
        amap={"countries": [country], "available": [], "all_accounts": [], "live": {},
              "splits": {}, "fallback": {}, "dates": [], "date_from": "2026-09-01",
              "date_to": "2026-09-28", "span": ""})
    cell = html[html.index('<td class="amap-status">'):]
    return cell[: cell.index("</td>")]


def test_a_problem_status_with_a_shot_has_the_copy_button():
    cell = _render({"status": "disabled", "status_text": "Account disabled", "at": "2026-09-30 10:00:00",
                    "at_short": "30.09.26", "stale": "", "shot_id": "status_Doc20__20260930-100001",
                    "shot_at": "2026-09-30 10:00:01"})
    assert "Copy screenshot" in cell
    assert 'data-shot="status_Doc20__20260930-100001"' in cell
    assert "Screenshot taken 2026-09-30 10:00:01" in cell


def test_no_shot_no_button():
    """Нет снимка — нет рабочей кнопки. С acl-132 у красного статуса кнопка
    остаётся, но выключена и говорит почему: подсказка «Copy screenshot …»
    под ней не должна указывать на кнопку, которой нет (Issue #31)."""
    cell = _render({"status": "disabled", "status_text": "Account disabled", "at": "2026-09-30 10:00:00",
                    "at_short": "30.09.26", "stale": "", "shot_id": "", "shot_at": ""})
    buttons = re.findall(r'<button[^>]*class="amap-shot"[^>]*>', cell)
    assert all(" disabled" in b for b in buttons), "кнопка без снимка нажимается"
    assert all("data-shot" not in b and "onclick" not in b for b in buttons)


def test_a_clear_status_never_has_the_button():
    cell = _render({"status": "clear", "at": "2026-09-30 10:00:00", "at_short": "30.09.26", "stale": "",
                    "shot_id": "status_Doc20__20260930-100001", "shot_at": "2026-09-30 10:00:01"})
    assert "Copy screenshot" not in cell


# ── кнопка в браузере: буфер обмена и запасной путь ─────────────────────

def _copy_js() -> str:
    src = (ROOT / "src" / "web" / "templates" / "report.html").read_text(encoding="utf-8")
    m = re.search(r"/\* ACL-1430 copy-shot \{ \*/(.*?)/\* \} ACL-1430 copy-shot \*/", src, re.S)
    assert m, "блок кнопки снимка в report.html не найден"
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


_SHOT = "status_Doc20__20260930-100001"


def _page(browser, *, clipboard: str):
    """Карта с одной кнопкой; `clipboard`: ok | refuse | missing."""
    ctx = browser.new_context()

    def serve(route):
        url = route.request.url
        if url.endswith("/map"):
            route.fulfill(content_type="text/html", body=(
                '<!doctype html><html><body><td class="amap-status"><span class="amap-st bad">disabled</span>'
                f'<button type="button" class="amap-shot" data-shot="{_SHOT}" '
                'title="Screenshot taken 2026-09-30 10:00:01" onclick="amapCopyShot(this)">Copy screenshot</button>'
                f'</td><script>{_copy_js()}</script></body></html>'))
        elif url.endswith(f"/account-spend/status-shot/{_SHOT}"):
            route.fulfill(content_type="image/png", body=PNG)
        else:
            route.fulfill(status=404, body="")

    ctx.route("http://acl.test/**", serve)
    ctx.route(re.compile(r"^(?!http://acl\.test/).*"), lambda r: r.abort())
    stub = {
        "ok": """
          window.__copied = [];
          window.ClipboardItem = function (items) { this.items = items; this.types = Object.keys(items); };
          Object.defineProperty(navigator, 'clipboard', {value: {write: async function (list) {
            for (const item of list) for (const t of item.types) {
              const blob = await item.items[t];
              window.__copied.push({type: t, blobType: blob.type, size: blob.size});
            }
          }}});""",
        "refuse": """
          window.ClipboardItem = function (items) { this.items = items; this.types = Object.keys(items); };
          Object.defineProperty(navigator, 'clipboard', {value: {write: async function () {
            throw new DOMException('Write permission denied.', 'NotAllowedError');
          }}});""",
        "missing": "delete window.ClipboardItem;",
    }[clipboard]
    ctx.add_init_script("window.__opened = []; window.open = function (u, t) { window.__opened.push([u, t]); return null; };" + stub)
    page = ctx.new_page()
    page.goto("http://acl.test/map")
    return ctx, page


def test_the_button_copies_the_png(browser):
    ctx, page = _page(browser, clipboard="ok")
    try:
        page.click(".amap-shot")
        page.wait_for_function("window.__copied.length > 0")
        copied = page.evaluate("window.__copied")
        assert copied == [{"type": "image/png", "blobType": "image/png", "size": len(PNG)}]
        assert page.evaluate("window.__opened") == []
        assert page.text_content(".amap-shot") == "Copied"
    finally:
        ctx.close()


@pytest.mark.parametrize("clipboard", ["refuse", "missing"])
def test_a_refused_clipboard_opens_the_image_in_a_new_tab(browser, clipboard):
    ctx, page = _page(browser, clipboard=clipboard)
    try:
        page.click(".amap-shot")
        page.wait_for_function("window.__opened.length > 0")
        assert page.evaluate("window.__opened") == [[f"/account-spend/status-shot/{_SHOT}/view", "_blank"]]
    finally:
        ctx.close()
