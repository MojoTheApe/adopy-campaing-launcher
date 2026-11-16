"""ACL-2662: превью объявлений не держат живых <video> и не пересоздают их каждую секунду.

Окно Publish во время проверки Meta (m.checking) перерисовывалось раз в секунду вместе с
превью (два autoplay-видео на объявление) -> «Aw, Snap! Out of Memory». Теперь видео
создаётся только по клику, а тик окна не трогает превью.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_builder_duplicate_buttons import browser  # noqa: E402,F401
from test_acl2353_review_browser import _open, _ready, _publish, _settle, _names  # noqa: E402


def _arm(p):
    """Превью-план с видео-объявлением; /publish-plan держится, пока тест его не отпустит."""
    state = {"files": [], "held": []}

    def preview(r):
        names = _names(r.request.post_data or "")
        ads = [{"creative_rel_path": "clip.mp4", "creative_type": "Video", "ad_body": "Body", "ad_title": "Head"}]
        r.fulfill(json={"plan": [{"Campaign Name": n, "Campaign Daily Budget": "$20/day", "Targeting": "Day trading",
                                   "Age Min": 25, "Age Max": 55, "Ad Title": "Head", "Creative Path": "clip.mp4",
                                   "errors": [], "warnings": [], "_ads_raw": ads} for n in names],
                        "review": {n: "fp-" + re.sub(r"\W", "", n)[-40:] for n in names}})

    def hold(r):
        if r.request.url.endswith("/publish-plan"):
            state["held"].append(r)
        else:
            r.fallback()

    def creative(r):
        state["files"].append(r.request.url)
        r.fulfill(status=200, content_type="video/mp4", body=b"\x00" * 16)

    p.route("**/preview-publish*", preview)
    p.route("**/publish-plan*", hold)
    p.route("**/creative-file*", creative)
    return state


def _videos(p):
    return p.evaluate("""() => ({
      total: document.querySelectorAll('video').length,
      autoplay: document.querySelectorAll('video[autoplay]').length,
      playing: [...document.querySelectorAll('video')].filter(v => !v.paused).length,
      inModal: document.querySelectorAll('#safe-confirm-modal video').length,
    })""")


def test_checking_modal_ticks_do_not_rebuild_or_fetch_videos(browser):
    p, fake, errors, ctx = _open(browser)
    try:
        state = _arm(p)
        _ready(p)
        p.locator(".cbd-publish-all").click()
        p.wait_for_selector("#safe-confirm-modal .rv-foot")
        p.wait_for_function("() => window.cbReview.model() && window.cbReview.model().checking")
        p.wait_for_selector("#safe-confirm-modal details.rv-vis", state="attached")
        assert state["held"], "the plan check must be pending"
        # Одно и то же превью-узел переживает тики.
        p.evaluate("() => { window.__vis = document.querySelector('#safe-confirm-modal details.rv-vis'); }")
        before = _videos(p)
        files_before = len(state["files"])
        p.wait_for_timeout(3600)
        assert p.evaluate("() => window.cbReview.model().checking") is True
        assert p.evaluate("() => window.__vis === document.querySelector('#safe-confirm-modal details.rv-vis')")
        after = _videos(p)
        assert after["total"] == before["total"] == 0, (before, after)
        assert after["autoplay"] == 0 and after["playing"] == 0
        assert len(state["files"]) == files_before == 0, state["files"]
        for r in state["held"]:
            r.fallback()
        _settle(p)
        assert _videos(p)["autoplay"] == 0
        assert not [e for e in errors if "Unexpected token ':'" not in e], errors
    finally:
        ctx.close()


def test_opening_a_preview_creates_and_plays_a_video_and_it_survives_ticks(browser):
    p, fake, errors, ctx = _open(browser)
    try:
        state = _arm(p)
        _ready(p)
        p.locator(".cbd-publish-all").click()
        p.wait_for_selector("#safe-confirm-modal details.rv-vis", state="attached")
        p.wait_for_function("() => window.cbReview.model() && window.cbReview.model().checking")
        p.locator("#safe-confirm-modal details.rv-camp > summary").first.click()
        p.locator("#safe-confirm-modal details.rv-vis > summary").click()
        assert _videos(p)["total"] == 0 and not state["files"]
        p.locator("#safe-confirm-modal [data-acl-vsrc]").first.click()
        p.wait_for_selector("#safe-confirm-modal video")
        v = _videos(p)
        assert v["inModal"] == 1 and v["autoplay"] == 0
        p.wait_for_timeout(2500)  # тики не пересоздают открытое видео
        assert _videos(p)["inModal"] == 1
        assert any("/creative-file" in u for u in state["files"])
        n = len(state["files"])
        p.wait_for_timeout(2200)
        assert len(state["files"]) == n
        for r in state["held"]:
            r.fallback()
    finally:
        ctx.close()


def test_builder_cards_use_a_placeholder_until_clicked(browser):
    p, fake, errors, ctx = _open(browser)
    try:
        state = _arm(p)
        _ready(p)
        p.evaluate("""() => { const h = document.querySelector('form.open-account-form input[type="radio"][value="Video"]');
          document.querySelectorAll('form.open-account-form input[type="radio"][value="Video"]').forEach(r => { r.checked = true; r.dispatchEvent(new Event('change', {bubbles: true})); }); }""")
        p.wait_for_timeout(500)
        p.evaluate("""() => document.querySelectorAll('form.open-account-form input[type="hidden"][name$="_creative_file"]').forEach(h => { h.value = 'clip.mp4'; h.dispatchEvent(new Event('input', {bubbles: true})); h.dispatchEvent(new Event('change', {bubbles: true})); })""")
        p.wait_for_timeout(600)
        assert p.evaluate("() => document.querySelectorAll('video').length") == 0
        assert p.evaluate("() => document.querySelectorAll('.cbp-cards [data-acl-vsrc]').length") >= 1
        assert not state["files"]
        p.evaluate("() => document.querySelector('.cbp-cards [data-acl-vsrc]').click()")  # панель может быть свёрнута
        p.wait_for_selector(".cbp-cards video.cbp-media", state="attached")
        assert _videos(p)["autoplay"] == 0
    finally:
        ctx.close()
