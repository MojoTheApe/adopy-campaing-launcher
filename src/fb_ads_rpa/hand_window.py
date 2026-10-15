"""Окно профиля, открытое руками: переоткрыть сразу (ACL-1388 → ACL-1562).

С ACL-1320 окно профиля, открытое руками, робот не трогал: остановка
однажды потеряла только что созданную сессию Facebook, которую Multilogin
ещё не сохранил (живой прогон 26.09). ACL-1388/1427 разрешили закрывать его
самому, но только при условиях: окну не меньше 15 минут, оно не на переднем
плане, минуту не было ввода (и ждать этой тишины до 2 минут).

Решение владельца 04.10 (ACL-1562): эти условия и ожидание убраны. Окно,
открытое руками, закрывается и профиль поднимается с автоматизацией СРАЗУ —
на любом действии (Publish, Read spend, Read all, «open profile»). 04.10
кнопка «open profile» у Facebook_9943 ответила «close its window in
Multilogin» сразу после того, как владелец развернул окно, — условия мешали
работе.

Обязательное и неизменное (в `browser.py`): закрытие — только мягкая
остановка ЭТОГО профиля через MLX API; процесс не убивается; другие профили
не трогаются; простой браузер вне Multilogin не открывается.

Остался только машинный выключатель `FB_RPA_REOPEN_HAND_WINDOW=0` — прежнее
поведение «всегда стоп `profile_open_by_hand`». Тесты держат его выключенным
(tests/conftest.py), чтобы случайный тест не звал MLX живой машины.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Iterable

ENV_ENABLED = "FB_RPA_REOPEN_HAND_WINDOW"

# Причины — устойчивые метки для телеметрии и Sentry.
REOPEN = "reopen"
DISABLED = "disabled"


@dataclass(frozen=True)
class Verdict:
    """Итог: переоткрывать ли окно, открытое руками, и почему."""

    safe: bool
    reason: str

    def diag(self) -> dict:
        return {"reason": self.reason}


def enabled() -> bool:
    return (os.environ.get(ENV_ENABLED) or "").strip().lower() not in ("0", "false", "no", "off")


def assess(pids: Iterable[int] = ()) -> Verdict:
    """Переоткрывать ли окно профиля, открытое руками (ACL-1562).

    Без условий: возраст окна, передний план и тишина ввода больше не
    проверяются. «Нет» — только когда переоткрытие выключено на этой машине.
    `pids` оставлены в подписи ради вызывающих; решение от них не зависит.
    """
    if not enabled():
        return Verdict(False, DISABLED)
    return Verdict(True, REOPEN)
