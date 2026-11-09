"""Run limits of this computer, changed in Settings without a restart (ACL-2293).

Three machine settings live in the machine `.env` and are applied to the running
process at once (`update_machine_env` also sets `os.environ`):

* `ACL_MAX_PROFILES`        - how many Multilogin profiles run at once (the ceiling);
* `ACL_WARM_MAX_HEAVY`      - how many heavy account actions run at once;
* `ACL_MEMORY_MAX_USED_PCT` - the share of memory in use above which no new profile starts.

`ProfileSlots` and `HeavyGate` re-read the environment on every decision, so a change
applies to the next start decision; running jobs are never stopped.
"""
from __future__ import annotations

import logging
import math
from typing import Any

from config.paths import update_machine_env

log = logging.getLogger(__name__)

CEILING_KEY = "ACL_MAX_PROFILES"
HEAVY_KEY = "ACL_WARM_MAX_HEAVY"
SHARE_KEY = "ACL_MEMORY_MAX_USED_PCT"

# Границы — решение владельца 09.11: потолок 1–8, тяжёлых 1–3, доля памяти 50–95 (как в ACL-2400).
CEILING_BOUNDS = (1, 8)
HEAVY_BOUNDS = (1, 3)
SHARE_BOUNDS = (50.0, 95.0)

#: Выше этих значений страница просит подтверждение (один диалог на всё сохранение).
CONFIRM_CEILING_ABOVE = 3
CONFIRM_SHARE_ABOVE = 80.0
#: Мягкое предупреждение: потолок выше 3 на машине с памятью меньше этого (МБ).
WARN_SMALL_MACHINE_MB = 32 * 1024

FIELDS = ("ceiling", "warm_heavy", "memory_max_used_pct")


class RunLimitError(ValueError):
    """A value the manager can correct: out of range, not a number."""


def defaults() -> dict[str, float | int]:
    from src.web import profile_slots as ps
    return {"ceiling": ps.DEFAULT_CEILING, "warm_heavy": 1,
            "memory_max_used_pct": ps.DEFAULT_MAX_USED_PCT}


def bounds() -> dict[str, list]:
    return {"ceiling": list(CEILING_BOUNDS), "warm_heavy": list(HEAVY_BOUNDS),
            "memory_max_used_pct": list(SHARE_BOUNDS)}


def _number(raw: Any, label: str) -> float:
    # Булево - это int в Python, но «true» в поле лимита - ошибка клиента.
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        raise RunLimitError(f"{label}: enter a number.")
    if isinstance(raw, str):
        try:
            value = float(raw.strip().replace(",", "."))
        except ValueError:
            raise RunLimitError(f"{label}: enter a number without units.") from None
    else:
        value = float(raw)
    if not math.isfinite(value):
        raise RunLimitError(f"{label}: enter a number.")
    return value


def _whole(raw: Any, label: str, lo: int, hi: int) -> int:
    value = _number(raw, label)
    if value != int(value) or not lo <= value <= hi:
        raise RunLimitError(f"{label}: enter a whole number from {lo} to {hi}.")
    return int(value)


def check_ceiling(raw: Any) -> int:
    return _whole(raw, "Profiles at once", *CEILING_BOUNDS)


def check_heavy(raw: Any) -> int:
    return _whole(raw, "Heavy actions at once", *HEAVY_BOUNDS)


def check_share(raw: Any) -> float:
    lo, hi = SHARE_BOUNDS
    value = _number(raw, "Memory in use")
    if not lo <= value <= hi:
        raise RunLimitError(f"Memory in use: enter a number from {lo:g} to {hi:g}.")
    return round(value, 1)


def _fmt(value: float | int) -> str:
    return str(int(value)) if float(value) == int(value) else str(value)


def effective() -> dict[str, float | int]:
    """The values the engine uses right now (the same readers as ProfileSlots / HeavyGate)."""
    from src.fb_api.warm import max_heavy
    from src.web.profile_slots import load_settings
    settings = load_settings()
    return {"ceiling": settings.ceiling, "warm_heavy": max_heavy(),
            "memory_max_used_pct": settings.max_used_pct}


