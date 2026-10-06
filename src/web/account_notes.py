"""Заметки менеджера к аккаунтам карты (ACL-1620).

Колонка Setup на карте аккаунтов никому не была нужна; на её месте теперь
свободный текст, который менеджер пишет про аккаунт сам. Хранится по подписи
аккаунта (ровно так, как она показана в строке) в машинной папке
`%LOCALAPPDATA%\ACL\account_notes.json`: заметки одного байера не должны
становиться заметками всех, поэтому в гит файл не попадает.

Запись атомарная (временный файл + replace), чтение-изменение-запись идёт под
машинным замком — два одновременных сохранения не портят JSON и не теряют
чужую заметку.
"""
from __future__ import annotations

import json
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from config.paths import get_account_notes_path, _atomic_write, machine_file_lock

#: Потолок длины одной заметки (символов) — «разумный», как в Issue.
MAX_NOTE_LEN = 10_000
#: Потолок подписи аккаунта: подпись — ключ, а не текст.
MAX_LABEL_LEN = 300

_lock = threading.RLock()


@contextmanager
def _transaction():
    with _lock:
        lock_path = Path(get_account_notes_path()).with_suffix(".lock")
        with machine_file_lock(lock_path):
            yield


def load() -> dict[str, str]:
    """Все заметки. Битый или отсутствующий файл — пустой словарь: страница
    не должна падать из-за испорченной заметки."""
    try:
        with open(get_account_notes_path(), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    items = data.get("notes") if isinstance(data, dict) else None
    if not isinstance(items, dict):
        return {}
    return {k: v for k, v in items.items() if isinstance(k, str) and isinstance(v, str) and v.strip()}


def _load_strict() -> dict[str, str]:
    """Чтение для записи: ничего не теряет молча.

    Нет файла — пусто. Файл есть, но не читается (OSError) — отказ: запись
    поверх стёрла бы все остальные заметки. Файл испорчен — откладываем его
    в `account_notes.json.corrupt-<время>` и начинаем с пустого."""
    path = Path(get_account_notes_path())
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    try:
        data = json.loads(raw)
        items = data.get("notes") if isinstance(data, dict) else None
        if not isinstance(items, dict):
            raise ValueError("bad shape")
    except ValueError:
        backup = path.with_name(f"{path.name}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}")
        path.replace(backup)
        return {}
    return {k: v for k, v in items.items() if isinstance(k, str) and isinstance(v, str) and v.strip()}


def _save(notes: dict[str, str]) -> None:
    _atomic_write(get_account_notes_path(),
                  json.dumps({"notes": notes}, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8"))


def get(account: str) -> str:
    return load().get(account, "")


def set_note(account: str, text: str) -> dict[str, str]:
    """Сохранить заметку; пустой текст удаляет её. Отдаёт все заметки.

    Подпись — ровно как в строке (только края обрезаются). Текст не
    нормализуем: переводы строк и пробелы внутри — часть заметки.
    ValueError — подпись пуста или слишком длинна, либо текст длиннее потолка.
    """
    label = (account or "").strip()
    if not label or len(label) > MAX_LABEL_LEN:
        raise ValueError("account is required")
    if len(text) > MAX_NOTE_LEN:
        raise ValueError(f"note is longer than {MAX_NOTE_LEN} characters")
    with _transaction():
        notes = _load_strict()
        if text.strip():
            notes[label] = text
        else:
            notes.pop(label, None)
        _save(notes)
        return notes
