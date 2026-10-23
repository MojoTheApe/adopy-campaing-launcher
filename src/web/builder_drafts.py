"""Черновики билдера на машине дашборда (ACL-1681).

Раньше черновики жили только в localStorage браузера: он свой у каждого
профиля Chrome и у каждого адреса (`127.0.0.1` и `localhost` — разные сайты),
а после краха Chrome владелец открыл билдер и увидел пустоту. Теперь копия
лежит в машинной папке `%LOCALAPPDATA%/ACL/builder_drafts.json`, как заметки
аккаунтов (account_notes.py); localStorage остаётся быстрым кэшем.

Три документа: `keep` (карточки кампаний), `jobs` (память заданий публикации),
`ui` (ящик открыт / растянут). У каждого своя метка `saved_at` (мс, как
Date.now() в браузере). Побеждает более новая; проигравшая копия не пропадает —
она лежит в `builder_drafts.<имя>.older.json` (одна, последняя).

Запись атомарная и идёт под машинным замком. Испорченный файл читается как
пустой, а при записи откладывается в `builder_drafts.json.corrupt-<время>`.
"""
from __future__ import annotations

import json
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from config.paths import _atomic_write, get_local_config_dir, machine_file_lock

NAMES = ("keep", "jobs", "ui")
#: Потолок одного документа (байт): черновики — десятки КБ, мегабайты — это мусор.
MAX_DOC_BYTES = 2_000_000

_lock = threading.RLock()


def path() -> Path:
    return Path(get_local_config_dir()) / "builder_drafts.json"


def _older_path(name: str) -> Path:
    return path().with_name(f"builder_drafts.{name}.older.json")


@contextmanager
def _transaction():
    with _lock:
        with machine_file_lock(path().with_suffix(".lock")):
            yield


def _valid_doc(doc) -> bool:
    return (isinstance(doc, dict) and isinstance(doc.get("saved_at"), (int, float))
            and not isinstance(doc.get("saved_at"), bool) and isinstance(doc.get("data"), dict))


def _clean(raw) -> dict[str, dict]:
    docs = raw.get("docs") if isinstance(raw, dict) else None
    if not isinstance(docs, dict):
        raise ValueError("bad shape")
    return {n: d for n, d in docs.items() if n in NAMES and _valid_doc(d)}


def load() -> dict[str, dict]:
    """Все документы. Нет файла или он испорчен — пусто (страница не падает)."""
    try:
        return _clean(json.loads(path().read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return {}


def _load_strict() -> dict[str, dict]:
    """Чтение для записи: нечитаемый файл — отказ (OSError), испорченный —
    откладывается в `.corrupt-<время>` и начинаем с пустого."""
    p = path()
    try:
        raw = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    try:
        return _clean(json.loads(raw))
    except ValueError:
        p.replace(p.with_name(f"{p.name}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}"))
        return {}


def _write(docs: dict[str, dict]) -> None:
    _atomic_write(path(), json.dumps({"docs": docs}, ensure_ascii=False, indent=1,
                                     sort_keys=True).encode("utf-8"))


def _keep_older(name: str, doc: dict) -> None:
    _atomic_write(_older_path(name), json.dumps(doc, ensure_ascii=False, indent=1,
                                                sort_keys=True).encode("utf-8"))


def save(name: str, saved_at: float, data: dict, base=None) -> tuple[bool, dict]:
    """Записать документ. (принят, документ на сервере после вызова).

    Входящая копия старше серверной — не принимается (серверная новее), а сама
    уходит в `.older`. Принятая заменяет серверную; если серверная при этом
    отличалась от той, что клиент видел (`base` ≠ её saved_at) и имела другое
    содержимое, она тоже уходит в `.older`: две машины/вкладки писали параллельно,
    и ни одна работа не должна пропасть молча.
    ValueError — неизвестное имя, форма или размер."""
    if name not in NAMES:
        raise ValueError("unknown document")
    if (not isinstance(saved_at, (int, float)) or isinstance(saved_at, bool)
            or not isinstance(data, dict)):
        raise ValueError("bad document")
    incoming = {"saved_at": saved_at, "data": data}
    if len(json.dumps(incoming, ensure_ascii=False).encode("utf-8")) > MAX_DOC_BYTES:
        raise ValueError("document is too large")
    with _transaction():
        docs = _load_strict()
        stored = docs.get(name)
        if stored and saved_at < stored["saved_at"]:
            if stored["data"] != data:
                _keep_older(name, incoming)
            return False, stored
        if stored and stored["data"] != data and base != stored["saved_at"]:
            _keep_older(name, stored)
        docs[name] = incoming
        _write(docs)
        return True, incoming


def load_older(name: str) -> dict | None:
    try:
        doc = json.loads(_older_path(name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if _valid_doc(doc) else None
