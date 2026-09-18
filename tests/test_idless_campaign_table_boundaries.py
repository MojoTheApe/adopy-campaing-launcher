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


def test_a_table_without_object_type_is_not_reported_as_campaigns(page):
    page.evaluate("""() => document.querySelectorAll('[data-surface*="level="]')
      .forEach(e => e.removeAttribute('data-surface'))""")
    rows, _, error = sp.read_level(page, "campaign")
    assert rows == []
    assert "did not say" in error
