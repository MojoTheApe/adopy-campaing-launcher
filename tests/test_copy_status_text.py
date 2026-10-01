"""Copy text for the account provider's WhatsApp group (acl-132).

Решение владельца 30.09: под плохим статусом аккаунта — кнопка Copy text,
копирующая одну строку «<имя до " - "> - Account <Status>», и подсказка «Copy
screenshot and account number to send in the group». Снимок копирует кнопка
из ACL-1430. Ничего не отправляется: человек вставляет и жмёт Send сам.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
TPL = ROOT / "src" / "web" / "templates"


def _render(account: str, status: dict) -> str:
    from src.analysis_agent.money_format import fmt_currency

    env = Environment(loader=FileSystemLoader(str(TPL)), autoescape=True)
    env.filters["fmt_currency"] = fmt_currency
    env.filters["fmt_usd"] = str
    country = {"country": "Kuwait", "key": "KW", "virtual": False,
               "rows": [{"account": account, "has_data": False, "ready": True,
                         "in_workbook": True, "status": status, "to_do": ""}]}
    html = env.get_template("_account_map_country.html").render(
        mc=country, mlx={},
        amap={"countries": [country], "available": [], "all_accounts": [], "live": {},
              "splits": {}, "fallback": {}, "dates": [], "date_from": "2026-09-01",
              "date_to": "2026-09-28", "span": ""})
    cell = html[html.index('<td class="amap-status">'):]
    return cell[: cell.index("</td>")]


@pytest.mark.parametrize("status,line", [
    ("disabled", "FacebookDoc20 - Account Disabled"),
    ("restricted", "FacebookDoc20 - Account Restricted"),
    ("blocked", "FacebookDoc20 - Account Blocked"),
])
def test_a_problem_status_offers_the_line_for_the_group(status, line):
    cell = _render("FacebookDoc20 - Mios Darwiti", {"status": status, "status_text": "x", "at": "2026-09-30 15:43:25", "at_short": "30.09"})
    assert f'data-copy="{line}"' in cell
    assert ">Copy text</button>" in cell
    assert "Copy screenshot and account number to send in the group" in cell


@pytest.mark.parametrize("status", ["clear", "unknown"])
def test_a_healthy_or_unread_status_offers_nothing(status):
    cell = _render("FacebookDoc20 - Mios Darwiti", {"status": status, "at": "2026-09-30 15:43:25", "at_short": "30.09"})
    assert "amap-copy-text" not in cell and "send in the group" not in cell


def test_a_name_without_a_dash_is_used_whole():
    cell = _render("Facebook_1443_61579302961692", {"status": "disabled", "at": "x", "at_short": "30.09"})
    assert 'data-copy="Facebook_1443_61579302961692 - Account Disabled"' in cell


def test_a_name_is_escaped_in_the_attribute():
    cell = _render('Evil"><b>x - Owner', {"status": "disabled", "at": "x", "at_short": "30.09"})
    assert "<b>x" not in cell, "имя аккаунта вставлено в разметку без экранирования"


def test_the_button_puts_the_line_on_the_clipboard():
    page_src = (TPL / "report.html").read_text(encoding="utf-8")
    fn = page_src[page_src.index("async function amapCopyText"):]
    fn = fn[: fn.index("\n}\n") + 2]
    cell = _render("FacebookDoc20 - Mios Darwiti", {"status": "disabled", "at": "x", "at_short": "30.09"})
    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=os.environ.get("ACL_BROWSER_EXECUTABLE") or None)
        ctx = b.new_context()
        p = ctx.new_page()
        p.route("**/*", lambda r: r.fulfill(content_type="text/html",
                body=f"<table><tr>{cell}</td></tr></table><script>window.__copied=null;"
                     "navigator.clipboard.writeText=async t=>{{window.__copied=t}};</script><script>" + fn + "</script>"))
        p.goto("http://localhost/")
        p.locator(".amap-copy-text").click()
        p.wait_for_function("window.__copied !== null")
        assert p.evaluate("window.__copied") == "FacebookDoc20 - Account Disabled"
        assert p.locator(".amap-copy-text").inner_text() == "Copied"
        b.close()
