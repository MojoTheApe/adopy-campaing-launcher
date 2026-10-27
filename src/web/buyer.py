"""Which campaign manager's dashboard this machine is showing.

Buyers are campaign managers, and the stats workbook already says who owns
what: its `CM` column names one for every row. So the list of buyers is not
something to configure — it is read from the data.

The choice is stored per machine (next to the account map, outside the
checkout) rather than in `.env`, because it is a preference someone flips
during the day, not installation config. `MY_CM_NAME` stays the default for
machines that never touch the picker, so nothing changes for an existing
install until someone chooses.

This is a view switch, not authentication. Everyone runs ACL on their own
machine with their own credentials; the picker decides whose numbers are on
screen, and does not claim to keep anyone out of anyone else's data.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

_FILE = "selected_cm.txt"
_RENDER_CM: ContextVar[str | None] = ContextVar("acl_render_cm", default=None)


def _path() -> Path:
    from config.paths import get_local_config_dir

    return Path(get_local_config_dir()) / _FILE


def default_cm() -> str:
    return (os.environ.get("MY_CM_NAME") or "").strip()


@contextmanager
def rendering_cm(name: str):
    token = _RENDER_CM.set((name or "").strip())
    try:
        yield
    finally:
        _RENDER_CM.reset(token)


def get_selected_cm() -> str:
    """The CM whose dashboard to render. Falls back to MY_CM_NAME."""
    current = _RENDER_CM.get()
    return _saved_selected_cm() if current is None else current


def _saved_selected_cm() -> str:
    try:
        stored = _path().read_text(encoding="utf-8").strip()
        if stored:
            return stored
    except Exception:
        pass
    return default_cm()


def set_selected_cm(name: str) -> str:
    """Persist the choice. An empty name clears it back to the default."""
    clean = (name or "").strip()
    try:
        f = _path()
        f.parent.mkdir(parents=True, exist_ok=True)
        if clean:
            f.write_text(clean, encoding="utf-8")
        elif f.exists():
            f.unlink()
    except Exception:
        pass
    return _saved_selected_cm()


def known_cms(df) -> list[str]:
    """Every campaign manager present in the stats, alphabetically.

    Derived from the data rather than a configured list: a new buyer appears
    the moment their first row lands in the workbook, with nothing to update.
    """
    try:
        if df is None or "CM" not in df.columns:
            return []
        names = (
            df["CM"].fillna("").astype(str).str.strip().replace("", None).dropna().unique()
        )
        return sorted({str(n) for n in names})
    except Exception:
        return []
