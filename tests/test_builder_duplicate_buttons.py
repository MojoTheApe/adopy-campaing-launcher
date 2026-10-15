"""Builder: visible Duplicate on ads and campaigns, no repeated buttons (ACL-1459).

01.10 владелец прислал снимок ящика билдера: дублирование объявления — бледный
значок в углу, дублирование кампании — отдельно внизу ящика, а «+ Add
Campaign» стоит рядом с «+ Campaign» того же аккаунта. Решение: «Duplicate» на
карточке объявления, «+ Ad», «⧉ Duplicate campaign» под объявлениями кампании и
без повторов. Проверяется кликами в настоящем ящике (офлайн-Chromium).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from urllib.parse import urlparse

import pytest
from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_builder_per_account import _render  # noqa: E402
from test_drawer_review_browser import add  # noqa: E402

REPORT = Path(__file__).resolve().parents[1] / "src/web/templates/report.html"


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=os.environ.get("ACL_BROWSER_EXECUTABLE") or None)
        yield b
        b.close()


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
        r.fulfill(json={"files": [], "texts": [], "rows": [], "profiles": [], "waiting": False})

    p.route("**/*", route)
    p.goto("http://acl.test/")
    p.evaluate("cbDrawer.open()")
    yield p
    assert not errors, errors
    ctx.close()


def _open_first(p, level="campaign"):
    add(p)
    p.locator(".cbd-camp-row").first.click()
    # ACL-1769: форма в правой колонке показывает один раздел за раз.
    if level != "campaign":
        p.locator(f".cbd-seg [data-level='{level}']").click()


def _slot(p):
    return p.locator(".cbd-host .cb-slot:not([hidden])")


def test_an_ad_has_a_visible_duplicate_and_the_add_button_reads_plus_ad(page):
    _open_first(page, "ad")
    row = _slot(page).locator(".cb-ad-row").first
    dup = row.locator(".cb-dup-ad-btn")
    assert dup.inner_text() == "Duplicate"
    assert dup.is_visible(), "Duplicate снова виден только при наведении"
    assert float(dup.evaluate("e => getComputedStyle(e).opacity")) == 1.0
    assert _slot(page).locator(".cb-add-btn-sm").inner_text() == "+ Ad"
    dup.click()
    assert _slot(page).locator(".cb-ad-row").count() == 2


def test_duplicate_campaign_sits_under_the_ads_and_opens_the_copy(page):
    _open_first(page)
    slot = _slot(page)
    assert slot.locator(".cb-slot-header .cb-dup-slot-btn").count() == 0, "значок в шапке вернулся"
    # ACL-1769: кнопка теперь в разделе Campaign, а не под объявлениями.
    btn = slot.locator(".cb-lvl-campaign .cb-dup-slot-btn")
    assert btn.count() == 1 and btn.inner_text() == "⧉ Duplicate campaign"
    rows_before = page.locator(".cbd-camp-row").count()
    btn.click()
    assert page.locator(".cbd-camp-row").count() == rows_before + 1, "копия кампании не появилась"
    assert page.locator(".cbd-host .cb-slot:not([hidden])").count() == 1, "копия не раскрылась"


def test_no_repeated_buttons_in_the_drawer(page):
    _open_first(page)
    assert page.locator(".cbd-tools").count() == 0, "запасная кнопка внизу ящика вернулась"
    add_campaign = page.locator(".cbd-host .cb-add-btn")
    assert add_campaign.count() == 0 or not add_campaign.first.is_visible(), \
        "«+ Add Campaign» в ящике рядом с «+ Campaign»"
    # ACL-1769: «+» стоит в строке аккаунта.
    assert page.locator("[data-act='add-here']").first.is_visible(), "«+» у аккаунта пропал"


def test_the_country_page_keeps_add_campaign():
    """На странице страны форма — единственный билдер, кнопке там место."""
    page = REPORT.read_text(encoding="utf-8")
    assert "onclick=\"cbAddSlot(this.closest('form'))\">+ Add Campaign</button>" in page
    assert ".cbd-host .cb-add-btn { display: none; }" in page
