"""Паспорт машины и нагрузка для хаба телеметрии.

Паспорт (`machine.profile`, раз за процесс) объясняет, почему у одного байера работает,
а у другого нет. Нагрузка (`publish.stats.load`) пишется на каждой кампании, и при успехе
тоже, чтобы у сбоев была контрольная группа (аудит 01.11).

Каждое поле читается в своём try: сбой одного не теряет остальные. Никаких имён
пользователя, путей и серийных номеров.
"""
from __future__ import annotations

import ctypes
import platform
import threading
import time
from typing import Any

import psutil

from src.acl_event_log.writer import emit

_emitted = False
_lock = threading.Lock()


def _safe(fn) -> Any:
    try:
        return fn()
    except Exception:
        return None


def _proc_names() -> list[str]:
    out = []
    for p in psutil.process_iter(["name"]):
        try:
            out.append((p.info.get("name") or "").lower())
        except Exception:
            continue
    return out


def _chrome() -> tuple[int, float]:
    count, rss = 0, 0
    for p in psutil.process_iter(["name", "memory_info"]):
        try:
            if (p.info.get("name") or "").lower() in ("chrome.exe", "chrome"):
                count += 1
                rss += p.info["memory_info"].rss if p.info.get("memory_info") else 0
        except Exception:
            continue
    return count, round(rss / 1024 ** 2, 1)


def _screen() -> tuple[int | None, int | None, int | None]:
    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    w, h = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
    dpi = _safe(lambda: user32.GetDpiForSystem())
    return w, h, round(dpi * 100 / 96) if dpi else None


def _system_drive_free_gb() -> float:
    import os

    root = os.environ.get("SystemDrive", "C:") + "\\" if platform.system() == "Windows" else "/"
    return round(psutil.disk_usage(root).free / 1024 ** 3, 1)


def collect_profile() -> dict:
    names = _safe(_proc_names) or []
    screen = _safe(_screen) or (None, None, None)
    battery = _safe(psutil.sensors_battery)
    return {
        "ram_gb": _safe(lambda: round(psutil.virtual_memory().total / 1024 ** 3, 1)),
        "cpu_model": _safe(lambda: platform.processor() or None),
        "cores_logical": _safe(lambda: psutil.cpu_count(logical=True)),
        "cores_physical": _safe(lambda: psutil.cpu_count(logical=False)),
        "windows": _safe(lambda: f"{platform.system()} {platform.release()} {platform.version()}"),
        "screen_w": screen[0], "screen_h": screen[1], "scale_pct": screen[2],
        "on_battery": (not battery.power_plugged) if battery is not None else None,
        "uptime_h": _safe(lambda: round((time.time() - psutil.boot_time()) / 3600, 1)),
        "mlx_running": any("mlx" in n or "multilogin" in n for n in names),
        "adspower_running": any("adspower" in n for n in names),
        "chrome_procs": _safe(lambda: _chrome()[0]),
        "disk_free_gb": _safe(_system_drive_free_gb),
    }


def load_snapshot() -> dict:
    chrome = _safe(_chrome) or (None, None)
    mem = _safe(psutil.virtual_memory)
    return {
        # interval=None: процент с прошлого вызова, без ожидания (кампания не тормозит).
        "cpu_pct": _safe(lambda: psutil.cpu_percent(interval=None)),
        "mem_pct": getattr(mem, "percent", None),
        "mem_avail_mb": round(mem.available / 1024 ** 2) if mem is not None else None,
        "chrome_procs": chrome[0], "chrome_mem_mb": chrome[1],
    }


def emit_machine_profile() -> None:
    """`machine.profile` один раз за процесс; сбой не выходит наружу."""
    global _emitted
    try:
        with _lock:
            if _emitted:
                return
            _emitted = True
        emit("machine.profile", collect_profile())
    except Exception:
        pass
