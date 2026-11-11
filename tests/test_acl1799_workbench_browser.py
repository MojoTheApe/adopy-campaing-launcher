"""ACL-1799: Country Workbench в настоящем (офлайн) Chromium.

Проверяется проводка, которую чистые функции доказать не могут: «Use» кладёт
улику в выбранный в билдере черновик — и ни в какой другой; без выбора не
кладёт никуда; страна -> ассет переключают KPI и таблицы; прежние разделы
остаются доступны по своим адресам. Запускать при свободной памяти больше 6 ГБ.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from test_acl1799_country_workbench import fixture_df  # noqa: E402
from test_drawer_review_browser import add  # noqa: E402


def _html(workbench_country="Saudi Arabia", fb_backend="rpa"):
    from src.analysis_agent import country_workbench as cw
    from src.web.app import _enrich_country_overview, mlx_instance_labels, templates
    from test_web_drill import _MOCK_REPORT

    df = fixture_df().assign(**{"Country Final": workbench_country})
    base = _MOCK_REPORT["countries"][0]
    c = {**base, "country": workbench_country, "open_recs": {"Demo": []}, "close_recs": {},
         "workbench": cw.build_country_workbench(df, workbench_country, "Eva")}
    report = {**_MOCK_REPORT, "countries": [c]}
    _enrich_country_overview(report)
    refs = {"targetings": [{"code": "FX", "label": "FX", "fb_interest": "Foreign exchange market"}]}
    return templates.env.get_template("report.html").render(
        mlx_labels=mlx_instance_labels(), request=MagicMock(), fb_backend=fb_backend, report=report, warnings=[],
        built_at="2026-06-08", upload_errors={}, account_preflight={}, account_preflight_json={},
        ref_options=refs, ref_options_json=refs, account_alias_map_json={}, draft_status={},
        cw_goals={"saudi arabia": {"ftd": 10, "cpa": 120.0, "cr": None}},
        period={"date_from": "2026-06-01", "date_to": "2026-06-08", "span": ""})


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=os.environ.get("ACL_BROWSER_EXECUTABLE") or None)
        yield b
        b.close()


@pytest.fixture
def page(browser):
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    p = ctx.new_page()
    errors: list[str] = []
    p.on("pageerror", lambda e: errors.append(str(e)))
    html = _html()

    def route(r):
        if r.request.resource_type == "document":
            r.fulfill(content_type="text/html", body=html)
            return
        r.fulfill(json={"files": [], "texts": [], "rows": [], "profiles": [], "waiting": False})

    p.route("**/*", route)
    p.goto("http://acl.test/#country-saudi-arabia-workbench")
    yield p
    assert not errors, errors
    ctx.close()


PANEL = "#view-country-saudi-arabia-workbench"


def _campaigns(p, n=2):
    add(p, count=n)
    p.evaluate("cbDrawer.open()")
    p.wait_for_function("document.querySelectorAll('.cbd-camp-row').length >= %d" % n)


def _pick(p, index, asset="Alpha"):
    """Выбрать в билдере кампанию номер index и дать ей ассет, как у улики."""
    p.locator(".cbd-camp-row").nth(index).click()
    p.wait_for_function("window.cbTree.active() && window.cbTree.active().id")
    p.evaluate("a => { cbTree.active().slot.querySelector('[name$=\"_asset\"]').value = a; }", asset)


def _targeting(p, index):
    """Значение скрытого поля таргетинга у кампании номер index (по порядку в форме)."""
    return p.evaluate(
        "i => document.querySelectorAll('form.open-account-form')[0].querySelectorAll('.cb-slot')[i]"
        ".querySelector('.cb-targeting-hidden').value", index)


def _click_use(p, label, nth=0):
    p.locator(f"{PANEL} .cw-use", has_text=label).nth(nth).click()


def test_country_to_asset_switches_the_kpis_and_tables(page):
    assert page.locator(f"{PANEL}.active").count() == 1, "Workbench не открылся по своему адресу"
    assert page.locator(f"{PANEL} .cw-chip").count() == 3          # All assets, Alpha, Beta
    assert page.locator(f"{PANEL} [data-kpi='cpa'] .cw-kpi-you").inner_text().startswith("$")
    page.locator(f"{PANEL} .cw-chip", has_text="Beta").click()
    # В Beta лидов меньше порога: честная пометка, а не сравнение.
    kpi = page.locator(f"{PANEL} [data-kpi='cpl']").inner_text()
    assert "insufficient" in kpi and "Not compared" in kpi
    for tab in ("Winning setups", "Landings", "Targets", "Creatives", "Buyers", "Campaigns"):
        page.locator(f"{PANEL} .cw-tab", has_text=tab).click()
        assert page.locator(f"{PANEL} .cw-tab[aria-selected='true']").inner_text() == tab


def test_creative_versions_are_listed_under_their_pack(page):
    page.locator(f"{PANEL} .cw-chip", has_text="Alpha").click()
    page.locator(f"{PANEL} .cw-tab", has_text="Creatives").click()
    page.locator(f"{PANEL} .cw-toggle").first.click()
    sub = page.locator(f"{PANEL} tr.cw-sub")
    assert sub.count() == 2
    text = sub.all_inner_texts()
    assert any("Version 1" in t for t in text) and any("Version 2" in t for t in text)


def test_goal_benchmark_and_peers_are_three_separate_cells(page):
    page.locator(f"{PANEL} .cw-chip", has_text="Alpha").click()
    refs = {r: page.locator(f"{PANEL} [data-ref='{r}']").inner_text() for r in ("goal", "benchmark", "peer")}
    assert "CPA ≤ $120" in refs["goal"]
    assert "benchmark" in refs["benchmark"].lower() and "CPA" in refs["peer"]
    assert len({refs["goal"], refs["benchmark"], refs["peer"]}) == 3


def test_use_without_a_selected_draft_adds_nothing_and_asks_to_pick(page):
    _campaigns(page, 2)
    page.evaluate("cbTree.close()")
    page.locator(f"{PANEL} .cw-chip", has_text="Alpha").click()
    page.locator(f"{PANEL} .cw-tab", has_text="Targets").click()
    _click_use(page, "Use target")
    status = page.locator(f"{PANEL} .cw-dest-status").inner_text()
    assert "No draft is selected" in status and "last campaign" in status
    assert _targeting(page, 0) == "" and _targeting(page, 1) == "", "улика ушла в кампанию без выбора"


def test_use_target_goes_to_the_selected_draft_and_not_to_the_last_one(page):
    _campaigns(page, 2)
    _pick(page, 0)                                         # выбрана ПЕРВАЯ из двух
    page.locator(f"{PANEL} .cw-chip", has_text="Alpha").click()
    page.locator(f"{PANEL} .cw-tab", has_text="Targets").click()
    dest = page.locator(f"{PANEL} .cw-dest-path").inner_text()
    assert "Saudi Arabia" in dest and "Ad set 1" in dest
    _click_use(page, "Use target")                         # цель FX есть в библиотеке
    assert _targeting(page, 0) == "FX"
    assert _targeting(page, 1) == "", "улика попала в последнюю кампанию"
    status = page.locator(f"{PANEL} .cw-dest-status").inner_text()
    assert "New combination" in status
    # Провенанс записан на выбранном черновике.
    ev = page.evaluate("JSON.parse(cbTree.active().slot.dataset.cwEvidence)")
    assert ev[0]["kind"] == "target" and ev[0]["value"] == "FX" and ev[0]["from"] == "2026-06-01"


def test_use_target_replaces_an_already_chosen_target_not_only_its_name(page):
    """Поле уже занято выбранным таргетом: Use target меняет и код с интересами, а не одно имя."""
    _campaigns(page, 1)
    _pick(page, 0)
    page.evaluate("""() => { const w = document.querySelector('form.open-account-form .cb-targeting-wrap');
      w.querySelector('.cb-targeting-search').value = 'ZZ';
      w.querySelector('.cb-targeting-hidden').value = 'ZZ';
      w.querySelector('.cb-targeting-hint').textContent = 'old interests'; }""")
    page.locator(f"{PANEL} .cw-chip", has_text="Alpha").click()
    page.locator(f"{PANEL} .cw-tab", has_text="Targets").click()
    _click_use(page, "Use target")
    assert _targeting(page, 0) == "FX", "сменилось имя в поле, а таргет остался прежним"
    assert page.evaluate("document.querySelector('.cb-targeting-search').value") == "FX"
    assert page.evaluate("document.querySelector('.cb-targeting-hint').textContent") == "Foreign exchange market"


def test_selecting_the_other_draft_moves_the_destination(page):
    _campaigns(page, 2)
    _pick(page, 1)
    page.locator(f"{PANEL} .cw-chip", has_text="Alpha").click()
    page.locator(f"{PANEL} .cw-tab", has_text="Targets").click()
    _click_use(page, "Use target")
    assert _targeting(page, 1) == "FX" and _targeting(page, 0) == ""


def test_landing_is_noted_not_written_into_a_field_that_does_not_exist(page):
    _campaigns(page, 1)
    _pick(page, 0)
    page.locator(f"{PANEL} .cw-chip", has_text="Alpha").click()
    page.locator(f"{PANEL} .cw-tab", has_text="Landings").click()
    _click_use(page, "Use landing")
    assert "no landing field" in page.locator(f"{PANEL} .cw-dest-status").inner_text()
    assert "LP1" in page.evaluate("cbTree.active().slot.dataset.cwEvidence")


def test_an_asset_mismatch_is_confirmed_before_use(page):
    _campaigns(page, 1)
    _pick(page, 0)
    page.evaluate("""() => {
      const slot = cbTree.active().slot; slot.querySelector('[name$="_asset"]').value = 'Gamma'; }""")
    page.locator(f"{PANEL} .cw-chip", has_text="Alpha").click()
    page.locator(f"{PANEL} .cw-tab", has_text="Targets").click()
    _click_use(page, "Use target")
    # ACL-141: вопрос — окно страницы; отказ — его Cancel.
    dlg = page.locator("dialog.acl-dlg")
    dlg.wait_for()
    asked = dlg.inner_text()
    assert "Gamma" in asked and "Alpha" in asked, asked
    dlg.locator(".acl-dlg-quiet").click()
    page.wait_for_function("!document.querySelector('dialog.acl-dlg')")
    assert "Cancelled" in page.locator(f"{PANEL} .cw-dest-status").inner_text()
    assert _targeting(page, 0) == "", "отказ в подтверждении всё равно записал улику"


def test_existing_views_stay_reachable_from_the_workbench(page):
    page.locator(f"{PANEL} [data-cw-go='country-saudi-arabia-manage']").click()
    assert page.locator("#view-country-saudi-arabia-manage.active").count() == 1
    assert page.evaluate("location.hash") == "#country-saudi-arabia-manage"


# ── ACL-1799 (поле-цель): «Use» кладёт в отмеченное поле билдера ─────────────────────

def _file(name, ctype="Image", partner=""):
    return {"filename": name, "rel_path": name, "folder": "", "type": ctype, "size_bytes": 1,
            **({"vertical_partner": partner} if partner else {})}


API_FILES = [_file("1_es_1121_post.png", partner="1_es_1121_story.png"), _file("1_es_1121_story.png"),
             _file("2_es_1121_post.png")]
API_TEXTS = [{"ad_title": "Story14", "headline": "H", "body": "B", "description": "D"}]


@pytest.fixture
def api_page(browser):
    """Тот же Workbench, но в режиме API: у объявления есть Stories / Reels и тексты 3-5."""
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    p = ctx.new_page()
    errors: list[str] = []
    p.on("pageerror", lambda e: errors.append(str(e)))
    html = _html(fb_backend="api")

    def route(r):
        url = r.request.url
        if r.request.resource_type == "document":
            r.fulfill(content_type="text/html", body=html)
        elif "/api/creatives" in url:
            r.fulfill(json={"files": API_FILES})
        elif "/api/ad-copy" in url:
            r.fulfill(json={"rows": API_TEXTS})
        else:
            r.fulfill(json={"files": [], "texts": [], "rows": [], "profiles": [], "waiting": False})

    p.route("**/*", route)
    p.goto("http://acl.test/#country-saudi-arabia-workbench")
    yield p
    assert not errors, errors
    ctx.close()


def _builder(p, ads=3, sets=2):
    """Одна кампания с `sets` наборами и `ads` объявлениями; открыта в билдере на уровне Ad."""
    add(p, count=1, valid=False)
    p.evaluate("cbDrawer.open()")
    p.wait_for_function("document.querySelectorAll('.cbd-camp-row').length >= 1")
    p.evaluate("""([ads, sets]) => {
      const slot = document.querySelector('form.open-account-form .cb-slot');
      for (let i = 1; i < ads; i++) cbAddAd(slot);
      for (let k = 1; k < sets; k++) cbAddAdset(slot);
    }""", [ads, sets])
    _pick(p, 0)
    p.evaluate("cbTree.setLevel('ad')")


def _mark(p, selector, ad=0):
    """Отметить поле, как рука: pointerdown + фокус. В API-режиме поле может быть скрыто."""
    p.evaluate("""([sel, ad]) => {
      const el = cbTree.active().slot.querySelectorAll('.cb-ad-row')[ad].querySelector(sel);
      el.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true })); el.focus(); }""", [selector, ad])


def _versions(p):
    p.locator(f"{PANEL} .cw-chip", has_text="Alpha").click()
    p.locator(f"{PANEL} .cw-tab", has_text="Creatives").click()
    p.locator(f"{PANEL} .cw-toggle").first.click()


def _val(p, name, ad=0):
    return p.evaluate("""([n, ad]) => cbTree.active().slot.querySelectorAll('.cb-ad-row')[ad]
      .querySelector('[name$="' + n + '"]').value""", [name, ad])


TEXT2 = ".cb-text2-sel-wrap:not([data-textx]) .cb-text2-search"


def test_use_version_fills_the_marked_stories_field_of_the_open_ad(api_page):
    p = api_page
    _builder(p)
    _mark(p, ".cb-vertical-search", ad=1)
    _versions(p)
    p.locator(f"{PANEL} .cw-use", has_text="Use version").first.click()
    assert _val(p, "creative_file_vertical", ad=1) == "1_es_1121_story.png"
    assert _val(p, "creative_file", ad=1) == "" and _val(p, "creative_file_vertical", ad=0) == ""
    assert _val(p, "copy", ad=1) == "", "текст не должен меняться при отмеченном файле"
    dest = p.locator(f"{PANEL} .cw-dest-path").inner_text()
    assert dest.endswith("Ad 2 / Stories / Reels file"), dest


def test_use_version_fills_the_marked_primary_text_2(api_page):
    p = api_page
    _builder(p)
    _mark(p, TEXT2, ad=0)
    _versions(p)
    p.locator(f"{PANEL} .cw-use", has_text="Use version").first.click()     # версия 1 — с текстом Story14
    assert _val(p, "copy2") == "Story14" and _val(p, "copy") == ""
    assert _val(p, "creative_file") == ""


def test_a_version_without_text_is_disabled_for_a_text_field(api_page):
    p = api_page
    _builder(p)
    _mark(p, TEXT2)
    _versions(p)
    buttons = p.locator(f"{PANEL} .cw-use", has_text="Use version")
    states = [(buttons.nth(i).is_disabled(), buttons.nth(i).get_attribute("title")) for i in range(buttons.count())]
    off = [t for d, t in states if d]
    assert off and all("no ad text" in t for t in off), states
    assert any(not d for d, _ in states), "версия с текстом должна остаться активной"


def test_use_target_goes_to_the_selected_ad_set(api_page):
    p = api_page
    _builder(p, ads=1, sets=2)
    p.evaluate("""() => { const sec = cbTree.active().slot.querySelectorAll('.cb-adset-sec')[1];
      const el = sec.querySelector('.cb-targeting-search');
      el.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true })); el.focus(); }""")
    p.locator(f"{PANEL} .cw-chip", has_text="Alpha").click()
    p.locator(f"{PANEL} .cw-tab", has_text="Targets").click()
    assert "Ad set 2" in p.locator(f"{PANEL} .cw-dest-path").inner_text()
    _click_use(p, "Use target")
    hidden = p.evaluate("[...cbTree.active().slot.querySelectorAll('.cb-targeting-hidden')].map(h => h.value)")
    assert hidden[1] == "FX" and hidden[0] == "", hidden


def test_without_a_marked_field_the_open_ad_gets_it_not_ad_1(api_page):
    p = api_page
    _builder(p, ads=3, sets=1)
    p.evaluate("cbTree.setLevel('adset')")
    p.evaluate("document.querySelector('.cbd-ed-inset [data-act=\"open-ad\"][data-ad=\"2\"]').click()")
    _versions(p)
    p.locator(f"{PANEL} .cw-use", has_text="Use version").nth(1).click()   # версия 2 пакета 1121, без текста
    assert _val(p, "creative_file", ad=2) == "2_es_1121_post.png"
    assert _val(p, "creative_file", ad=0) == "", "улика ушла в Ad 1"


def test_the_destination_line_follows_a_builder_click_without_rerender(api_page):
    p = api_page
    _builder(p)
    p.locator(f"{PANEL} .cw-tab", has_text="Targets").click()
    p.evaluate("window.__marker = document.querySelector('%s .cw-table')" % PANEL)
    _mark(p, ".cb-vertical-search", ad=2)
    p.wait_for_function("document.querySelector('%s .cw-dest-path').textContent.includes('Ad 3 / Stories / Reels file')" % PANEL,
                        timeout=900)   # быстрее секундного опроса: сработало событие
    assert p.evaluate("window.__marker === document.querySelector('%s .cw-table')" % PANEL), "таблица перерисована"
    assert p.locator(".cb-use-target").count() == 1 and "Use goes here" in p.locator(".cb-use-tag").inner_text()


def test_the_target_stays_after_use_and_clears_on_campaign_switch(api_page):
    p = api_page
    _builder(p)
    _mark(p, ".cb-vertical-search", ad=0)
    _versions(p)
    p.locator(f"{PANEL} .cw-use", has_text="Use version").first.click()
    assert p.locator(".cb-use-target").count() == 1, "цель пропала после вставки"
    p.locator(".cb-use-tag-x").evaluate("b => b.click()")
    assert p.locator(".cb-use-target").count() == 0 and p.evaluate("cbTree.active().field") == ""
    _mark(p, ".cb-vertical-search", ad=0)
    assert p.evaluate("cbTree.active().field") == "vertical"
    p.evaluate("cbTree.close()")
    p.evaluate("cbTree.open(document.querySelector('.cbd-camp[data-camp]').dataset.camp)")
    assert p.evaluate("cbTree.active().field") == "", "цель пережила смену кампании"
