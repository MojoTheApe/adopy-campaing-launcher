# -*- coding: utf-8 -*-
"""Билдер: «Primary text 2 (optional)» на месте «CTA (preview only)» (ACL-1344).

Настоящая отрисованная страница в офлайн-Chromium (как
test_drawer_review_browser.py): сервер подменён маршрутами, библиотека
текстов отдаётся из `/api/ad-copy`. Проверяется, что поле есть и выбирается
из библиотеки, что выбор CTA исчез, что значение переживает дублирование
объявления и кампании, перезагрузку страницы и уходит в «Publish all».
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from urllib.parse import urlparse

import pytest
from playwright.sync_api import expect, sync_playwright
from test_builder_per_account import _render
from test_drawer_review_browser import add, all_confirm, fields, ready

from tests._retry_once import goto, launch_chromium

TEXTS = [
    {"ad_title": "Text6", "headline": "Regístrate", "body": "Texto uno", "description": ""},
    {"ad_title": "Text7", "headline": "Otro", "body": "Texto dos", "description": ""},
    {"ad_title": "Text17", "headline": "Tercero", "body": "Texto tres", "description": ""},
]


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        b = launch_chromium(pw)
        yield b
        b.close()


@pytest.fixture
def ui(browser):
    context = browser.new_context()
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    state = {"posts": [], "previews": []}
    html = _render({"Demo": [], "Other": []})

    def route(r):
        path = urlparse(r.request.url).path
        if r.request.resource_type == "document":
            r.fulfill(content_type="text/html", body=html)
            return
        data = {"files": [], "texts": [], "profiles": [], "waiting": False}
        if path == "/api/ad-copy":
            data = {"rows": TEXTS}
        elif path == "/preview-publish":
            f = fields(r.request)
            state["previews"].append(f)
            plan = [{"Campaign Name": f[f"camp_{i}_name"], "errors": [], "warnings": [], "_ads_raw": []}
                    for i in range(int(f["camp_count"]))]
            data = {"plan": plan}
        elif path == "/publish-jobs" and r.request.method == "POST":
            state["posts"].append(fields(r.request))
            data = {"job_id": "offline-job-" + str(len(state["posts"]))}
        elif path.startswith("/publish-jobs/"):
            data = {"status": "done", "publishable": True,
                    "results": [{"success": True, "publishable": True}], "errors": []}
        r.fulfill(json=data)

    page.route("**/*", route)
    # ACL-1491: CI 01.10 — первый переход упёрся в таймаут 30 с на
    # нагруженном раннере; один повтор, проверки ниже от него не зависят.
    goto(page, "http://acl.test/")
    page.evaluate("cbDrawer.open()")
    yield page, state
    assert not errors, errors
    context.close()


def _pick(page, row_sel, query, title):
    """Набрать в поле второго текста и выбрать строку списка — как человек."""
    box = page.locator(f"{row_sel} .cb-text2-search")
    box.click()
    box.fill(query)
    opt = page.locator(f"{row_sel} .cb-text2-sel-wrap .cb-sel-opt[data-val='{title}']")
    expect(opt).to_be_visible()
    opt.dispatch_event("mousedown")


def _text2(page, name):
    return page.evaluate(
        "n => { const h = document.querySelector('[name=\"' + n + '\"]');"
        " return h ? [h.value, h.closest('.cb-text2-sel-wrap').querySelector('.cb-text2-search').value] : null; }",
        name)


def _show(page, idx=0):
    page.locator(".cbd-camp-row").nth(idx).click()
    # ACL-1769: поля объявления лежат в разделе Ad правой колонки.
    page.locator(".cbd-seg [data-level='ad']").click()


def test_the_ad_block_has_primary_text_2_and_no_cta_select(ui):
    p, _ = ui
    add(p)
    _show(p)
    row = p.locator(".cb-slot:not([hidden]) .cb-ad-row").first
    expect(row.locator(".cb-text2-search")).to_have_count(1)
    expect(row.locator(".cb-text2-search")).to_be_visible()
    expect(row.locator("input[type=hidden][name='camp_0_ad_0_copy2']")).to_have_count(1)
    assert "Primary text 2" in row.inner_text() and "(optional)" in row.inner_text()
    expect(p.locator(".cb-cta-select")).to_have_count(0)
    # ACL-1769: CTA показан выключенным (список — ACL-1767), а не рабочим полем.
    assert "preview only" not in row.inner_text()
    expect(row.locator("[data-future='cta'] select")).to_be_disabled()
    assert "ACL-1767" in row.locator("[data-future='cta']").get_attribute("title")
    # Превью живо и показывает постоянную кнопку (перерисовка — по событию
    # смены библиотеки текстов, как на живой странице).
    p.evaluate("t => document.dispatchEvent(new CustomEvent('acl:adtextschange', {detail: {texts: t}}))", TEXTS)
    p.wait_for_function("document.querySelector('.cbp-cta')")
    assert p.locator(".cbp-cta").first.inner_text() == "Learn More"


def test_primary_text_2_is_chosen_from_the_ad_text_library(ui):
    p, _ = ui
    add(p)
    _show(p)
    row = ".cb-slot:not([hidden]) .cb-ad-row[data-ad-idx='0']"
    _pick(p, row, "text1", "Text17")
    assert _text2(p, "camp_0_ad_0_copy2") == ["Text17", "Text17"]
    # Поле первого текста не тронуто.
    assert p.input_value("[name='camp_0_ad_0_copy']") == ""
    # Набрано не название записи — выбора нет, поле подсвечено.
    box = p.locator(f"{row} .cb-text2-search")
    box.fill("Text1")
    assert _text2(p, "camp_0_ad_0_copy2") == ["", "Text1"]
    expect(box).to_have_class(re.compile(r"cb-text2-unmatched"))
    # Название целиком — выбрано; стёрли — второго текста нет.
    box.fill("Text7")
    assert _text2(p, "camp_0_ad_0_copy2") == ["Text7", "Text7"]
    expect(box).not_to_have_class(re.compile(r"cb-text2-unmatched"))
    box.fill("")
    assert _text2(p, "camp_0_ad_0_copy2") == ["", ""]


def test_duplicate_ad_and_duplicate_campaign_carry_primary_text_2(ui):
    p, _ = ui
    add(p)
    _show(p)
    _pick(p, ".cb-slot:not([hidden]) .cb-ad-row[data-ad-idx='0']", "Text7", "Text7")
    p.locator(".cb-slot:not([hidden]) .cb-ad-row[data-ad-idx='0'] .cb-dup-ad-btn").click()
    assert _text2(p, "camp_0_ad_1_copy2") == ["Text7", "Text7"]
    p.evaluate("() => cbDuplicateSlot(document.querySelector('.cb-slot .cb-dup-slot-btn'))")
    assert _text2(p, "camp_1_ad_0_copy2") == ["Text7", "Text7"]
    assert _text2(p, "camp_1_ad_1_copy2") == ["Text7", "Text7"]


def test_primary_text_2_survives_a_reload(ui):
    p, _ = ui
    add(p)
    _show(p)
    _pick(p, ".cb-slot:not([hidden]) .cb-ad-row[data-ad-idx='0']", "Text7", "Text7")
    p.evaluate("() => { cbKeep.save(); location.reload(); }")
    p.wait_for_load_state("load")
    p.wait_for_function("document.querySelector('[name=\"camp_0_ad_0_copy2\"]')")
    assert _text2(p, "camp_0_ad_0_copy2") == ["Text7", "Text7"]


def test_an_unpicked_primary_text_2_stays_outlined_after_a_reload(ui):
    """Ревью PR #346: недонабранный текст 2 после перезагрузки виден в поле,
    а на сервер уходит пусто — обводка «не выбрано» должна вернуться."""
    p, _ = ui
    add(p)
    _show(p)
    box = p.locator(".cb-slot:not([hidden]) .cb-ad-row[data-ad-idx='0'] .cb-text2-search")
    box.click()
    box.fill("Text1")
    assert _text2(p, "camp_0_ad_0_copy2") == ["", "Text1"]
    p.evaluate("() => { cbKeep.save(); location.reload(); }")
    p.wait_for_load_state("load")
    p.wait_for_function("document.querySelector('[name=\"camp_0_ad_0_copy2\"]')")
    assert _text2(p, "camp_0_ad_0_copy2") == ["", "Text1"]
    restored = p.locator(".cb-text2-search").first
    expect(restored).to_have_class(re.compile(r"cb-text2-unmatched"))


def test_publish_all_sends_primary_text_2_with_the_ad(ui):
    p, state = ui
    add(p)
    _show(p)
    p.evaluate("() => cbAddAd(document.querySelector('.cb-slot .cb-add-btn-sm'))")
    _pick(p, ".cb-slot:not([hidden]) .cb-ad-row[data-ad-idx='0']", "Text7", "Text7")
    p.evaluate("() => document.querySelector('form.open-account-form')"
               ".dispatchEvent(new Event('input', {bubbles: true}))")
    ready(p)
    all_confirm(p)
    p.wait_for_function("window.CB_QUEUE && !CB_QUEUE.active")
    assert len(state["posts"]) == 1
    sent = state["posts"][0]
    assert sent["camp_0_ad_0_copy2"] == "Text7"
    assert sent.get("camp_0_ad_1_copy2", "") == "", "объявление без второго текста шлёт пустое поле"
    assert "camp_0_ad_0_cta" not in sent
    # Проверка перед публикацией видит то же значение.
    assert any(f.get("camp_0_ad_0_copy2") == "Text7" for f in state["previews"])


def test_primary_text_1_names_the_library_pick_and_shows_its_headline(ui):
    """ACL-1769: прежний «Ad Title» подписан «Primary text 1»; заголовок записи
    виден строкой под ним, а имя поля, уходящего на сервер, не изменилось."""
    p, _ = ui
    add(p)
    _show(p)
    row = p.locator(".cb-slot:not([hidden]) .cb-ad-row[data-ad-idx='0']")
    text = row.inner_text()
    assert "Primary text 1" in text and "Ad Title" not in text
    assert "Primary text 2" in text and "+ Primary text 3–5" in text
    expect(row.locator("[data-headline]")).to_be_hidden()
    box = row.locator(".cb-adtitle-search")
    box.click()
    box.fill("text7")
    opt = p.locator(".cb-slot:not([hidden]) .cb-ad-row[data-ad-idx='0'] .cb-adtitle-sel-wrap .cb-sel-opt[data-val='Text7']")
    expect(opt).to_be_visible()
    opt.evaluate("e => e.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, cancelable: true}))")
    expect(row.locator("[data-headline]")).to_have_text("Headline: Otro, from the ad-title library")
    assert p.input_value("[name='camp_0_ad_0_copy']") == "Text7", "имя поля или значение изменились"


# ── ACL-1769, правка владельца 13.10: объявления набора — стопкой карточек ──

MODES = ["docked", "expanded", "narrow"]


def _mode(p, mode):
    if mode == "narrow":
        p.set_viewport_size({"width": 420, "height": 900})
    elif mode == "expanded":
        p.locator("#cbd-preset-wide").click()


def _cards(p):
    return p.locator(".cb-slot:not([hidden]) .cb-ad-row")


def _assert_stack(p, count):
    """Все карточки видны и идут одним вертикальным списком в порядке номеров."""
    cards = _cards(p)
    assert cards.count() == count
    assert p.locator(".cbd-ed-ad, .cbd-ed-head select").count() == 0, "выбор объявления остался"
    boxes = []
    for i in range(count):
        expect(cards.nth(i)).to_be_visible()
        boxes.append(cards.nth(i).bounding_box())
    for a, b in zip(boxes, boxes[1:]):
        assert b["y"] >= a["y"] + a["height"] - 1, "карточки перекрываются или стоят в ряд"
    assert [cards.nth(i).get_attribute("data-ad-idx") for i in range(count)] == [str(i) for i in range(count)]
    labels = [cards.nth(i).locator(".cb-ad-row-label").inner_text() for i in range(count)]
    assert labels == [f"Ad {i + 1}" for i in range(count)]


@pytest.mark.parametrize("mode", MODES)
def test_ad_tab_lists_every_ad_and_add_duplicate_delete_keep_them_visible(ui, mode):
    p, _ = ui
    add(p)
    _mode(p, mode)
    _show(p)
    _assert_stack(p, 1)
    _pick(p, ".cb-slot:not([hidden]) .cb-ad-row[data-ad-idx='0']", "Text7", "Text7")
    # Add: новая карточка сразу в списке, первая на месте.
    p.evaluate("() => cbAddAd(document.querySelector('.cb-slot:not([hidden]) .cb-add-btn-sm'))")
    _assert_stack(p, 2)
    # Duplicate: копия вставлена следом и видна, значения скопированы.
    _cards(p).nth(0).locator(".cb-dup-ad-btn").click()
    _assert_stack(p, 3)
    assert _text2(p, "camp_0_ad_0_copy2") == ["Text7", "Text7"]
    assert _text2(p, "camp_0_ad_2_copy2") == ["Text7", "Text7"] or _text2(p, "camp_0_ad_1_copy2") == ["Text7", "Text7"]
    # Delete: остальные остаются, нумерация и имена полей идут подряд.
    _cards(p).nth(1).locator(".cb-remove-ad-btn").click()
    _assert_stack(p, 2)
    names = p.evaluate("() => Array.from(document.querySelectorAll('.cb-slot:not([hidden]) .cb-ad-row [name$=\"_copy2\"]')).map(e => e.name)")
    assert names == ["camp_0_ad_0_copy2", "camp_0_ad_1_copy2"]


@pytest.mark.parametrize("mode", MODES)
def test_several_ads_are_restored_from_the_draft_as_a_stack(ui, mode):
    p, _ = ui
    add(p)
    _mode(p, mode)
    _show(p)
    p.evaluate("() => cbAddAd(document.querySelector('.cb-slot:not([hidden]) .cb-add-btn-sm'))")
    _pick(p, ".cb-slot:not([hidden]) .cb-ad-row[data-ad-idx='0']", "Text6", "Text6")
    _pick(p, ".cb-slot:not([hidden]) .cb-ad-row[data-ad-idx='1']", "Text7", "Text7")
    p.evaluate("() => { cbKeep.save(); location.reload(); }")
    p.wait_for_load_state("load")
    p.wait_for_function("document.querySelector('[name=\"camp_0_ad_1_copy2\"]')")
    assert _text2(p, "camp_0_ad_0_copy2") == ["Text6", "Text6"]
    assert _text2(p, "camp_0_ad_1_copy2") == ["Text7", "Text7"]
    p.evaluate("cbDrawer.open()")
    _mode(p, mode)
    _show(p)
    _assert_stack(p, 2)
