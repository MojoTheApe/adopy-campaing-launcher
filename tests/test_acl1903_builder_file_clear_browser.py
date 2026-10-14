"""ACL-1903: стёртое поле файла (основной или Stories / Reels) не оставляет старый файл в плане (офлайн-Chromium)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_builder_duplicate_buttons import _open_first, _slot, browser  # noqa: E402,F401
from test_builder_per_account import _render  # noqa: E402
from test_acl1774_builder_browser import _backend  # noqa: E402

FILES = [
    {"filename": "main_1x1.png", "rel_path": "a/main_1x1.png", "folder": "a", "type": "Image", "size_bytes": 1000},
    {"filename": "story_9x16.png", "rel_path": "a/story_9x16.png", "folder": "a", "type": "Image", "size_bytes": 1000},
    {"filename": "other_9x16.png", "rel_path": "a/other_9x16.png", "folder": "a", "type": "Image", "size_bytes": 1000},
]


@pytest.fixture
def page(browser):
    ctx = browser.new_context()
    p = ctx.new_page()
    errors: list[str] = []
    p.on("pageerror", lambda e: errors.append(str(e)))
    html = _render({"Demo": [], "Other": []})

    def route(r):
        if r.request.resource_type == "document":
            r.fulfill(content_type="text/html", body=html); return
        r.fulfill(json={"files": FILES, "texts": [], "rows": [], "profiles": [], "waiting": False})

    p.route("**/*", route)
    p.goto("http://acl.test/")
    p.evaluate("cbDrawer.open()")
    p.wait_for_timeout(200)
    yield p
    assert not errors, errors
    ctx.close()


def _row(p):
    return _slot(p).locator(".cb-ad-row").first


def _pick(p, box, name):
    """Живой выбор: печатаем в окно поиска и жмём на строку списка."""
    box.click()
    box.fill("")
    box.type(name[:5])
    opt = box.locator("xpath=..").locator(f".cb-sel-opt[data-val='{name}']")
    opt.dispatch_event("mousedown")


def _hidden(row, suffix):
    return row.locator(f"input[type='hidden'][name$='{suffix}']")


def _form_value(p, row, suffix):
    return row.evaluate(
        """(r, s) => { const f = r.closest('form'); const el = r.querySelector('input[type="hidden"][name$="' + s + '"]');
                       return new FormData(f).get(el.name); }""", suffix)


@pytest.mark.parametrize("sel,suffix,name,other", [
    (".cb-vertical-search", "_creative_file_vertical", "story_9x16.png", "other_9x16.png"),
    (".cb-creative-search", "_creative_file", "main_1x1.png", "story_9x16.png"),
])
def test_clearing_the_box_clears_the_file(page, sel, suffix, name, other):
    _open_first(page, "ad")
    _backend(page, "api")
    row = _row(page)
    box = row.locator(sel)
    _pick(page, box, name)
    assert _hidden(row, suffix).input_value() == name
    assert box.input_value() == name
    # стёрли текст, как это сделал владелец
    box.fill("")
    assert _hidden(row, suffix).input_value() == ""
    assert _form_value(page, row, suffix) == ""
    # снова выбор работает
    _pick(page, box, other)
    assert _hidden(row, suffix).input_value() == other
    # правка текста до другого значения тоже не оставляет старый файл
    box.fill("zzz")
    assert _hidden(row, suffix).input_value() == ""


def test_clearing_one_box_keeps_the_other_and_duplicate_copies_a_real_pick(page):
    _open_first(page, "ad")
    _backend(page, "api")
    row = _row(page)
    _pick(page, row.locator(".cb-creative-search"), "main_1x1.png")
    _pick(page, row.locator(".cb-vertical-search"), "story_9x16.png")
    row.locator(".cb-vertical-search").fill("")
    assert _hidden(row, "_creative_file").input_value() == "main_1x1.png"
    assert _hidden(row, "_creative_file_vertical").input_value() == ""
    _pick(page, row.locator(".cb-vertical-search"), "story_9x16.png")
    row.locator(".cb-dup-ad-btn").click()
    page.wait_for_timeout(150)
    copy = _slot(page).locator(".cb-ad-row").nth(1)
    assert _hidden(copy, "_creative_file").input_value() == "main_1x1.png"
    assert _hidden(copy, "_creative_file_vertical").input_value() == "story_9x16.png"


def test_a_prefilled_pair_survives_focus_without_typing(page):
    """Send-to-Builder кладёт значение без событий ввода; фокус и закрытие списка его не стирают."""
    _open_first(page, "ad")
    _backend(page, "api")
    row = _row(page)
    row.evaluate("""r => { r.querySelector('.cb-vertical-search').value = 'story_9x16.png';
                           r.querySelector('input[name$="_creative_file_vertical"]').value = 'story_9x16.png'; }""")
    row.locator(".cb-vertical-search").focus()
    page.locator("body").click(position={"x": 2, "y": 2})
    assert _hidden(row, "_creative_file_vertical").input_value() == "story_9x16.png"
