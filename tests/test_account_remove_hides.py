"""Remove in Accounts = remove from ACL: map row gone, the account hidden here.

Reuses the per-machine hidden flag (src/web/hidden_accounts.py) that
/api/accounts-hidden sets, so a sheet-only account also leaves the list.
"""
import csv

import pytest
from fastapi.testclient import TestClient

from src.web import hidden_accounts


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ACL_CONFIG_DIR", str(tmp_path))
    path = tmp_path / "ad_account_map.csv"
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["account_name", "ad_account_id"])
        w.writeheader()
        w.writerow({"account_name": "Acc One", "ad_account_id": "act_1234567890"})
    monkeypatch.setattr("src.web.app.get_ad_account_map_path", lambda: str(path))
    from src.web import app as web

    return TestClient(web.app, base_url="http://localhost"), path


def test_remove_drops_row_and_hides(client):
    c, path = client
    r = c.post("/account-map-remove", json={"account_name": "Acc One"}).json()
    assert r["ok"] and r["removed"] == ["Acc One"]
    assert "Acc One" not in path.read_text(encoding="utf-8")
    assert hidden_accounts.is_hidden("acc one")


def test_remove_sheet_only_account_is_hidden(client):
    c, _ = client
    r = c.post("/account-map-remove", json={"account_name": "Sheet Only"}).json()
    assert r["ok"] and r["removed"] == [] and r["missing"] == ["Sheet Only"]
    assert hidden_accounts.is_hidden("Sheet Only")


def test_patch_unhides(client):
    c, _ = client
    c.post("/account-map-remove", json={"account_name": "Acc One"})
    assert hidden_accounts.is_hidden("Acc One")
    assert c.post("/account-map-patch", json={"account_name": "Acc One"}).json()["ok"]
    assert not hidden_accounts.is_hidden("Acc One")
