from __future__ import annotations

import os
import threading
import time
from pathlib import Path


class CreativeNotFoundError(FileNotFoundError):
    pass


class AmbiguousCreativeError(ValueError):
    pass


def _is_within(root_real: str, target_real: str) -> bool:
    """`target_real` совпадает с `root_real` или лежит под ним — оба уже
    реальные пути (`os.path.realpath`), сравнение по тексту, без
    `os.path.commonpath`: на Windows он падает `ValueError`, если пути
    оказались на разных дисках (ровно то, что здесь и нужно отвергнуть)."""
    root_norm = os.path.normcase(root_real.rstrip(os.sep))
    target_norm = os.path.normcase(target_real.rstrip(os.sep))
    return target_norm == root_norm or target_norm.startswith(root_norm + os.sep)


# ACL-1615: индекс «имя файла (lower) -> относительные пути», один обход дерева
# на корень вместо `rglob("*")` на каждый запрос предпросмотра. Индекс хранит
# только относительные строки (не Path и не stat) — на большом дереве это
# десятки байт на файл. Снимок неизменяемый: перестройка собирает новый и
# подменяет ссылку целиком, поэтому читатели не видят полупостроенный индекс.
_RECHECK_SECONDS = 5.0  # как часто проверять mtime каталогов (stat без листинга)
_FULL_REBUILD_SECONDS = 60.0  # страховка для ФС, где mtime каталога не меняется
_MAX_ROOTS = 4  # корней в кэше; корень один, но тесты и смена настроек — нет


class _Snapshot:
    __slots__ = ("by_name", "dir_mtimes", "checked_at", "built_at")

    def __init__(self, by_name: dict[str, list[str]], dir_mtimes: dict[str, int]):
        self.by_name = by_name
        self.dir_mtimes = dir_mtimes
        self.checked_at = self.built_at = time.monotonic()


_index_lock = threading.Lock()
_snapshots: dict[str, _Snapshot] = {}


def _build_snapshot(root: str) -> _Snapshot:
    """Один обход дерева. Каталоги-ссылки не раскрываем (как и `rglob`);
    ссылка-файл индексируется, а выход за корень отсекается при поиске."""
    by_name: dict[str, list[str]] = {}
    dir_mtimes: dict[str, int] = {}
    stack: list[tuple[str, str]] = [(root, "")]
    while stack:
        abs_dir, rel_dir = stack.pop()
        try:
            dir_mtimes[abs_dir] = os.stat(abs_dir).st_mtime_ns
            with os.scandir(abs_dir) as it:
                entries = list(it)
        except OSError:
            continue
        for entry in entries:
            rel = f"{rel_dir}/{entry.name}" if rel_dir else entry.name
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append((entry.path, rel))
                    continue
                if not entry.is_file():
                    continue
            except OSError:
                continue
            by_name.setdefault(entry.name.lower(), []).append(rel)
    for rels in by_name.values():
        rels.sort()
    return _Snapshot(by_name, dir_mtimes)


def _dirs_changed(snap: _Snapshot) -> bool:
    """Дёшево: только stat каталогов, без листинга и без файлов."""
    for d, mtime in snap.dir_mtimes.items():
        try:
            if os.stat(d).st_mtime_ns != mtime:
                return True
        except OSError:
            return True  # каталог исчез
    return False


def _get_snapshot(root: str, *, seen: _Snapshot | None = None, recheck: bool = False) -> _Snapshot:
    """`seen` — снимок, на котором поиск споткнулся (удалённый файл): перестроить,
    но только если его ещё никто не заменил — N параллельных вызовов дают одну
    перестройку. `recheck` — проверить mtime каталогов вне троттлинга (промах
    поиска или публикация: файл мог только что появиться)."""
    with _index_lock:
        snap = _snapshots.get(root)
        now = time.monotonic()
        stale = snap is None or (seen is not None and snap is seen)
        if not stale and now - snap.built_at >= _FULL_REBUILD_SECONDS:
            stale = True
        if not stale and (recheck or now - snap.checked_at >= _RECHECK_SECONDS):
            stale = _dirs_changed(snap)
            if not stale:
                snap.checked_at = now
        if stale:
            snap = _build_snapshot(root)
            if root not in _snapshots and len(_snapshots) >= _MAX_ROOTS:
                _snapshots.pop(next(iter(_snapshots)))
            _snapshots[root] = snap
        return snap


