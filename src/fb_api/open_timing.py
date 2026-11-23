"""ACL-2822: одна сводка на каждое открытие профиля — аккаунт, слот, исход, секунды по шагам.

Сеть не трогает и ничего не ждёт: только часы и запись в локальный журнал событий
(`acl_event_log.emit` не бросает исключений). Открытие через `warm.open_one` заводит
замер само и вкладывает в него шаги `profile.open_profile`; чтение и публикация идут
прямо в `open_profile` — там замер заводится, если внешнего нет. Так на одно открытие
выходит ровно одно событие.
"""
from __future__ import annotations

import contextlib
import contextvars
import time
from typing import Callable, Iterator

EVENT_TYPE = "profile.open"

_current: contextvars.ContextVar["OpenTimer | None"] = contextvars.ContextVar("acl_open_timer", default=None)


class OpenTimer:
    def __init__(self, account: str, slot: str, path: str, clock: Callable[[], float] = time.monotonic):
        self.account = account
        self.slot = slot
        self.path = path
        self._clock = clock
        self._began = clock()
        self.steps: dict[str, float] = {}
        self._emitted = False

    @contextlib.contextmanager
    def step(self, name: str) -> Iterator[None]:
        began = self._clock()
        try:
            yield
        finally:
            # Повторный шаг (например, второй опрос) складывается в одну строку.
            self.steps[name] = round(self.steps.get(name, 0.0) + (self._clock() - began), 3)

    def payload(self, outcome: str) -> dict:
        return {"slot": self.slot, "path": self.path, "outcome": outcome or "unknown",
                "total_s": round(self._clock() - self._began, 3), "steps": dict(self.steps)}

    def emit(self, outcome: str) -> None:
        if self._emitted:
            return
        self._emitted = True
        try:
            from src.acl_event_log import emit

            emit(EVENT_TYPE, self.payload(outcome), account=self.account or None)
        except Exception:  # noqa: BLE001 — журнал не должен ронять открытие
            pass


def current() -> "OpenTimer | None":
    return _current.get()


@contextlib.contextmanager
def activate(timer: OpenTimer) -> Iterator[OpenTimer]:
    token = _current.set(timer)
    try:
        yield timer
    finally:
        _current.reset(token)


@contextlib.contextmanager
def step(name: str) -> Iterator[None]:
    """Шаг внешнего замера, если он есть; иначе ничего не делает."""
    timer = _current.get()
    if timer is None:
        yield
        return
    with timer.step(name):
        yield
