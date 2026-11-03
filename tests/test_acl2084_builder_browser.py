"""ACL-2084: «Use main file» в слоте Stories / Reels и смена основного файла (браузер)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_acl1903_builder_file_clear_browser import _form_value, _hidden, _pick, _row, browser, page  # noqa: F401
from test_acl1774_builder_browser import _backend
from test_builder_duplicate_buttons import _open_first


def _api_row(page):
    _open_first(page, "ad")
    _backend(page, "api")
    return _row(page)


def _sync(row):
    row.evaluate("row => cbSyncTextRows(row, true)")


def _no_main(row):
    row.evaluate("""row => {
        row.querySelector('input[type="hidden"][name$="_creative_file"]').value = '';
        row.querySelector('.cb-creative-search').value = '';
    }""")


def test_the_link_appears_with_a_main_file_fills_the_slot_once_and_hides(page):
    row = _api_row(page)
    link = row.locator("[data-use-main]")
    _no_main(row)
    _sync(row)
    assert not link.is_visible(), row.evaluate("r => r.querySelector('input[name$=\"_creative_file\"]').value")
    _pick(page, row.locator(".cb-creative-search"), "main_1x1.png")
    _sync(row)
    assert link.is_visible() and link.inner_text() == "Use main file"
    link.click()
    assert _hidden(row, "_creative_file_vertical").input_value() == "main_1x1.png"
    assert row.locator(".cb-vertical-search").input_value() == "main_1x1.png"
    assert _form_value(page, row, "_creative_file_vertical") == "main_1x1.png"
    assert not link.is_visible()
    note = row.locator("[data-vertical-note]")
    assert "square file is used for both Feed and Stories / Reels" in note.inner_text()


def test_a_cleared_slot_stays_cleared(page):
    row = _api_row(page)
    _pick(page, row.locator(".cb-creative-search"), "main_1x1.png")
    _sync(row)
    row.locator("[data-use-main]").click()
    row.locator(".cb-vertical-search").fill("")
    _sync(row)
    assert _form_value(page, row, "_creative_file_vertical") == ""
    assert row.locator("[data-use-main]").is_visible()


def test_the_link_is_api_only(page):
    _open_first(page, "ad")
    _backend(page, "rpa")
    row = _row(page)
    _pick(page, row.locator(".cb-creative-search"), "main_1x1.png")
    row.evaluate("row => cbSyncTextRows(row, false)")
    assert not row.locator("[data-use-main]").is_visible()


def test_changing_the_main_file_clears_a_slot_that_held_the_old_main(page):
    row = _api_row(page)
    _pick(page, row.locator(".cb-creative-search"), "main_1x1.png")
    _sync(row)
    row.locator("[data-use-main]").click()
    _pick(page, row.locator(".cb-creative-search"), "other_9x16.png")
    assert _form_value(page, row, "_creative_file_vertical") == ""
    assert row.locator(".cb-vertical-search").input_value() == ""


def test_changing_the_main_file_keeps_a_separately_chosen_stories_file(page):
    row = _api_row(page)
    _pick(page, row.locator(".cb-creative-search"), "main_1x1.png")
    _pick(page, row.locator(".cb-vertical-search"), "story_9x16.png")
    _pick(page, row.locator(".cb-creative-search"), "other_9x16.png")
    assert _form_value(page, row, "_creative_file_vertical") == "story_9x16.png"


def test_a_ready_post_story_pair_still_fills_first_and_the_link_stays_hidden(page):
    row = _api_row(page)
    _pick(page, row.locator(".cb-creative-search"), "7_post.png")
    _sync(row)
    assert _form_value(page, row, "_creative_file_vertical") == "7_story.png"
    assert not row.locator("[data-use-main]").is_visible()


def test_a_restored_video_ad_searches_videos_in_both_file_boxes(page):
    """Найдено на живой проверке ACL-2084: после перезагрузки видео-объявление искало среди картинок."""
    _open_first(page, "ad")
    _backend(page, "api")
    slot = page.locator(".cbd-host .cb-slot:not([hidden])")
    slot.evaluate("""slot => {
        const data = cbKeep.readSlot(slot);
        const radio = slot.querySelector('input[type="radio"][name$="_creative_type"]');
        data.fields[radio.name.replace(/^camp_\d+_/, '')] = 'Video';
        cbKeep.writeSlot(slot, data);
    }""")
    row = _row(page)
    assert row.evaluate("""r => [...r.querySelectorAll('.cb-creative-sel-wrap')].map(w => w.dataset.ctype)""") == ["Video", "Video"]
