"""Recorded Media rows establish identity; unrelated page content does not.

Error/hidden/busy cases below deliberately mutate the captured ready layout.
They are adversarial DOM tests, not recordings of live rejected videos.
"""
from pathlib import Path
import os
import sys

import pytest
from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.fb_ads_rpa import meta_dom as md
from src.fb_ads_rpa import page_capture

FIX = Path(__file__).parent / "fixtures" / "meta"
IDENTITY = "813948786_38351125907864671_2841079256059848182_n"
OTHER = "999999999_88888888888888888_7777777777777777777_n"
REMOVE = '[data-surface$="lib:ads-pacer-remove-all-button"]'
EDIT = '[data-surface$="lib:pacer-item-button-edit"]'


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get("ACL_BROWSER_EXECUTABLE") or None)
        yield browser
        browser.close()


@pytest.fixture
def page(browser, monkeypatch):
    context = browser.new_context()
    page = context.new_page()
    page.route("**/*", lambda route: route.abort())
    monkeypatch.setattr(md, "_sleep_h", lambda *_: None)
    monkeypatch.setattr(md, "_MEDIA_PROCESSING_MAX_S", 0)
    monkeypatch.setattr(page_capture, "capture", lambda *a, **k: None)
    yield page
    context.close()


def attached(page, extra=""):
    page.set_content(extra + (FIX / "ad_media_attached.html").read_text())


def verify(page):
    return md._verify_ad_media_is_the_file(page, "1741_1.mp4", [IDENTITY])


def test_unrelated_navigation_thumbnail_is_not_attachment(page):
    page.set_content(f'<nav><img src="https://offline.invalid/{IDENTITY}.jpg" width="48" height="48"></nav>')
    assert md._ad_media_thumb_ids(page) == []


def test_recorded_unnamed_attachment_has_positive_identity(page):
    attached(page)
    ok, detail = verify(page)
    assert ok is True
    assert detail["media_identity"] == IDENTITY


def test_unrelated_wrong_filename_and_thumbnail_do_not_poison_attachment(page):
    attached(page, f'<nav>wrong.mp4<img src="https://offline.invalid/{OTHER}.jpg" width="48" height="48"></nav>')
    assert md._ad_media_thumb_ids(page) == [IDENTITY]
    assert verify(page)[0] is True


@pytest.mark.parametrize("mutation", ["wrong", "surplus", "hidden", "dialog", "missing", "disabled", "busy", "duplicate"])
def test_incomplete_or_wrong_attachment_never_passes(page, mutation):
    attached(page)
    if mutation == "wrong":
        page.set_content(page.content().replace(IDENTITY, OTHER))
    elif mutation == "surplus":
        page.locator('[role=list] > [role=listitem]').last.evaluate(
            '(el, ids) => el.innerHTML = el.innerHTML.replaceAll(ids[0], ids[1])', [IDENTITY, OTHER])
    elif mutation == "hidden":
        page.locator('[role=list]').evaluate('(el) => el.setAttribute("aria-hidden", "true")')
    elif mutation == "dialog":
        page.set_content('<div role="dialog">' + page.content() + '</div>')
    elif mutation == "missing":
        page.locator(REMOVE).evaluate('(el) => el.remove()')
    elif mutation == "disabled":
        page.locator(EDIT).first.evaluate('(el) => el.setAttribute("aria-disabled", "true")')
    elif mutation == "busy":
        page.locator(EDIT).first.evaluate('(el) => el.setAttribute("aria-busy", "true")')
    elif mutation == "duplicate":
        page.set_content(page.content() + (FIX / 'ad_media_attached.html').read_text())
    assert verify(page)[0] is False


@pytest.mark.parametrize("text", ["Processing your video", "Video rejected", "This media couldn't be processed"])
def test_media_errors_block_a_matching_identity(page, text):
    attached(page)
    page.locator('[role=list]').evaluate('(el, text) => { const x=document.createElement("div"); x.textContent=text; el.append(x); }', text)
    assert verify(page)[0] is False


def test_filename_alone_is_never_identity(page):
    page.set_content('<div>Uploaded media</div><div>1741_1.mp4</div><div>Change selections</div>')
    assert md._verify_ad_media_is_the_file(page, '1741_1.mp4')[0] is False