def reset_creative_index() -> None:
    """Сбросить индекс (тесты; следующий поиск построит его заново)."""
    with _index_lock:
        _snapshots.clear()


def _lookup(root_path: Path, root_real: str, snap: _Snapshot, name_norm: str,
            has_path: bool, target_basename: str) -> tuple[list[Path], bool]:
    """-> (совпадения, был ли в индексе путь, которого уже нет на диске)."""
    matches: list[Path] = []
    stale = False
    suffix = name_norm.lower()
    for rel in snap.by_name.get(target_basename, ()):
        if has_path and not rel.lower().endswith(suffix):
            continue
        path = root_path.joinpath(*rel.split("/"))
        if not path.is_file():
            stale = True  # файл удалён/перенесён после построения индекса
            continue
        # ACL-1342 S02: `is_file()` идёт по символической ссылке молча — сама
        # ссылка может лежать внутри корня, а указывать наружу (например, на
        # системный файл). Сверяем реальную цель, а не путь ссылки.
        try:
            target_real = os.path.realpath(path)
        except OSError:
            continue
        if not _is_within(root_real, target_real):
            continue
        matches.append(path)
    return matches, stale


def find_creative_file(root: str | Path, filename: str, *, fresh: bool = False) -> Path:
    """
    Search root for filename (basename match, case-insensitive) via a cached
    per-root index (ACL-1615), not a tree walk per call.

    If filename contains path separators (e.g. 'Latam/Local/CL/file.mp4'), a
    path-suffix match is used so callers can disambiguate duplicates.

    Never returns a path that no longer exists: a stale index entry or a miss
    triggers a re-check/rebuild and one retry.

    `fresh=True` (публикация) всегда сверяет mtime каталогов, без троттлинга:
    дубликат, добавленный секунду назад, всё равно даст AmbiguousCreativeError.

    Raises CreativeNotFoundError or AmbiguousCreativeError.
    """
    root_path = Path(root)
    if not root_path.is_dir():
        raise FileNotFoundError(f"Creatives root not found: {root_path}")

    name = (filename or "").strip()
    if not name:
        raise CreativeNotFoundError("Creative filename is empty")

    # Normalize separators so both / and \ work
    name_norm = name.replace("\\", "/")
    has_path = "/" in name_norm

    # Basename — из нормализованного имени: `Path(name).name` на POSIX не
    # считает обратный слэш разделителем, и результат зависел бы от ОС.
    target_basename = name_norm.rsplit("/", 1)[-1].lower()

    # ACL-1342 S02: реальный путь корня — один раз, не на каждой итерации.
    root_real = os.path.realpath(root_path)
    key = os.path.normcase(os.path.abspath(root_path))

    snap = _get_snapshot(key, recheck=fresh)
    matches, stale = _lookup(root_path, root_real, snap, name_norm, has_path, target_basename)
    if stale:
        # Устаревший путь в индексе — перестраиваем один раз и ищем заново.
        snap = _get_snapshot(key, seen=snap)
        matches, _ = _lookup(root_path, root_real, snap, name_norm, has_path, target_basename)
    elif not matches:
        # Промах: файл мог появиться только что — внеочередная проверка mtime.
        snap = _get_snapshot(key, recheck=True)
        matches, _ = _lookup(root_path, root_real, snap, name_norm, has_path, target_basename)

    if not matches:
        raise CreativeNotFoundError(f"Creative not found under {root_path}: {name}")
    if len(matches) > 1:
        rel = ", ".join(str(m.relative_to(root_path)) for m in matches[:5])
        suffix = " …" if len(matches) > 5 else ""
        raise AmbiguousCreativeError(
            f"Multiple files named {name!r} under {root_path}: {rel}{suffix}"
        )
    return matches[0]
