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
from typing import Iterable
from pathlib import Path
from contextlib import contextmanager
import threading

from config.paths import get_hidden_accounts_path, _atomic_write, machine_file_lock

_lock = threading.RLock()


@contextmanager
def _transaction():
    # Тот же машинный замок, что у targeting_registry (ACL-1495): ждёт с
    # пределом, EDEADLK не смертелен, файл замка не пишется.
    with _lock:
        lock_path = Path(get_hidden_accounts_path()).with_suffix(".lock")
        with machine_file_lock(lock_path):
            yield


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
    return {_key(x) for x in items if isinstance(x, str) and x.strip()}


def _save(labels: Iterable[str]) -> None:
    _atomic_write(get_hidden_accounts_path(),
                  json.dumps({"hidden": sorted(labels)}, ensure_ascii=False, indent=2).encode("utf-8"))


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
    with _transaction():
        current = load()
        if hidden:
            current.add(key)
        else:
            current.discard(key)
        _save(current)
        return current
