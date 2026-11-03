"""Паспорт машины и нагрузка для журнала (ACL-2347).

Только чтение локальной машины: ни Meta, ни Multilogin, ни браузерных вкладок.
Каждое поле в своём `try`: чего прочитать нельзя, то остаётся `None`, а сбой не
мешает ни заданию, ни отправке журнала.
"""
from __future__ import annotations

import os
import platform
import sys
import threading
from typing import Any

#: Имена процессов браузера профилей: Multilogin (mimic = Chromium) и AdsPower (SunBrowser).
_BROWSER_EXES = frozenset({"chrome.exe", "chrome", "mimic.exe", "sunbrowser.exe", "flowerbrowser.exe"})
_MB = 1024 ** 2
_GB = 1024 ** 3

_cpu_last = threading.local()


def _psutil():
    import psutil  # type: ignore

    return psutil


def browsers_open() -> int | None:
    """Сколько окон браузера открыто: главные процессы без `--type=` (вкладки и GPU не считаются)."""
    try:
        psutil = _psutil()
        n = 0
        for proc in psutil.process_iter(["name", "cmdline"]):
            name = (proc.info.get("name") or "").lower()
            if name in _BROWSER_EXES and not any(str(a).startswith("--type=")
                                                 for a in proc.info.get("cmdline") or []):
                n += 1
        return n
    except Exception:
        return None


def _cpu_since_last() -> float | None:
    """CPU машины в % с прошлого замера в этом потоке; первый замер в потоке — None."""
    try:
        now = _psutil().cpu_times()
    except Exception:
        return None
    prev = getattr(_cpu_last, "times", None)
    _cpu_last.times = now
    if prev is None:
        return None
    total = sum(now) - sum(prev)
    if total <= 0:
        return None
    return round(100.0 * (1.0 - (now.idle - prev.idle) / total), 1)


def load_now() -> dict[str, Any]:
    """Нагрузка сейчас: на каждой кампании, и при успехе тоже — контрольная группа к сбоям.

    `cpu_pct` — среднее с прошлого замера в этом же потоке (то есть за кампанию), не мгновенный пик.
    """
    out: dict[str, Any] = {"cpu_pct": _cpu_since_last(), "memory_pct": None, "available_mb": None,
                           "browsers": browsers_open()}
    try:
        vm = _psutil().virtual_memory()
        out["memory_pct"] = round(float(vm.percent), 1)
        out["available_mb"] = int(vm.available // _MB)
    except Exception:
        pass
    return out


def _screen() -> dict[str, Any]:
    """Экран: физическое разрешение основного монитора, масштаб Windows, число мониторов.

    Осознанность DPI процесса не меняется: это настройка всего процесса дешборда.
    """
    out: dict[str, Any] = {"screen_px": None, "scale_pct": None, "monitors": None}
    if not sys.platform.startswith("win"):
        return out
    try:
        import ctypes

        user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
        dc = user32.GetDC(0)
        try:
            out["screen_px"] = [gdi32.GetDeviceCaps(dc, 118), gdi32.GetDeviceCaps(dc, 117)]  # DESKTOPHORZRES/VERTRES
        finally:
            user32.ReleaseDC(0, dc)
        out["monitors"] = int(user32.GetSystemMetrics(80))  # SM_CMONITORS
    except Exception:
        pass
    try:
        import ctypes
        from ctypes import wintypes

        monitor = ctypes.windll.user32.MonitorFromPoint(wintypes.POINT(0, 0), 1)  # MONITOR_DEFAULTTOPRIMARY
        scale = ctypes.c_int()
        if ctypes.windll.shcore.GetScaleFactorForMonitor(monitor, ctypes.byref(scale)) == 0:
            out["scale_pct"] = int(scale.value)
    except Exception:
        pass
    return out


def _windows_build() -> int | None:
    try:
        return int(sys.getwindowsversion().build)  # type: ignore[attr-defined]
    except Exception:
        return None


def machine_profile() -> dict[str, Any]:
    """Паспорт машины (`machine.profile`, раз на запуск дешборда): почему у одного байера работает, а у другого нет."""
    out: dict[str, Any] = {
        "os": platform.platform(), "os_build": _windows_build(), "python": platform.python_version(),
        "cpu": platform.processor() or None, "cores_logical": None, "cores_physical": None,
        "cpu_max_mhz": None, "ram_gb": None, "disk_free_gb": None,
        "on_battery": None, "battery_pct": None,
    }
    try:
        psutil = _psutil()
    except Exception:
        psutil = None
    if psutil is not None:
        for key, read in (
            ("cores_logical", lambda: psutil.cpu_count()),
            ("cores_physical", lambda: psutil.cpu_count(logical=False)),
            ("cpu_max_mhz", lambda: int(psutil.cpu_freq().max) or None),
            ("ram_gb", lambda: round(psutil.virtual_memory().total / _GB, 1)),
            ("disk_free_gb", lambda: round(psutil.disk_usage(os.path.abspath(os.sep)).free / _GB, 1)),
        ):
            try:
                out[key] = read()
            except Exception:
                pass
        try:
            battery = psutil.sensors_battery()   # у настольного компьютера батареи нет — остаётся None
            if battery is not None:
                out["on_battery"] = not battery.power_plugged
                out["battery_pct"] = round(float(battery.percent))
        except Exception:
            pass
    out.update(_screen())
    out["load"] = load_now()
    return out
