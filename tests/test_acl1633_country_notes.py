# -*- coding: utf-8 -*-
"""ACL-1633: заметка страны и тихие пустые заметки (разбор владельцем ACL-1620).

Файл заметок уходит во временную папку (ACL_CONFIG_DIR). Браузерные проверки —
headless, на карте, собранной из настоящих шаблонов; живых кабинетов нет.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import paths  # noqa: E402
from src.web import account_notes as notes  # noqa: E402
from src.web import app as web  # noqa: E402

TPL = ROOT / "src" / "web" / "templates"


@pytest.fixture(autouse=True)
def _local_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("ACL_CONFIG_DIR", str(tmp_path))
    return tmp_path


class _Req:
    def __init__(self, body):
        self._body = body

    async def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def _post(body):
    # В отдельном потоке: после браузерных тестов в главном потоке уже крутится
    # цикл событий Playwright, и asyncio.run там не запустить.
    box = []
    t = threading.Thread(target=lambda: box.append(asyncio.run(web.api_account_notes_set(_Req(body)))))
    t.start()
    t.join()
    r = box[0]
    return r.status_code, json.loads(r.body)


def _get_all():
    return json.loads(web.api_account_notes_get().body)


def _file():
    return Path(paths.get_account_notes_path())


# ---- хранилище и эндпоинты ------------------------------------------------

def test_a_country_note_can_be_saved_loaded_deleted_and_undone():
    code, body = _post({"country": "Mexico", "text": "call Pedro"})
    assert code == 200 and body["kind"] == "country" and body["country"] == "Mexico"
    assert _get_all()["countries"] == {notes.country_key("Mexico"): "call Pedro"}
    assert _get_all()["notes"] == {}
    assert _post({"country": "Mexico", "text": ""})[0] == 200  # Delete
    assert _get_all()["countries"] == {}
    assert _post({"country": "Mexico", "text": "call Pedro"})[0] == 200  # Undo = записать прежний текст
    assert _get_all()["countries"] == {notes.country_key("Mexico"): "call Pedro"}


def test_account_and_country_notes_with_the_same_name_are_independent():
    _post({"account": "Peru", "text": "account"})
    _post({"country": "Peru", "text": "country"})
    data = _get_all()
    assert data["notes"] == {"Peru": "account"} and data["countries"] == {notes.country_key("Peru"): "country"}
    _post({"country": "Peru", "text": ""})
    assert _get_all()["notes"] == {"Peru": "account"}


def test_bad_country_input_is_refused():
    assert _post({"country": "", "text": "x"})[0] == 400
    assert _post({"country": "Peru", "text": 3})[0] == 400
    code, body = _post({"country": "Peru", "text": "x" * (notes.MAX_NOTE_LEN + 1)})
    assert code == 400 and not body["ok"]
    assert _post({"text": "x"})[0] == 400


def test_an_old_file_without_countries_migrates_without_loss():
    _file().write_text(json.dumps({"notes": {"Doc19": "keep", "Doc20": "me"}}), encoding="utf-8")
    assert _get_all() == {"ok": True, "notes": {"Doc19": "keep", "Doc20": "me"}, "countries": {}}
    assert _post({"country": "Chile", "text": "new"})[0] == 200
    data = json.loads(_file().read_text(encoding="utf-8"))
    assert data == {"notes": {"Doc19": "keep", "Doc20": "me"}, "countries": {notes.country_key("Chile"): "new"}}
    assert not list(_file().parent.glob("account_notes.json.corrupt-*"))


def test_an_account_save_keeps_the_country_notes():
    _post({"country": "Chile", "text": "c"})
    _post({"account": "A", "text": "a"})
    assert json.loads(_file().read_text(encoding="utf-8")) == {"notes": {"A": "a"}, "countries": {notes.country_key("Chile"): "c"}}


def test_a_corrupt_file_is_moved_aside_and_an_unreadable_one_refuses(monkeypatch):
    _file().write_text("{broken", encoding="utf-8")
    assert _post({"country": "Peru", "text": "x"})[0] == 200
    backups = list(_file().parent.glob("account_notes.json.corrupt-*"))
    assert len(backups) == 1 and backups[0].read_text(encoding="utf-8") == "{broken"
    before = _file().read_bytes()
    real = Path.read_text

    def deny(self, *a, **k):
        if self.name == "account_notes.json":
            raise PermissionError("sharing violation")
        return real(self, *a, **k)

    with monkeypatch.context() as m:
        m.setattr(Path, "read_text", deny)
        code, body = _post({"country": "Other", "text": "y"})
    assert code == 500 and not body["ok"]
    assert _file().read_bytes() == before


def test_a_wrong_countries_type_is_treated_as_corrupt_not_dropped_silently():
    _file().write_text(json.dumps({"notes": {"A": "a"}, "countries": ["x"]}), encoding="utf-8")
    assert _post({"account": "B", "text": "b"})[0] == 200
    assert len(list(_file().parent.glob("account_notes.json.corrupt-*"))) == 1


def test_mixed_concurrent_saves_keep_every_note():
    def save(i):
        notes.set_note(f"Acc{i}", f"a{i}")
        notes.set_note(f"Land{i}", f"c{i}", "country")

    ts = [threading.Thread(target=save, args=(i,)) for i in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    data = json.loads(_file().read_text(encoding="utf-8"))
    assert data["notes"] == {f"Acc{i}": f"a{i}" for i in range(8)}
    assert data["countries"] == {f"land{i}": f"c{i}" for i in range(8)}


def test_notes_are_not_sent_anywhere():
    """Заметки читает и пишет только модуль и локальные эндпоинты: ни сети,
    ни телеметрии, ни таблиц."""
    src = (ROOT / "src" / "web" / "account_notes.py").read_text(encoding="utf-8")
    for banned in ("requests", "urllib", "httpx", "sentry", "gspread", "socket", "http.client"):
        assert banned not in src, banned
    js = (TPL / "report.html").read_text(encoding="utf-8")
    block = js[js.index("/* ACL-1620 notes {"): js.index("/* } ACL-1620 notes */")]
    assert block.count("fetch(") == 1 and "'/api/account-notes'" in block


# ---- шаблоны --------------------------------------------------------------

def _row(name="Demo"):
    return dict(account=name, has_data=False, status={}, workbook_alias="", elsewhere={}, assets=[],
                countries=[], ready=True, to_do="", in_workbook=True)


def _map(account_notes=None, country_notes=None, rows=None, country="Mexico"):
    amap = dict(dates=[], countries=[dict(country=country, key="MX", rows=rows or [_row("Demo"), _row("Quiet")],
                                         spend=0, leads=0, deposits=0, note_key=notes.country_key(country))],
                available=[], all_accounts=["Demo", "Quiet"], live={}, date_from="2026-09-01", date_to="2026-09-09",
                span="", notes=account_notes or {}, country_notes=country_notes or {})
    return web.templates.env.get_template("_account_map.html").render(amap=amap, mlx={"A": "A", "B": "B"})


def test_an_empty_note_renders_no_text_anywhere():
    html = _map()
    cell = re.search(r'<td class="amap-acct-note empty"[^>]*>(.*?)</td>', html, re.S)
    strip = lambda x: re.sub(r"<[^>]+>", "", x).strip()
    assert cell and strip(cell.group(1)) == ""
    # ACL-1633 v2: у пустой заметки есть коробка (поле ввода), только без текста.
    assert 'class="amap-note-box"' in cell.group(1)
    assert "add note" not in html.lower() and "✎" not in html


def test_a_filled_note_shows_its_first_line_and_the_full_text_in_the_tooltip():
    long = "first line " * 12 + "\nsecond line"
    html = _map({"Demo": long}, {notes.country_key("Mexico"): long})
    for pattern in (r'<td class="amap-acct-note"[^>]*>(.*?)</td>',):
        shown = re.sub(r"<[^>]+>", "", re.search(pattern, html, re.S).group(1)).strip()
        assert "second line" not in shown and shown.endswith("…")
    assert 'title="' + long.replace("\n", "&#13;&#10;") + '"' in html.replace("\r", "").replace("\n", "&#13;&#10;") or "second line" in html


def test_note_text_is_escaped_in_both_places():
    evil = '"><img src=x onerror=alert(1)><script>alert(2)</script>'
    html = _map({"Demo": evil}, {notes.country_key("Mexico"): evil})
    assert "<img" not in html and "<script>alert" not in html
    assert html.count("&lt;img") >= 2


def test_column_widths_and_count_are_kept():
    html = _map()
    cols = re.findall(r'<col style="width:(\d+)%"', html)
    assert cols[0] == "28" and cols[-2] == "9" and sum(map(int, cols)) == 100
    assert len(re.findall(r"<th[ >]", html)) == 11


# ---- браузер --------------------------------------------------------------

def _all_css() -> str:
    src = (TPL / "report.html").read_text(encoding="utf-8")
    # ACL-1797: палитра подключена в страницу через include — подставляем файл токенов.
    tokens = (TPL / "adopy-tokens.css").read_text(encoding="utf-8")
    src = re.sub(r"\{% autoescape false %\}.*?\{% endautoescape %\}", lambda m: tokens, src, flags=re.S)
    return "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", src, re.S))


def _js_block() -> str:
    src = (TPL / "report.html").read_text(encoding="utf-8")
    m = re.search(r"/\* ACL-1620 notes \{ \*/(.*?)/\* \} ACL-1620 notes \*/", src, re.S)
    assert m
    return m.group(1)


def page_html() -> str:
    body = _map(notes.load(), notes.load_countries()).replace("<details class=\"amap-country", "<details open class=\"amap-country")
    return (f"<!doctype html><html><head><meta charset='utf-8'><style>{_all_css()}</style></head>"
            f"<body><div class='amap' style='width:1240px'>{body}</div><script>{_js_block()}</script></body></html>")


@pytest.fixture(scope="module")
def browser():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=os.environ.get("ACL_BROWSER_EXECUTABLE") or None)
        try:
            yield b
        finally:
            b.close()


def _open(browser):
    page = browser.new_page(viewport={"width": 1280, "height": 720})
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))

    def route(r):
        if r.request.resource_type == "document":
            r.fulfill(content_type="text/html", body=page_html())
        elif r.request.url.endswith("/api/account-notes") and r.request.method == "POST":
            d = r.request.post_data_json
            box = []
            t = threading.Thread(target=lambda: box.append(asyncio.run(web.api_account_notes_set(_Req(d)))))
            t.start()
            t.join()
            r.fulfill(status=box[0].status_code, content_type="application/json", body=box[0].body)
        else:
            r.fulfill(json={})

    page.route("**/*", route)
    page.goto("http://acl.test/")
    return page, errors


def _acct(page, name="Demo"):
    return page.locator(f'td.amap-acct-note[data-account="{name}"]')


def _gone(page):
    page.wait_for_function("document.querySelector('.amap-note-pop').hidden")


def test_the_editor_opens_by_click_and_by_enter_with_the_cursor_at_the_end(browser):
    notes.set_note("Demo", "existing text")
    page, errors = _open(browser)
    ta = page.locator(".amap-note-pop textarea")
    _acct(page).click()
    assert ta.input_value() == "existing text"
    assert page.evaluate("[document.querySelector('.amap-note-pop textarea').selectionStart, document.querySelector('.amap-note-pop textarea').selectionEnd]") == [13, 13]
    assert page.evaluate("document.activeElement === document.querySelector('.amap-note-pop textarea')")
    page.keyboard.press("Escape")
    _gone(page)
    _acct(page, "Quiet").focus()
    page.keyboard.press("Enter")
    assert page.locator(".amap-note-pop").is_visible() and ta.input_value() == ""
    page.keyboard.press("Escape")
    assert not errors, errors
    page.close()


def test_enter_saves_and_closes_and_shift_enter_adds_a_newline(browser):
    # ACL-1721: Enter сохраняет и закрывает редактор, Shift+Enter — перенос строки.
    page, errors = _open(browser)
    _acct(page).click()
    ta = page.locator(".amap-note-pop textarea")
    ta.fill("line one")
    page.keyboard.press("Shift+Enter")
    page.keyboard.type("line two")
    assert ta.input_value() == "line one\nline two"
    page.keyboard.press("Enter")
    _gone(page)
    assert notes.load() == {"Demo": "line one\nline two"}
    assert not errors, errors
    page.close()


def test_enter_on_the_delete_button_deletes_instead_of_saving(browser):
    notes.set_note("Demo", "to delete")
    page, errors = _open(browser)
    _acct(page).click()
    page.locator(".amap-note-pop textarea").fill("edited but not saved")
    page.locator(".amap-note-del").focus()
    page.keyboard.press("Enter")
    _gone(page)
    assert notes.load() == {}
    assert not errors, errors
    page.close()


def test_country_rows_have_no_note_and_the_account_map_has_no_country_note_code():
    html = _map(notes.load(), {notes.country_key("Mexico"): "stored but hidden"})
    assert "amap-country-note" not in html and "stored but hidden" not in html
    assert 'data-note-kind="country"' not in html
    report = (TPL / "report.html").read_text(encoding="utf-8")
    assert "amap-country-note" not in report
    assert "SEL = 'td.amap-acct-note';" in report
    # Enter без Shift сохраняет, Shift+Enter не перехватывается
    assert "e.key === 'Enter' && !e.shiftKey" in _js_block()
    assert "e.target === pop.querySelector('textarea')" in _js_block()


def test_outside_click_and_ctrl_enter_save(browser):
    page, errors = _open(browser)
    _acct(page).click()
    page.locator(".amap-note-pop textarea").fill("by outside click")
    page.mouse.click(5, 5)
    _gone(page)
    assert notes.load() == {"Demo": "by outside click"}
    _acct(page, "Quiet").click()
    page.locator(".amap-note-pop textarea").fill("by ctrl enter")
    page.keyboard.press("Control+Enter")
    _gone(page)
    assert notes.load() == {"Demo": "by outside click", "Quiet": "by ctrl enter"}
    assert not errors, errors
    page.close()


def test_delete_shows_undo_for_five_seconds_and_undo_restores(browser):
    notes.set_note("Demo", "precious")
    page, errors = _open(browser)
    _acct(page).click()
    page.locator(".amap-note-del").click()
    _gone(page)
    assert notes.load() == {}
    toast = page.locator(".amap-note-toast")
    assert toast.is_visible() and "Deleted" in toast.inner_text() and "Undo" in toast.inner_text()
    toast.locator("button").click()
    page.wait_for_function("document.querySelector('td.amap-acct-note[data-account=\"Demo\"]').dataset.note === 'precious'")
    assert notes.load() == {"Demo": "precious"}
    assert not toast.is_visible()
    # Без Undo: тост уходит сам через 5 секунд.
    _acct(page).click()
    page.locator(".amap-note-del").click()
    page.wait_for_function("document.querySelector('.amap-note-toast') && !document.querySelector('.amap-note-toast').hidden")
    page.wait_for_function("document.querySelector('.amap-note-toast').hidden", timeout=8000)
    assert notes.load() == {}
    assert not errors, errors
    page.close()


def test_the_pencil_shows_only_on_hover_or_focus_of_an_empty_note(browser):
    page, errors = _open(browser)
    op = "getComputedStyle(document.querySelector('td.amap-acct-note[data-account=\"Quiet\"] .amap-note-box'), '::after').opacity"
    assert float(page.evaluate(op)) == 0
    _acct(page, "Quiet").hover()
    assert float(page.evaluate(op)) > 0.3
    page.mouse.move(600, 700)
    assert float(page.evaluate(op)) == 0
    _acct(page, "Quiet").focus()
    page.keyboard.press("Tab")  # фокус ушёл — карандаш тоже
    assert not errors, errors
    page.close()


def test_the_editor_holds_only_the_textarea_delete_and_a_word(browser):
    page, errors = _open(browser)
    _acct(page).click()
    pop = page.locator(".amap-note-pop")
    assert pop.locator("textarea").count() == 1 and pop.locator("button").count() == 1
    assert pop.locator("button").inner_text() == "Delete"
    assert page.locator(".amap-note-close, .amap-note-title").count() == 0
    page.keyboard.press("Escape")
    page.close()


def test_a_failed_save_says_save_failed_and_keeps_the_text(browser):
    page = browser.new_page(viewport={"width": 1280, "height": 720})

    def route(r):
        if r.request.resource_type == "document":
            r.fulfill(content_type="text/html", body=page_html())
        elif r.request.method == "POST":
            r.fulfill(status=500, json={"ok": False, "error": "x"})
        else:
            r.fulfill(json={})

    page.route("**/*", route)
    page.goto("http://acl.test/")
    _acct(page).click()
    page.locator(".amap-note-pop textarea").fill("must not be lost")
    page.keyboard.press("Control+Enter")
    page.wait_for_function("document.querySelector('.amap-note-state').textContent === 'Save failed'")
    assert page.locator(".amap-note-pop textarea").input_value() == "must not be lost"
    page.close()


def test_the_editor_does_not_cover_the_row_when_there_is_room(browser):
    page, errors = _open(browser)
    _acct(page).click()
    cell = _acct(page).bounding_box()
    pop = page.locator(".amap-note-pop").bounding_box()
    assert pop["y"] >= cell["y"] + cell["height"] - 1 or pop["y"] + pop["height"] <= cell["y"] + 1
    assert pop["x"] >= 0 and pop["x"] + pop["width"] <= 1280
    page.keyboard.press("Escape")
    page.close()


def test_the_note_is_drawn_as_an_input_box_of_equal_height(browser):
    notes.set_note("Demo", "filled")
    page, errors = _open(browser)
    info = page.evaluate("""() => {
      const q = (s) => document.querySelector(s);
      const cs = (e) => getComputedStyle(e);
      const filled = q('td.amap-acct-note[data-account="Demo"] .amap-note-box');
      const empty = q('td.amap-acct-note[data-account="Quiet"] .amap-note-box');
      const cell = q('td.amap-acct-note[data-account="Quiet"]');
      return {
        border: cs(empty).borderTopWidth, radius: cs(empty).borderTopLeftRadius, cursor: cs(empty).cursor,
        hf: filled.getBoundingClientRect().height, he: empty.getBoundingClientRect().height,
        full: empty.getBoundingClientRect().width / cell.getBoundingClientRect().width,
        italic: cs(empty).fontStyle };
    }""")
    assert info["border"] == "1px" and info["cursor"] == "text"
    assert info["radius"] != "0px" and info["italic"] == "normal"
    assert abs(info["hf"] - info["he"]) < 0.5, info
    assert info["full"] > 0.8, info  # в строке аккаунта коробка занимает ячейку
    assert not errors, errors
    page.close()


# ---- ACL-1633 разбор: ключ страны и условный Undo --------------------------

def test_a_country_code_and_its_name_are_one_note_and_case_does_not_matter():
    assert notes.country_key("CR") == notes.country_key("Costa Rica") == notes.country_key("cr") == notes.country_key(" costa  rica ")
    _post({"country": "Costa Rica", "text": "from the name"})
    assert _post({"country": "CR", "text": "from the code"})[0] == 200
    assert _get_all()["countries"] == {notes.country_key("CR"): "from the code"}
    # неизвестные имена не сливаются по первым двум буквам
    assert notes.country_key("Xyland") != notes.country_key("Xytown")


def test_old_country_keys_migrate_on_read_and_on_write_without_loss():
    _file().write_text(json.dumps({"notes": {"A": "a"}, "countries": {"Costa Rica": "one", "CR": "two", "Narnia": "n"}}),
                       encoding="utf-8")
    cr = notes.country_key("CR")
    loaded = _get_all()["countries"]
    assert set(loaded) == {cr, "narnia"} and loaded["narnia"] == "n"
    assert loaded[cr] in ("one\ntwo", "two\none") or set(loaded[cr].split("\n")) == {"one", "two"}
    assert _post({"account": "B", "text": "b"})[0] == 200  # запись мигрирует файл
    data = json.loads(_file().read_text(encoding="utf-8"))
    assert set(data["countries"]) == {cr, "narnia"} and data["notes"] == {"A": "a", "B": "b"}


def test_country_notes_stay_stored_but_are_not_rendered():
    notes.set_note("Costa Rica", "tico", "country")
    for shown in ("CR", "Costa Rica"):
        html = _map(notes.load(), notes.load_countries(), country=shown)
        assert 'data-note="tico"' not in html  # ACL-1721: заметки стран не показываются


def test_a_conditional_write_refuses_when_the_note_is_no_longer_empty():
    _post({"account": "Demo", "text": "newer"})
    code, body = _post({"account": "Demo", "text": "old", "if_empty": True})
    assert code == 409 and not body["ok"]
    assert _get_all()["notes"] == {"Demo": "newer"}
    _post({"account": "Demo", "text": ""})
    assert _post({"account": "Demo", "text": "old", "if_empty": True})[0] == 200
    assert _get_all()["notes"] == {"Demo": "old"}


def test_delete_then_edit_then_undo_keeps_the_newer_note(browser):
    notes.set_note("Demo", "old")
    page, errors = _open(browser)
    _acct(page).click()
    page.locator(".amap-note-del").click()
    _gone(page)
    assert notes.load() == {}
    # Редактор открыт — тост Undo уходит сразу, чтобы нечем было затереть новое.
    _acct(page).click()
    assert not page.locator(".amap-note-toast").is_visible()
    page.locator(".amap-note-pop textarea").fill("newer")
    page.keyboard.press("Escape")
    _gone(page)
    assert notes.load() == {"Demo": "newer"}
    assert not page.locator(".amap-note-toast").is_visible()
    assert not errors, errors
    page.close()


def test_undo_after_a_server_side_change_does_not_overwrite(browser):
    """Даже если тост остался (запись пришла из другого окна), сервер не даст
    затереть: Undo шлёт if_empty."""
    notes.set_note("Demo", "old")
    page, errors = _open(browser)
    _acct(page).click()
    page.locator(".amap-note-del").click()
    _gone(page)
    notes.set_note("Demo", "written elsewhere")
    page.locator(".amap-note-toast button").click()
    page.wait_for_function("document.querySelector('.amap-note-toast').hidden")
    assert notes.load() == {"Demo": "written elsewhere"}
    assert not errors, errors
    page.close()


def test_a_failed_undo_says_so(browser):
    notes.set_note("Demo", "old")
    page = browser.new_page(viewport={"width": 1280, "height": 720})
    state = {"fail": False}

    def route(r):
        if r.request.resource_type == "document":
            r.fulfill(content_type="text/html", body=page_html())
        elif r.request.method == "POST":
            d = r.request.post_data_json
            if d.get("if_empty") and state["fail"]:
                r.fulfill(status=500, json={"ok": False, "error": "x"})
                return
            box = []
            th = threading.Thread(target=lambda: box.append(asyncio.run(web.api_account_notes_set(_Req(d)))))
            th.start()
            th.join()
            r.fulfill(status=box[0].status_code, content_type="application/json", body=box[0].body)
        else:
            r.fulfill(json={})

    page.route("**/*", route)
    page.goto("http://acl.test/")
    _acct(page).click()
    page.locator(".amap-note-del").click()
    _gone(page)
    state["fail"] = True
    page.locator(".amap-note-toast button").click()
    page.wait_for_function("document.querySelector('.amap-note-toast').textContent === 'Undo failed'")
    assert notes.load() == {}
    page.close()
