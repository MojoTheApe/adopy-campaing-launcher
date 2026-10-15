"""Where this computer keeps the Stats workbook and the Creatives folder (ACL-1239).

The setup wizard asks for both once; afterwards the manager changes them in the
dashboard instead of editing `.env`. Values live in the machine `.env` under the
keys the rest of ACL already reads (`FB_STATS_EXCEL`, `FB_CREATIVES_ROOT`) and are
applied to the running process at once.

ACL-1447: the manager label (`ACL_MACHINE_LABEL`) lives here too — every Sentry
event from this computer carries it as the `manager` tag. The setup wizard sets
it to the manager's name; this screen changes it without touching `MY_CM_NAME`,
which decides whose accounts the report shows.
"""
from __future__ import annotations

import os
from pathlib import Path

from src.analysis_agent.stats_workbook import StatsWorkbookError, validate_stats_workbook

from config.paths import get_creatives_root, get_excel_path, update_machine_env


class MachinePathError(ValueError):
    """A path the manager can correct: missing file, wrong type, no access."""


def manager_name() -> str:
    """The label run reports carry: `ACL_MACHINE_LABEL`, else `MY_CM_NAME`."""
    for key in ("ACL_MACHINE_LABEL", "MY_CM_NAME"):
        value = (os.environ.get(key) or "").strip()
        if value:
            return value
    return ""


def current() -> dict:
    stats, creatives = get_excel_path(), get_creatives_root()
    return {
        "stats_workbook": stats,
        "stats_workbook_found": Path(stats).is_file(),
        "creatives_folder": creatives,
        "creatives_folder_found": Path(creatives).is_dir(),
        "manager_name": manager_name(),
    }


def check_manager_name(raw: str) -> str:
    name = " ".join((raw or "").split())
    if len(name) < 2:
        raise MachinePathError("Enter your name (at least 2 characters).")
    if len(name) > 60:
        raise MachinePathError("The name is too long (60 characters at most).")
    if any(ch in name for ch in '"\\=<>'):
        raise MachinePathError("Use letters, digits and spaces in the name.")
    return name


def _clean(raw: str) -> str:
    # Путь из проводника («Копировать как путь») приходит в кавычках.
    return (raw or "").strip().strip('"').strip()


def check_stats_workbook(raw: str) -> str:
    path = Path(_clean(raw))
    if not str(path) or str(path) == ".":
        raise MachinePathError("Enter the path to the Stats workbook.")
    if path.suffix.casefold() not in {".xlsx", ".xlsm"}:
        raise MachinePathError("The Stats workbook must be an .xlsx or .xlsm file.")
    if not path.is_file():
        raise MachinePathError(f"There is no file at {path}. Check the path and try again.")
    try:
        return validate_stats_workbook(str(path))
    except StatsWorkbookError as exc:
        raise MachinePathError(str(exc)) from exc


def check_creatives_folder(raw: str) -> tuple[str, bool]:
    """Returns the folder and whether it was created just now."""
    path = Path(_clean(raw))
    if not str(path) or str(path) == ".":
        raise MachinePathError("Enter the path to the Creatives folder.")
    if path.exists() and not path.is_dir():
        raise MachinePathError(f"{path} is a file, not a folder.")
    created = False
    if not path.exists():
        try:
            path.mkdir(parents=True)
            created = True
        except OSError as exc:
            raise MachinePathError(f"The Creatives folder could not be created: {exc}") from exc
    if not os.access(path, os.R_OK):
        raise MachinePathError(f"ACM cannot read {path}.")
    return str(path.resolve()), created


def save(stats_workbook: str | None = None, creatives_folder: str | None = None,
         manager: str | None = None) -> dict:
    """Validate everything first, then write all values in one update."""
    values: dict[str, str] = {}
    created = False
    if manager is not None:
        values["ACL_MACHINE_LABEL"] = check_manager_name(manager)
    if stats_workbook is not None:
        values["FB_STATS_EXCEL"] = check_stats_workbook(stats_workbook)
    if creatives_folder is not None:
        values["FB_CREATIVES_ROOT"], created = check_creatives_folder(creatives_folder)
    if values:
        try:
            update_machine_env(values)
        except OSError as exc:
            raise MachinePathError("The settings could not be saved. Check access to the ACL data folder.") from exc
    return {**current(), "creatives_created": created,
            "stats_changed": "FB_STATS_EXCEL" in values}
