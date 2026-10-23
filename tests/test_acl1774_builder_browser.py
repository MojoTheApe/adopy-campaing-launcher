"""ACL-1774: тексты 3-5 и файл Stories / Reels в настоящем ящике билдера (офлайн-Chromium)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_builder_duplicate_buttons import _open_first, _slot, browser, page  # noqa: E402,F401


def _row(p):
    return _slot(p).locator(".cb-ad-row").first


def _backend(p, value):
    """Бэкенд страницы задаётся при рендере; здесь меняем и переоткрываем раздел, чтобы сработала сверка."""
    p.evaluate("v => { window.ACL_FB_BACKEND = v; }", value)
    p.locator(".cbd-seg [data-level='campaign']").click()
    p.locator(".cbd-seg [data-level='ad']").click()
    p.wait_for_timeout(150)


def test_rows_3_to_5_open_one_by_one_and_stop_at_five(page):
    _open_first(page, "ad")
    _backend(page, "api")
    row = _row(page)
    labels = row.locator(".cb-textx-label")
    assert [labels.nth(i).is_visible() for i in range(3)] == [False, False, False]
    btn = row.locator(".cb-textx-add")
    assert btn.inner_text() == "+ Primary text 3" and btn.is_enabled()
    for n in (3, 4, 5):
        btn.click()
        page.wait_for_timeout(100)
        assert row.locator(f".cb-textx-label[data-textx='{n}']").inner_text() == f"Primary text {n}"
        assert row.locator(f".cb-textx-label[data-textx='{n}']").is_visible()
    assert btn.is_disabled() and "5" in btn.inner_text()


def test_a_filled_row_is_open_after_a_reload_of_values(page):
    _open_first(page, "ad")
    _backend(page, "api")
    row = _row(page)
    row.locator("input[name$='_copy4']").evaluate("e => { e.value = 'T4'; e.closest('.cb-sel-wrap').querySelector('.cb-textx-search').value = 'T4'; }")
    _backend(page, "api")
    assert row.locator(".cb-textx-label[data-textx='4']").is_visible()


def test_under_rpa_the_empty_fields_are_disabled_with_a_reason(page):
    _open_first(page, "ad")
    _backend(page, "rpa")
    row = _row(page)
    assert row.locator(".cb-textx-add").is_disabled()
    assert "API backend only" in row.locator(".cb-textx-add").get_attribute("title")
    assert row.locator(".cb-vertical-search").is_disabled()
    _backend(page, "api")
    assert row.locator(".cb-vertical-search").is_enabled()
    assert row.locator(".cb-text2-search").first.is_enabled(), "текст 2 работает и у API, и у RPA"


def test_the_ad_name_attributes_are_what_the_server_reads(page):
    _open_first(page, "ad")
    row = _row(page)
    names = row.evaluate("r => Array.from(r.querySelectorAll('input[type=hidden]')).map(e => e.name)")
    suffixes = {n.split("_ad_0_")[-1] for n in names}
    assert {"copy", "copy2", "copy3", "copy4", "copy5", "creative_file", "creative_file_vertical"} <= suffixes


def test_website_media_and_cta_stay_outside_the_hidden_form_section(page):
    _open_first(page, "ad")
    _backend(page, "api")
    row = _row(page)
    assert row.locator('.cb-vertical-search').is_visible()
    assert row.locator('select.cb-cta').is_visible()
    assert row.locator('.cb-form-wrap .cb-vertical-search, .cb-form-wrap .cb-cta').count() == 0
    assert _slot(page).locator('.cb-ad-rows .cb-ad-count').count() == 0
    assert _slot(page).locator('.cb-ad-count').input_value() == '1'


def test_the_type_switch_clears_both_files_and_duplicate_copies_everything(page):
    _open_first(page, "ad")
    row = _row(page)
    row.evaluate("""r => {
      r.querySelector('input[name$="_creative_file_vertical"]').value = 'v.png';
      r.querySelector('.cb-vertical-search').value = 'v.png';
      r.querySelector('input[name$="_copy3"]').value = 'T3';
      r.querySelector('.cb-textx-search[data-x], .cb-textx-sel-wrap[data-textx="3"] .cb-textx-search').value = 'T3';
      r.dataset.textsShown = '3';
    }""")
    row.locator(".cb-dup-ad-btn").click()
    page.wait_for_timeout(150)
    copy = _slot(page).locator(".cb-ad-row").nth(1)
    assert copy.locator("input[name$='_creative_file_vertical']").input_value() == "v.png"
    assert copy.locator("input[name$='_copy3']").input_value() == "T3"
    row.evaluate("r => { const v = r.querySelector('input[type=radio][value=Video]'); v.checked = true; cbAdTypeChange(v); }")
    assert row.locator("input[name$='_creative_file_vertical']").input_value() == ""
    assert row.locator(".cb-vertical-search").input_value() == ""
