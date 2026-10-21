# -*- coding: utf-8 -*-
"""ACL-1202: the Accounts overview finds a profile by name, open or not.

One search (`/multilogin-search`, cached by instance+text like the country
card), one account-key rule (`profile_name_key`) — the page holds no copy of it.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
from pathlib import Path

import httpx
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.web import account_links as links  # noqa: E402
from src.web import app as web_app  # noqa: E402
from src.web.app import app  # noqa: E402

TEMPLATE = ROOT / "src" / "web" / "templates" / "report.html"
HARNESS = ROOT / "tests" / "acl_1202_overview_search_harness.cjs"
HEADER = "account_name,ad_account_id,profile_id,folder_id,multilogin_instance,country\n"


def _call(method: str, url: str, **kw):
    async def go():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as c:
            return await c.request(method, url, **kw)
    return asyncio.run(go())


def test_picker_logic_in_the_page():
    """Closed found by name and marked closed, open first, folder id carried,
    suggestion on search results, empty answer, catalogue not answering."""
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required to execute dashboard handlers")
    r = subprocess.run([node, str(HARNESS), str(TEMPLATE)], text=True, capture_output=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr


def test_the_row_search_reuses_the_shared_search_and_no_second_copy():
    html = TEMPLATE.read_text(encoding="utf-8")
    block = html[html.index("/* ACL-1202:begin */"):html.index("/* ACL-1202:end */")]
    assert "amapProfiles(" in block, "the cached /multilogin-search path"
    assert "fetch(" not in block, "no second search implementation"
    assert "ac-prof-find" in html


def test_search_answer_carries_the_account_key_and_folder_id(monkeypatch):
    monkeypatch.setenv("MULTILOGIN_AUTOMATION_TOKEN_A", "t")
    monkeypatch.setattr(web_app, "_mlx_search_all", lambda *a, **k: ([
        {"profile_id": "2a5d0c87", "folder_id": "b8301b4d", "name": "FacebookDQ501 - Duan Guyen"},
    ], ""))
    monkeypatch.setattr("requests.get", lambda *a, **k: type("R", (), {"status_code": 500, "json": lambda s: {}})())
    d = _call("POST", "/multilogin-search", json={"search_text": "DQ501"}).json()
    assert d["ok"]
    (p,) = d["profiles"]
    assert p["folder_id"] == "b8301b4d"
    assert p["key"] == "Q501"


def test_overview_rows_carry_the_account_key(monkeypatch, tmp_path):
    map_csv = tmp_path / "map.csv"
    map_csv.write_text(HEADER, encoding="utf-8")
    monkeypatch.setattr(web_app, "get_ad_account_map_path", lambda: str(map_csv))
    monkeypatch.setattr(links, "get_ad_account_map_path", lambda: str(map_csv))
    monkeypatch.setattr(links, "get_local_config_dir", lambda: str(tmp_path))
    monkeypatch.setattr("src.web.buyer.get_selected_cm", lambda: "Evgeni Sedin")
    monkeypatch.setattr(
        "src.analysis_agent.gsheet_accounts.fetch_account_rows",
        lambda *a, **k: [{"account_label": "FacebookDQ501 SV", "account_status": "Active"}])
    monkeypatch.setattr("src.fb_ads_rpa.mlx_profiles.open_profiles_detailed", lambda: [])
    df = pd.DataFrame([{"Account Alias": "FacebookDQ501 SV", "CM": "Evgeni Sedin", "Country": "Saudi Arabia"}])
    monkeypatch.setattr(web_app, "get_cached_report", lambda *a, **k: ({}, df, []))
    d = _call("GET", "/api/accounts-overview").json()
    row = {a["label"]: a for a in d["accounts"]}["FacebookDQ501 SV"]
    assert row["label_key"] == "Q501"
    assert row["suggested_profile"] == "", "no open profile; the closed one comes from the search"
