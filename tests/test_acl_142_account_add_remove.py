"""ACL-142: reading the account in-page for confirmation, and two removals that
cannot be mistaken for each other. No live Meta or Multilogin: everything is faked."""
from __future__ import annotations

import asyncio
import csv
import json
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.analysis_agent import account_spend as sp  # noqa: E402
from src.web import account_links  # noqa: E402
from src.web.app import account_map_remove  # noqa: E402

TPL = ROOT / "src" / "web" / "templates"


def test_the_dialog_and_connect_flow_behave_in_node():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required to execute dashboard scripts")
    r = subprocess.run(
        [node, str(ROOT / "tests" / "acl_142_account_dialog_harness.cjs"), str(TPL / "report.html")],
        text=True, capture_output=True, encoding="utf-8",
    )
    assert r.returncode == 0, r.stderr + r.stdout


def test_connect_no_longer_uses_a_browser_prompt_for_the_read_result():
    body = (TPL / "report.html").read_text(encoding="utf-8")
    start = body.index("async function amapConnect(")
    flow = body[start: body.index("function amapAddThisCountry(")]
    assert "amapAskAccount(" in flow
    assert "prompt(head, seed)" not in flow


def test_a_read_from_the_profile_is_marked_as_needing_confirmation(monkeypatch):
    class Page:
        def goto(self, *a, **k):
            return None

    class Conn:
        page = Page()

        def close(self, **k):
            return None

    monkeypatch.setattr(sp, "_probe_entry", lambda *a, **k: object())
    monkeypatch.setattr(sp, "_attach_only", lambda entry, label: Conn())
    monkeypatch.setattr(sp, "_act_from_url", lambda page, tries=20: "act_555")
    monkeypatch.setattr(sp, "_ads_manager_account_name", lambda page: "Default One")
    out = sp.read_ad_account("11111111-2222-4333-8444-555555555555", "f-1", "A")
    assert out["ok"] and out["act"] == "act_555"
    assert out["needs_confirmation"] is True, "the redirected default account is a proposal"


def _remove(body: dict):
    raw = json.dumps(body).encode()
    scope = {"type": "http", "method": "POST", "path": "/account-map-remove",
             "headers": [(b"content-type", b"application/json")]}

    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}

    async def run():
        from starlette.requests import Request
        return await account_map_remove(Request(scope, receive=receive))

    return json.loads(asyncio.run(run()).body)


@pytest.fixture()
def machine(tmp_path, monkeypatch):
    csv_path = tmp_path / "ad_account_map.csv"
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["account_name", "ad_account_id", "profile_id"])
        w.writeheader()
        w.writerow({"account_name": "Acc A", "ad_account_id": "act_1", "profile_id": "p1"})
        w.writerow({"account_name": "Acc B", "ad_account_id": "act_2", "profile_id": "p2"})
    monkeypatch.setattr(account_links, "links_path", lambda: tmp_path / "account_links.csv")
    account_links.link("Acc A", "country", "Chile")
    account_links.link("Acc A", "country", "Peru")
    account_links.link("Acc B", "country", "Chile")
    with patch("src.web.app.get_ad_account_map_path", return_value=str(csv_path)):
        yield csv_path


def _names(csv_path):
    with open(csv_path, encoding="utf-8") as f:
        return [r["account_name"] for r in csv.DictReader(f)]


def test_taking_an_account_out_of_a_country_keeps_it_in_acl(machine):
    assert account_links.unlink("Acc A", "country", "Chile") is True
    assert _names(machine) == ["Acc A", "Acc B"], "the map row stays"
    assert account_links.values_for("Acc A", "country") == ["Peru"], "other countries untouched"


def test_remove_from_acl_drops_the_row_and_every_placement(machine):
    out = _remove({"account_name": "Acc A", "also_unlink": True})
    assert out["ok"] and out["removed"] == ["Acc A"]
    assert {(u["kind"], u["value"]) for u in out["unlinked"]} == {("country", "Chile"), ("country", "Peru")}
    assert _names(machine) == ["Acc B"]
    assert account_links.values_for("Acc A", "country") == []
    assert account_links.values_for("Acc B", "country") == ["Chile"], "other accounts keep their place"


def test_the_plain_map_removal_is_unchanged(machine):
    out = _remove({"account_name": "Acc A"})
    assert out["ok"] and out["unlinked"] == []
    assert account_links.values_for("Acc A", "country") == ["Chile", "Peru"]


def test_the_country_row_offers_both_actions_in_plain_words():
    row = (TPL / "_account_map_country.html").read_text(encoding="utf-8")
    assert "amapUnlink(" in row and "amapRemoveFromAcl(" in row
    assert "Remove from ACL</button>" in row
    page = (TPL / "report.html").read_text(encoding="utf-8")
    start = page.index("function amapUnlink(")
    unlink = page[start: page.index("function amapRemoveFromAcl(")]
    assert "confirm(" in unlink and "moves to Available accounts" in unlink
    removal = page[page.index("function amapRemoveFromAcl("): page.index("function amapAccountModel(")]
    assert "confirm(" in removal and "also_unlink: true" in removal
    assert "shared sheet" in removal and "Multilogin profile" in removal
