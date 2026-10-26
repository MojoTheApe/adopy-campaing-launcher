"""ACL-1772: видимый выбор, сохранение, копирование и RPA guard (офлайн Chromium)."""
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_acl1903_builder_file_clear_browser import browser, page, _open_first, _slot  # noqa: F401
from test_acl1774_builder_browser import _backend


def setup(page):
    _open_first(page, "adset")
    _backend(page, "api")
    page.locator(".cbd-seg [data-level='adset']").click()
    return _slot(page).locator('.cb-adset-sec').first


def test_facebook_feed_manual_selection_survives_modes_copy_and_reload(page):
    sec = setup(page)
    sec.locator('[data-placement-platform="instagram"]').uncheck()
    for position in ['story', 'facebook_reels', 'instream_video']:
        sec.locator(f'[data-platform="facebook"][data-placement-position="{position}"]').uncheck()
    expected = {'mode': 'manual', 'facebook_positions': ['feed'], 'instagram_positions': []}
    assert json.loads(sec.locator('.cb-placement-value').input_value()) == expected
    sec.locator('.cb-placement-mode').select_option('advantage_plus')
    assert json.loads(sec.locator('.cb-placement-value').input_value()) == {'mode': 'advantage_plus'}
    page.evaluate('cbKeep.save()')
    page.reload()
    page.evaluate('cbDrawer.open()')
    page.wait_for_timeout(250)
    page.locator('.cbd-camp-row').first.click()
    _backend(page, 'api')
    page.locator(".cbd-seg [data-level='adset']").click()
    sec = _slot(page).locator('.cb-adset-sec').first
    assert sec.locator('.cb-placement-mode').input_value() == 'advantage_plus'
    sec.locator('.cb-placement-mode').select_option('manual')
    assert json.loads(sec.locator('.cb-placement-value').input_value()) == expected
    page.evaluate('cbCopyAdset(document.querySelector(".cbd-host .cb-slot"), 0)')
    copied = _slot(page).locator('.cb-adset-sec').nth(1)
    assert json.loads(copied.locator('.cb-placement-value').input_value()) == expected
    assert copied.locator('[data-placement-platform="instagram"]').is_checked() is False


def test_legacy_draft_and_rpa_do_not_erase_saved_selection(page):
    sec = setup(page)
    assert sec.locator('.cb-placement-value').input_value() == ''
    assert sec.locator('[data-placement-position]:checked').count() == 7
    sec.locator('[data-placement-platform="instagram"]').uncheck()
    raw = sec.locator('.cb-placement-value').input_value()
    _backend(page, 'rpa')
    assert sec.locator('.cb-placement-mode').is_disabled()
    assert sec.locator('[data-placement-position]:enabled').count() == 0
    assert sec.locator('.cb-placement-value').input_value() == raw
    assert not sec.locator('.cb-placement-value').is_disabled()
    assert 'API backend only' in sec.locator('[data-placement-note]').inner_text()
    _backend(page, 'api')
    assert sec.locator('.cb-placement-value').input_value() == raw
