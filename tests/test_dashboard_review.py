"""Behavioral regressions for the header/theme/account-drill integration."""
from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

import pandas as pd
import pytest
from starlette.requests import Request

from src.web import account_links, app as web_app


@pytest.fixture
def isolated_links(monkeypatch, tmp_path):
    monkeypatch.setattr(account_links, "get_local_config_dir", lambda: str(tmp_path))
    monkeypatch.setattr(account_links, "get_ad_account_map_path", lambda: str(tmp_path / "missing.csv"))
    monkeypatch.setattr(web_app, "get_ad_account_map_path", lambda: str(tmp_path / "missing.csv"))
    monkeypatch.setattr(web_app, "_account_status", lambda label: {})


def _frame(first_alias="BookOneX01", second_alias="BookTwoX01"):
    return pd.DataFrame({
        "Country Final": ["Colombia", "Colombia"],
        "Account Alias": [first_alias, second_alias],
        "Date": ["2026-09-01", "2026-09-02"],
        "Adset Name": ["Older campaign", "Another account campaign"],
        "Ad Title": ["Older ad", "Another account ad"],
        "Ad Asset": ["demo", "demo"],
        "CM": ["Demo", "Demo"],
        "# FTDs": [0, 0], "# Leads": [1, 2], "$ Spend": [10.0, 20.0],
    })


def _details(monkeypatch, frame, account, section="account", extra=""):
    monkeypatch.setattr(web_app, "get_cached_report", lambda: ({}, frame, []))
    query = (f"section={section}&country=Colombia&account={account}"
             "&date_from=2026-09-02&date_to=2026-09-02" + extra)
    request = Request({"type": "http", "method": "GET", "path": "/details",
                       "query_string": query.encode(), "headers": []})
    return asyncio.run(web_app.details(request)).body.decode()


@pytest.mark.parametrize("first_alias", ["BookOneX01", "Linked_X01"])
def test_period_does_not_change_account_identity(isolated_links, monkeypatch, first_alias):
    """An older exact match or tail collision cannot disappear from resolution."""
    df = _frame(first_alias=first_alias)
    account_links.link("Linked_X01", "country", "CO")
    view = web_app._build_account_map_view({}, df, "2026-09-02", "2026-09-02")
    row = view["countries"][0]["rows"][0]
    assert not row["has_data"], "selected period borrowed another account's spend"
    assert row["workbook_alias"] == ""
    html = _details(monkeypatch, df, "Linked_X01")
    assert "No matching rows" in html
    assert "Another account campaign" not in html


def test_unique_account_drills_into_zero_deposit_ads_in_the_same_period(isolated_links, monkeypatch):
    df = _frame("BookOneX01", "BookOneX01")
    account_links.link("Linked_X01", "country", "CO")
    view = web_app._build_account_map_view({}, df, "2026-09-02", "2026-09-02")
    row = view["countries"][0]["rows"][0]
    assert row["workbook_alias"] == "BookOneX01"
    assert row["spend"] == 20.0 and row["deposits"] == 0
    html = _details(monkeypatch, df, "Linked_X01")
    assert "1 ad set" in html and "with deposits" not in html
    assert "Another account campaign" in html and "Older campaign" not in html
    assert "account=BookOneX01" in html
    nested = _details(monkeypatch, df, "BookOneX01", "optimization", "&adset=Another%20account%20campaign")
    assert "Another account ad" in nested and "Older ad" not in nested


def test_alias_resolution_does_not_borrow_from_another_country():
    df = _frame()
    df["Country Final"] = "Mexico"
    assert web_app.workbook_alias_for(df, "Colombia", "Linked_X01") == "Linked_X01"


@pytest.mark.parametrize("scenario", [
    "automatic", "media_change", "explicit", "unreadable_storage", "unwritable_storage",
])
def test_theme_preference_behavior(scenario):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for theme event behavior")
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [node, str(root / "tests/theme_ui_harness.cjs"),
         str(root / "src/web/templates/report.html"), scenario],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert result.returncode == 0, result.stdout + result.stderr
