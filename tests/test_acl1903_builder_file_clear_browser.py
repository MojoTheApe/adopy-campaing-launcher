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
    {"filename": "7_post.png", "rel_path": "a/7_post.png", "folder": "a", "type": "Image", "size_bytes": 1000,
     "vertical_partner": "a/7_story.png"},
    {"filename": "7_story.png", "rel_path": "a/7_story.png", "folder": "a", "type": "Image", "size_bytes": 1000},
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


def _form_pairs(p, row):
    """Всё, что уходит на сервер из этой строки: по ним читает план и публикация."""
    return row.evaluate(
        r"""r => { const out = {}; new FormData(r.closest('form')).forEach((v, k) => {
                    if (/creative_file/.test(k)) out[k.replace(/^camp_\d+_ad_\d+_/, '')] = v; }); return out; }""")


def test_form_carries_no_file_after_both_boxes_are_cleared(page):
    _open_first(page, "ad")
    _backend(page, "api")
    row = _row(page)
    _pick(page, row.locator(".cb-creative-search"), "main_1x1.png")
    _pick(page, row.locator(".cb-vertical-search"), "story_9x16.png")
    assert _form_pairs(page, row) == {"creative_file": "main_1x1.png", "creative_file_vertical": "story_9x16.png"}
    row.locator(".cb-creative-search").fill("")
    row.locator(".cb-vertical-search").fill("")
    pairs = _form_pairs(page, row)
    assert pairs == {"creative_file": "", "creative_file_vertical": ""}
    # скрытое и видимое пусты вместе
    assert row.locator(".cb-creative-search").input_value() == ""
    assert row.locator(".cb-vertical-search").input_value() == ""


def test_prefill_and_duplicate_do_not_resurrect_a_cleared_vertical_file(page):
    _open_first(page, "ad")
    _backend(page, "api")
    row = _row(page)
    _pick(page, row.locator(".cb-creative-search"), "7_post.png")
    # ACL-1774: пара _story подставилась в Stories / Reels
    assert _hidden(row, "_creative_file_vertical").input_value() == "7_story.png"
    row.locator(".cb-vertical-search").fill("")
    assert _hidden(row, "_creative_file_vertical").input_value() == ""
    # фокус, закрытие списка и правка основного поля без нового выбора пару не возвращают
    row.locator(".cb-vertical-search").focus()
    page.locator("body").click(position={"x": 2, "y": 2})
    row.locator(".cb-creative-search").fill("")
    assert _hidden(row, "_creative_file").input_value() == ""
    assert _hidden(row, "_creative_file_vertical").input_value() == ""
    # дубль очищенной строки остаётся чистым
    row.locator(".cb-dup-ad-btn").click()
    page.wait_for_timeout(150)
    copy = _slot(page).locator(".cb-ad-row").nth(1)
    assert _hidden(copy, "_creative_file").input_value() == ""
    assert _hidden(copy, "_creative_file_vertical").input_value() == ""
    assert copy.locator(".cb-creative-search").input_value() == ""
    assert copy.locator(".cb-vertical-search").input_value() == ""
    # новый выбор основного файла по-прежнему подставляет пару (решение ACL-1774)
    _pick(page, row.locator(".cb-creative-search"), "7_post.png")
    assert _hidden(row, "_creative_file").input_value() == "7_post.png"
    assert _hidden(row, "_creative_file_vertical").input_value() == "7_story.png"


def test_text_put_by_code_that_matches_nothing_does_not_keep_the_old_file(page):
    """Заполнение с другого экрана ставит текст кодом; прежний выбор не должен остаться в скрытом поле."""
    _open_first(page, "ad")
    _backend(page, "api")
    row = _row(page)
    _pick(page, row.locator(".cb-creative-search"), "main_1x1.png")
    row.evaluate("""r => { const b = r.querySelector('.cb-creative-search'); b.value = 'no-such-pack';
                           cbCreativeTyped(b); }""")
    assert _hidden(row, "_creative_file").input_value() == ""
    assert _form_pairs(page, row)["creative_file"] == ""


@pytest.mark.parametrize("sel,suffix,name", [
    (".cb-vertical-search", "_creative_file_vertical", "story_9x16.png"),
    (".cb-creative-search", "_creative_file", "main_1x1.png"),
])
def test_undo_after_clearing_restores_the_pick_with_the_text(page, sel, suffix, name):
    """Ctrl+Z возвращает имя в окно; выбор возвращается вместе с ним, окно не врёт."""
    _open_first(page, "ad")
    _backend(page, "api")
    row = _row(page)
    box = row.locator(sel)
    _pick(page, box, name)
    box.focus()
    box.press("Control+a")
    box.press("Delete")
    assert _hidden(row, suffix).input_value() == ""
    box.press("Control+z")
    assert box.input_value() == name
    assert _hidden(row, suffix).input_value() == name
