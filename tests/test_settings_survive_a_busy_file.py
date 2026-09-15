# -*- coding: utf-8 -*-
"""Правка оператора не теряется, если файл в этот момент кем-то открыт.

Настройки ACL пишутся из нескольких процессов сразу: дешборд, RPA, вторая
машина. Запись сделана атомарной — во временный файл и `os.replace` поверх
цели, — и на POSIX этого достаточно. На Windows нет: `os.replace` падает с
`PermissionError [WinError 5]`, если цель в этот момент просто открыта, хотя
никто её намеренно не блокировал.

Поймано полным прогоном 15.09: `test_processes_preserve_each_update` уронил
`hidden_accounts.json` — то есть оператор спрятал бы аккаунт, а тот остался бы
на экране. Проверки ниже подменяют `os.replace`, а не ловят живую гонку:
воспроизводить её таймингом означало бы мигающий тест, а мигающий тест учит
людей не смотреть.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import paths  # noqa: E402

_REPLACE = os.replace


class _BusyThenFree:
    """`os.replace`, который отказывает первые `times` раз."""

    def __init__(self, times: int):
        self.left = times
        self.calls = 0

    def __call__(self, temp, target):
        self.calls += 1
        if self.left > 0:
            self.left -= 1
            raise PermissionError(5, "Access is denied")
        _REPLACE(temp, target)


def test_a_briefly_busy_target_does_not_lose_the_write(monkeypatch, tmp_path):
    replace = _BusyThenFree(times=3)
    monkeypatch.setattr(paths.os, "replace", replace)
    monkeypatch.setattr(paths, "_REPLACE_PAUSE", 0)

    target = tmp_path / "hidden_accounts.json"
    target.write_bytes(b'{"hidden": []}')
    paths._atomic_write(target, b'{"hidden": ["KLD_BFL_X84"]}')

    assert target.read_bytes() == b'{"hidden": ["KLD_BFL_X84"]}'
    assert replace.calls == 4


def test_a_target_held_for_good_still_fails(monkeypatch, tmp_path):
    """Ждать вечно — хуже, чем упасть: оператор должен увидеть отказ."""
    replace = _BusyThenFree(times=10_000)
    monkeypatch.setattr(paths.os, "replace", replace)
    monkeypatch.setattr(paths, "_REPLACE_PAUSE", 0)

    target = tmp_path / "settings.json"
    target.write_bytes(b'{"saved": true}')
    with pytest.raises(PermissionError):
        paths._atomic_write(target, b"{}")
    assert target.read_bytes() == b'{"saved": true}'

    assert replace.calls == paths._REPLACE_ATTEMPTS


def test_nothing_is_left_behind_when_the_write_gives_up(monkeypatch, tmp_path):
    """Иначе папка настроек зарастает `.tmp` от каждой неудачной попытки."""
    monkeypatch.setattr(paths.os, "replace", _BusyThenFree(times=10_000))
    monkeypatch.setattr(paths, "_REPLACE_PAUSE", 0)

    with pytest.raises(PermissionError):
        paths._atomic_write(tmp_path / "settings.json", b"{}")

    assert list(tmp_path.iterdir()) == []


def test_an_unrelated_error_is_not_retried(monkeypatch, tmp_path):
    """Ждать имеет смысл только чужого дескриптора.

    `IsADirectoryError`, нехватка места, сломанный путь — от ожидания не
    исправятся, а повтор растянул бы отказ и спрятал бы его причину.
    """
    calls = {"n": 0}

    def _broken(_temp, _target):
        calls["n"] += 1
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(paths.os, "replace", _broken)
    with pytest.raises(OSError):
        paths._atomic_write(tmp_path / "settings.json", b"{}")
    assert calls["n"] == 1
