"""Аккаунт, убранный кнопкой Remove на Accounts, не должен быть в билдере (ACL-2495)."""
from __future__ import annotations

from src.analysis_agent import morning_service as ms
from src.web import hidden_accounts


def _rows(*labels):
    return [{"account_label": label, "alias": label, "country_code": "KW"} for label in labels]


def _labels(rows):
    return sorted(r["account_label"] for r in rows)


def test_removed_account_is_not_in_the_builder(tmp_path, monkeypatch):
    monkeypatch.setattr(hidden_accounts, "get_hidden_accounts_path", lambda: str(tmp_path / "hidden_accounts.json"))
    hidden_accounts.set_hidden("FacebookMKTOBFLV97 KW", True)
    out = ms._drop_hidden_accounts(_rows("facebookmktobflv97  kw", "W72 KW", "Other"))
    assert _labels(out) == ["Other", "W72 KW"]


def test_restored_account_is_listed_again(tmp_path, monkeypatch):
    monkeypatch.setattr(hidden_accounts, "get_hidden_accounts_path", lambda: str(tmp_path / "hidden_accounts.json"))
    hidden_accounts.set_hidden("W72 KW", True)
    hidden_accounts.set_hidden("W72 KW", False)
    assert _labels(ms._drop_hidden_accounts(_rows("W72 KW", "Other"))) == ["Other", "W72 KW"]


def test_accounts_never_removed_are_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr(hidden_accounts, "get_hidden_accounts_path", lambda: str(tmp_path / "hidden_accounts.json"))
    rows = _rows("A", "B")
    assert ms._drop_hidden_accounts(rows) == rows
