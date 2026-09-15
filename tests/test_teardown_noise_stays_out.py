# -*- coding: utf-8 -*-
"""Шум закрытия соединений не топит настоящие отказы (ACL-354).

15.09 за один вечер приёмник получил три вида системного шума подряд:

    PYTHON-N  CancelledError        подтверждение кадра скринкаста
    PYTHON-Q  TargetClosedError     брошенный Future после закрытия страницы
    PYTHON-R  ConnectionResetError  [WinError 10054] при гашении сокета

Все три помечены `handled`, ни одна не влияет на прогон: это цена того, что
браузер и вкладки закрываются, пока цикл событий ещё что-то доделывает. На
одной машине — мелочь. У десяти байтеров такой шум утопит настоящие отказы
шагов, ровно как чуть раньше их топили выдуманные отказы из тестов.

Ключевое решение здесь — **фильтровать по виду исключения, а не по логгеру**.
Через тот же логгер `asyncio` приходит `RuntimeError: loop is not the running
loop`, от которого дешборд в тот день умирал дважды (ACL-1163). Заглушить
логгер значило бы спрятать единственный вредный случай вместе с безобидными.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import acl_sentry  # noqa: E402


def _event(logger: str, *types: str) -> dict:
    return {
        "logger": logger,
        "exception": {"values": [{"type": t, "value": t} for t in types]},
    }


@pytest.mark.parametrize("kind", [
    "CancelledError",
    "ConnectionResetError",
    "TargetClosedError",
    "BrokenPipeError",
])
def test_the_three_kinds_seen_that_night_are_dropped(kind):
    assert acl_sentry._before_send(_event("asyncio", kind), {}) is None


def test_the_one_that_killed_the_dashboard_still_gets_through():
    """`loop is not the running loop` приходит тем же логгером, и это ACL-1163.

    Если однажды его тоже отфильтруют «за компанию», этот тест скажет об этом
    раньше, чем мы перестанем замечать падения дешборда.
    """
    event = _event("asyncio", "RuntimeError")
    assert acl_sentry._before_send(event, {}) is not None


def test_the_same_exception_from_our_own_code_is_kept():
    """Фильтр — про шум закрытия, а не про тип исключения вообще.

    `ConnectionResetError` из нашего кода (упал запрос к Ads Manager) — это
    настоящая поломка, и молчать о ней нельзя.
    """
    event = _event("src.fb_ads_rpa.browser", "ConnectionResetError")
    assert acl_sentry._before_send(event, {}) is not None


def test_a_mixed_chain_is_kept():
    """Цепочка, где рядом с шумом лежит что-то своё, уезжает целиком:
    выбрасывать её значило бы терять настоящую причину."""
    event = _event("asyncio", "CancelledError", "ValueError")
    assert acl_sentry._before_send(event, {}) is not None


def test_step_failures_are_untouched():
    """Отказы шагов приходят без исключения вовсе — их фильтр не касается."""
    event = {"message": "placements: still-loading", "logger": "acl"}
    assert acl_sentry._before_send(event, {}) is not None


def test_the_manual_report_is_untouched():
    event = {"message": "Report: кампания встала", "logger": None}
    assert acl_sentry._before_send(event, {}) is not None
