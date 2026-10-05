# -*- coding: utf-8 -*-
"""ACL-1588: аккаунт, подключённый на этой машине, не прячется за CM из статистики.

Выбранный байер по-прежнему фильтрует ЦИФРЫ (спенд, лиды, цели), но никогда не
скрывает аккаунт из карты этой машины. Подсказки для подключения — все аккаунты.
"""
from __future__ import annotations

import asyncio
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
from src.web.app import _add_linked_accounts_to_builder, _build_account_map_view, app  # noqa: E402

TEMPLATE = ROOT / "src" / "web" / "templates" / "report.html"
HARNESS = ROOT / "tests" / "acl_1588_connect_harness.cjs"

BUYER = "Evgeni Sedin"
HEADER = "account_name,ad_account_id,profile_id,folder_id,multilogin_instance,country\n"


@pytest.fixture
def machine(monkeypatch, tmp_path):
    """Карта машины: FacebookDoc30 подключён, в статистике он у Dan Dicker."""
    map_csv = tmp_path / "map.csv"
    map_csv.write_text(
        HEADER + "FacebookDoc30,act_30,prof-30,fold-30,A,Saudi Arabia\n", encoding="utf-8")
    monkeypatch.setattr(web_app, "get_ad_account_map_path", lambda: str(map_csv))
    monkeypatch.setattr(links, "get_ad_account_map_path", lambda: str(map_csv))
    monkeypatch.setattr(links, "get_local_config_dir", lambda: str(tmp_path))
    monkeypatch.setattr("src.web.buyer.get_selected_cm", lambda: BUYER)
    monkeypatch.setattr(
        "src.analysis_agent.gsheet_accounts.fetch_account_rows",
        lambda *a, **k: [{"account_label": "FacebookDoc30", "account_status": "Active"},
                         {"account_label": "FacebookOther77", "account_status": "Active"}])
    monkeypatch.setattr("src.fb_ads_rpa.mlx_profiles.open_profiles_detailed", lambda: [])
    df = pd.DataFrame([
        {"Account Alias": "FacebookDoc30", "CM": "Dan Dicker", "Country": "Saudi Arabia"},
        {"Account Alias": "FacebookOther77", "CM": "Dan Dicker", "Country": "Saudi Arabia"},
        {"Account Alias": "FacebookMine1", "CM": BUYER, "Country": "Saudi Arabia"},
        {"Account Alias": "FacebookTheirs9", "CM": "Dan Dicker", "Country": "Saudi Arabia"},
    ])
    monkeypatch.setattr(web_app, "get_cached_report", lambda *a, **k: ({}, df, []))
    return tmp_path


def _overview() -> dict:
    async def go():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as c:
            return (await c.get("/api/accounts-overview")).json()
    return asyncio.run(go())


def test_accounts_lists_a_connected_account_whatever_the_stats_cm(machine):
    data = _overview()
    assert data["ok"], data
    by_label = {a["label"]: a for a in data["accounts"]}
    assert "FacebookDoc30" in by_label, "connected here, so it is never hidden"
    assert by_label["FacebookDoc30"]["connected"] is True
    # Строка идёт из листа, а не из «только здесь»: старый фильтр в цикле листа
    # возвращал аккаунт через local_only без статуса.
    assert by_label["FacebookDoc30"]["status"] == "Active"
    assert not by_label["FacebookDoc30"].get("local_only")
    assert by_label["FacebookDoc30"]["owner_hint"] == "owner in stats: Dan Dicker"


def test_a_local_only_account_is_not_hidden_either(machine):
    (machine / "map.csv").write_text(
        HEADER + "FacebookLocalZ,act_5,prof-5,fold-5,A,\n", encoding="utf-8")
    web_app_df = pd.DataFrame([{"Account Alias": "FacebookLocalZ", "CM": "Dan Dicker", "Country": "X"}])
    web_app.get_cached_report = lambda *a, **k: ({}, web_app_df, [])
    data = _overview()
    row = {a["label"]: a for a in data["accounts"]}["FacebookLocalZ"]
    assert row["local_only"] and row["owner_hint"] == "owner in stats: Dan Dicker"


