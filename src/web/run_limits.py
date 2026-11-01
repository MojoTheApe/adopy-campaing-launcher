"""Run limits of this computer, changed in Settings without a restart (ACL-2293).

Three numbers decide how much ACM runs at once on a machine: how many Multilogin
profiles (`ACL_MAX_PROFILES`), how many heavy warm actions (`ACL_WARM_MAX_HEAVY`)
and how much of the memory may be in use before a new profile starts
(`ACL_MEMORY_MAX_USED_PCT`). `ProfileSlots` and `HeavyGate` re-read the
environment on every decision and `update_machine_env` writes the machine `.env`
and the live `os.environ` together, so a saved value applies to the next start
decision. Running jobs are never stopped or closed.
"""
from __future__ import annotations

import math
from typing import Any

from config.paths import update_machine_env
from src.web.profile_slots import DEFAULT_CEILING, DEFAULT_MAX_USED_PCT, load_settings, read_memory

CEILING_KEY = "ACL_MAX_PROFILES"
HEAVY_KEY = "ACL_WARM_MAX_HEAVY"
SHARE_KEY = "ACL_MEMORY_MAX_USED_PCT"

DEFAULT_HEAVY = 1
BOUNDS = {"ceiling": (1, 8), "warm_heavy": (1, 3), "memory_max_used_pct": (50, 90)}
DEFAULTS = {"ceiling": DEFAULT_CEILING, "warm_heavy": DEFAULT_HEAVY,
            "memory_max_used_pct": int(DEFAULT_MAX_USED_PCT)}
FIELDS = tuple(BOUNDS)

#: Above these the page asks for one confirmation and shows a warning.
CONFIRM_CEILING = 3
CONFIRM_SHARE = 80
SMALL_MACHINE_MB = 32 * 1024

_LABELS = {"ceiling": "Profiles at once", "warm_heavy": "Heavy actions at once",
           "memory_max_used_pct": "Memory limit"}


class RunLimitError(ValueError):
    """A value the manager can correct: out of range or not a number."""


def _number(field: str, raw: Any) -> float:
    label = _LABELS[field]
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        raise RunLimitError(f"{label}: enter a number.")
    if isinstance(raw, str):
        text = raw.strip().replace(",", ".")
        try:
            value = float(text)
        except ValueError:
            raise RunLimitError(f"{label}: enter a number without units.") from None
    else:
        value = float(raw)
    if not math.isfinite(value):
        raise RunLimitError(f"{label}: enter a number.")
    return value


def _check(field: str, raw: Any) -> int | float:
    low, high = BOUNDS[field]
    value = _number(field, raw)
    if field != "memory_max_used_pct":
        if value != int(value):
            raise RunLimitError(f"{_LABELS[field]}: enter a whole number.")
        value = int(value)
    else:
        value = round(value, 1)
        if value == int(value):
            value = int(value)
    if not low <= value <= high:
        raise RunLimitError(f"{_LABELS[field]}: enter a value from {low} to {high}.")
    return value


def _text(value: int | float) -> str:
    return str(int(value)) if float(value) == int(value) else str(value)


def _running() -> int:
    try:
        from src.web import publish_jobs
        return len(publish_jobs.slots_snapshot().get("running") or [])
    except Exception:  # noqa: BLE001 - the numbers are context, never a reason to fail
        return 0


def effective() -> dict[str, int | float]:
    """The numbers the engine uses right now (after legacy-key fallbacks)."""
    from src.fb_api.warm import max_heavy
    settings = load_settings()
    pct = settings.max_used_pct
    return {"ceiling": int(settings.ceiling), "warm_heavy": int(max_heavy()),
            "memory_max_used_pct": int(pct) if float(pct) == int(pct) else round(float(pct), 1)}


def warnings_for(values: dict[str, Any], total_mb: int | None) -> list[str]:
    out: list[str] = []
    if values["ceiling"] > CONFIRM_CEILING and (total_mb is None or total_mb < SMALL_MACHINE_MB):
        out.append("Each profile takes about 2-3.5 GB. Runs on 2026-10-30 lost accounts with 3 profiles at once "
                   "on a laptop; more than 3 can overload this computer.")
    if values["memory_max_used_pct"] > CONFIRM_SHARE:
        out.append("Above 80% the computer has little memory left for the browser tabs; tabs may stop responding.")
    return out


def current() -> dict:
    memory = read_memory()
    from src.web.profile_slots import used_pct
    values = effective()
    total = None if memory is None else int(memory.total_mb)
    return {"ok": True, **{k: values[k] for k in FIELDS},
            "defaults": dict(DEFAULTS), "bounds": {k: list(v) for k, v in BOUNDS.items()},
            "total_mb": total, "used_pct": None if memory is None else round(used_pct(memory), 1),
            "memory_readable": memory is not None, "running": _running(),
            "confirm_above": {"ceiling": CONFIRM_CEILING, "memory_max_used_pct": CONFIRM_SHARE},
            "warnings": warnings_for(values, total)}


def _telemetry(old: dict, new: dict, total_mb: int | None, used: float | None) -> None:
    """One numbers-only row per save: no paths, labels or profile ids."""
    try:
        from src.fb_ads_rpa import run_telemetry as rt
        snap: dict[str, Any] = {}
        try:
            from src.web import publish_jobs
            snap = publish_jobs.slots_snapshot()
        except Exception:  # noqa: BLE001
            pass
        diag = {f"old_{k}": old[k] for k in FIELDS}
        diag.update({f"new_{k}": new[k] for k in FIELDS})
        diag.update(running=len(snap.get("running") or []), waiting=len(snap.get("waiting") or []),
                    total_mb=total_mb, used_pct=used, source="settings")
        rt.record("job_phase", True, note="run_limits_changed", diag=diag)
    except Exception:  # noqa: BLE001 - telemetry must not break a save
        pass


def save(ceiling: Any = None, warm_heavy: Any = None, memory_max_used_pct: Any = None) -> dict:
    """Validate everything first, then write all given values in one update.

    Values are always written as explicit numbers: a blank `ACL_MAX_PROFILES`
    would fall through to the legacy keys and give a different ceiling.
    """
    given = {"ceiling": ceiling, "warm_heavy": warm_heavy, "memory_max_used_pct": memory_max_used_pct}
    checked = {k: _check(k, v) for k, v in given.items() if v is not None}
    if checked:
        old = effective()
        keys = {"ceiling": CEILING_KEY, "warm_heavy": HEAVY_KEY, "memory_max_used_pct": SHARE_KEY}
        try:
            update_machine_env({keys[k]: _text(v) for k, v in checked.items()})
        except OSError as exc:
            raise RunLimitError("The settings could not be saved. Check access to the ACL data folder.") from exc
        new = effective()
        memory = read_memory()
        from src.web.profile_slots import used_pct
        _telemetry(old, new, None if memory is None else int(memory.total_mb),
                   None if memory is None else round(used_pct(memory), 1))
        try:
            from src.web import publish_jobs
            publish_jobs.wake_slots()
        except Exception:  # noqa: BLE001 - waiting jobs re-check on their own every 2 s
            pass
    return {**current(), "applies": "next start decision", "running_jobs_affected": False,
            "changed": sorted(checked)}


def reset() -> dict:
    """Back to the defaults, written explicitly (no fall-through to legacy keys)."""
    return save(**DEFAULTS)
