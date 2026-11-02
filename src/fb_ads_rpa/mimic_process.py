"""Единое распознавание браузера Mimic (Multilogin) на Windows и macOS.

На Windows Mimic — это `chrome.exe` в `~/mlx/deps/mimic_*`. На macOS имя
процесса заранее не известно, поэтому Mimic узнаём по месту исполняемого
файла: он лежит внутри MLX home (`~/mlx`, `$MLX_HOME`) либо в пути есть
`mimic`. Обычный Google Chrome НИКОГДА не считается Mimic: аккаунт
открывается только внутри Multilogin.

Платформа берётся из параметра `platform` (None -> `sys.platform`), чтобы
тесты проверяли обе ветки на любой ОС. Все платформы кроме darwin возвращают ровно то, что
возвращал прежний код.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def _is_mac(platform: str | None) -> bool:
    return (platform or sys.platform) == "darwin"


def _is_win(platform: str | None) -> bool:
    """Прежнее (не-macOS) поведение: Windows и Linux ведут себя как до macOS-поддержки."""
    return not _is_mac(platform)


def default_mlx_home() -> Path:
    """Корень данных Multilogin: `$MLX_HOME` или `~/mlx`."""
    return Path(os.environ.get("MLX_HOME") or Path.home() / "mlx")


def is_browser_process_name(name: str | None, platform: str | None = None) -> bool:
    """Похоже ли имя процесса на браузер, который может быть Mimic."""
    low = (name or "").lower()
    if not low:
        return False
    if _is_win(platform):
        return "chrome" in low
    return any(part in low for part in ("mimic", "chrome", "chromium"))


def is_root_mimic_name(name: str | None, platform: str | None = None) -> bool:
    """Имя процесса для подсчёта профилей (`profile_slots._mimic_processes`)."""
    if _is_win(platform):
        return str(name or "").lower() == "chrome.exe"
    return is_browser_process_name(name, platform)


def _norm(path: str | Path) -> str:
    return str(path).lower().replace("\\", "/")


_NOT_MIMIC_PREFIXES = ("/applications/", "/opt/homebrew/", "/usr/local/")


def is_mimic_executable(exe: str | None, mlx_home: str | Path | None = None) -> bool:
    """Исполняемый файл принадлежит Mimic (а не обычному Chrome).

    Порядок: внутри MLX home (по префиксу с `/` на конце, `~/mlx-old` не
    совпадает) -> да; обычные места установки Chrome -> нет; иначе по слову
    `mimic` в пути.
    """
    if not exe:
        return False
    low = _norm(exe)
    home = _norm(mlx_home if mlx_home is not None else default_mlx_home()).rstrip("/")
    if home and low.startswith(home + "/"):
        return True
    user_apps = _norm(Path.home()).rstrip("/") + "/applications/"
    if (
        low.startswith(_NOT_MIMIC_PREFIXES)
        or low.startswith(user_apps)
        or "/ms-playwright/" in low
        or "/program files" in low
    ):
        return False
    return "mimic" in low


def mimic_executable_candidates(deps: Path, platform: str | None = None) -> list[Path]:
    """Исполняемые файлы Mimic под `mlx/deps`."""
    if _is_win(platform):
        return [p for p in deps.glob("mimic_*/chrome.exe") if p.is_file()]
    return [
        p for p in deps.glob("mimic_*/**/*.app/Contents/MacOS/*")
        if p.is_file() and "Helper" not in str(p.relative_to(deps))
    ]


def mimic_version_dir(exe: Path) -> Path | None:
    """Ближайший предок `mimic_*` (на Windows это `exe.parent`)."""
    for parent in exe.parents:
        if parent.name.startswith("mimic_"):
            return parent
    return None
