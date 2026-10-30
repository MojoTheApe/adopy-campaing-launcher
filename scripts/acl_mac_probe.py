"""Read-only зонд Mimic для Mac: печатает JSON-отчёт в stdout.

Запуск: .venv/bin/python scripts/acl_mac_probe.py

Отчёт безопасно вставлять в чат: домашний путь, имя пользователя, имена томов
/Volumes, UUID (id профилей) и значения флагов заменяются заглушками; это делает
как точечная чистка, так и финальный проход по каждой строке отчёта. Читаются
только имена каталогов в <home>/deps и список процессов; из окружения берётся
один MLX_HOME (через scrub_path). Сеть, запуск и остановка процессов не
используются. Проектные модули не импортируются: хватает venv с psutil.
"""
from __future__ import annotations

import json
import os
import re
import sys

import psutil

_MLX_CANDIDATES = (
    "mlx",
    ".mlx",
    "Library/Application Support/Multilogin",
    "Library/Application Support/mlx",
)
_PROC_MARKERS = ("mimic", "chrome", "chromium", "mlx", "multilogin")
_PROC_ATTRS = ["name", "exe", "cmdline"]
# Без якоря: /System/Volumes/Data/Users/<имя> и /private/Users/<имя> тоже.
_USER_DIR = re.compile(r"(/Users|/home)/[^/]+")
# /System/Volumes/Data — системный том, имя не секрет; остальные тома скрываем.
_VOLUME = re.compile(r"(?<!/System)/Volumes/[^/]+")
_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_FLAG_NAME = re.compile(r"^--?[A-Za-z][A-Za-z0-9-]*$")


def scrub_path(path: str | None, home: str) -> str | None:
    """Путь с ~ вместо home; чужие /Users/<имя> и /home/<имя> без имени."""
    if path is None:
        return None
    norm_home = home.rstrip("/\\")
    p = path
    if norm_home and (p.rstrip("/") == norm_home or p.startswith(norm_home + "/")):
        rest = p[len(norm_home):].rstrip("/")
        return "~" + rest
    p = _USER_DIR.sub(lambda m: m.group(1) + "/<user>", p)
    return _VOLUME.sub("/Volumes/<volume>", p)


def scrub_args(args: list[str] | None) -> list[str]:
    """Имена флагов остаются; значения флагов и позиционные аргументы скрыты.

    Флагом считается только токен вида -x / --long-name; всё остальное
    (-javaagent:/путь, --token:abc, -1.2.3.4:80) печатается как <arg>.
    """
    out = []
    for a in args or []:
        if a.startswith("-") and a != "-":
            name, eq, _ = a.partition("=")
            name = _UUID.sub("<id>", name)
            if not _FLAG_NAME.match(name):
                out.append("<arg>")
            else:
                out.append(name + "=<hidden>" if eq else name)
        else:
            out.append("<arg>")
    return out


def collect_mlx_homes(home, env):
    found = []
    for rel in _MLX_CANDIDATES:
        full = os.path.join(home, *rel.split("/"))
        if os.path.isdir(full):
            found.append(scrub_path(full.replace(os.sep, "/"), home.replace(os.sep, "/")))
    env_home = env.get("MLX_HOME")
    return {
        "found": found,
        "MLX_HOME": scrub_path(env_home.replace(os.sep, "/"), home.replace(os.sep, "/"))
        if env_home
        else None,
    }


def collect_deps(mlx_home, home):
    """Только <mlx_home>/deps: профили рядом не читаются."""
    deps = os.path.join(mlx_home, "deps")
    names, executables = [], []
    if not os.path.isdir(deps):
        return {"names": names, "executables": executables}
    h = home.replace(os.sep, "/")
    try:
        for l1 in sorted(os.listdir(deps)):
            p1 = os.path.join(deps, l1)
            if not os.path.isdir(p1):
                continue
            names.append(l1)
            for l2 in sorted(os.listdir(p1)):
                if os.path.isdir(os.path.join(p1, l2)):
                    names.append(l1 + "/" + l2)
    except OSError:
        pass
    for root, dirs, files in os.walk(deps):
        depth = len(os.path.relpath(root, deps).split(os.sep))
        if depth >= 6:
            dirs[:] = []
        parts = root.replace(os.sep, "/").split("/")
        if len(parts) >= 3 and parts[-1] == "MacOS" and parts[-2] == "Contents" and parts[-3].endswith(".app"):
            for f in sorted(files):
                executables.append(scrub_path(root.replace(os.sep, "/") + "/" + f, h))
    return {"names": names, "executables": sorted(executables)}


def collect_processes(procs, home):
    out = []
    h = home.replace(os.sep, "/")
    for proc in procs:
        try:
            info = proc.as_dict(attrs=_PROC_ATTRS)
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
        name = info.get("name") or ""
        exe = info.get("exe")
        hay = (name + " " + (exe or "")).lower()
        if not any(m in hay for m in _PROC_MARKERS):
            continue
        out.append(
            {
                "name": name,
                "exe": scrub_path(exe.replace(os.sep, "/"), h) if exe else None,
                "args": scrub_args(info.get("cmdline")),
            }
        )
    return out


def _final_pass(obj, user):
    """Последний проход по КАЖДОЙ строке отчёта: UUID и литерал имени пользователя."""
    if isinstance(obj, str):
        obj = _UUID.sub("<id>", obj)
        if user:
            obj = re.sub(r"(?<![A-Za-z0-9])" + re.escape(user) + r"(?![A-Za-z0-9])", "<user>", obj)
        return obj
    if isinstance(obj, list):
        return [_final_pass(x, user) for x in obj]
    if isinstance(obj, dict):
        return {k: _final_pass(v, user) for k, v in obj.items()}
    return obj


def build_report(home: str, env, procs) -> dict:
    h = home.replace(os.sep, "/")
    homes = collect_mlx_homes(home, env)
    # deps ищем в найденных каталогах и в MLX_HOME (реальные пути, не scrubbed)
    roots = [os.path.join(home, *r.split("/")) for r in _MLX_CANDIDATES]
    if env.get("MLX_HOME"):
        roots.append(env["MLX_HOME"])
    deps, seen = [], set()
    for r in roots:
        real = os.path.normpath(r)
        if real in seen:
            continue
        seen.add(real)
        d = collect_deps(r, home)
        if d["names"] or d["executables"]:
            deps.append({"root": scrub_path(r.replace(os.sep, "/"), h), **d})
    report = {
        "mlx_homes": homes,
        "deps": deps,
        "processes": collect_processes(procs, home),
    }
    user = os.path.basename(home.rstrip("/\\"))
    return _final_pass(report, user)


def main() -> int:
    report = build_report(os.path.expanduser("~"), os.environ, psutil.process_iter())
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
