# -*- coding: utf-8 -*-
"""ACL-1620: заметка менеджера к аккаунту вместо колонки Setup.

Эндпоинты зовём напрямую (TestClient в этой сборке не используем), файл
заметок уходит во временную папку через ACL_CONFIG_DIR — настоящая машинная
папка не трогается. Браузерная часть — headless, без живых кабинетов.
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
    r = asyncio.run(web.api_account_notes_set(_Req(body)))
    return r.status_code, json.loads(r.body)


def _get():
    return json.loads(web.api_account_notes_get().body)["notes"]


def test_save_load_overwrite_and_empty_deletes():
    assert _get() == {}
    assert _post({"account": "Doc19", "text": "first"})[0] == 200
    assert _get() == {"Doc19": "first"}
    _post({"account": "Doc19", "text": "second\nline"})
    assert _get() == {"Doc19": "second\nline"}
    _post({"account": "Doc20", "text": "other"})
    assert _post({"account": "Doc19", "text": "   "})[0] == 200
    assert _get() == {"Doc20": "other"}


def test_length_cap_and_bad_input():
    assert _post({"account": "A", "text": "x" * notes.MAX_NOTE_LEN})[0] == 200
    code, body = _post({"account": "A", "text": "x" * (notes.MAX_NOTE_LEN + 1)})
    assert code == 400 and not body["ok"]
    assert len(_get()["A"]) == notes.MAX_NOTE_LEN
    assert _post({"account": "", "text": "x"})[0] == 400
    assert _post({"text": "x"})[0] == 400
    assert _post({"account": "A", "text": 5})[0] == 400
    assert _post([1])[0] == 400
    assert _post(ValueError("not json"))[0] == 400


def test_file_lives_in_the_machine_folder_not_in_the_repo(_local_dir):
    _post({"account": "A", "text": "x"})
    path = Path(paths.get_account_notes_path())
    assert path.parent == _local_dir and path.name == "account_notes.json"
    assert ROOT not in path.parents
    assert not (ROOT / "config" / "account_notes.json").exists()


def test_a_failed_write_leaves_the_previous_file_intact(monkeypatch):
    _post({"account": "A", "text": "keep me"})
    path = Path(paths.get_account_notes_path())
    before = path.read_bytes()

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(paths, "_replace_when_free", boom)
    code, body = _post({"account": "A", "text": "new"})
    assert code == 500 and not body["ok"]
    assert path.read_bytes() == before
    assert [p for p in path.parent.iterdir() if p.name.endswith(".tmp")] == []


def test_a_broken_file_renders_empty_but_a_save_keeps_the_old_content():
    path = Path(paths.get_account_notes_path())
    path.write_text("{not json", encoding="utf-8")
    assert _get() == {}  # отрисовка не падает
    assert _post({"account": "A", "text": "x"})[0] == 200
    assert _get() == {"A": "x"}
    backups = list(path.parent.glob("account_notes.json.corrupt-*"))
    assert len(backups) == 1 and backups[0].read_text(encoding="utf-8") == "{not json"


def test_a_wrong_root_is_preserved_too():
    path = Path(paths.get_account_notes_path())
    path.write_text('["a"]', encoding="utf-8")
    assert _post({"account": "A", "text": "x"})[0] == 200
    assert [p.read_text(encoding="utf-8") for p in path.parent.glob("account_notes.json.corrupt-*")] == ['["a"]']


def test_an_unreadable_file_refuses_the_save_and_loses_nothing(monkeypatch):
    _post({"account": "Keep", "text": "precious"})
    path = Path(paths.get_account_notes_path())
    before = path.read_bytes()
    real = Path.read_text

    def deny(self, *a, **k):
        if self.name == "account_notes.json":
            raise PermissionError("sharing violation")
        return real(self, *a, **k)

    with monkeypatch.context() as m:  # откат только подмены чтения, не ACL_CONFIG_DIR
        m.setattr(Path, "read_text", deny)
        code, body = _post({"account": "Other", "text": "new"})
    assert code == 500 and not body["ok"]
    assert path.read_bytes() == before
    assert _get() == {"Keep": "precious"}


def test_the_label_is_stripped_on_both_sides():
    _post({"account": "  Padded  ", "text": "n"})
    html = _render([_row("  Padded  ")], notes.load())
    assert 'data-note="n"' in html and 'data-account="Padded"' in html


def test_concurrent_saves_keep_valid_json_and_every_note():
    errs = []

    def save(i):
        try:
            notes.set_note(f"Acc{i}", f"text {i}")
        except Exception as exc:  # pragma: no cover
            errs.append(exc)

    ts = [threading.Thread(target=save, args=(i,)) for i in range(12)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errs
    data = json.loads(Path(paths.get_account_notes_path()).read_text(encoding="utf-8"))
    assert data["notes"] == {f"Acc{i}": f"text {i}" for i in range(12)}


def _row(name="Demo"):
    return dict(account=name, ready=True, to_do="Open recommendations", in_workbook=True, has_data=True,
                spend=12, leads=2, deposits=1, cpl=6, cpa=12, cr=50, status={})


def _render(rows, note_map):
    return web.templates.env.get_template("_account_map_country.html").render(
        mc={"country": "Mexico", "key": "MX", "rows": rows},
        amap={"live": {}, "span": "day", "date_from": "2026-09-09", "all_accounts": [], "notes": note_map},
        mlx={"A": "Demo A", "B": "Demo B"})


def test_template_shows_truncated_note_with_full_text_in_title_and_no_setup():
    long = "word " * 40 + "END"
    html = _render([_row("Demo"), _row("Empty")], {"Demo": long})
    cell = re.search(r'<td class="amap-acct-note"[^>]*>(.*?)</td>', html, re.S)
    assert "END" not in cell.group(1) and "…" in cell.group(1)
    assert f'title="{long}"' in html and f'data-note="{long}"' in html
    # ACL-1633: пустая ячейка не пишет ничего (карандаш рисует CSS при наведении).
    empty = re.search(r'<td class="amap-acct-note empty"[^>]*>(.*?)</td>', html, re.S)
    assert empty and re.sub(r"<[^>]+>", "", empty.group(1)).strip() == ""
    assert 'class="amap-note-box"' in empty.group(1)  # коробка есть и у пустой
    assert "add note" not in html.lower()
    assert "Setup" not in html and "amap-setup" not in html and "needs setup" not in html
    assert ">Note<" in html
    assert len(re.findall(r"<th[ >]", html)) == len(re.findall(r"<td[ >]", html)) // 2 == 11


def test_html_in_a_note_is_escaped_in_the_template():
    evil = '"><img src=x onerror=alert(1)><script>alert(2)</script>'
    html = _render([_row("Demo")], {"Demo": evil})
    assert "<img" not in html and "<script>alert" not in html
    assert "&lt;img" in html and "&#34;&gt;" in html


def test_the_map_view_carries_the_notes():
    src = (ROOT / "src" / "web" / "app.py").read_text(encoding="utf-8")
    assert '"notes": _account_notes.load()' in src


# ---- браузер --------------------------------------------------------------

def _js_block() -> str:
    src = (TPL / "report.html").read_text(encoding="utf-8")
    m = re.search(r"/\* ACL-1620 notes \{ \*/(.*?)/\* \} ACL-1620 notes \*/", src, re.S)
    assert m, "блок заметок в report.html не найден"
    return m.group(1)


def _css() -> str:
    src = (TPL / "report.html").read_text(encoding="utf-8")
    m = re.search(r"(\.amap table\.amap-grid td\.amap-acct-note \{.*?\.amap-note-toast \{[^}]*\})", src, re.S)
    from _design_tokens import tokens_css  # ACL-1797: палитра — отдельный файл
    return tokens_css() + (m.group(1) if m else "")


def _page_html() -> str:
    table = _render([_row("Demo"), _row("Evil")], notes.load())
    return (f"<!doctype html><html><head><style>{_css()}</style></head><body><div class='amap'>{table}</div>"
            f"<script>{_js_block()}</script></body></html>")


@pytest.fixture(scope="module")
def browser():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=os.environ.get("ACL_BROWSER_EXECUTABLE") or None)
        try:
            yield b
        finally:
            b.close()


def _open_page(browser, fail_posts=False, hold=None):
    page = browser.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))

    def route(r):
        url = r.request.url
        if r.request.resource_type == "document":
            r.fulfill(content_type="text/html", body=_page_html())
        elif url.endswith("/api/account-notes") and r.request.method == "POST":
            if fail_posts:
                r.fulfill(status=500, json={"ok": False, "error": "x"})
            else:
                d = r.request.post_data_json
                # В обработчике Playwright уже крутится цикл событий: asyncio.run
                # там не запустить, поэтому — в отдельном потоке.
                box = []
                t = threading.Thread(target=lambda: box.append(asyncio.run(web.api_account_notes_set(_Req(d)))))
                t.start()
                t.join()
                resp = box[0]
                if hold is not None:  # ответ придерживаем: сохранение «в пути»
                    hold.append((r, resp))
                    return
                r.fulfill(status=resp.status_code, content_type="application/json", body=resp.body)
        else:
            r.fulfill(json={})

    page.route("**/*", route)
    page.goto("http://acl.test/")
    return page, errors


def _cell(page, name):
    return page.locator(f'td.amap-acct-note[data-account="{name}"]')


def _hidden(page):
    page.wait_for_function("document.querySelector('.amap-note-pop').hidden")


def test_click_type_escape_reload_keeps_the_text(browser):
    page, errors = _open_page(browser)
    _cell(page, "Demo").click()
    assert page.locator(".amap-note-pop").is_visible()
    page.locator(".amap-note-pop textarea").fill("call the client\nsecond line")
    page.keyboard.press("Escape")
    _hidden(page)
    assert "call the client" in _cell(page, "Demo").inner_text()
    assert _get() == {"Demo": "call the client\nsecond line"}
    page.reload()
    assert "call the client" in _cell(page, "Demo").inner_text()
    assert _cell(page, "Demo").get_attribute("title") == "call the client\nsecond line"
    assert not errors, errors
    page.close()


def test_outside_click_and_close_button_save_and_empty_deletes(browser):
    page, errors = _open_page(browser)
    _cell(page, "Demo").click()
    page.locator(".amap-note-pop textarea").fill("by outside click")
    page.mouse.click(5, 5)
    _hidden(page)
    assert _get() == {"Demo": "by outside click"}
    _cell(page, "Demo").click()
    page.locator(".amap-note-pop textarea").fill("")
    page.keyboard.press("Control+Enter")
    _hidden(page)
    assert _get() == {}
    assert _cell(page, "Demo").inner_text().strip() == ""
    _cell(page, "Evil").click()
    page.locator(".amap-note-pop textarea").fill("by close")
    page.keyboard.press("Control+Enter")
    _hidden(page)
    assert _get() == {"Evil": "by close"}
    assert not errors, errors
    page.close()


def test_html_note_is_inert_and_cells_added_later_work(browser):
    page, errors = _open_page(browser)
    page.evaluate("window.__x = 0")
    evil = '<img src=x onerror="window.__x=1"><b>bold</b>'
    _cell(page, "Evil").click()
    page.locator(".amap-note-pop textarea").fill(evil)
    page.keyboard.press("Control+Enter")
    _hidden(page)
    assert page.locator("td.amap-acct-note img, td.amap-acct-note b").count() == 0
    assert _cell(page, "Evil").inner_text().startswith("<img")
    page.reload()
    page.wait_for_timeout(200)  # onerror, если бы разметка ожила, сработал бы здесь
    assert not page.evaluate("window.__x") and page.locator("td.amap-acct-note img").count() == 0
    # Ячейка, появившаяся после загрузки (как строки дерева из htmx), открывается тем же делегатом.
    page.evaluate("""() => { const t = document.querySelector('td.amap-acct-note').cloneNode(true);
        t.setAttribute('data-account', 'Late'); t.setAttribute('data-note', ''); document.querySelector('tbody tr').appendChild(t); }""")
    _cell(page, "Late").click()
    assert page.locator(".amap-note-pop").is_visible()
    page.keyboard.press("Escape")
    assert not errors, errors
    page.close()


def test_a_failed_save_keeps_the_editor_open_with_the_text(browser):
    page, errors = _open_page(browser, fail_posts=True)
    _cell(page, "Demo").click()
    page.locator(".amap-note-pop textarea").fill("must not be lost")
    page.keyboard.press("Control+Enter")
    page.wait_for_function("document.querySelector('.amap-note-state').textContent.includes('Save failed')")
    assert page.locator(".amap-note-pop").is_visible()
    assert page.locator(".amap-note-pop textarea").input_value() == "must not be lost"
    page.close()


def test_reopening_right_after_close_shows_the_new_text(browser):
    held = []
    page, errors = _open_page(browser, hold=held)  # ответ сервера придержан
    _cell(page, "Demo").click()
    page.locator(".amap-note-pop textarea").fill("fresh text")
    page.keyboard.press("Control+Enter")
    page.wait_for_timeout(300)
    assert held, "сохранение не ушло"
    # Сохранение в пути: открытие игнорируется, окно не перерисовывается старым текстом.
    page.evaluate("document.querySelector('td.amap-acct-note[data-account=\"Demo\"]').click()")
    assert page.locator(".amap-note-pop textarea").input_value() == "fresh text"
    for route, resp in held:
        route.fulfill(status=resp.status_code, content_type="application/json", body=resp.body)
    _hidden(page)
    page.wait_for_function("document.querySelector('td.amap-acct-note[data-account=\"Demo\"]').dataset.note === 'fresh text'")
    _cell(page, "Demo").click()
    assert page.locator(".amap-note-pop textarea").input_value() == "fresh text"
    assert _get() == {"Demo": "fresh text"}
    assert not errors, errors
    page.close()


def test_a_damaged_file_is_reported_in_the_save_response_and_logged(caplog):
    path = Path(paths.get_account_notes_path())
    path.write_text("{not json", encoding="utf-8")
    with caplog.at_level("WARNING"):
        code, body = _post({"account": "A", "text": "x"})
    backup = next(path.parent.glob("account_notes.json.corrupt-*"))
    assert code == 200 and body["set_aside"] == str(backup)
    assert body["warning"] == f"Notes file was damaged; a copy was kept at {backup}"
    assert any("damaged" in r.getMessage() and str(backup) in r.getMessage() for r in caplog.records)
    # Healthy file afterwards: no warning.
    assert "set_aside" not in _post({"account": "B", "text": "y"})[1]


def test_two_corruptions_in_the_same_millisecond_keep_both_copies(monkeypatch):
    class _Fixed:
        @staticmethod
        def now():
            import datetime as _dt
            return _dt.datetime(2026, 1, 2, 3, 4, 5, 678000)

    monkeypatch.setattr(notes, "datetime", _Fixed)
    path = Path(paths.get_account_notes_path())
    for bad in ("{one", "{two"):
        path.write_text(bad, encoding="utf-8")
        assert _post({"account": "A", "text": "x"})[0] == 200
    names = sorted(p.name for p in path.parent.glob("account_notes.json.corrupt-*"))
    assert names == ["account_notes.json.corrupt-20260102-030405-678",
                     "account_notes.json.corrupt-20260102-030405-678-2"]
    assert sorted(p.read_text(encoding="utf-8") for p in path.parent.glob("account_notes.json.corrupt-*")) == ["{one", "{two"]


def test_the_backup_name_has_millisecond_resolution():
    path = Path(paths.get_account_notes_path())
    path.write_text("{bad", encoding="utf-8")
    _post({"account": "A", "text": "x"})
    name = next(path.parent.glob("account_notes.json.corrupt-*")).name
    assert re.fullmatch(r"account_notes\.json\.corrupt-\d{8}-\d{6}-\d{3}", name)


def test_the_page_shows_the_set_aside_warning():
    src = (TPL / "report.html").read_text(encoding="utf-8")
    assert "notifyAside(res.j.warning)" in src and "amap-note-aside" in src
