# -*- coding: utf-8 -*-
"""Запись связей ждёт, пока Windows отпустит файл, а не падает с первого отказа.

16.09 полный набор один раз упал в `test_country_live_totals`:

    src\\web\\account_links.py:90: in _write
        os.replace(temp, path)
    PermissionError: [WinError 5] Access is denied: '….tmp' -> '…account_links.csv'

Девять связей подряд — девять перезаписей одного файла, и на Windows
`os.replace` отвечает `PermissionError`, когда цель лишь на миг открыта: ещё
не закрытой ручкой прошлой замены, Defender'ом, индексатором. Тот же класс,
что `config/paths.py` уже лечит для файла настроек ожиданием
(`_replace_when_free`). Это не тест мигал — это оператор, быстро кликающий
на карте аккаунтов, получил бы ошибку на записи, которая удалась бы через
50 мс.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import paths  # noqa: E402
from src.web import account_links as links  # noqa: E402


@pytest.fixture
def store(monkeypatch, tmp_path):
    monkeypatch.setattr(links, "get_local_config_dir", lambda: str(tmp_path))
    monkeypatch.setattr(paths, "_REPLACE_PAUSE", 0.0)
    return tmp_path / "account_links.csv"


def test_a_momentary_refusal_is_waited_out(store, monkeypatch):
    real = links.os.replace
    refusals = {"left": 2}

    def flaky(src, dst):
        if refusals["left"]:
            refusals["left"] -= 1
            raise PermissionError(5, "Access is denied")
        real(src, dst)

    monkeypatch.setattr(paths.os, "replace", flaky)
    assert links.link("Acc", "country", "KW") is True
    assert links.values_for("Acc", "country") == ["KW"]
    assert refusals["left"] == 0
    assert not list(store.parent.glob(".*.tmp")), "временный файл остался"


def test_a_file_that_never_frees_still_raises(store, monkeypatch):
    def never(src, dst):
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(paths.os, "replace", never)
    with pytest.raises(PermissionError):
        links.link("Acc", "country", "KW")
    assert not list(store.parent.glob(".*.tmp"))


def test_nine_links_in_a_row_land(store):
    """Ровно то, что делал упавший тест."""
    for name in "ABCDEFGHI":
        assert links.link(name, "country", "KW") is True
    assert links.accounts_for("country", "KW") == list("ABCDEFGHI")