def test_suggestions_include_other_managers_accounts(machine):
    data = _overview()
    names = {s.lower() for s in data["suggestions"]}
    assert "facebooktheirs9" in names, "any account can be connected by anyone"
    assert "facebookmine1" in names
    # an unconnected sheet account of another manager is searchable too
    assert "facebookother77" in names


def test_the_buyer_no_longer_narrows_account_visibility(machine):
    """Owner hint is empty for the buyer's own accounts."""
    from src.web.app import _owner_hint
    assert _owner_hint(BUYER, BUYER) == ""
    assert _owner_hint("", BUYER) == ""
    assert _owner_hint("Dan Dicker", BUYER) == "owner in stats: Dan Dicker"


def test_home_map_shows_a_connected_account_of_another_stats_cm(machine):
    links.link("FacebookDoc30", "country", "Saudi Arabia")
    report = {"my_cm": BUYER, "countries": [{"country": "Saudi Arabia", "account_rows": []}]}
    view = _build_account_map_view(report)
    labels = {r["account"]
              for c in view["countries"] for r in c.get("rows", [])}
    assert "FacebookDoc30" in labels


def test_builder_lists_a_connected_account_of_another_stats_cm(machine):
    links.link("FacebookDoc30", "country", "Saudi Arabia")
    report = {"my_cm": BUYER, "countries": [{"country": "Saudi Arabia", "open_recs": {}}]}
    _add_linked_accounts_to_builder(report)
    assert "FacebookDoc30" in report["countries"][0]["open_recs"]


def test_stats_numbers_still_follow_the_selected_buyer():
    """Цифры по-прежнему считаются для выбранного CM: ключ периода и отчёт."""
    from src.web.app import _period_key
    a = _period_key({"my_cm": "A", "_cache_revision": 1}, "", "")
    b = _period_key({"my_cm": "B", "_cache_revision": 1}, "", "")
    assert a != b
    src = (ROOT / "src" / "web" / "app.py").read_text(encoding="utf-8")
    assert 'my_cm=report.get("my_cm") or get_selected_cm()' in src


def _post_open(profile_id="p1"):
    return {"data": {"profile_id": profile_id, "folder_id": "f", "instance": "A", "name": "n"}}


def test_a_second_open_of_the_same_profile_does_not_start(monkeypatch):
    """B3: один открывающий вызов на profile_id, второй отвечает сразу."""
    import threading
    from src.analysis_agent import account_spend

    gate, started = threading.Event(), []

    def slow_open(*a, **k):
        started.append(a[0])
        gate.wait(5)
        return True, "opened"

    monkeypatch.setattr(account_spend, "open_profile_for_automation", slow_open)
    monkeypatch.setattr(web_app, "OPEN_WAIT_S", 0.3)

    async def go():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as c:
            first = await c.post("/mlx-profile-open", **_post_open())
            second = await c.post("/mlx-profile-open", **_post_open())
            other = await c.post("/mlx-profile-open", **_post_open("p2"))
            gate.set()
            await asyncio.sleep(0.2)
            third = await c.post("/mlx-profile-open", **_post_open())
            return first, second, other, third

    first, second, other, third = asyncio.run(go())
    assert first.json()["timeout"] is True, "bounded server wait, same English message"
    assert "did not answer" in first.json()["message"]
    assert second.json().get("already_opening") is True and "already being opened" in second.json()["message"]
    assert started.count("p1") == 1 + 1, "p1 opened once while in flight, then again only after it finished"
    assert "p2" in started, "another profile is independent"
    assert third.json()["ok"] is True


needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


@needs_node
def test_connect_ui_empty_country_and_timeout():
    r = subprocess.run(["node", str(HARNESS), str(TEMPLATE)], capture_output=True, text=True,
                       encoding="utf-8", timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr


def test_manual_form_prefills_from_the_last_found_profile():
    html = TEMPLATE.read_text(encoding="utf-8")
    assert "AMAP_LAST_FOUND" in html and "found by search" in html
    assert "owner_hint" in html
