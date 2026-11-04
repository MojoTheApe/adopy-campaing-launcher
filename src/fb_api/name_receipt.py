"""ACL-2250: локальная квитанция имени кампании (`X`, `X copy 1`, `X copy 2` ...).

Только Meta не отличает повтор (нужна новая копия) от возобновления после обрыва (копия не нужна):
в обоих случаях на кабинете лежит такая же кампания. Эту разницу хранит маленький файл на машине:
какое имя выбрано для (кабинет, имя из билдера) и, когда кампания записана, её id.
Квитанцию удаляет конец задания (`publisher.finish_job`), а не конец одной кампании.
Ни записей в Meta, ни токенов: только файл. Как `creative_recovery.Receipt`.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from config.paths import get_local_config_dir, machine_file_lock, _atomic_write

SCHEMA = 1


class NameReceiptError(Exception):
    """Файл квитанции нельзя прочитать или записать: стоп до любой записи в Meta (kind `name_receipt`)."""


def path_for(act: str, base: str) -> Path:
    key = hashlib.sha256((str(act) + "\n" + str(base)).encode("utf-8")).hexdigest()
    return Path(get_local_config_dir()) / "campaign_name_receipts" / (key + ".json")


def _read(path: Path) -> dict | None:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise ValueError("unexpected receipt content")
    return data


def _locked(act: str, base: str):
    path = path_for(act, base)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path, machine_file_lock(path.with_suffix(".lock"))


def load(act: str, base: str) -> dict | None:
    try:
        path, lock = _locked(act, base)
        with lock:
            return _read(path)
    except (OSError, ValueError, TimeoutError) as exc:
        raise NameReceiptError(f"name receipt cannot be read: {exc}") from exc


def save(act: str, base: str, name: str, n: int) -> None:
    """Выбранное имя; id кампании сбрасывается (это новый выбор)."""
    data = {"schema": SCHEMA, "act": act, "base": base, "name": name, "n": int(n), "campaign_id": ""}
    try:
        path, lock = _locked(act, base)
        with lock:
            _atomic_write(path, json.dumps(data, sort_keys=True, ensure_ascii=False).encode("utf-8"))
    except (OSError, ValueError, TimeoutError) as exc:
        raise NameReceiptError(f"name receipt cannot be written: {exc}") from exc


def attach_id(act: str, base: str, campaign_id: str) -> None:
    try:
        path, lock = _locked(act, base)
        with lock:
            data = _read(path)
            if data is None:
                return
            data["campaign_id"] = str(campaign_id)
            _atomic_write(path, json.dumps(data, sort_keys=True, ensure_ascii=False).encode("utf-8"))
    except (OSError, ValueError, TimeoutError) as exc:
        raise NameReceiptError(f"name receipt cannot be written: {exc}") from exc


def delete(act: str, base: str) -> None:
    try:
        path, lock = _locked(act, base)
        with lock:
            path.unlink(missing_ok=True)
    except (OSError, TimeoutError) as exc:
        raise NameReceiptError(f"name receipt cannot be removed: {exc}") from exc
