"""Machine-local library of ad texts — headline, body, description.

Устроено как реестр таргетов и по той же причине: тексты жили вкладкой в чужой
таблице, а таблица однажды уже отказала в доступе целиком (8 сентября,
сервисному аккаунту `403`). Здесь они лежат в обычном файле рядом с остальными
машинными данными; вкладка участвует только в наполнении.

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
    source: str = "dashboard"  # dashboard | sheet | local

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
        return {"version": 1, "texts": {}, "hidden": []}
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
        return data
    except Exception as exc:
        raise AdTextError(
            "Не удалось прочитать библиотеку текстов. Файл сохранён без изменений; "
            "восстановите его из резервной копии или загрузите заново из таблицы."
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
        tmp.replace(p)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def all_texts() -> list[AdText]:
    with _transaction():
        return sorted((AdText(**v) for v in _load_raw()["texts"].values()),
                      key=lambda t: normalise_one(t.name))


def ensure_loaded() -> dict:
    """Bootstrap a new machine once; an intentionally empty library stays empty."""
    with _transaction():
        initialized = _store().exists()
    if not initialized:
        return refresh()
    return {"tried": False, "error": "", "added": 0, "updated": 0, "skipped": [], "renamed": []}


def builder_texts() -> list[AdText]:
    report = ensure_loaded()
    if report.get("error") and not _store().exists():
        raise AdTextError("Не удалось загрузить библиотеку текстов: " + report["error"])
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
        raise AdTextError("Название, заголовок, тело и описание должны быть строками.")
    # Перенос строки в теле объявления законен — им разбивают абзацы.
    # Остальные управляющие символы приезжают из копипаста и ломают разметку.
    if any(ord(c) < 32 and c != "\n" for v in values for c in v):
        raise AdTextError("Уберите управляющие символы из полей текста.")
    if any(c == "\n" for c in name):
        raise AdTextError("Название текста должно быть в одну строку.")
    if not tidy(name):
        raise AdTextError("У текста должно быть название.")
    if not tidy(headline) and not tidy(body):
        raise AdTextError("Пустой текст сохранять незачем: заполните заголовок или тело.")
    for raw in data["texts"].values():
        t = AdText(**raw)
        if normalise_one(t.name) == normalise_one(ignore):
            continue
        if normalise_one(t.name) == normalise_one(name):
            raise AdTextError(f"Имя «{tidy(name)}» уже занято другим текстом.")
        if fingerprint(t.headline, t.body, t.description) == fingerprint(headline, body, description):
            raise AdTextError(
                f"Такой текст уже есть — «{t.name}». Заголовок, тело и описание "
                "совпадают; регистр и лишние пробелы значения не имеют."
            )


def _make(name: str, headline: str, body: str, description: str,
          asset: str, language: str, added_at: str, source: str) -> AdText:
    return AdText(
        name=tidy(name),
        headline=tidy(headline),
        body=tidy(body),
        description=tidy(description),
        asset=tidy(asset),
        language=tidy(language).upper(),
        added_at=added_at,
        source=source,
    )


def add(name: str, headline: str, body: str, description: str = "",
        asset: str = "", language: str = "", *, source: str = "dashboard") -> AdText:
    with _transaction():
        data = _load_raw()
        _check(data, name, headline, body, description)
        t = _make(name, headline, body, description, asset, language,
                  time.strftime("%Y-%m-%d %H:%M:%S"), source)
        data["texts"][t.name] = t.to_dict()
        data["hidden"] = [n for n in data["hidden"] if n != normalise_one(t.name)]
        _save_raw(data)
        return t


def update(old_name: str, name: str, headline: str, body: str, description: str = "",
           asset: str = "", language: str = "") -> AdText:
    with _transaction():
        data = _load_raw()
        if old_name not in data["texts"]:
            raise AdTextError(f"Текста «{old_name}» нет в библиотеке.")
        _check(data, name, headline, body, description, ignore=old_name)
        was = data["texts"].pop(old_name)
        t = _make(name, headline, body, description, asset, language, was["added_at"], "local")
        # Метка на прежнее имя: следующая загрузка из таблицы не воскресит его.
        data["hidden"] = sorted(set(data["hidden"]) | {normalise_one(old_name)})
        data["texts"][t.name] = t.to_dict()
        _save_raw(data)
        return t


def remove(name: str) -> None:
    with _transaction():
        data = _load_raw()
        if name not in data["texts"]:
            raise AdTextError(f"Текста «{name}» нет в библиотеке.")
        del data["texts"][name]
        data["hidden"] = sorted(set(data["hidden"]) | {normalise_one(name)})
        _save_raw(data)


def lookup(name: str) -> AdText | None:
    key = normalise_one(name)
    return next((t for t in all_texts() if normalise_one(t.name) == key), None)


#: Колонки вкладки «ad texts» в том порядке, в каком они там лежат.
SHEET_COLUMNS = ("Ad Title", "Headline", "Body", "Description", "Asset", "Language")


def import_texts(rows: Iterable) -> dict:
    """Наполнить библиотеку строками вкладки. Возвращает отчёт, а не число.

    Отчёт, потому что вкладка неровная: 8 сентября в ней было 327 строк, из них
    шесть без названия, шесть повторяющихся имён и шесть пар с одинаковым
    содержанием. Молчаливый перенос оставил бы человека в уверенности, что
    доехало всё, — а треть месяца спустя обнаружилось бы, что нет.

    Строки без названия получают имя по номеру строки: содержимое в них есть, и
    терять его из-за пустой первой клетки не за что. Имя видно в списке, его
    можно переписать.
    """
    with _transaction():
        data = _load_raw()
        before = json.dumps(data, sort_keys=True)
        report: dict = {"added": 0, "updated": 0, "skipped": [], "renamed": []}
        by_name = {normalise_one(k): k for k in data["texts"]}
        by_text = {fingerprint(v["headline"], v["body"], v["description"]): k
                   for k, v in data["texts"].items()}
        seen_here: set[str] = set()
        now = time.strftime("%Y-%m-%d %H:%M:%S")

        for number, row in enumerate(rows, start=2):  # строка 1 — заголовки
            cells = [str(c or "") for c in list(row)[:6]]
            cells += [""] * (6 - len(cells))
            name, headline, body, description, asset, language = cells
            if not any(tidy(c) for c in cells):
                continue
            if not tidy(headline) and not tidy(body):
                report["skipped"].append(f"строка {number}: ни заголовка, ни тела")
                continue
            if not tidy(name):
                name = f"Без названия · строка {number}"
                report["renamed"].append(name)

            key = normalise_one(name)
            if key in data["hidden"]:
                continue  # удалён или переименован на этой машине — не воскрешать

            mark = fingerprint(headline, body, description)
            twin = by_text.get(mark)
            if twin is not None and normalise_one(twin) != key:
                report["skipped"].append(f"строка {number} «{tidy(name)}»: то же содержание, что у «{twin}»")
                continue
            if key in seen_here:
                # Во вкладке имя встречается дважды. Побеждает первая строка —
                # так же, как в загрузчике таргетов, и так же предсказуемо.
                report["skipped"].append(f"строка {number}: имя «{tidy(name)}» уже занято строкой выше")
                continue

            old_key = by_name.get(key)
            if old_key and data["texts"][old_key]["source"] != "sheet":
                continue  # правку руками таблица не перебивает

            try:
                _check(data, name, headline, body, description, ignore=old_key or "")
            except AdTextError as exc:
                report["skipped"].append(f"строка {number}: {exc}")
                continue

            t = _make(name, headline, body, description, asset, language,
                      (data["texts"].get(old_key) or {}).get("added_at") or now, "sheet")
            seen_here.add(key)
            if old_key and data["texts"][old_key] == t.to_dict():
                continue  # строка не изменилась — это не «обновление»
            if old_key:
                old = data["texts"][old_key]
                old_mark = fingerprint(old["headline"], old["body"], old["description"])
                if by_text.get(old_mark) == old_key:
                    del by_text[old_mark]
            if old_key and old_key != t.name:
                del data["texts"][old_key]
            data["texts"][t.name] = t.to_dict()
            by_name[key] = t.name
            by_text[mark] = t.name
            report["added" if old_key is None else "updated"] += 1

        if json.dumps(data, sort_keys=True) != before:
            _save_raw(data)
        return report


def refresh(*, force: bool = False) -> dict:
    """Забрать вкладку и наполнить библиотеку. Ошибка чтения — не ошибка страницы."""
    from src.analysis_agent import gsheet_ad_texts as gs
    rows = gs.load_ad_text_rows(force=force)
    report = import_texts(rows) if rows else {"added": 0, "updated": 0, "skipped": [], "renamed": []}
    return {"tried": True, "error": gs.last_error, **report}
