"""Which Multilogin profiles exist on this machine, and which are open now.

Connecting an ad account means pairing it with a Multilogin profile id. Buyers
were doing that by copying a UUID out of the Multilogin window into a CSV — the
kind of step that is easy to get subtly wrong and impossible to verify by eye.

Multilogin's own CATALOGUE is not usable here: the launcher answers 404 for
`/profile`, and the cloud API answers about a different workspace than the one
these profiles live in (checked 2026-09-16). So the full list of profiles — the
closed ones included — stays out of reach until a token for the right workspace
exists.

What does work is noticing which profiles are open right now. That fits how the
work actually happens: to connect an account you open its profile anyway. So
the dashboard can offer "the profiles you have open" instead of asking for an
identifier.

For those open profiles Multilogin will say who they are. The local launcher
answers `GET /api/v1/profile/statuses` with the profile id, its NAME and its
folder id (measured 2026-09-16 with the token already in `.env`). That is the
whole answer, so it is the first place we ask.

Everything below it is the older way of finding out, kept for when Multilogin
is not answering: the running processes are matched to profile directories, and
the name is read off the browser window title. Both are guesses. Windows
shortens the path in the process command line
(`…\\mlx\\profiles\\B18D03~1\\3EF7F4~1\\359D3E~1`), so an id is recovered by
prefix — and two profiles sharing a prefix are dropped rather than confused.
The window title carries the name only when Mimic is showing it.
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


# The Accounts overview asks this on every load, so a Multilogin that is not
# running has to cost a moment rather than a page. The launcher is on this same
# machine: it answers in milliseconds or it is not there at all.
_LAUNCHER_TIMEOUT = 4.0


def _launcher_instances() -> list:
    """Configured Multilogin instances that actually carry a token.

    `get_multilogin_instance` raises when a slot is unset, and most machines
    have exactly one filled: the second slot belongs to the KLD account and a
    buyer who never uses it leaves it empty. So an unset slot is skipped rather
    than raised over — it is the normal state, not a fault.

    Either token will do. What we are asking about is this machine, not a
    workspace: slot B answered about profiles belonging to slot A's workspace
    (measured 2026-09-16).
    """
    from src.meta_campaign_agent.multilogin_config import get_multilogin_instance

    out = []
    for key in ("A", "B"):
        try:
            out.append(get_multilogin_instance(key))
        except Exception:
            continue
    return out


def running_profiles_from_launcher() -> list[dict]:
    """Open profiles as Multilogin itself reports them: id, name, folder id.

    `GET /api/v1/profile/statuses` on the local launcher gives all three
    outright, which is better than every way of working them out from the
    machine: the name is the real one instead of whatever the window title
    happens to show, the id is exact instead of a prefix match, and `folder_id`
    arrives with it rather than being read off the profile's path.

    Empty list on anything unexpected — no Multilogin, no token, a shape we did
    not expect. The caller then reads the machine, as it always did. This feeds
    a picker, and one that raises would be worse than one that offers less.
    """
    try:
        import requests
    except Exception:
        return []

    for cfg in _launcher_instances():
        try:
            resp = requests.get(
                f"{cfg.launcher_base_v1}/profile/statuses",
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {cfg.automation_token}",
                },
                timeout=_LAUNCHER_TIMEOUT,
            )
            if resp.status_code != 200:
                continue
            states = ((resp.json() or {}).get("data") or {}).get("states") or {}
        except Exception:
            continue
        if not isinstance(states, dict):
            continue

        found: list[dict] = []
        for pid, state in states.items():
            if not isinstance(state, dict):
                continue
            # A launcher upgrade or partial response must not break Accounts.
            # Reject a malformed record before using any of its string fields.
            fields = (state.get("profile_id"), state.get("name"),
                      state.get("folder_id"), state.get("status"))
            if any(value is not None and not isinstance(value, str) for value in fields):
                continue
            profile_id = fields[0] or pid
            if not isinstance(profile_id, str):
                continue
            profile_id = profile_id.strip()
            # `states` lists the active profiles, but one caught mid-shutdown
            # would be offered as connectable and then disappear.
            if not profile_id or "stop" in (state.get("status") or "").lower():
                continue
            found.append({
                "profile_id": profile_id,
                "name": (state.get("name") or "").strip(),
                "folder_id": (state.get("folder_id") or "").strip(),
            })
        if found:
            return found
    return []


def open_profiles_detailed() -> list[dict]:
    """Open profiles as {profile_id, name, folder_id}, blank where unknown.

    Multilogin's own answer first; the machine is read only when it has none.
    """
    told = running_profiles_from_launcher()
    if told:
        return told

    try:
        import psutil
    except Exception:
        return [{"profile_id": p, "name": "", "folder_id": ""} for p in open_profile_ids()]

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

    return [
        {"profile_id": pid, "name": nm, "folder_id": ""}
        for pid, nm in by_profile.items()
    ]


def profile_name_key(name: str) -> str:
    """Trailing letter+digits token of a profile name — its identity.

    Multilogin names read "KLD_BFL_W27" while the same account is
    "FacebookMKTOBFLW27 SV Ait" in the sheet. The prefixes differ and always
    will; what matches is the tail, so compare on that alone.

    The tail of the FIRST word, because a name can carry a person after it:
    "FacebookDQ501 - Duan Guyen" ends in a surname, and read whole it has no
    key at all. That is the same rule the account side of the comparison has
    always applied to its own label, so the two now agree; a name without
    spaces is unaffected.
    """
    import re as _re

    words = (name or "").split()
    first = words[0] if words else ""
    # Явное правило имён (config/account_name_rules.json): id после префикса —
    # точная идентичность, любой длины, без угадывания по хвосту.
    try:
        from src.analysis_agent.account_names import id_suffix

        ident = id_suffix(first)
        if ident:
            return ident
    except Exception:  # pragma: no cover - конфиг правил не должен ронять поиск профиля
        pass
    m = _re.search(r"([A-Za-z])(\d{1,4})\s*$", first)
    return (m.group(1) + m.group(2)).upper() if m else ""


def ad_account_id_for(profile_id: str) -> str:
    """The ad account this profile actually works in, or "".

    Read from the profile's own browser history: every Ads Manager URL carries
    `act=<id>`, so the answer is already on disk. This is the last field a
    person still had to find and copy by hand.

    Purely a local file read — no network call, and nothing drives the
    account's browser. That distinction matters here: polling a live account
    through its browser is what preceded a checkpoint on 2026-08-25, and this
    deliberately does not do that.

    Picks the id seen most often among recent visits rather than simply the
    newest: one stray click into another account should not rename the
    profile's owner.
    """
    pid = (profile_id or "").strip()
    if not pid:
        return ""

    import os as _os
    import re as _re
    import shutil as _shutil
    import sqlite3 as _sqlite3
    import tempfile as _tempfile
    from collections import Counter

    hist = next(iter(_profiles_root().glob(f"*/*/{pid}/Default/History")), None)
    if hist is None:
        return ""

    # ACL-1697: свой временный файл на каждый вызов — параллельные отрисовки
    # дашборда не должны затирать копию друг друга.
    tmp = _os.path.join(
        _tempfile.gettempdir(), f"acl_hist_{pid[:8]}_{_os.getpid()}_{_os.urandom(4).hex()}.db")
    try:
        # The live file is locked by the running browser; work on a copy.
        _shutil.copyfile(hist, tmp)
        con = _sqlite3.connect(tmp)
        try:
            rows = con.execute(
                "SELECT url FROM urls WHERE url LIKE '%adsmanager.facebook.com%act=%' "
                "ORDER BY last_visit_time DESC LIMIT 200"
            ).fetchall()
        finally:
            con.close()
    except Exception:
        return ""
    finally:
        try:
            _os.remove(tmp)
        except Exception:
            pass

    ids = [m.group(1) for (u,) in rows if (m := _re.search(r"act=(\d{6,})", u or ""))]
    if not ids:
        return ""
    return f"act_{Counter(ids).most_common(1)[0][0]}"
