"""Аккаунты, убранные байером с глаз на этой машине.

Во вкладке Accounts двенадцать строк, а работать можно с двумя. Остальные
пришли из общей таблицы: там аккаунт есть, на этой машине про него не
записано ничего. Удалить их нельзя — удалять нечего, локальной записи не
существует, а вычеркнуть строку из общей таблицы дешборд не вправе: она
вернётся на следующем обновлении.

Поэтому не «удалить», а «спрятать». Это настройка вида и только она:
аккаунт остаётся в таблице, сохраняет свои цифры и работает везде, где
работал. Спрятанное видно счётчиком и возвращается одним нажатием —
список, молча глотающий строки, хуже длинного списка.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Iterable

from config.paths import get_hidden_accounts_path

log = logging.getLogger(__name__)


def _key(label: str) -> str:
    """Сравниваем прощающе: один и тот же аккаунт приходит из таблицы и из
    карты с разным регистром и лишними пробелами, а спрятанным он должен
    быть в обоих случаях."""
    return " ".join(str(label or "").split()).casefold()


def load() -> set[str]:
    """Спрятанные ключи. Битый или отсутствующий файл — пустой набор:
    не показать список из-за испорченной настройки вида хуже, чем показать
    его целиком."""
    path = get_hidden_accounts_path()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return set()
    items = data.get("hidden") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return set()
    return {_key(x) for x in items if str(x or "").strip()}


def _save(labels: Iterable[str]) -> None:
    path = get_hidden_accounts_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"hidden": sorted(labels)}, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)          # либо целый файл, либо прежний


def is_hidden(label: str, hidden: set[str] | None = None) -> bool:
    return _key(label) in (load() if hidden is None else hidden)


def set_hidden(label: str, hidden: bool) -> set[str]:
    """Спрятать или вернуть один аккаунт. Отдаёт новый набор ключей.

    Хранятся ключи, а не исходные подписи: по ним и сравнение. Подпись в
    таблице могут поправить — регистром, пробелом, — и спрятанное не должно
    от этого выныривать обратно.
    """
    key = _key(label)
    if not key:
        return load()
    current = load()
    if hidden:
        current.add(key)
    else:
        current.discard(key)
    try:
        _save(current)
    except OSError as exc:
        # Настройка вида не стоит того, чтобы ронять страницу аккаунтов.
        log.warning("Не удалось записать список спрятанных аккаунтов: %s", exc)
    return current
