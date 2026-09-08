"""Реестр таргетов: хранится у нас, а не в чужой таблице.

Библиотека таргетов жила в Google Sheets, и это делало её заложницей чужого
доступа: 8 сентября лист начал отвечать `403`, а на дешборде было написано
«The library is empty or the sheet is unavailable» — две совершенно разные
беды одной строкой. Пустая библиотека и недоступная библиотека требуют разных
действий, и человек не мог узнать, какое из двух с ним случилось.

Здесь таргеты лежат в обычном файле рядом с остальными машинными данными.
Таблица, пока она читается, остаётся источником для первого наполнения — но
перестаёт быть условием работы.

**Что делает таргет уникальным.** Таргет — это набор интересов, а не строка.
Порядок в наборе ничего не значит: «один, два, три» и «три, один, два» —
один и тот же таргет, и второй раз его заводить незачем. А вот добавленное или
убранное значение делает таргет другим, и другое написание тоже: `investment`
и `investing` — разные интересы, потому что Meta разные и покажет.

Сужение (`narrow`) входит в опознание: те же интересы с разным сужением — это
разные аудитории, и складывать их в одну запись было бы неправдой.
"""
from __future__ import annotations

import json
import re
import time
import unicodedata
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable

from config import paths

#: Разделители внутри отпечатка. Пара непечатаемых знаков, которых в названии
#: интереса быть не может, — иначе интерес со скобкой или запятой в имени
#: слился бы с соседним и два разных таргета оказались бы «одинаковыми».
_SEP = "\x1f"
_PART = "\x1e"

_SPACE_RE = re.compile(r"\s+")


class TargetingError(RuntimeError):
    """Человеку есть что поправить: пустое поле, повтор, занятое имя."""


@dataclass
class Target:
    name: str
    interests: str          # как ввёл человек, через запятую
    narrow: str = ""
    added_at: str = ""
    source: str = "dashboard"   # dashboard | sheet

    def to_dict(self) -> dict:
        return asdict(self)


def _store() -> Path:
    return Path(paths.get_local_config_dir()) / "targeting_registry.json"


def normalise_one(name: str) -> str:
    """Одно значение интереса в сравнимом виде.

    Регистр и лишние пробелы — это способ записи, а не смысл: `Investment` и
    ` investment ` в Meta один и тот же интерес. Слова при этом не
    подрезаются: `investment` и `investing` остаются разными, потому что и
    аудитории у них разные.
    """
    text = unicodedata.normalize("NFKC", name or "")
    return _SPACE_RE.sub(" ", text).strip().casefold()


def split_values(raw: str) -> list[str]:
    """Строку через запятую — в список значений, без пустых."""
    return [v.strip() for v in (raw or "").split(",") if v.strip()]


def _key_part(raw: str) -> str:
    """Набор значений в виде, где порядок уже не важен.

    Именно здесь и живёт правило: сортируем, поэтому перестановка перестаёт
    что-либо менять; дубли внутри одного набора схлопываем, потому что
    «один, два, один» — это тот же набор, что «один, два».
    """
    seen = {normalise_one(v) for v in split_values(raw)}
    seen.discard("")
    return _SEP.join(sorted(seen))


def fingerprint(interests: str, narrow: str = "") -> str:
    """Отпечаток таргета: по нему и решается, новый он или уже есть."""
    return _key_part(interests) + _PART + _key_part(narrow)


def _load_raw() -> dict:
    p = _store()
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        # Испорченный файл — потеря реестра, а не повод уронить страницу.
        return {}


def _save_raw(data: dict) -> None:
    p = _store()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def all_targets() -> list[Target]:
    """Весь реестр, по имени."""
    known = {f.name for f in Target.__dataclass_fields__.values()}
    out = []
    for raw in _load_raw().values():
        if isinstance(raw, dict):
            out.append(Target(**{k: v for k, v in raw.items() if k in known}))
    return sorted(out, key=lambda t: t.name.casefold())


def find_duplicate(interests: str, narrow: str = "", *, ignore: str = "") -> Target | None:
    """Таргет с тем же набором значений — или ничего.

    `ignore` — имя записи, которую правят: сама себе она не повтор.
    """
    want = fingerprint(interests, narrow)
    for t in all_targets():
        if ignore and t.name.casefold() == ignore.casefold():
            continue
        if fingerprint(t.interests, t.narrow) == want:
            return t
    return None


def _check(name: str, interests: str, narrow: str, *, ignore: str = "") -> None:
    if not name.strip():
        raise TargetingError("У таргета должно быть название.")
    if not split_values(interests):
        raise TargetingError("Укажите хотя бы один интерес.")

    existing = {t.name.casefold() for t in all_targets()}
    if ignore:
        existing.discard(ignore.casefold())
    if name.strip().casefold() in existing:
        raise TargetingError(f"Имя «{name.strip()}» уже занято другим таргетом.")

    twin = find_duplicate(interests, narrow, ignore=ignore)
    if twin is not None:
        raise TargetingError(
            f"Такой таргет уже есть — «{twin.name}»: {twin.interests}"
            + (f" (сужение: {twin.narrow})" if twin.narrow else "")
            + ". Набор значений тот же, порядок значения не имеет."
        )


def share(target: Target) -> str:
    """Отправить таргет в общую таблицу. Возвращает ошибку или пустую строку.

    Реестр на этой машине — только половина дела: таргет, которого нет в общей
    таблице, не увидит никто, кроме автора. Ошибка возвращается, а не глотается,
    потому что «сохранено здесь» и «увидят все» — разные вещи, и человек должен
    знать, которая из них с ним случилась.
    """
    try:
        from src.analysis_agent.gsheet_targeting import add_targeting_profile

        add_targeting_profile(target.name, target.interests, target.narrow)
        return ""
    except Exception as exc:  # noqa: BLE001 — текст уходит человеку
        return str(exc)


def add(name: str, interests: str, narrow: str = "", *, source: str = "dashboard") -> Target:
    _check(name, interests, narrow)
    t = Target(
        name=name.strip(),
        interests=", ".join(split_values(interests)),
        narrow=", ".join(split_values(narrow)),
        added_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        source=source,
    )
    data = _load_raw()
    data[t.name] = t.to_dict()
    _save_raw(data)
    return t


def update(old_name: str, name: str, interests: str, narrow: str = "") -> Target:
    data = _load_raw()
    if old_name not in data:
        raise TargetingError(f"Таргета «{old_name}» нет в реестре.")
    _check(name, interests, narrow, ignore=old_name)
    was = data.pop(old_name)
    t = Target(
        name=name.strip(),
        interests=", ".join(split_values(interests)),
        narrow=", ".join(split_values(narrow)),
        added_at=str(was.get("added_at") or ""),
        source=str(was.get("source") or "dashboard"),
    )
    data[t.name] = t.to_dict()
    _save_raw(data)
    return t


def remove(name: str) -> None:
    data = _load_raw()
    if name not in data:
        raise TargetingError(f"Таргета «{name}» нет в реестре.")
    del data[name]
    _save_raw(data)


def import_targets(profiles: Iterable) -> int:
    """Перенести в реестр то, что есть в таблице. Возвращает, сколько добавлено.

    Повторы пропускаются молча: перенос — не повод спорить с человеком о том,
    что в таблице лежат два одинаковых набора под разными именами.
    """
    added = 0
    for p in profiles:
        try:
            add(
                getattr(p, "name", "") or "",
                getattr(p, "interests", "") or "",
                getattr(p, "narrow", "") or "",
                source="sheet",
            )
            added += 1
        except TargetingError:
            continue
    return added
