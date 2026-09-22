"""The recorded idless table must not borrow headings or hidden rows."""
import os
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from src.analysis_agent import account_spend as sp


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        instance = pw.chromium.launch(executable_path=os.environ.get("ACL_BROWSER_EXECUTABLE") or None)
        yield instance
        instance.close()


@pytest.fixture
def page(browser):
    page = browser.new_page()
    page.route("**/*", lambda route: route.abort())
    page.set_content((Path(__file__).parent / "fixtures/meta/campaign_table_without_row_ids.html").read_text(encoding="utf-8"))
    yield page
    page.close()


def test_a_single_campaign_reads_its_cells_not_the_headers(page):
    page.evaluate("""() => {
      const row = document.querySelector('[data-tooltip-content] a')
        .closest('[role=presentation]').parentElement;
      [...row.parentElement.children].filter(e => e !== row).forEach(e => e.remove());
    }""")
    rows, _, error = sp.read_level(page, "campaign")
    assert len(rows) == 1, error
    assert (rows[0].delivery, rows[0].results, rows[0].spend) == ("Active", "6", "$115.59")


def test_hidden_campaigns_do_not_appear_in_the_report(page):
    page.evaluate("""() => document.querySelector('[data-tooltip-content] a')
      .closest('[role=presentation]').setAttribute('hidden', '')""")
    rows, _, _ = sp.read_level(page, "campaign")
    assert [r.name for r in rows] == [f"Campaign {n}" for n in range(2, 6)]


_DROP_LEVEL_MARKS = """() => document.querySelectorAll('[data-surface*="level="]')
  .forEach(e => e.removeAttribute('data-surface'))"""


def _name_heading(page, word):
    """Заменить слово в заголовке колонки имени (там же стрелка сортировки)."""
    page.evaluate("""(word) => {
      const head = document.querySelector('[data-surface*="table_column_header:name"]');
      const leaf = [...head.querySelectorAll('*')].concat([head])
        .find(e => [...e.childNodes].some(n => n.nodeType === 3 && n.textContent.trim()));
      [...leaf.childNodes].filter(n => n.nodeType === 3 && n.textContent.trim())
        .forEach((n, i) => { n.textContent = i ? '' : word; });
    }""", word)


def test_a_table_without_object_type_is_not_reported_as_campaigns(page):
    """Ни пометок уровня, ни знакомого заголовка — что это за строки, неизвестно."""
    page.evaluate(_DROP_LEVEL_MARKS)
    _name_heading(page, "Nombre")
    rows, _, error = sp.read_level(page, "campaign")
    assert rows == []
    assert "did not say" in error


def test_without_level_marks_the_name_heading_names_the_level(page):
    """22.09 Doc19: на списке объявлений пометок level= нет вовсе, а заголовок
    колонки имени у Meta называет уровень словом — Campaign / Ad set / Ad."""
    page.evaluate(_DROP_LEVEL_MARKS)
    _name_heading(page, "Campaign")
    rows, _, error = sp.read_level(page, "campaign")
    assert len(rows) == 5, error


def test_a_heading_of_another_level_is_refused(page):
    """Заголовок «Ad set» на месте кампаний — не тот список, брать нельзя."""
    page.evaluate(_DROP_LEVEL_MARKS)
    _name_heading(page, "Ad set")
    rows, _, error = sp.read_level(page, "campaign")
    assert rows == []
    assert "expected CAMPAIGN_GROUP" in error


@pytest.fixture
def ads_page(page):
    """Список объявлений той же новой таблицы, как на Doc19 22.09: имя
    объявления — текст с подсказкой без ссылки, пометок level= нет, заголовок
    колонки имени — «Ad»."""
    page.evaluate(_DROP_LEVEL_MARKS)
    _name_heading(page, "Ad")
    page.evaluate("""() => document.querySelectorAll('[data-tooltip-content] a').forEach(a => {
      a.replaceWith(...a.childNodes);
    })""")
    return page


def test_ads_whose_names_are_not_links_are_read(ads_page):
    rows, how, error = sp.read_level(ads_page, "ad")
    assert how == "by-name"
    assert [r.name for r in rows] == [f"Campaign {n}" for n in range(1, 6)], error
    assert all(r.id == "" for r in rows)
    assert "no ad ids" in error


def test_the_name_heading_itself_is_not_a_row(ads_page):
    """У заголовка «Ad» тоже есть подсказка, но ни строки, ни тумблера."""
    rows, _, _ = sp.read_level(ads_page, "ad")
    assert "Ad" not in [r.name for r in rows]


def test_an_ads_list_is_not_taken_for_campaigns(ads_page):
    rows, _, error = sp.read_level(ads_page, "campaign")
    assert rows == []
    assert "expected CAMPAIGN_GROUP" in error