def _context() -> dict[str, Any]:
    from src.web import profile_slots as ps
    total_mb = used = None
    memory = ps.read_memory()
    if memory is not None:
        total_mb, used = int(memory.total_mb), ps.used_pct(memory)
    running = waiting = 0
    nxt: dict = {}
    try:
        from src.web.publish_jobs import slots_snapshot
        snap = slots_snapshot()
        running, waiting = len(snap.get("running") or []), len(snap.get("waiting") or [])
        nxt = snap.get("next") or {}
    except Exception:  # noqa: BLE001 - the page still shows the values
        log.debug("run limits: slots snapshot failed", exc_info=True)
    return {"total_mb": total_mb, "used_pct": used, "running": running, "waiting": waiting,
            "ceiling_reached": nxt.get("reason") == "ceiling"}


def warnings_for(values: dict, total_mb: int | None) -> list[str]:
    out = []
    if values["ceiling"] > CONFIRM_CEILING_ABOVE and (total_mb is None or total_mb < WARN_SMALL_MACHINE_MB):
        out.append("Each profile takes about 2-3.5 GB. More than 3 profiles at once on a computer "
                   "with less than 32 GB can overload it.")
    if values["memory_max_used_pct"] > CONFIRM_SHARE_ABOVE:
        out.append("A high memory share leaves little room for the system and can freeze tabs.")
    return out


def current() -> dict[str, Any]:
    values = effective()
    ctx = _context()
    return {**values, "defaults": defaults(), "bounds": bounds(),
            "confirm_above": {"ceiling": CONFIRM_CEILING_ABOVE, "memory_max_used_pct": CONFIRM_SHARE_ABOVE},
            "memory_readable": ctx["total_mb"] is not None, **ctx,
            "warnings": warnings_for(values, ctx["total_mb"])}


def save(ceiling: Any = None, warm_heavy: Any = None, memory_max_used_pct: Any = None,
         *, reset: bool = False) -> dict[str, Any]:
    """Validate everything first, then write all values in one update.

    `reset` writes the defaults explicitly - never blank keys: a blank ceiling would fall
    through to the legacy `ACL_MAX_PARALLEL_*` keys and give a different number.
    """
    old = effective()
    values: dict[str, str] = {}
    if reset:
        d = defaults()
        values = {CEILING_KEY: _fmt(d["ceiling"]), HEAVY_KEY: _fmt(d["warm_heavy"]),
                  SHARE_KEY: _fmt(d["memory_max_used_pct"])}
    else:
        if ceiling is not None:
            values[CEILING_KEY] = _fmt(check_ceiling(ceiling))
        if warm_heavy is not None:
            values[HEAVY_KEY] = _fmt(check_heavy(warm_heavy))
        if memory_max_used_pct is not None:
            values[SHARE_KEY] = _fmt(check_share(memory_max_used_pct))
    if values:
        try:
            update_machine_env(values)
        except OSError as exc:
            raise RunLimitError("The settings could not be saved. Check access to the ACL data folder.") from exc
        _after_change(old)
    return {**current(), "applies": "next start decision", "running_jobs_affected": False,
            "changed": bool(values)}


def _after_change(old: dict) -> None:
    """Wake waiting jobs and leave one numbers-only telemetry row."""
    ctx: dict[str, Any] = {}
    try:
        from src.web import publish_jobs
        publish_jobs._slots.wake()
        ctx = _context()
    except Exception:  # noqa: BLE001
        log.debug("run limits: wake failed", exc_info=True)
    try:
        from src.fb_ads_rpa import run_telemetry as _rt
        _rt.record("job_phase", True, campaign="", note="run_limits_changed",
                   diag={"old": old, "new": effective(), "running": ctx.get("running"),
                         "waiting": ctx.get("waiting"), "total_mb": ctx.get("total_mb"),
                         "used_pct": ctx.get("used_pct"), "source": "settings"})
    except Exception:  # noqa: BLE001 - telemetry never fails a save
        log.debug("run limits: telemetry failed", exc_info=True)
