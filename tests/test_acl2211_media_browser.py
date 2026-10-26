import sys
import json
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_acl1903_builder_file_clear_browser import _form_value, _hidden, _row, browser, FILES
from test_builder_per_account import _render
from src.fb_api import preflight_rules as pf
from test_acl1774_builder_browser import _backend
from test_builder_duplicate_buttons import _open_first, _slot


@pytest.fixture
def page(browser):
    ctx = browser.new_context()
    p = ctx.new_page()
    errors = []
    p.on('pageerror', lambda error: errors.append(str(error)))
    html = _render({'Demo': [], 'Other': []})
    html = html.replace('window.CB_PREFLIGHT_RULES = {};',
                        'window.CB_PREFLIGHT_RULES = ' + json.dumps(pf.export_rules()) + ';', 1)
    files = FILES + [dict(filename='shared.png', rel_path=folder + '/shared.png', folder=folder,
                          type='Image', size_bytes=1000) for folder in ('one', 'two')]

    def route(r):
        if r.request.resource_type == 'document':
            r.fulfill(content_type='text/html', body=html)
        else:
            r.fulfill(json={'files': files, 'texts': [], 'rows': [], 'profiles': [], 'waiting': False})

    p.route('**/*', route)
    p.goto('http://acl.test/')
    p.evaluate('cbDrawer.open()')
    yield p
    assert not errors, errors
    ctx.close()


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


def test_legacy_selection_stays_visible_when_ad_and_ad_set_are_copied(page):
    _open_first(page, "ad")
    _backend(page, "api")
    _slot(page).evaluate("""slot => {
        const data = cbKeep.readSlot(slot);
        for (const suffix of ['_creative_file', '_creative_file_vertical']) {
            const hidden = slot.querySelector('input[name$="' + suffix + '"]');
            const key = hidden.name.replace(/^camp_\\d+_/, '');
            data.fields[key] = suffix === '_creative_file' ? 'main_1x1.png' : 'story_9x16.png';
            data.shown[key] = '';
        }
        cbKeep.writeSlot(slot, data);
    }""")
    _row(page).locator('.cb-dup-ad-btn').click()
    page.locator(".cbd-seg [data-level='adset']").click()
    page.locator("[data-act='copy-adset']").click()
    rows = _slot(page).locator('.cb-ad-row')
    assert rows.count() == 4
    for row in rows.all():
        for suffix, selector, expected in [
            ('_creative_file', '.cb-creative-search', 'main_1x1.png'),
            ('_creative_file_vertical', '.cb-vertical-search', 'story_9x16.png'),
        ]:
            assert row.locator(selector).input_value() == expected
            assert _hidden(row, suffix).input_value() == expected
            assert _form_value(page, row, suffix) == expected


@pytest.mark.parametrize('alias', ['a\\story_9x16.png', 'A/STORY_9X16.PNG', 'STORY_9X16.PNG'])
def test_accepted_path_aliases_do_not_block_multiple_texts_in_real_client_audit(page, tmp_path, alias):
    from src.meta_campaign_agent.creative_finder import find_creative_file
    from src.fb_api.creator import _same_media_file

    root = tmp_path / 'creatives'
    (root / 'a').mkdir(parents=True)
    (root / 'a' / 'story_9x16.png').write_bytes(b'fixture')
    assert _same_media_file(str(find_creative_file(root, alias)),
                            str(find_creative_file(root, 'a/story_9x16.png')))
    _open_first(page, 'ad')
    _backend(page, 'api')
    _row(page).evaluate("""(row, alias) => {
        row.querySelector('input[name$="_creative_file"]').value = alias;
        row.querySelector('input[name$="_creative_file_vertical"]').value = 'a/story_9x16.png';
        row.querySelector('input[name$="_copy"]').value = 'First';
        row.querySelector('input[name$="_copy2"]').value = 'Second';
        window.cbAdLibraryEntry = name => ({body: name, headline: name, description: ''});
        cbTree.refreshEditor();
    }""", alias)
    rule = page.locator('.cbd-pf-line[data-rule="texts_vertical"]')
    assert rule.count() == 1, 'Real exported rulebook must be present'
    assert 'cbd-pf-ok' in rule.get_attribute('class'), rule.inner_text()


def test_client_alias_normalization_preserves_ambiguous_and_distinct_files(page):
    _open_first(page, 'ad')
    assert not page.evaluate("cbSameCreativeFile('SHARED.PNG', 'one/shared.png')")
    assert not page.evaluate("cbSameCreativeFile('ONE/SHARED.PNG', 'two/shared.png')")
    assert page.evaluate("cbSameCreativeFile('ONE/SHARED.PNG', 'one/shared.png')")
