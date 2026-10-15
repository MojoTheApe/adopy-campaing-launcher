"""A brief file hold on Windows no longer loses a spend reading (ACL-1466).

`_save_all` подменял файл показаний голым `os.replace`. Windows на миг держит
файл (прошлая подмена, Defender, индексатор) и отвечает WinError 5 — показание
Read терялось, а тест двенадцати одновременных записей мигал (10 падений с
15.09). Теперь подмена ждёт, как у файла связей (ACL-1216).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import paths  # noqa: E402
from src.analysis_agent import account_spend as sp  # noqa: E402

DAY = "2026-09-30"


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(sp, "_store_path", lambda: tmp_path / "account_spend.json")
    monkeypatch.setattr(paths, "_REPLACE_PAUSE", 0)
    return tmp_path


def _flaky_replace(monkeypatch, fail_times: int):
    real = os.replace
    calls = {"n": 0}

    def fake(src, dst):
        calls["n"] += 1
        if calls["n"] <= fail_times:
            raise PermissionError(5, "Access is denied")
        return real(src, dst)

    monkeypatch.setattr(paths.os, "replace", fake)
    return calls


def test_a_brief_hold_does_not_lose_the_reading(store, monkeypatch):
    calls = _flaky_replace(monkeypatch, fail_times=3)
    sp._put(sp.SpendReading(account="Doc20", date_from=DAY, date_to=DAY, spend=12.5, state="ok", status="disabled"))
    assert calls["n"] == 4, "подмена не повторялась"
    assert sp.get_reading("Doc20", DAY, DAY).spend == 12.5
    assert sp.get_status("Doc20")["status"] == "disabled"
    assert not list(store.glob("*.tmp")), "временный файл остался"


def test_a_file_that_stays_held_still_fails_loudly(store, monkeypatch):
    _flaky_replace(monkeypatch, fail_times=10_000)
    with pytest.raises(PermissionError):
        sp._save_all({"x": {"a": 1}})
    assert not list(store.glob("*.tmp")), "временный файл остался после отказа"
