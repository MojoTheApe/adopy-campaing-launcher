"""Передача управления человеку: кто сейчас держит мышь.

Когда RPA не может сделать шаг сама — например, клик по «Website», — она
останавливается и просит человека. Всё это время она обязана считать, что
управление у него: иначе две руки работают в одном браузере, и результат
непредсказуем для обеих.

Проверяется именно это соответствие: «жду человека» и «управление у человека»
не должны расходиться. Ровно на нём модуль и был сломан — присваивание без
`global` создавало локальную переменную, и флаг паузы никогда не выставлялся.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import pacing  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_state():
    """Состояние модульное и глобальное — каждый тест начинает с чистого."""
    pacing.release_takeover()
    pacing.end_teach_wait()
    yield
    pacing.release_takeover()
    pacing.end_teach_wait()


def test_takeover_is_off_until_somebody_asks():
    assert pacing.is_takeover_active() is False


def test_asking_and_releasing_flip_the_flag():
    pacing.request_takeover()
    assert pacing.is_takeover_active() is True
    pacing.release_takeover()
    assert pacing.is_takeover_active() is False


def test_waiting_for_the_human_means_the_human_has_control():
    """Главная проверка этого файла.

    `begin_teach_wait` объявляет, что RPA ждёт действия человека. Пока она
    ждёт, управление у него — иначе паузы в `pacing` не сработают, и RPA
    продолжит действовать в том же браузере, где в эту минуту кликает человек.

    До 8 сентября здесь было расхождение: `waiting` выставлялся, а
    `is_takeover_active()` оставался False. Причина — присваивание
    `_HUMAN_TAKEOVER` без объявления `global`: создавалась локальная
    переменная, а модульный флаг не менялся. Комментарий рядом обещал паузу,
    которой не было.
    """
    pacing.begin_teach_wait("website_click")
    assert pacing.get_teach_state()["waiting"] is True
    assert pacing.is_takeover_active() is True, (
        "RPA ждёт человека, но считает, что управление у неё — она продолжит "
        "действовать в том же браузере"
    )


def test_the_step_being_taught_is_remembered():
    pacing.begin_teach_wait("website_click")
    state = pacing.get_teach_state()
    assert state["step"] == "website_click"
    assert state["result"] == {}


def test_finishing_returns_control_to_the_rpa():
    pacing.begin_teach_wait("website_click")
    pacing.end_teach_wait({"selector": "a[href]"})
    state = pacing.get_teach_state()
    assert state["waiting"] is False
    assert state["step"] == ""
    assert state["result"] == {"selector": "a[href]"}
    assert pacing.is_takeover_active() is False, (
        "человек закончил, а RPA всё ещё считает себя отстранённой — прогон "
        "не сдвинется с места"
    )


def test_finishing_without_a_capture_clears_the_result():
    pacing.begin_teach_wait("website_click")
    pacing.end_teach_wait()
    assert pacing.get_teach_state()["result"] == {}


def test_a_new_wait_forgets_the_previous_capture():
    """Иначе прошлый ответ выдаётся за результат нового ожидания."""
    pacing.begin_teach_wait("first")
    pacing.end_teach_wait({"selector": "старое"})
    pacing.begin_teach_wait("second")
    assert pacing.get_teach_state()["result"] == {}
