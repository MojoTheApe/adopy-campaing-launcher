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


def _html(workbench_country="Saudi Arabia"):
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
        mlx_labels=mlx_instance_labels(), request=MagicMock(), report=report, warnings=[],
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
