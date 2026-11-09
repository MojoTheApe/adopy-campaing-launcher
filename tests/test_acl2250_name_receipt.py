"""ACL-2250: локальная квитанция имени кампании (copy 1, 2, 3 ...)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_api import name_receipt as nr  # noqa: E402

ACT = "act_123"


def test_missing_receipt_is_none():
    assert nr.load(ACT, "X") is None


def test_save_load_attach_and_delete():
    nr.save(ACT, "X", "X copy 2", 2)
    got = nr.load(ACT, "X")
    assert got == {"schema": 1, "act": ACT, "base": "X", "name": "X copy 2", "n": 2, "campaign_id": ""}
    nr.attach_id(ACT, "X", "555")
    assert nr.load(ACT, "X")["campaign_id"] == "555"
    nr.delete(ACT, "X")
    assert nr.load(ACT, "X") is None
    nr.delete(ACT, "X")                       # повторное удаление не ошибка


def test_save_overwrites_and_drops_old_id():
    nr.save(ACT, "X", "X", 0)
    nr.attach_id(ACT, "X", "1")
    nr.save(ACT, "X", "X copy 1", 1)
    assert nr.load(ACT, "X")["campaign_id"] == "" and nr.load(ACT, "X")["n"] == 1


def test_receipts_are_per_account_and_base():
    nr.save(ACT, "X", "X", 0)
    nr.save(ACT, "Y", "Y", 0)
    nr.save("act_9", "X", "X copy 1", 1)
    assert nr.load(ACT, "X")["name"] == "X" and nr.load(ACT, "Y")["name"] == "Y"
    assert nr.load("act_9", "X")["name"] == "X copy 1"


def test_attach_without_receipt_is_a_noop():
    nr.attach_id(ACT, "Z", "7")
    assert nr.load(ACT, "Z") is None


def test_unreadable_file_raises(tmp_path):
    nr.save(ACT, "X", "X", 0)
    nr.path_for(ACT, "X").write_text("{not json", encoding="utf-8")
    with pytest.raises(nr.NameReceiptError):
        nr.load(ACT, "X")


def test_wrong_shape_raises():
    nr.save(ACT, "X", "X", 0)
    nr.path_for(ACT, "X").write_text("[1]", encoding="utf-8")
    with pytest.raises(nr.NameReceiptError):
        nr.load(ACT, "X")


def test_unwritable_folder_raises(monkeypatch):
    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(nr, "_atomic_write", boom)
    with pytest.raises(nr.NameReceiptError):
        nr.save(ACT, "X", "X", 0)
