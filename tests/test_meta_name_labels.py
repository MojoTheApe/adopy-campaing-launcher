"""Имя объекта без надписей Meta (ACL-1395).

30.09, Facebook_6540: в дереве аккаунта объявление называлось
«1741 - Pro Bancolombia Unpublished edits Unpublished edits». Meta рисует
надпись о несохранённых правках в той же ячейке, что и имя, и чтение брало
`innerText` ячейки целиком. С таким именем книга статистики не находила ни
одной строки, и у объявления стояли прочерки даже там, где оно одно.

Доказательства — сохранённые чтения этой машины (`account_spend.json`: три
имени с «Unpublished edits» дважды, объявления и кампания) и снятые страницы
Ads Manager (`rpa_pages`: у каждой строки кампании в ячейке имени есть
подсказка `data-tooltip-content` с чистым именем, а у черновиков Meta пишет
«In draft»).
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.analysis_agent import account_spend as sp  # noqa: E402

REPORT = ROOT / "src" / "web" / "templates" / "report.html"


# ── правило по тексту ─────────────────────────────────────────────────────

def test_a_single_trailing_label_is_removed():
    assert sp.clean_object_name("1741 - Pro Bancolombia Unpublished edits") == "1741 - Pro Bancolombia"


def test_a_repeated_label_is_removed_entirely():
    """Так это пришло с живого аккаунта: надпись дважды подряд."""
    assert sp.clean_object_name(
        "1741 - Pro Bancolombia Unpublished edits Unpublished edits") == "1741 - Pro Bancolombia"
    assert sp.clean_object_name(
        "CL_ML_All_Trading_35_15.07 Unpublished edits Unpublished edits") == "CL_ML_All_Trading_35_15.07"


@pytest.mark.parametrize("label", ["In draft", "In review", "Draft"])
def test_the_other_labels_are_removed_too(label):
    assert sp.clean_object_name(f"CO_bnc_All_Video_FX_35_29.09 {label}") == "CO_bnc_All_Video_FX_35_29.09"


def test_mixed_labels_in_a_row_are_all_removed():
    assert sp.clean_object_name("2002 - L In review Unpublished edits") == "2002 - L"


@pytest.mark.parametrize("name", [
    "Draft Picks - Mix_35",
    "2002 - Draft 5 promo",
    "SA In review test campaign",
    "Unpublished edits recap 2",
])
def test_a_label_word_inside_a_real_name_stays(name):
    assert sp.clean_object_name(name) == name


def test_a_name_that_is_only_a_label_word_is_kept():
    """Пустое имя хуже: по нему нельзя ни найти строку, ни показать её."""
    assert sp.clean_object_name("Draft") == "Draft"


def test_spaces_are_collapsed_like_the_table_collapses_them():
    assert sp.clean_object_name("  ad   four  Unpublished edits ") == "ad four"


def test_the_label_must_match_exactly():
    """Надпись Meta пишет одинаково; похожее слово в конце имени — часть имени."""
    assert sp.clean_object_name("Summer draft") == "Summer draft"
    assert sp.clean_object_name("Promo Drafts") == "Promo Drafts"


def test_the_dashboard_uses_the_same_list():
    """Список один; в шаблоне — его копия для уже сохранённых чтений, и она
    не должна разойтись с оригиналом."""
    src = REPORT.read_text(encoding="utf-8")
    m = re.search(r"var AMAP_META_NAME_LABELS = \[([^\]]*)\];", src)
    assert m, "в report.html нет списка надписей"
    in_page = tuple(re.findall(r"'([^']*)'", m.group(1)))
    assert in_page == sp.META_NAME_LABELS


# ── чтение таблицы, на настоящем браузере ─────────────────────────────────

@pytest.fixture(scope="module")
def browser():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=os.environ.get("ACL_BROWSER_EXECUTABLE") or None)
        yield b
        b.close()


@pytest.fixture
def page(browser):
    p = browser.new_page()
    p.route("**/*", lambda route: route.abort())
    yield p
    p.close()


def _name_cell(base: str, kind: str, name: str, labels: str, *, tooltip: bool) -> str:
    """Ячейка имени, как на снятой странице: подсказка с чистым именем вокруг
    ссылки, рядом — надпись Meta."""
    link = f'<span><a href="#"><span>{name}</span></a></span>'
    if tooltip:
        link = (f'<div data-hover="tooltip" data-tooltip-content="{name}" '
                f'data-tooltip-display="overflow">{link}</div>')
    return (f'<span data-surface="{base}forObjectType(name,{kind})">'
            f'<div>{link}<div>{labels}</div></div></span>')


def _grid(rid: str, kind: str, name: str, labels: str, *, tooltip: bool = True) -> str:
    base = f"/am/table/table_row:{rid}unit/table_cell:"
    row = (
        f'<div data-surface="/am/table/table_row:{rid}unit">'
        f'<div data-surface="{base}forObjectType(toggle,{kind})">'
        f'<input type="checkbox" role="switch" aria-checked="true" aria-label="On/off"></div>'
        + _name_cell(base, kind, name, labels, tooltip=tooltip)
        + f'<span data-surface="{base}forObjectType(delivery,{kind})">Active</span>'
        f'<span data-surface="{base}spend">$12.00</span>'
        "</div>"
    )
    return f'<div role="table"><div role="row"><div role="columnheader">Ad</div></div>' \
           f'<div role="presentation">{row}</div></div>'


LABELS = "Unpublished edits Unpublished edits"


def test_the_name_is_read_without_the_label_beside_it(page):
    page.set_content(_grid("120250000000000001", "ADGROUP", "1741 - Pro Bancolombia",
                           "<div>Unpublished edits</div><div>Unpublished edits</div>"))
    (r,), _, err = sp.read_level(page, "ad")
    assert not err, err
    assert r.name == "1741 - Pro Bancolombia"


def test_the_tooltip_name_wins_over_an_unlisted_label(page):
    """Ревью F1: надпись, которой нет в списке, правило не снимет — имя
    должно прийти из подсказки ячейки, а не из её текста."""
    page.set_content(_grid("120250000000000006", "ADGROUP", "1741 - Pro Bancolombia",
                           "<div>Pending edits</div>"))
    (r,), _, err = sp.read_level(page, "ad")
    assert not err, err
    assert r.name == "1741 - Pro Bancolombia"


def test_without_a_tooltip_the_text_rule_still_cleans_it(page):
    page.set_content(_grid("120250000000000002", "CAMPAIGN_GROUP", "CL_ML_All_Trading_35_15.07",
                           LABELS, tooltip=False))
    (r,), _, err = sp.read_level(page, "campaign")
    assert not err, err
    assert r.name == "CL_ML_All_Trading_35_15.07"


def test_a_real_name_with_a_label_word_inside_is_read_whole(page):
    page.set_content(_grid("120250000000000003", "ADGROUP", "2002 - Draft 5 promo", ""))
    (r,), _, err = sp.read_level(page, "ad")
    assert not err, err
    assert r.name == "2002 - Draft 5 promo"


def test_turning_off_finds_the_row_by_its_clean_name(page):
    """Дешборд теперь показывает и присылает чистое имя. Поиск тумблера
    сверяет имя строки — и должен сверять так же, иначе такую строку было бы
    не выключить («row name changed»)."""
    page.set_content(_grid("120250000000000004", "ADGROUP", "1741 - Pro Bancolombia", LABELS))
    for sent in ("1741 - Pro Bancolombia", "1741 - Pro Bancolombia " + LABELS):
        got = page.evaluate(sp._FIND_SWITCH_JS,
                            {"id": "120250000000000004", "kind": "ADGROUP", "name": sent})
        assert got.get("found"), (sent, got)
        assert got["name"] == "1741 - Pro Bancolombia"


def test_turning_off_still_refuses_another_name(page):
    page.set_content(_grid("120250000000000005", "ADGROUP", "1741 - Pro Bancolombia", LABELS))
    got = page.evaluate(sp._FIND_SWITCH_JS,
                        {"id": "120250000000000005", "kind": "ADGROUP", "name": "1741 - Pro"})
    assert not got.get("found") and got.get("why") == "row name changed"
