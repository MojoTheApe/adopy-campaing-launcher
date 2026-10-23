"""Написания папки профиля Multilogin (ACL-1184).

Жило в `analysis_agent.account_spend`, а проверка «профиль запущен» в
`browser.py` о втором написании не знала: дешборд и RPA отвечали на один
вопрос по-разному. Теперь написания здесь, и ими пользуется единственная
проверка — `MultiloginDirectCDPProvider.profile_is_running`.

Модуль лёгкий (без playwright): его зовёт дешборд на каждом чтении.
"""
from __future__ import annotations

import os
from typing import Callable


def spellings(path, short: "Callable[[str], str] | None" = None) -> list[str]:
    """Написания одной и той же папки, которые Windows считает равными.

    9 сентября чтение спенда X84 отказывало словами «профиль открыт вручную»
    сразу после того, как дешборд сам его и открыл. Прицепиться мешало
    единственное: **у папки два написания**. Multilogin запускает Mimic с
    коротким именем 8.3 —

        C:\\Users\\EXAMPL~1\\mlx\\profiles\\WORKSP~1\\FOLDER~1\\PROFIL~1

    — а мы вычисляем длинное, и проверка владения портом сравнивает их как
    строки. `os.path.abspath` и `normcase` короткое имя не разворачивают, и
    два имени одной папки расходятся.

    Проверять владение по папке — правильно: без этого можно прицепиться к
    браузеру **другого** аккаунта, а это хуже любого непрочитанного числа.
    Поэтому сравнение остаётся, а сюда добавляется второе написание.

    Разворачивать короткое в длинное было бы половиной решения: неизвестно,
    какое из двух придёт следующим, и на машине с выключенным 8.3 короткого
    имени нет вовсе. Поэтому возвращаются оба, без повторов и в понятном
    порядке — длинное первым. Пустой путь — пустой список.

    `short` — чем получать имя 8.3 (по умолчанию `short_path`).
    """
    out: list[str] = []
    text = str(path or "")
    if not text:
        return out
    for value in (text, (short or short_path)(text)):
        if value and value not in out:
            out.append(value)
    return out


def short_path(path: str) -> str:
    """Имя 8.3 для существующей папки — или пусто, если его нет.

    Пусто — обычный ответ, а не сбой: на томах с выключенным 8.3 короткого
    имени не существует, и тогда написание всего одно.
    """
    if os.name != "nt":
        return ""
    try:
        import ctypes

        buf = ctypes.create_unicode_buffer(4096)
        length = ctypes.windll.kernel32.GetShortPathNameW(str(path), buf, 4096)
        return buf.value if 0 < length < 4096 and buf.value != str(path) else ""
    except Exception:
        return ""
