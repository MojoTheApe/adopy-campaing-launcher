# -*- coding: utf-8 -*-
"""Подтверждение кадра скринкаста не шумит, когда сессия закрывается.

15.09 в Sentry приехало первое настоящее исключение со стеком — PYTHON-N,
`CancelledError` в `browser._ack`. Среди двадцати двух тикетов за сутки оно
было единственным не нашим сообщением о шаге, и причина оказалась учебной:

    except Exception:   # НЕ ловит CancelledError

С Python 3.8 `asyncio.CancelledError` наследуется от `BaseException`, а не от
`Exception`. Когда сессия закрывается под нами — кончился прогон, закрыли
профиль, — подтверждение кадра отменяется на полпути, исключение улетает в
машинерию asyncio, и в лог падает «Exception in callback».

Сам скринкаст важен: он заставляет Chrome рисовать кадры в свёрнутом окне
(замер 30.07 — rAF 26/с против 43.6/с со скринкастом). А вот подтверждение
кадра необязательно: не дошло — не дошло.
"""
from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SOURCE = (ROOT / "src" / "fb_ads_rpa" / "browser.py").read_text(encoding="utf-8")


def test_cancelled_error_is_a_base_exception():
    """Основание починки, записанное как проверка.

    Если однажды это перестанет быть правдой, `except Exception` снова станет
    достаточным — и тогда этот тест первым скажет, что комментарий устарел.
    """
    assert issubclass(asyncio.CancelledError, BaseException)
    assert not issubclass(asyncio.CancelledError, Exception)


def test_the_ack_catches_more_than_exception():
    """Проверяется текстом, и это осознанно.

    `_ack` — замыкание внутри функции, которой нужна живая CDP-сессия: чтобы
    выполнить его в тесте, пришлось бы поднять браузер. Поэтому проверяем
    форму защиты, а не её срабатывание. Слабее, чем хотелось бы, но лучше,
    чем ничего: вернуть прежний `except Exception` теперь нельзя молча.
    """
    block = re.search(
        r"def _ack\(params: dict\) -> None:.*?\n(?=\s{8}session\.on)",
        SOURCE, re.S,
    )
    assert block, "функция _ack переехала — проверку надо поправить, а не удалить"
    body = block.group(0)
    assert "CancelledError" in body, "CancelledError снова не ловится"
    assert "except Exception:\n" not in body, "вернулся except, который его не ловит"


def test_the_screencast_itself_is_still_there():
    """Скринкаст — не украшение: без него свёрнутое окно тормозит шаги в разы.

    Если кто-то решит убрать его вместе с шумом в логе, пусть этот тест
    напомнит, чем за тишину заплачено.
    """
    assert "Page.startScreencast" in SOURCE
    assert "Page.screencastFrameAck" in SOURCE
