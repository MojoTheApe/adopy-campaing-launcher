"""Заметки менеджера к аккаунтам и странам карты (ACL-1620, ACL-1633).

Колонка Setup на карте аккаунтов никому не была нужна; на её месте теперь
свободный текст, который менеджер пишет про аккаунт сам. Хранится по подписи
аккаунта (ровно так, как она показана в строке) в машинной папке
`%LOCALAPPDATA%/ACL/account_notes.json`: заметки одного байера не должны
становиться заметками всех, поэтому в гит файл не попадает.

Запись атомарная (временный файл + replace), чтение-изменение-запись идёт под
машинным замком — два одновременных сохранения не портят JSON и не теряют
чужую заметку.

Формат файла: {"notes": {аккаунт: текст}, "countries": {страна: текст}}. Старый файл
(ACL-1620) без ключа countries читается как есть и при первой записи получает
его — заметки аккаунтов не теряются, миграция не нужна.
"""
from __future__ import annotations

import json
import logging
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from config.paths import get_account_notes_path, _atomic_write, machine_file_lock

#: Потолок длины одной заметки (символов) — «разумный», как в Issue.
MAX_NOTE_LEN = 10_000
#: Потолок подписи аккаунта: подпись — ключ, а не текст.
MAX_LABEL_LEN = 300

_lock = threading.RLock()
log = logging.getLogger(__name__)


@contextmanager
def _transaction():
    with _lock:
        lock_path = Path(get_account_notes_path()).with_suffix(".lock")
        with machine_file_lock(lock_path):
            yield


class NoteChanged(Exception):
    """Условная запись (if_empty) отказала: заметку успели написать заново."""


def country_key(name: str) -> str:
    """Канонический ключ страны: «CR», «cr» и «Costa Rica» — одна заметка.

    Известное имя страны и двухбуквенный код сводятся к коду (тот же справочник,
    что у карты); неизвестное имя остаётся собой, без регистра — укорачивать
    его до двух букв, как делает _country_code, нельзя: две разные страны
    слились бы в одну заметку."""
    text = " ".join(str(name or "").split())
    if not text:
        return ""
    try:
        from src.web.app import _COUNTRY_CODES, _country_code
    except Exception:  # справочника нет под рукой — хотя бы без регистра
        return text.casefold()
    if len(text) == 2 and text.isalpha():
        return text.upper()
    if text.lower() in _COUNTRY_CODES:
        return _country_code(text)
    return text.casefold()


def _canon_countries(items: dict[str, str]) -> dict[str, str]:
    """Ключи стран — в каноническую форму. Два старых ключа, сошедшихся в один
    («CR» и «Costa Rica»), не теряются: тексты склеиваются."""
    out: dict[str, str] = {}
    for raw in sorted(items):
        key = country_key(raw)
        if not key:
            continue
        text = items[raw]
        out[key] = text if key not in out or out[key] == text else out[key] + "\n" + text
    return out


def _clean(items) -> dict[str, str]:
    if not isinstance(items, dict):
        return {}
    return {k: v for k, v in items.items() if isinstance(k, str) and isinstance(v, str) and v.strip()}


def _load_all() -> tuple[dict[str, str], dict[str, str]]:
    """(заметки аккаунтов, заметки стран). Битый или отсутствующий файл —
    пусто: страница не должна падать из-за испорченной заметки."""
    try:
        with open(get_account_notes_path(), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}, {}
    if not isinstance(data, dict):
        return {}, {}
    return _clean(data.get("notes")), _canon_countries(_clean(data.get("countries")))


def load() -> dict[str, str]:
    """Заметки аккаунтов."""
    return _load_all()[0]


def load_countries() -> dict[str, str]:
    """Заметки стран по каноническому ключу (старые ключи приводятся на чтении)."""
    return _load_all()[1]


def _backup_path(path: Path) -> Path:
    """Имя копии испорченного файла: время с миллисекундами; если такое имя
    всё же занято (две порчи в одну миллисекунду), добавляется счётчик, чтобы
    ни одна копия не затёрла другую."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
    backup = path.with_name(f"{path.name}.corrupt-{stamp}")
    n = 1
    while backup.exists():
        n += 1
        backup = path.with_name(f"{path.name}.corrupt-{stamp}-{n}")
    return backup


def _load_strict(report: dict | None = None) -> tuple[dict[str, str], dict[str, str]]:
    """Чтение для записи: ничего не теряет молча.

    Нет файла — пусто. Файл есть, но не читается (OSError) — отказ: запись
    поверх стёрла бы все остальные заметки. Файл испорчен — откладываем его
    в `account_notes.json.corrupt-<время>` и начинаем с пустого. Ключа
    `countries` может не быть (файл ACL-1620) — тогда страны пусты. Ключи стран
    приводятся к канонической форме, так что запись заодно мигрирует файл.

    report — необязательный словарь: если файл отложен в сторону, туда пишется
    `set_aside` = путь копии (так вызывающий может сказать об этом пользователю)."""
    path = Path(get_account_notes_path())
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}, {}
    try:
        data = json.loads(raw)
        if (not isinstance(data, dict) or not isinstance(data.get("notes"), dict)
                or not isinstance(data.get("countries", {}), dict)):
            raise ValueError("bad shape")
    except ValueError:
        backup = _backup_path(path)
        path.replace(backup)
        log.warning("Notes file %s was damaged; a copy was kept at %s", path, backup)
        if report is not None:
            report["set_aside"] = str(backup)
        return {}, {}
    return _clean(data["notes"]), _canon_countries(_clean(data.get("countries")))


def _save(notes: dict[str, str], countries: dict[str, str]) -> None:
    _atomic_write(get_account_notes_path(),
                  json.dumps({"notes": notes, "countries": countries}, ensure_ascii=False,
                             indent=2, sort_keys=True).encode("utf-8"))


def get(account: str) -> str:
    return load().get(account, "")


def set_note(account: str, text: str, kind: str = "account", if_empty: bool = False,
             report: dict | None = None) -> dict[str, str]:
    """Сохранить заметку; пустой текст удаляет её. Отдаёт заметки того же вида.

    kind: "account" или "country" (страна — по каноническому ключу). Подпись —
    ровно как в строке (только края обрезаются). Текст не нормализуем: переводы
    строк и пробелы внутри — часть заметки. if_empty — записать, только если
    заметки сейчас нет (так работает Undo: он не затирает свежий текст);
    иначе NoteChanged. ValueError — подпись пуста или слишком длинна, текст
    длиннее потолка или неизвестный вид. report — см. _load_strict: если
    испорченный файл отложен, в нём окажется `set_aside` (путь копии).
    """
    if kind not in ("account", "country"):
        raise ValueError("unknown note kind")
    label = (account or "").strip()
    if not label or len(label) > MAX_LABEL_LEN:
        raise ValueError("account is required" if kind == "account" else "country is required")
    if len(text) > MAX_NOTE_LEN:
        raise ValueError(f"note is longer than {MAX_NOTE_LEN} characters")
    key = country_key(label) if kind == "country" else label
    with _transaction():
        notes, countries = _load_strict(report)
        target = notes if kind == "account" else countries
        if if_empty and target.get(key, "").strip():
            raise NoteChanged(key)
        if text.strip():
            target[key] = text
        else:
            target.pop(key, None)
        _save(notes, countries)
        return target
