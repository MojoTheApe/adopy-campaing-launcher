"""ACL-2084: смена основного файла очищает слот Stories / Reels; тип файла после перезагрузки (браузер)."""
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


def test_changing_the_main_file_clears_a_slot_that_held_the_old_main(page):
    row = _api_row(page)
    _pick(page, row.locator(".cb-creative-search"), "main_1x1.png")
    _pick(page, row.locator(".cb-vertical-search"), "main_1x1.png")
    _pick(page, row.locator(".cb-creative-search"), "other_9x16.png")
    assert _form_value(page, row, "_creative_file_vertical") == ""
    assert row.locator(".cb-vertical-search").input_value() == ""


def test_changing_the_main_file_keeps_a_separately_chosen_stories_file(page):
    row = _api_row(page)
    _pick(page, row.locator(".cb-creative-search"), "main_1x1.png")
    _pick(page, row.locator(".cb-vertical-search"), "story_9x16.png")
    _pick(page, row.locator(".cb-creative-search"), "other_9x16.png")
    assert _form_value(page, row, "_creative_file_vertical") == "story_9x16.png"


def test_a_ready_post_story_pair_still_fills_the_slot(page):
    row = _api_row(page)
    _pick(page, row.locator(".cb-creative-search"), "7_post.png")
    assert _form_value(page, row, "_creative_file_vertical") == "7_story.png"


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
