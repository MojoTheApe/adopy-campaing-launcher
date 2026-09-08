"""Machine-local targeting library with cached shared profiles and local overrides.

New profiles can be appended to Sheets. Edits and removals affect this machine;
removal tombstones prevent the next refresh from resurrecting a hidden profile.
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


class TargetingError(RuntimeError):
    """Invalid input or a registry that cannot safely be read/written."""


@dataclass
class Target:
    name: str
    interests: str
    narrow: str = ""
    added_at: str = ""
    source: str = "dashboard"
    shared: bool = False
    shared_error: str = ""

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
    with _lock:
        p = _store().with_suffix(".lock")
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a+b") as f:
            if f.tell() == 0:
                f.write(b"0")
                f.flush()
            f.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(f, fcntl.LOCK_EX)
            try:
                yield
            finally:
                f.seek(0)
                if os.name == "nt":
                    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(f, fcntl.LOCK_UN)


def _load_raw() -> dict:
    p = _store()
    if not p.exists():
        return {"version": 1, "targets": {}, "hidden": []}
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
            if key != t.name or any(not isinstance(getattr(t, k), str) for k in known if k != "shared"):
                raise ValueError("invalid target fields")
            if not t.name.strip() or not t.interests.strip() or not isinstance(t.shared, bool):
                raise ValueError("invalid target")
            data["targets"][key] = t.to_dict()
        return data
    except Exception as exc:
        raise TargetingError("Не удалось прочитать реестр таргетов. Файл сохранён без изменений; восстановите его из резервной копии.") from exc


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
        tmp.replace(p)
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


def push_all(*, snapshot: bool = True) -> tuple[int, str]:
    """Explicit export of one consistent local snapshot, always with a backup."""
    if not snapshot:
        raise TargetingError("Полная выгрузка разрешена только с резервной копией таблицы.")
    from src.analysis_agent.gsheet_targeting import export_profiles
    targets = all_targets()  # corrupt storage must fail before touching Sheets
    written, backup = export_profiles([(t.name, t.interests, t.narrow) for t in targets])
    # Preserve edits/removals made while the network request was in flight.
    try:
        with _transaction():
            data = _load_raw()
            for t in targets:
                current = data["targets"].get(t.name)
                if current and (current["interests"], current["narrow"]) == (t.interests, t.narrow):
                    current.update(shared=True, shared_error="", source="sheet")
                    data["hidden"] = [n for n in data["hidden"] if n != normalise_one(t.name)]
            _save_raw(data)
    except Exception as exc:
        raise TargetingError(
            f"Экспорт выполнен ({written} таргетов), но локальный статус не сохранён: {exc}. "
            f"Резервная копия: {backup}. Повторять экспорт для сохранения статуса не нужно."
        ) from exc
    return written, backup


def find_duplicate(interests: str, narrow: str = "", *, ignore: str = "") -> Target | None:
    want = fingerprint(interests, narrow)
    return next((t for t in all_targets() if normalise_one(t.name) != normalise_one(ignore)
                 and fingerprint(t.interests, t.narrow) == want), None)


def _check(data: dict, name: str, interests: str, narrow: str, *, ignore: str = "") -> None:
    if any(not isinstance(v, str) for v in (name, interests, narrow)):
        raise TargetingError("Название, интересы и сужение должны быть строками.")
    if any(any(ord(c) < 32 for c in v) for v in (name, interests, narrow)):
        raise TargetingError("Уберите управляющие символы из полей таргета.")
    if not name.strip():
        raise TargetingError("У таргета должно быть название.")
    if not split_values(interests) or fingerprint(interests) == "[]":
        raise TargetingError("Укажите хотя бы один интерес.")
    for raw in data["targets"].values():
        t = Target(**raw)
        if normalise_one(t.name) == normalise_one(ignore):
            continue
        if normalise_one(t.name) == normalise_one(name):
            raise TargetingError(f"Имя «{name.strip()}» уже занято другим таргетом.")
        if fingerprint(t.interests, t.narrow) == fingerprint(interests, narrow):
            raise TargetingError(f"Такой таргет уже есть — «{t.name}». Набор значений тот же, порядок значения не имеет.")


def add(name: str, interests: str, narrow: str = "", *, source: str = "dashboard") -> Target:
    with _transaction():
        data = _load_raw()
        _check(data, name, interests, narrow)
        t = Target(name.strip(), ", ".join(split_values(interests)), ", ".join(split_values(narrow)),
                   time.strftime("%Y-%m-%d %H:%M:%S"), source)
        data["targets"][t.name] = t.to_dict()
        data["hidden"] = [n for n in data["hidden"] if n != normalise_one(t.name)]
        _save_raw(data)
        return t


def update(old_name: str, name: str, interests: str, narrow: str = "") -> Target:
    with _transaction():
        data = _load_raw()
        if old_name not in data["targets"]:
            raise TargetingError(f"Таргета «{old_name}» нет в реестре.")
        _check(data, name, interests, narrow, ignore=old_name)
        was = data["targets"].pop(old_name)
        t = Target(name.strip(), ", ".join(split_values(interests)), ", ".join(split_values(narrow)), was["added_at"], "local")
        data["hidden"] = sorted(set(data["hidden"]) | {normalise_one(old_name)})
        data["targets"][t.name] = t.to_dict()
        _save_raw(data)
        return t


def remove(name: str) -> None:
    with _transaction():
        data = _load_raw()
        if name not in data["targets"]:
            raise TargetingError(f"Таргета «{name}» нет в реестре.")
        del data["targets"][name]
        data["hidden"] = sorted(set(data["hidden"]) | {normalise_one(name)})
        _save_raw(data)


def import_targets(profiles: Iterable) -> int:
    """Refresh sheet-owned entries; preserve aliases, local edits and removals."""
    with _transaction():
        data = _load_raw()
        before = json.dumps(data, sort_keys=True)
        added = 0
        by_name = {normalise_one(k): k for k in data["targets"]}
        for p in profiles:
            key = normalise_one(p.name)
            if not key or not p.interests.strip() or key in data["hidden"]:
                continue
            old_key = by_name.get(key)
            if old_key and data["targets"][old_key]["source"] != "sheet":
                continue
            # Keep historical sheet aliases even if they describe the same audience.
            t = Target(old_key or p.name, p.interests, p.narrow, source="sheet", shared=True)
            data["targets"][t.name] = t.to_dict()
            by_name[key] = t.name
            added += int(old_key is None)
        if json.dumps(data, sort_keys=True) != before:
            _save_raw(data)
        return added


def refresh(*, force: bool = False) -> dict:
    from src.analysis_agent import gsheet_targeting as gs
    with gs._lock:
        profiles = gs.load_targeting_profiles(force=force)
        return {"tried": True, "error": gs.last_error, "imported": import_targets(profiles.values())}


def lookup(name: str) -> Target | None:
    key = normalise_one(name)
    return next((t for t in all_targets() if normalise_one(t.name) == key), None)


def resolve_interests(name: str) -> str:
    t = lookup(name)
    if t is None:
        refresh()
        t = lookup(name)
    if t is not None:
        return t.rpa_interests()
    with _transaction():
        if normalise_one(name) in _load_raw()["hidden"]:
            raise TargetingError(f"Таргет «{name}» удалён или переименован на этой машине. Выберите другой таргет.")
    return ""


def share(target: Target) -> str:
    """Append-only sharing; retries are idempotent. Never overwrite a shared row."""
    try:
        from src.analysis_agent.gsheet_targeting import add_targeting_profile
        add_targeting_profile(target.name, target.interests, target.narrow)
        error = ""
    except Exception as exc:
        error = str(exc)
    with _transaction():
        data = _load_raw()
        current = data["targets"].get(target.name)
        if current and (current["interests"], current["narrow"]) == (target.interests, target.narrow):
            current["shared"] = not error
            current["shared_error"] = error
            if not error:
                current["source"] = target.source = "sheet"
            _save_raw(data)
        elif current:
            error = error or "Таргет изменился во время отправки. Новые значения сохранены только локально."
    target.shared, target.shared_error = not error, error
    return error
