# -*- coding: utf-8 -*-
"""Укороченные паузы и выключатель к ним (ACL-1284).

Решение владельца 21.09: крупные паузы между действиями были длиннее нужного
— «All edits saved» уже на экране, а робот всё ещё выжидает, — и укорочены
примерно вдвое. Это значит, что робот стучится в живой кабинет вдвое чаще, и
единственный способ откатиться, не трогая код, — `FB_RPA_SLOW_PACING=1`.

Ревью 24.09: у этого выключателя не было ни одной проверки, а забыть его при
следующей правке пауз ничего не стоит. Если аккаунт начнёт получать
предупреждения, вернуть прежний темп надо будет в ту же минуту.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import pacing  # noqa: E402

#: Пауза -> (быстрый диапазон, медленный). Медленный — тот, что был до 21.09.
PAUSES = {
    "pause_short": ((0.25, 0.6), (0.4, 0.9)),
    "pause_medium": ((0.6, 1.3), (1.0, 2.2)),
    "pause_long": ((1.0, 1.8), (2.5, 4.5)),
    "pause_after_navigation": ((1.0, 1.7), (2.0, 3.5)),
}


@pytest.fixture
def slept(monkeypatch):
    """Ничего не ждём по-настоящему — записываем, сколько собирались."""
    out: list[float] = []
    monkeypatch.setattr(pacing.time, "sleep", lambda s: out.append(s))
    monkeypatch.setattr(pacing, "check_human_takeover", lambda page=None: False)
    monkeypatch.delenv("FB_RPA_SLOW_PACING", raising=False)
    return out


@pytest.mark.parametrize("name", sorted(PAUSES))
def test_the_pause_is_the_short_one_by_default(slept, name):
    fast, _ = PAUSES[name]
    for _ in range(40):
        getattr(pacing, name)()
    assert slept, f"{name} не спала вовсе"
    assert min(slept) >= fast[0] - 1e-9 and max(slept) <= fast[1] + 1e-9, (
        f"{name}: {min(slept):.2f}…{max(slept):.2f} вместо {fast}"
    )


@pytest.mark.parametrize("name", sorted(PAUSES))
def test_the_old_pace_comes_back_with_the_switch(slept, monkeypatch, name):
    """Выключатель возвращает ровно то, что было до 21.09."""
    monkeypatch.setenv("FB_RPA_SLOW_PACING", "1")
    slow = PAUSES[name][1]
    for _ in range(40):
        getattr(pacing, name)()
    assert min(slept) >= slow[0] - 1e-9 and max(slept) <= slow[1] + 1e-9, (
        f"{name}: {min(slept):.2f}…{max(slept):.2f} вместо {slow}"
    )


@pytest.mark.parametrize("value", ["1", "true", "yes"])
def test_the_switch_answers_to_the_words_people_type(monkeypatch, value):
    monkeypatch.setenv("FB_RPA_SLOW_PACING", value)
    assert pacing._slow() is True


@pytest.mark.parametrize("value", ["", "0", "no", "ДА"])
def test_anything_else_leaves_the_short_pace(monkeypatch, value):
    monkeypatch.setenv("FB_RPA_SLOW_PACING", value)
    assert pacing._slow() is False


def test_every_pause_asks_about_a_human_at_the_keyboard(slept, monkeypatch):
    """Человек, взявший управление, останавливает робота на любой паузе —
    укорачивание не должно было это потерять."""
    asked: list[str] = []
    monkeypatch.setattr(pacing, "check_human_takeover",
                        lambda page=None: asked.append("asked") or False)
    for name in PAUSES:
        getattr(pacing, name)()
    assert len(asked) == len(PAUSES)
