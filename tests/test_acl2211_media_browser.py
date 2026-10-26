import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_acl1903_builder_file_clear_browser import _form_value, _hidden, _row, browser, page
from test_acl1774_builder_browser import _backend
from test_builder_duplicate_buttons import _open_first, _slot


@pytest.mark.parametrize("shown", ["", "other_9x16.png"])
def test_legacy_restore_shows_the_retained_vertical_selection_then_clear_survives_reload(page, shown):
    _open_first(page, "ad")
    _backend(page, "api")
    _slot(page).evaluate('''(slot, shown) => {
        const data = cbKeep.readSlot(slot);
        const hidden = slot.querySelector('input[name$="_creative_file_vertical"]');
        const key = hidden.name.replace(/^camp_\\d+_/, '');
        data.fields[key] = 'story_9x16.png';
        data.shown[key] = shown;
        cbKeep.writeSlot(slot, data);
    }''', shown)
    row = _row(page)
    assert _hidden(row, "_creative_file_vertical").input_value() == "story_9x16.png"
    assert row.locator(".cb-vertical-search").input_value() == "story_9x16.png"
    row.locator(".cb-vertical-search").fill("")
    assert _form_value(page, row, "_creative_file_vertical") == ""
    page.evaluate("cbKeep.save()")
    page.reload()
    page.evaluate("cbDrawer.open()")
    page.locator(".cbd-camp-row").first.click()
    page.locator(".cbd-seg [data-level='ad']").click()
    row = _row(page)
    assert row.locator(".cb-vertical-search").input_value() == ""
    assert _form_value(page, row, "_creative_file_vertical") == ""


def test_duplicate_file_selection_has_a_visible_single_asset_explanation(page):
    _open_first(page, "ad")
    _backend(page, "api")
    row = _row(page)
    row.evaluate('''row => {
        row.querySelector('input[name$="_creative_file"]').value = 'story_9x16.png';
        row.querySelector('.cb-creative-search').value = 'story_9x16.png';
        row.querySelector('input[name$="_creative_file_vertical"]').value = 'story_9x16.png';
        row.querySelector('.cb-vertical-search').value = 'story_9x16.png';
        cbSyncTextRows(row, true);
    }''')
    note = row.locator("[data-vertical-note]")
    assert note.is_visible() and "same file" in note.inner_text().lower()
    assert "once" in note.inner_text().lower()


def test_client_file_identity_uses_paths_without_collapsing_distinct_basenames(page):
    assert page.evaluate("cbSameCreativeFile('story_9x16.png', 'a/story_9x16.png')")
    assert not page.evaluate("cbSameCreativeFile('a/story.png', 'b/story.png')")
    assert not page.evaluate("cbSameCreativeFile('', '')")
    assert not page.evaluate("cbSameCreativeFile('main_1x1.png', 'story_9x16.png')")
