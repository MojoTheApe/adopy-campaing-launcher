"""ACL-2268: лимит автопрогрева — одна автоматическая попытка на физический профиль за 30 минут.

Хранится в файле в каталоге конфигурации машины, поэтому перезапуск дешборда лимит не сбрасывает.
Файл нечитаем или не пишется -> попытки нет (закрыто по умолчанию): Publish и Check plan всё равно
откроют профиль сами. Ключ — физический профиль, а не подпись аккаунта.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from config.paths import _atomic_write, get_local_config_dir, machine_file_lock

log = logging.getLogger(__name__)

#: Окно между автоматическими попытками на один профиль.
AUTO_RETRY_S = 30 * 60

_clock = time.time


def _path() -> Path:
    return Path(get_local_config_dir()) / "auto_warm_attempts.json"


def _read(path: Path) -> dict[str, float]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("auto warm attempts file is not an object")
    return {str(k): float(v) for k, v in raw.items()}


def take_attempt(profile_key: str, window_s: float = AUTO_RETRY_S) -> bool:
    """True — попытку можно делать (и она записана). False — рано или файл недоступен."""
    key = (profile_key or "").strip()
    if not key:
        return False
    path = _path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with machine_file_lock(path.with_suffix(".lock")):
            now = _clock()
            data = _read(path)
            last = data.get(key)
            if last is not None and 0 <= now - last < window_s:
                return False
            data[key] = now
            data = {k: v for k, v in data.items() if 0 <= now - v < window_s}
            _atomic_write(path, json.dumps(data, sort_keys=True).encode())
            return True
    except Exception:  # noqa: BLE001 — не уверены -> автоматической попытки нет
        log.warning("Automatic warm-up attempts file is unavailable; no automatic attempt", exc_info=True)
        return False
