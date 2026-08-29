"""Which Multilogin profiles exist on this machine, and which are open now.

Connecting an ad account means pairing it with a Multilogin profile id. Buyers
were doing that by copying a UUID out of the Multilogin window into a CSV — the
kind of step that is easy to get subtly wrong and impossible to verify by eye.

Multilogin's own profile-listing API is not usable here: the launcher answers
404 for `/profile` and the cloud API answers 403 with the automation token we
hold (checked 2026-08-29). So profile NAMES are unavailable, and a dropdown of
140 bare UUIDs would be no better than typing one.

What does work is noticing which profiles are open right now. That fits how the
work actually happens: to connect an account you open its profile anyway. So
the dashboard can offer "the profiles you have open" instead of asking for an
identifier.

Windows shortens the path in the process command line
(`…\\mlx\\profiles\\B18D03~1\\3EF7F4~1\\359D3E~1`), so the running profile is
matched back to its full id by prefix against the profile directories on disk.
"""
from __future__ import annotations

import os
from pathlib import Path


def _profiles_root() -> Path:
    home = (os.environ.get("MLX_HOME") or "").strip() or os.path.join(
        os.path.expanduser("~"), "mlx"
    )
    return Path(home) / "profiles"


def all_profile_ids() -> list[str]:
    """Every profile directory on this machine, newest first.

    Ordered by modification time because the profile someone just used is the
    one they are most likely connecting.
    """
    root = _profiles_root()
    if not root.is_dir():
        return []
    dirs = [p for p in root.glob("*/*/*") if p.is_dir()]
    dirs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return [p.name for p in dirs]


def _expand_short_name(short: str, known: list[str]) -> str | None:
    """Full profile id for a Windows 8.3 short directory name, if unambiguous."""
    key = short.split("~")[0].replace("-", "").lower()
    if not key:
        return None
    hits = [p for p in known if p.replace("-", "").lower().startswith(key)]
    # Only trust a unique match — two profiles sharing a prefix would otherwise
    # silently attach an account to the wrong browser identity.
    return hits[0] if len(hits) == 1 else None


def open_profile_ids() -> list[str]:
    """Profile ids whose browser is running right now.

    Empty list on any failure — this feeds a convenience picker, and a
    diagnostic that raises would be worse than one that offers nothing.
    """
    try:
        import psutil
    except Exception:
        return []

    known = all_profile_ids()
    if not known:
        return []

    found: list[str] = []
    for proc in psutil.process_iter(["name", "cmdline"]):
        try:
            if "chrome" not in (proc.info.get("name") or "").lower():
                continue
            for arg in proc.info.get("cmdline") or []:
                if not arg.startswith("--user-data-dir=") or "mlx" not in arg.lower():
                    continue
                short = os.path.basename(arg.split("=", 1)[1].rstrip("\\/"))
                full = short if short in known else _expand_short_name(short, known)
                if full and full not in found:
                    found.append(full)
        except Exception:
            continue
    return found


def folder_id_for(profile_id: str) -> str:
    """The MLX folder a profile lives in, read off its path on disk.

    Profiles are stored as …/profiles/<workspace>/<folder_id>/<profile_id>, so
    the folder is knowable without asking Multilogin — one less identifier for
    a person to copy by hand, and one less place to mistype.
    """
    pid = (profile_id or "").strip()
    if not pid:
        return ""
    root = _profiles_root()
    if not root.is_dir():
        return ""
    for p in root.glob(f"*/*/{pid}"):
        if p.is_dir():
            folder = p.parent.name
            # Folder ids are UUIDs; anything else is a directory that merely
            # happens to sit at that depth.
            if len(folder) == 36 and folder.count("-") == 4:
                return folder
    return ""


def _window_titles_by_pid() -> dict[int, str]:
    """Visible top-level window titles, keyed by owning process id.

    Multilogin puts the PROFILE NAME at the front of its window title
    ("KLD_BFL_W27: (28) Ads Manager … - Mimic"), which is the only place a
    human-readable name is available at all — the profile-listing API answers
    404/403 with the token we hold. Without it the picker can only offer bare
    UUIDs, which is no better than typing one.

    ctypes rather than pywin32: no dependency to add for a few lines, and this
    runs on the operator's own machine only.
    """
    import ctypes
    from ctypes import wintypes

    titles: dict[int, str] = {}
    try:
        user32 = ctypes.windll.user32
    except Exception:
        return titles

    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _cb(hwnd, _lparam):
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if not length:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            # First window wins: a browser has one main window and several
            # helper ones, and the main window is enumerated first.
            titles.setdefault(int(pid.value), buf.value)
        except Exception:
            pass
        return True

    try:
        user32.EnumWindows(WNDENUMPROC(_cb), 0)
    except Exception:
        pass
    return titles


def _name_from_title(title: str) -> str:
    """Profile name out of a Mimic window title, or "" when it isn't one."""
    text = (title or "").strip()
    if not text or ":" not in text:
        return ""
    name = text.split(":", 1)[0].strip()
    # Guard against ordinary page titles that merely contain a colon.
    return name if name and len(name) <= 60 and " " not in name else ""


def open_profiles_detailed() -> list[dict]:
    """Open profiles as {profile_id, name}, name empty when it can't be read."""
    try:
        import psutil
    except Exception:
        return [{"profile_id": p, "name": ""} for p in open_profile_ids()]

    known = all_profile_ids()
    titles = _window_titles_by_pid()
    by_profile: dict[str, str] = {}

    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            if "chrome" not in (proc.info.get("name") or "").lower():
                continue
            args = proc.info.get("cmdline") or []
            udd = next((a.split("=", 1)[1] for a in args if a.startswith("--user-data-dir=")), "")
            if not udd or "mlx" not in udd.lower():
                continue
            short = os.path.basename(udd.rstrip("\\/"))
            full = short if short in known else _expand_short_name(short, known)
            if not full:
                continue
            name = _name_from_title(titles.get(int(proc.info["pid"]), ""))
            if name and not by_profile.get(full):
                by_profile[full] = name
            by_profile.setdefault(full, "")
        except Exception:
            continue

    return [{"profile_id": pid, "name": nm} for pid, nm in by_profile.items()]


def profile_name_key(name: str) -> str:
    """Trailing letter+digits token of a profile name — its identity.

    Multilogin names read "KLD_BFL_W27" while the same account is
    "FacebookMKTOBFLW27 SV Ait" in the sheet. The prefixes differ and always
    will; what matches is the tail, so compare on that alone.
    """
    import re as _re

    m = _re.search(r"([A-Za-z])(\d{1,4})\s*$", (name or "").strip())
    return (m.group(1) + m.group(2)).upper() if m else ""
