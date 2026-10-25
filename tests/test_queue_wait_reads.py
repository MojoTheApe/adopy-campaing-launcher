"""Очередь на профиль (17.10): Страницы из кэша на машине.

На 9095 Check plan по B90 стоял за чтением Страниц того же профиля и через 120 с
падал с «This profile is busy in another ACM operation». Чтение Страниц открывало
профиль при каждой загрузке билдера.
"""
from __future__ import annotations

import time

import pytest

from src.web import account_pages as ap


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(ap, "_cache_path", lambda: tmp_path / "account_pages_cache.json")
    return tmp_path


@pytest.fixture
def reads(monkeypatch):
    calls = []

    def fake_read(account):
        calls.append(account)
        return {"ok": True, "state": "ready", "pages": [{"id": "111", "name": "P"}], "account": account}

    from src.analysis_agent import account_spend as accounts

    monkeypatch.setattr(accounts, "serialized_browser", lambda **kw: (lambda fn: fake_read))
    return calls


def test_a_second_read_comes_from_the_cache_without_the_profile(cache_dir, reads):
    first = ap.read_pages("Doc120")
    again = ap.read_pages("Doc120")
    assert reads == ["Doc120"]
    assert first["pages"] == again["pages"] and "cached_at" in again


def test_refresh_pages_goes_past_the_cache(cache_dir, reads):
    ap.read_pages("Doc120")
    ap.read_pages("Doc120", force=True)
    assert reads == ["Doc120", "Doc120"]


def test_a_stale_entry_is_read_again(cache_dir, reads):
    ap.remember_pages("Doc120", {"ok": True, "state": "ready", "pages": []}, now=time.time() - ap.CACHE_TTL_S - 1)
    ap.read_pages("Doc120")
    assert reads == ["Doc120"]


def test_a_failed_read_is_not_cached(cache_dir, monkeypatch):
    from src.analysis_agent import account_spend as accounts

    calls = []

    def boom(account):
        calls.append(account)
        raise RuntimeError("profile did not open")

    monkeypatch.setattr(accounts, "serialized_browser", lambda **kw: (lambda fn: boom))
    assert ap.read_pages("Doc120")["ok"] is False
    assert ap.read_pages("Doc120")["ok"] is False
    assert calls == ["Doc120", "Doc120"]
    assert ap.cached_pages("Doc120") is None