def test_grid_identity_is_required_even_with_ready_attachment(page):
    attached(page)
    assert md._verify_ad_media_is_the_file(page, '1741_1.mp4')[0] is False


def _reuse(page, monkeypatch, *, fresh=False, wrong=False, warning=False):
    page.unroute("**/*")
    page.route("**/*", lambda route: route.fulfill(body="<html></html>")
               if route.request.is_navigation_request() else route.abort())
    page.goto('https://offline.invalid/ads/edit/test')
    page.set_content('<div role="dialog">' + (FIX / 'library_one_picked.html').read_text() + '</div>')
    # The capture excludes Meta's stylesheets. Keep the footer clickable
    # without changing its attributes, selection or attachment evidence.
    page.add_style_tag(content='[data-surface$="lib:ads-omp-primary-button"] '
                               '{position:fixed;bottom:0;right:0;z-index:10;background:white}')
    tile = md._library_tiles(page, '1741_1.mp4').first
    # First tile is the recorded selection. Mutation models an initially
    # empty grid, and its counter changes in the same event as the mark.
    if fresh:
        page.locator('[data-test-decoration="selected"]').evaluate('(el) => el.removeAttribute("data-test-decoration")')
        page.get_by_text('1 selected', exact=True).evaluate('(el) => el.textContent="0 selected"')
        tile.evaluate('''el => el.addEventListener('click', () => {
          el.parentElement.setAttribute('data-test-decoration','selected');
          const counter = [...document.querySelectorAll('*')].find(n => !n.children.length && n.textContent==='0 selected');
          counter.textContent='1 selected';
        })''')
    if warning:
        tile.evaluate('el => el.insertAdjacentHTML("beforeend", \'<svg width="16" height="16"><path d="M0 0L16 16"/></svg>\')')
    monkeypatch.setattr(md, '_library_search', lambda *args: True)
    monkeypatch.setattr(md, '_verify_ad_media_present', lambda *_: True)
    monkeypatch.setattr(md, '_close_creative_setup_modal', lambda *_: page.set_content(
        (FIX / 'ad_media_attached.html').read_text().replace(IDENTITY, OTHER) if wrong
        else (FIX / 'ad_media_attached.html').read_text()))
    records = []
    monkeypatch.setattr(md._telemetry, 'record', lambda step, ok=None, **kw: records.append((step, ok, kw)))
    result = md._try_reuse_library_media(page, '1741_1.mp4')
    return result, records


@pytest.mark.parametrize('fresh', [False, True])
def test_recorded_selection_to_attachment_preserves_identity(page, monkeypatch, fresh):
    result, records = _reuse(page, monkeypatch, fresh=fresh)
    assert result == md.REUSE_ATTACHED
    successes = [kw for step, ok, kw in records if step == 'ad_creative_reuse' and ok is True]
    assert successes and successes[-1]['diag']['media_identity'] == IDENTITY


def test_a_wrong_attachment_remains_dirty(page, monkeypatch):
    assert _reuse(page, monkeypatch, wrong=True)[0] == md.REUSE_DIRTY


def test_a_warning_with_unknown_meaning_is_not_treated_as_ready(page, monkeypatch):
    assert _reuse(page, monkeypatch, warning=True)[0] == md.REUSE_DIRTY


@pytest.mark.parametrize('known', [False, True])
def test_outer_retry_cannot_override_a_dirty_identity_check(monkeypatch, known):
    calls = []
    monkeypatch.setattr(md, 'upload_already_failed', lambda *_: False)
    monkeypatch.setattr(md, 'upload_already_succeeded', lambda *_: known)
    monkeypatch.setattr(md, '_media_block_filled_now', lambda *_: True)
    monkeypatch.setattr(md, '_verify_ad_media_present', lambda *_: True)
    monkeypatch.setattr(md._telemetry, 'record', lambda *a, **kw: None)
    def upload(*a, **kw):
        calls.append(kw)
        md._set_last_upload_diag({'reuse_unverified': True})
        return False
    monkeypatch.setattr(md, 'upload_ad_creative', upload)
    assert md.upload_ad_creative_with_retry(object(), 'video', '1741_1.mp4') is False
    assert len(calls) == 1
