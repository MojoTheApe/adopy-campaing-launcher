"""One ACL settings folder for every launch path (ACL-1680).

A process started inside Claude Desktop's MSIX package sees a virtualised
%LOCALAPPDATA%, so it used to read and write a private copy of ``ACL`` while
Explorer, the installer and the buyer's shortcut used the real one. The two
``.env`` files and the state files (notes, registries, draft status, goals)
drifted apart.

``prepare_config_dir`` is called first by every launcher. It

1. pins ACL_CONFIG_DIR to the real folder (``config.paths``),
2. merges any package copy into it - never overwriting newer data, backing up
   everything it replaces,
3. logs which folder is used and warns when a different ``ACL\\.env`` is left.

It never raises: a convenience merge must not stop anyone from launching.
"""
from __future__ import annotations

import logging
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from config import paths

log = logging.getLogger("acl.config_dir")

# Disposable or machine-specific trees: never merged between folders.
SKIP_DIRS = frozenset({"runtimes", "logs", "updates", "diagnostics", "backups", "protected-backup", "__pycache__"})
SKIP_SUFFIXES = (".lock", ".tmp", ".pid")
ENV_NAME = ".env"
# Same-instant writes on different filesystems differ by rounding.
_MTIME_TOLERANCE = 2.0


@dataclass
class UnifyResult:
    config_dir: str
    copied: list[str] = field(default_factory=list)
    replaced: list[str] = field(default_factory=list)  # shadow was newer, old copy backed up
    kept: list[str] = field(default_factory=list)  # real was newer, shadow copy saved to backup
    env_merged: list[str] = field(default_factory=list)
    backup_dir: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.copied or self.replaced or self.env_merged)


def _same(a: Path, b: Path) -> bool:
    try:
        return a.stat().st_size == b.stat().st_size and a.read_bytes() == b.read_bytes()
    except OSError:
        return False


def _parse_env(text: str) -> tuple[list[str], dict[str, str]]:
    lines = text.splitlines()
    values: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value
    return lines, values


def merge_env_text(real_text: str, shadow_text: str, *, shadow_newer: bool) -> str:
    """Union of keys. On conflict the newer file wins; empty values never win."""
    lines, real = _parse_env(real_text)
    _, shadow = _parse_env(shadow_text)
    out = list(lines)
    for key, value in shadow.items():
        if key not in real:
            out.append(f"{key}={value}")
        elif real[key] != value and shadow_newer and value.strip():
            out = [
                f"{key}={value}" if ln.strip().partition("=")[0].strip() == key and not ln.strip().startswith("#") else ln
                for ln in out
            ]
        elif real[key] != value and not real[key].strip() and value.strip():
            out = [
                f"{key}={value}" if ln.strip().partition("=")[0].strip() == key and not ln.strip().startswith("#") else ln
                for ln in out
            ]
    return "\n".join(out) + "\n"


def _backup(target: Path, backup_root: Path, rel: Path) -> None:
    dest = backup_root / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(target, dest)


def _atomic_write(dest: Path, data: bytes, mtime: float | None = None) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".unify.tmp")
    tmp.write_bytes(data)
    if mtime is not None:
        os.utime(tmp, (mtime, mtime))
    os.replace(tmp, dest)


def merge_shadow_into(real_dir: Path, shadow_dir: Path, *, now: float | None = None) -> UnifyResult:
    """Merge one package copy into the real folder. The shadow copy is left untouched."""
    result = UnifyResult(config_dir=str(real_dir))
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now or time.time()))
    backup_root = real_dir / "backups" / f"config-unify-{stamp}"
    if not shadow_dir.is_dir():
        return result
    real_dir.mkdir(parents=True, exist_ok=True)

    def note_backup() -> None:
        result.backup_dir = str(backup_root)

    for src in sorted(shadow_dir.rglob("*")):
        if not src.is_file():
            continue
        rel = src.relative_to(shadow_dir)
        if rel.parts[0] in SKIP_DIRS or any(p in SKIP_DIRS for p in rel.parts) or src.name.endswith(SKIP_SUFFIXES):
            continue
        dest = real_dir / rel
        try:
            src_mtime = src.stat().st_mtime
            if not dest.exists():
                _atomic_write(dest, src.read_bytes(), src_mtime)
                result.copied.append(rel.as_posix())
                continue
            if _same(src, dest):
                continue
            shadow_newer = src_mtime > dest.stat().st_mtime + _MTIME_TOLERANCE
            if rel.as_posix() == ENV_NAME:
                merged = merge_env_text(
                    dest.read_text(encoding="utf-8", errors="replace"),
                    src.read_text(encoding="utf-8", errors="replace"),
                    shadow_newer=shadow_newer,
                )
                if merged != dest.read_text(encoding="utf-8", errors="replace").rstrip("\n") + "\n":
                    _backup(dest, backup_root, rel)
                    note_backup()
                    _atomic_write(dest, merged.encode("utf-8"))
                    result.env_merged.append(rel.as_posix())
                continue
            if shadow_newer:
                _backup(dest, backup_root, rel)
                note_backup()
                _atomic_write(dest, src.read_bytes(), src_mtime)
                result.replaced.append(rel.as_posix())
            else:
                # The real file is newer: keep it, but never lose the other copy.
                _backup(src, backup_root, Path("shadow") / rel)
                note_backup()
                result.kept.append(rel.as_posix())
        except OSError as exc:
            result.warnings.append(f"{rel.as_posix()}: {type(exc).__name__}: {exc}")
    return result


def _env_values(path: Path) -> dict[str, str]:
    try:
        return _parse_env(path.read_text(encoding="utf-8", errors="replace"))[1]
    except OSError:
        return {}


def prepare_config_dir() -> UnifyResult:
    """Pin the real folder, merge package copies into it, log and warn."""
    real = Path(paths.pin_config_dir_env())
    result = UnifyResult(config_dir=str(real))
    try:
        log.info("ACL settings folder: %s", real)
        print(f"[acl] settings folder: {real}")
        for shadow in paths.get_shadow_config_dirs():
            merged = merge_shadow_into(real, Path(shadow))
            result.copied += merged.copied
            result.replaced += merged.replaced
            result.kept += merged.kept
            result.env_merged += merged.env_merged
            result.warnings += merged.warnings
            result.backup_dir = merged.backup_dir or result.backup_dir
            if merged.changed:
                print(
                    f"[acl] merged settings from the package copy {shadow}: "
                    f"{len(merged.copied)} new, {len(merged.replaced)} updated, "
                    f"{len(merged.env_merged)} .env merged; backups in {merged.backup_dir or 'n/a'}"
                )
            other_env = Path(shadow) / ENV_NAME
            if other_env.is_file() and _env_values(other_env) != _env_values(real / ENV_NAME):
                msg = (
                    f"a different {other_env} exists next to the real settings "
                    f"({real / ENV_NAME}); the real folder is used. Edit settings there only."
                )
                result.warnings.append(msg)
                log.warning(msg)
                print(f"[acl] WARNING: {msg}")
        if paths.is_virtualized_environment():
            note = (
                "this process runs inside an MSIX package (virtual %LOCALAPPDATA%); "
                "ACL_CONFIG_DIR is pinned to the real folder"
            )
            log.warning(note)
            print(f"[acl] note: {note}")
    except Exception as exc:  # never block a launch
        result.warnings.append(f"{type(exc).__name__}: {exc}")
        log.warning("config unify failed: %s", exc)
    return result
