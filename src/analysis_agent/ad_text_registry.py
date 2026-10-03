"""Library of ad texts — headline, body, description.

Устроено как реестр таргетов: общая копия живёт в репозитории библиотеки
(ACL-1241, `team_library`), каждая правка уходит туда сразу, а на машине лежит
копия для чтения. Вкладка Google-таблицы больше не участвует ни в чём.

**Что делает текст неповторимым — не то же, что у таргета.** Таргет это набор,
и порядок значений в нём ничего не меняет. Текст — последовательность: «купите
сейчас, скидка» и «скидка, купите сейчас» человек различает с первого взгляда.
Поэтому сравнение здесь по порядку, а не по множеству.

Одинаковыми считаются тексты, у которых совпали **заголовок, тело и описание**
разом. Регистр и лишние пробелы — способ записи: Facebook покажет один и тот же
текст, набранный с двойным пробелом и без него. А вот перенос строки значим —
им разбивают абзацы, и объявление читается иначе.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
import unicodedata
from collections import Counter, defaultdict
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

from config import paths
from src.analysis_agent import team_library


class AdTextError(RuntimeError):
    """Человеку есть что поправить: пустое поле, повтор, занятое имя."""


@dataclass
class AdText:
    name: str                 # колонка «Ad Title» — как текст называют между собой
    headline: str = ""
    body: str = ""
    description: str = ""
    asset: str = ""
    language: str = ""
    added_at: str = ""
    source: str = "dashboard"  # dashboard | sheet (перенесённые из таблицы)
    added_by: str = ""
    updated_at: str = ""
    updated_by: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _store() -> Path:
    return Path(paths.get_local_config_dir()) / "ad_text_registry.json"


#: Пробелы схлопываются, переносы строк — нет.
_SPACES = re.compile(r"[^\S\n]+")
_BLANK_LINES = re.compile(r"\n{3,}")


def tidy(text: str) -> str:
    """Текст в том виде, в каком его показывают: без хвостов и лишних пробелов."""
    t = unicodedata.normalize("NFKC", text or "").replace("\r\n", "\n").replace("\r", "\n")
    t = _SPACES.sub(" ", t)
    t = "\n".join(line.strip() for line in t.split("\n"))
    return _BLANK_LINES.sub("\n\n", t).strip()


def normalise_one(name: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", name or "")).strip().casefold()


def fingerprint(headline: str, body: str, description: str = "") -> str:
    """Отпечаток текста: по нему и решается, новый он или уже есть.

    JSON, а не склейка через разделитель: любой разделитель когда-нибудь
    встретится внутри текста, и два разных объявления сольются в одно.
    """
    return json.dumps([tidy(headline).casefold(), tidy(body).casefold(),
                       tidy(description).casefold()], ensure_ascii=False)


_lock = threading.RLock()


@contextmanager
def _transaction():
    # Второй процесс дешборда работает с тем же каталогом конфигурации.
    # ACL-1512: общий замок из paths (ACL-1495) — ограниченное ожидание вместо
    # LK_LOCK (10 × 1 с и EDEADLK) и файл замка без гонки первой записи.
    with _lock:
        with paths.machine_file_lock(_store().with_suffix(".lock")):
            yield


def _load_raw() -> dict:
    p = _store()
    if not p.exists():
        return {"version": 1, "texts": {}, "hidden": [], "deleted": {}}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("expected an object")
        if data.get("version") != 1 or not isinstance(data.get("texts"), dict):
            raise ValueError("unsupported registry format")
        if not isinstance(data.get("hidden"), list) or any(not isinstance(v, str) for v in data["hidden"]):
            raise ValueError("invalid hidden names")
        known = AdText.__dataclass_fields__
        for key, raw in data["texts"].items():
            if not isinstance(raw, dict):
                raise ValueError("invalid record")
            t = AdText(**{k: v for k, v in raw.items() if k in known})
            if key != t.name or any(not isinstance(getattr(t, k), str) for k in known):
                raise ValueError("invalid text fields")
            if not t.name.strip() or not (t.headline.strip() or t.body.strip()):
                raise ValueError("empty text")
            data["texts"][key] = t.to_dict()
        if not isinstance(data.setdefault("deleted", {}), dict):
            raise ValueError("invalid deleted section")
        return data
    except Exception as exc:
        raise AdTextError(
            "The ad text library on this machine could not be read. The file was left as it is; "
            "restore it from a backup."
        ) from exc


def _save_raw(data: dict) -> None:
    p = _store()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=p.parent,
                                         prefix=p.name, suffix=".tmp", delete=False) as f:
            tmp = Path(f.name)
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        # ACL-1512: under load Windows refuses the replace (WinError 5) while
        # the target is open for a moment elsewhere — the previous writer's
        # handle, Defender, the indexer. Wait it out like targeting does
        # (ACL-1419); failing here lost the edit.
        paths._replace_when_free(tmp, p)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def all_texts() -> list[AdText]:
    with _transaction():
        return sorted((AdText(**v) for v in _load_raw()["texts"].values()),
                      key=lambda t: normalise_one(t.name))


def builder_texts(*, force: bool = False) -> list[AdText]:
    pull(force=force)
    return all_texts()


def duplicates(texts: Iterable[AdText] | None = None) -> dict[str, list]:
    """Осмотр всей библиотеки, а не только правило при заведении.

    Файл можно поправить руками, а прежние записи могли лечь до появления
    правила: перенос из таблицы принёс 327 строк, среди них были повторы.
    """
    texts = list(texts) if texts is not None else all_texts()
    names = Counter(normalise_one(t.name) for t in texts)
    same = defaultdict(list)
    for t in texts:
        same[fingerprint(t.headline, t.body, t.description)].append(t.name)
    return {"names": sorted(n for n, count in names.items() if count > 1),
            "texts": [group for group in same.values() if len(group) > 1]}


def _check(data: dict, name: str, headline: str, body: str, description: str,
           *, ignore: str = "") -> None:
    values = (name, headline, body, description)
    if any(not isinstance(v, str) for v in values):
        raise AdTextError("Title, headline, body and description must be text.")
    # Перенос строки в теле объявления законен — им разбивают абзацы.
    # Остальные управляющие символы приезжают из копипаста и ломают разметку.
    if any(ord(c) < 32 and c != "\n" for v in values for c in v):
        raise AdTextError("Remove control characters from the text fields.")
    if any(c == "\n" for c in name):
        raise AdTextError("The ad title must be a single line.")
    if not tidy(name):
        raise AdTextError("The text needs an ad title.")
    if not tidy(headline) and not tidy(body):
        raise AdTextError("Fill in the headline or the body — an empty text cannot be saved.")
    known = AdText.__dataclass_fields__
    for raw in data["texts"].values():
        t = AdText(**{k: v for k, v in raw.items() if k in known})
        if normalise_one(t.name) == normalise_one(ignore):
            continue
        if normalise_one(t.name) == normalise_one(name):
            raise AdTextError(f"The title “{tidy(name)}” is already taken by another text.")
        if fingerprint(t.headline, t.body, t.description) == fingerprint(headline, body, description):
            raise AdTextError(
                f"This text already exists as “{t.name}”: headline, body and description match "
                "(letter case and extra spaces do not count)."
            )


def _make(name: str, headline: str, body: str, description: str,
          asset: str, language: str, added_at: str, source: str, **meta) -> AdText:
    return AdText(
        name=tidy(name),
        headline=tidy(headline),
        body=tidy(body),
        description=tidy(description),
        asset=tidy(asset),
        language=tidy(language).upper(),
        added_at=added_at,
        source=source,
        **meta,
    )


# ── правки: в репозиторий библиотеки или, без него, в файл на машине ──────

def _clean(raw_texts: dict) -> dict:
    known = AdText.__dataclass_fields__
    out = {}
    for key, raw in (raw_texts or {}).items():
        try:
            t = AdText(**{k: v for k, v in raw.items() if k in known})
        except Exception:
            continue
        if key == t.name and t.name.strip() and (t.headline.strip() or t.body.strip()) \
                and all(isinstance(getattr(t, k), str) for k in known):
            out[key] = t.to_dict()
    return out


def _apply_library(lib: dict) -> None:
    """Свежая версия библиотеки становится копией на этой машине."""
    deleted = lib.get("deleted") or {}
    with _transaction():
        team_library.preserve_local_copy(_store())
        _save_raw({"version": 1, "texts": _clean(lib.get("texts")),
                   "hidden": sorted({normalise_one(n) for n in deleted}), "deleted": deleted,
                   "library_synced_at": lib.get("library_synced_at", "")})


team_library.on_update("ad_texts", _apply_library, lambda: _load_raw().get("library_synced_at", ""))


def _mutate(change, message: str):
    if team_library.configured():
        try:
            return team_library.transact("ad_texts", change, message)[1]
        except team_library.WriteUncertain as exc:
            raise AdTextError(str(exc)) from exc
        except team_library.LibraryError as exc:
            raise AdTextError(f"{exc} Nothing was saved.") from exc
    with _transaction():
        data = _load_raw()
        result = change(data)
        data["hidden"] = sorted(set(data["hidden"]) | {normalise_one(n) for n in data["deleted"]})
        _save_raw(data)
        return result


def _forget_deleted(lib: dict, name: str) -> None:
    for key in [k for k in lib["deleted"] if normalise_one(k) == normalise_one(name)]:
        del lib["deleted"][key]
    if "hidden" in lib:
        lib["hidden"] = [n for n in lib["hidden"] if n != normalise_one(name)]


def add(name: str, headline: str, body: str, description: str = "",
        asset: str = "", language: str = "", *, source: str = "dashboard") -> AdText:
    def change(lib):
        _check(lib, name, headline, body, description)
        t = _make(name, headline, body, description, asset, language,
                  team_library.now(), source, added_by=team_library.author())
        lib["texts"][t.name] = t.to_dict()
        _forget_deleted(lib, t.name)
        return t
    return _mutate(change, f"Add ad text {tidy(name)}")


def update(old_name: str, name: str, headline: str, body: str, description: str = "",
           asset: str = "", language: str = "") -> AdText:
    def change(lib):
        if old_name not in lib["texts"]:
            raise AdTextError(f"There is no text “{old_name}” in the library. It may have been removed on another machine.")
        _check(lib, name, headline, body, description, ignore=old_name)
        was = lib["texts"].pop(old_name)
        t = _make(name, headline, body, description, asset, language,
                  was.get("added_at", ""), was.get("source", "dashboard"),
                  added_by=was.get("added_by", ""), updated_at=team_library.now(),
                  updated_by=team_library.author())
        lib["texts"][t.name] = t.to_dict()
        _forget_deleted(lib, t.name)
        return t
    return _mutate(change, f"Edit ad text {old_name}")


def remove(name: str) -> None:
    def change(lib):
        if name not in lib["texts"]:
            raise AdTextError(f"There is no text “{name}” in the library. It may have been removed on another machine.")
        lib["deleted"][name] = {"data": lib["texts"].pop(name),
                                "deleted_by": team_library.author(), "deleted_at": team_library.now()}
    _mutate(change, f"Remove ad text {name}")


def restore(name: str) -> AdText:
    def change(lib):
        rec = lib["deleted"].get(name)
        if rec is None:
            raise AdTextError(f"“{name}” is not in Recently deleted. It may have been restored on another machine.")
        was = rec.get("data") or {}
        _check(lib, name, was.get("headline", ""), was.get("body", ""), was.get("description", ""))
        t = _make(name, was.get("headline", ""), was.get("body", ""), was.get("description", ""),
                  was.get("asset", ""), was.get("language", ""), was.get("added_at", ""),
                  was.get("source", "dashboard"), added_by=was.get("added_by", ""),
                  updated_at=team_library.now(), updated_by=team_library.author())
        del lib["deleted"][name]
        lib["texts"][t.name] = t.to_dict()
        _forget_deleted(lib, t.name)
        return t
    return _mutate(change, f"Restore ad text {name}")


def recently_deleted() -> list[dict]:
    with _transaction():
        return team_library.recently_deleted(_load_raw())


def snapshot() -> dict:
    """Библиотека этой машины в формате репозитория — для первого наполнения."""
    with _transaction():
        data = _load_raw()
    return {"version": 1, "texts": data["texts"], "deleted": data["deleted"]}


def pull(*, force: bool = False) -> dict:
    return team_library.pull("ad_texts", force=force)


def lookup(name: str) -> AdText | None:
    key = normalise_one(name)
    return next((t for t in all_texts() if normalise_one(t.name) == key), None)
