"""Targeting library: the team copy lives in the library repository (ACL-1241).

Every change goes straight to the repository through `team_library`; this
machine keeps a read copy that each open of the section and each publish
refreshes. Without a connected library the registry works on this machine only.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
import unicodedata
from contextlib import contextmanager
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

from config import paths
from src.analysis_agent import team_library


class TargetingError(RuntimeError):
    """Invalid input or a registry that cannot safely be read/written."""


@dataclass
class Target:
    name: str
    interests: str
    narrow: str = ""
    added_at: str = ""
    source: str = "dashboard"
    added_by: str = ""
    updated_at: str = ""
    updated_by: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    def rpa_interests(self) -> str:
        return self.interests + (" [AND] " + self.narrow if self.narrow else "")


def _store() -> Path:
    return Path(paths.get_local_config_dir()) / "targeting_registry.json"


def normalise_one(name: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", name)).strip().casefold()


def split_values(raw: str) -> list[str]:
    return [v.strip() for v in raw.split(",") if v.strip()]


def fingerprint(interests: str, narrow: str = "") -> str:
    # Match RPA's AND syntax as well as comma-separated OR terms. JSON avoids
    # delimiter collisions; group order and repeated terms do not change an audience.
    groups = re.split(r"\[AND\]|[|\\]", interests + (" [AND] " + narrow if narrow else ""), flags=re.I)
    return json.dumps(sorted({tuple(sorted({normalise_one(v) for v in split_values(g)}))
                              for g in groups if g.strip()}), ensure_ascii=False)


_lock = threading.RLock()


@contextmanager
def _transaction():
    # Also protect a second dashboard/CLI process using the same config directory.
    # ACL-1495: общий замок из paths — ограниченное ожидание вместо LK_LOCK
    # (10 × 1 с и EDEADLK) и файл замка без гонки первой записи.
    with _lock:
        with paths.machine_file_lock(_store().with_suffix(".lock")):
            yield


def _load_raw() -> dict:
    p = _store()
    if not p.exists():
        return {"version": 1, "targets": {}, "hidden": [], "deleted": {}, "renamed": {}}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("expected an object")
        if not isinstance(data.get("version"), int):
            data = {"version": 1, "targets": data, "hidden": []}  # legacy registry
        if data["version"] != 1 or not isinstance(data["targets"], dict):
            raise ValueError("unsupported registry format")
        if not isinstance(data["hidden"], list) or any(not isinstance(v, str) for v in data["hidden"]):
            raise ValueError("invalid hidden names")
        known = Target.__dataclass_fields__
        for key, raw in data["targets"].items():
            t = Target(**{k: v for k, v in raw.items() if k in known})
            if key != t.name or any(not isinstance(getattr(t, k), str) for k in known):
                raise ValueError("invalid target fields")
            if not t.name.strip() or not t.interests.strip():
                raise ValueError("invalid target")
            data["targets"][key] = t.to_dict()
        if not isinstance(data.setdefault("deleted", {}), dict):
            raise ValueError("invalid deleted section")
        if not isinstance(data.setdefault("renamed", {}), dict):
            raise ValueError("invalid renamed section")
        return data
    except Exception as exc:
        raise TargetingError("Could not read the targeting registry. The file was left unchanged; restore it from the backup copy.") from exc


def _save_raw(data: dict) -> None:
    p = _store()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=p.parent, prefix=p.name, suffix=".tmp", delete=False) as f:
            tmp = Path(f.name)
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        # ACL-1419: on Windows the replace is refused (WinError 5) while the
        # target is open for a moment elsewhere: the previous writer's
        # handle, Defender, the indexer. Wait it out like the other machine
        # files do; failing here refused the edit with an error.
        paths._replace_when_free(tmp, p)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def all_targets() -> list[Target]:
    with _transaction():
        return sorted((Target(**v) for v in _load_raw()["targets"].values()), key=lambda t: normalise_one(t.name))


def duplicates(targets: Iterable[Target] | None = None) -> dict[str, list]:
    """Inspect existing aliases without deleting or rewriting any saved profile."""
    targets = list(targets) if targets is not None else all_targets()
    names = Counter(normalise_one(t.name) for t in targets)
    sets = defaultdict(list)
    for t in targets:
        sets[fingerprint(t.interests, t.narrow)].append(t.name)
    return {"names": sorted(n for n, count in names.items() if count > 1),
            "sets": [group for group in sets.values() if len(group) > 1]}


def find_duplicate(interests: str, narrow: str = "", *, ignore: str = "") -> Target | None:
    want = fingerprint(interests, narrow)
    return next((t for t in all_targets() if normalise_one(t.name) != normalise_one(ignore)
                 and fingerprint(t.interests, t.narrow) == want), None)


def _check(data: dict, name: str, interests: str, narrow: str, *, ignore: str = "") -> None:
    if any(not isinstance(v, str) for v in (name, interests, narrow)):
        raise TargetingError("Name, interests and narrowing must be text.")
    if any(any(ord(c) < 32 for c in v) for v in (name, interests, narrow)):
        raise TargetingError("Remove control characters from the target fields.")
    if not name.strip():
        raise TargetingError("The target needs a name.")
    if not split_values(interests) or fingerprint(interests) == "[]":
        raise TargetingError("Enter at least one interest.")
    for raw in data["targets"].values():
        t = Target(**{k: v for k, v in raw.items() if k in Target.__dataclass_fields__})
        if normalise_one(t.name) == normalise_one(ignore):
            continue
        if normalise_one(t.name) == normalise_one(name):
            raise TargetingError(f"The name “{name.strip()}” is already taken by another target.")
        if fingerprint(t.interests, t.narrow) == fingerprint(interests, narrow):
            raise TargetingError(f"This target already exists as “{t.name}”: the same set of values, in any order.")


# ── правки: в репозиторий библиотеки или, без него, в файл на машине ──────

def _clean(raw_targets: dict) -> dict:
    """Записи библиотеки, которые можно положить в копию; битые пропускаются."""
    known = Target.__dataclass_fields__
    out = {}
    for key, raw in (raw_targets or {}).items():
        try:
            t = Target(**{k: v for k, v in raw.items() if k in known})
        except Exception:
            continue
        if key == t.name and t.name.strip() and t.interests.strip() \
                and all(isinstance(getattr(t, k), str) for k in known):
            out[key] = t.to_dict()
    return out


def _apply_library(lib: dict) -> None:
    """Свежая версия библиотеки становится копией на этой машине."""
    deleted = lib.get("deleted") or {}
    with _transaction():
        team_library.preserve_local_copy(_store())
        _save_raw({"version": 1, "targets": _clean(lib.get("targets")),
                   "hidden": sorted({normalise_one(n) for n in deleted}), "deleted": deleted,
                   "renamed": lib.get("renamed") or {},
                   "library_synced_at": lib.get("library_synced_at", "")})


team_library.on_update("targets", _apply_library, lambda: _load_raw().get("library_synced_at", ""))


def _mutate(change, message: str):
    if team_library.configured():
        try:
            return team_library.transact("targets", change, message)[1]
        except team_library.WriteUncertain as exc:
            raise TargetingError(str(exc)) from exc
        except team_library.LibraryError as exc:
            raise TargetingError(f"{exc} Nothing was saved.") from exc
    with _transaction():
        data = _load_raw()
        result = change(data)
        data["hidden"] = sorted(set(data["hidden"]) | {normalise_one(n) for n in data["deleted"]})
        _save_raw(data)
        return result


def _forget_deleted(lib: dict, name: str) -> None:
    for key in [k for k in lib["deleted"] if normalise_one(k) == normalise_one(name)]:
        del lib["deleted"][key]
    renamed = lib.setdefault("renamed", {})
    for key in [k for k in renamed if normalise_one(k) == normalise_one(name)]:
        del renamed[key]
    if "hidden" in lib:
        lib["hidden"] = [n for n in lib["hidden"] if n != normalise_one(name)]


def add(name: str, interests: str, narrow: str = "", *, source: str = "dashboard") -> Target:
    def change(lib):
        _check(lib, name, interests, narrow)
        t = Target(name.strip(), ", ".join(split_values(interests)), ", ".join(split_values(narrow)),
                   team_library.now(), source, added_by=team_library.author())
        lib["targets"][t.name] = t.to_dict()
        _forget_deleted(lib, t.name)
        return t
    return _mutate(change, f"Add target {name.strip()}")


def update(old_name: str, name: str, interests: str, narrow: str = "") -> Target:
    def change(lib):
        if old_name not in lib["targets"]:
            raise TargetingError(f"There is no target “{old_name}” in the library. It may have been removed on another machine.")
        _check(lib, name, interests, narrow, ignore=old_name)
        was = lib["targets"].pop(old_name)
        t = Target(name.strip(), ", ".join(split_values(interests)), ", ".join(split_values(narrow)),
                   was.get("added_at", ""), was.get("source", "dashboard"), was.get("added_by", ""),
                   team_library.now(), team_library.author())
        lib["targets"][t.name] = t.to_dict()
        _forget_deleted(lib, t.name)
        if normalise_one(old_name) != normalise_one(t.name):
            # Старое имя остаётся в кампаниях и черновиках — публикация
            # должна назвать новое, а не молча остаться без таргетинга.
            lib["renamed"][old_name] = t.name
        return t
    return _mutate(change, f"Edit target {old_name}")


def remove(name: str) -> None:
    def change(lib):
        if name not in lib["targets"]:
            raise TargetingError(f"There is no target “{name}” in the library. It may have been removed on another machine.")
        lib["deleted"][name] = {"data": lib["targets"].pop(name),
                                "deleted_by": team_library.author(), "deleted_at": team_library.now()}
    _mutate(change, f"Remove target {name}")


def restore(name: str) -> Target:
    def change(lib):
        rec = lib["deleted"].get(name)
        if rec is None:
            raise TargetingError(f"“{name}” is not in Recently deleted. It may have been restored on another machine.")
        was = rec.get("data") or {}
        _check(lib, name, was.get("interests", ""), was.get("narrow", ""))
        t = Target(name, was.get("interests", ""), was.get("narrow", ""), was.get("added_at", ""),
                   was.get("source", "dashboard"), was.get("added_by", ""),
                   team_library.now(), team_library.author())
        del lib["deleted"][name]
        lib["targets"][t.name] = t.to_dict()
        _forget_deleted(lib, t.name)
        return t
    return _mutate(change, f"Restore target {name}")


def recently_deleted() -> list[dict]:
    with _transaction():
        return team_library.recently_deleted(_load_raw())


def snapshot() -> dict:
    """Библиотека этой машины в формате репозитория — для первого наполнения."""
    with _transaction():
        data = _load_raw()
    return {"version": 1, "targets": data["targets"], "deleted": data["deleted"], "renamed": data["renamed"]}


def pull(*, force: bool = False) -> dict:
    return team_library.pull("targets", force=force)


def lookup(name: str) -> Target | None:
    key = normalise_one(name)
    return next((t for t in all_targets() if normalise_one(t.name) == key), None)


def resolve_interests(name: str) -> str:
    pull()
    t = lookup(name)
    if t is not None:
        return t.rpa_interests()
    with _transaction():
        data = _load_raw()
    renamed = {normalise_one(k): v for k, v in data["renamed"].items()}
    if normalise_one(name) in renamed:
        raise TargetingError(f"The target “{name}” was renamed to “{renamed[normalise_one(name)]}”. Choose it in the builder.")
    if normalise_one(name) in {normalise_one(n) for n in data["deleted"]} or normalise_one(name) in data["hidden"]:
        raise TargetingError(f"The target “{name}” was removed from the library. Restore it in Targeting or choose another target.")
    if team_library.configured():
        raise TargetingError(f"The target “{name}” is not available in the team library copy. "
                             "Refresh Targeting and choose an available target before publishing.")
    return ""
