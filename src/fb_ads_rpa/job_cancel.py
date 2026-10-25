"""Кооперативная остановка задания (ACL-1614).

Кнопка Stop раньше рвала соединение Playwright из чужого потока: поток
задания, стоявший внутри `page.evaluate`, после этого бесконечно крутил
sync-цикл Playwright, не отпуская GIL, и весь дешборд тормозил (05.10, задание
1ad447f9). Теперь Stop только ставит флаг (`threading.Event`) — соединение и
браузер закрывает сам поток задания, когда заметит флаг.

Поток задания привязывает свой Event (`bind`); длинные ожидания и опросы
зовут `check()` — он бросает `JobCancelled`. От `BaseException`, как
`FlowBudgetExceeded`: мягкие отказы шагов ловят `except Exception` и пошли бы
дальше после Stop. Вне задания (поток не привязан) `check()` ничего не делает.
Стоимость проверки — одно чтение thread-local и `Event.is_set()`.
"""
from __future__ import annotations

import threading


class JobCancelled(BaseException):
    """Задание остановлено кнопкой Stop; поток разматывается обычным путём."""


_tls = threading.local()


def bind(event: "threading.Event | None", job_id: str | None = None) -> None:
    """Привязать Event отмены к ЭТОМУ потоку (вызывает поток задания).

    ACL-1142: `job_id` — чьё это задание; по нему `pacing` ведёт состояние
    обучения (teach) отдельно для каждого задания.
    """
    _tls.event = event
    _tls.job_id = job_id


def unbind() -> None:
    _tls.event = None
    _tls.job_id = None


def current_job_id() -> str | None:
    """Задание, которому принадлежит этот поток, или None (вне задания)."""
    return getattr(_tls, "job_id", None)


def requested() -> bool:
    event = getattr(_tls, "event", None)
    return event is not None and event.is_set()


def heartbeat() -> None:
    """Известное тихое ожидание (ожидание человека, сжатие видео): задание живо.

    Строк телеметрии такие ожидания не пишут, и сторож `step_stuck` счёл бы их
    зависанием. Heartbeat обновляет время последней активности задания, не
    меняя названия последнего шага. Вне задания — ничего не делает.
    """
    if getattr(_tls, "event", None) is None:
        return
    try:
        from src.fb_ads_rpa import run_telemetry

        run_telemetry.beat()
    except Exception:
        pass


def check() -> None:
    """Бросить `JobCancelled`, если Stop нажат. Дёшево: зовётся в каждом цикле ожидания."""
    event = getattr(_tls, "event", None)
    if event is not None and event.is_set():
        raise JobCancelled("Stopped by the user")
