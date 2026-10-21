"""Библиотека шаблонов Instant Form и их превращение в запрос Graph (ACL-1776).

Шаблон - переносимое содержание формы (заголовок, вопросы, политика, экран
«спасибо», язык). Сама форма на Странице создаётся роботом при публикации
(`POST /{page_id}/leadgen_forms`), по одному разу: имя формы несёт метку
робота и короткий хэш содержания, поэтому повторный запуск находит свою форму
по точному имени и не создаёт дубликат.

Условия Lead Ads (`tos_accepted`) здесь не встречаются вообще: ни в шаблоне,
ни в запросе. Страница, не принявшая условия, - остановка с просьбой к
человеку принять их в Meta.

Хранение (ACL-2054). Библиотека шаблонов общая на всю команду - третий вид
`team_library` (`form_templates.json` в приватном репозитории данных, ключ
записи - id шаблона): правка сразу в репозиторий с проверкой sha, удаление
мягкое (`deleted` + Restore). На машине лежит копия для чтения
(`form_templates_library.json`), по ней работает публикация. Не настроена
библиотека (нет ACL_LIBRARY_REPO / ACL_LIBRARY_TOKEN) - всё как раньше: файл
`form_templates.json` в машинной папке (`get_local_config_dir()`).

Запись, не прошедшая `validate` (старая, поправленная руками, с полем из
будущей версии), не выбрасывается и не стирается следующей записью: она
остаётся как есть, в списке помечается invalid, в публикацию не попадает.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

#: Потолки (Meta режет длинное молча; лучше отказать здесь, по-английски).
MAX_QUESTIONS = 5
MAX_OPTIONS = 10
MAX_NAME = 60
MAX_HEADLINE = 60
MAX_INTRO = 500
MAX_LABEL = 80
MAX_OPTION = 60
MAX_THANKS = 300
MAX_LINK_TEXT = 70
MAX_TEMPLATES = 200

#: Контактные поля шаблона -> тип вопроса Graph.
CONTACT_TYPES: dict[str, str] = {"name": "FULL_NAME", "email": "EMAIL", "phone": "PHONE"}
#: Тип пользовательского вопроса шаблона.
SHORT, CHOICE = "short_answer", "multiple_choice"
QUESTION_KINDS = (SHORT, CHOICE)

DEFAULT_LOCALE = "EN_US"
DEFAULT_LINK_TEXT = "Privacy Policy"
DEFAULT_THANKS_TITLE = "Thank you"
DEFAULT_BUTTON_TEXT = "Visit website"
#: Метка робота в имени формы: «<шаблон> · ACL <6 hex>».
ROBOT_NAME_RE = re.compile(r" · ACL [0-9a-f]{6}$")

_HTTPS_RE = re.compile(r"^https://[^\s/?#:]+\.[^\s/?#:]+(:\d+)?([/?#]\S*)?$", re.I)
_LOCALE_RE = re.compile(r"^[A-Z]{2}_[A-Z]{2}$")
_ID_RE = re.compile(r"^[0-9a-f]{12}$")

_lock = threading.RLock()


def is_https_url(url: str) -> bool:
    return bool(_HTTPS_RE.match(str(url or "").strip()))


# ───────────────────────── проверка и нормализация ─────────────────────────

def _s(value: Any) -> str:
    return " ".join(str(value if value is not None else "").split())


def _multiline(value: Any) -> str:
    text = str(value if value is not None else "").replace("\r\n", "\n").strip()
    return "\n".join(" ".join(line.split()) for line in text.split("\n"))


def validate(raw: Any) -> tuple[dict[str, Any], list[str]]:
    """Шаблон -> (чистый шаблон, ошибки). Без ошибок шаблон годен для формы.

    Ничего не угадывает: пустое обязательное поле - ошибка, лишний вопрос
    сверх потолка - ошибка (не обрезается молча).
    """
    errors: list[str] = []
    src = raw if isinstance(raw, dict) else {}
    out: dict[str, Any] = {"id": _s(src.get("id")).lower()}
    if out["id"] and not _ID_RE.match(out["id"]):
        out["id"] = ""
    out["name"] = _s(src.get("name"))
    if not out["name"]:
        errors.append("Template name is required")
    elif len(out["name"]) > MAX_NAME:
        errors.append(f"Template name is longer than {MAX_NAME} characters")
    out["headline"] = _s(src.get("headline"))
    if not out["headline"]:
        errors.append("Headline is required")
    elif len(out["headline"]) > MAX_HEADLINE:
        errors.append(f"Headline is longer than {MAX_HEADLINE} characters")
    out["intro"] = _multiline(src.get("intro"))
    if len(out["intro"]) > MAX_INTRO:
        errors.append(f"Intro is longer than {MAX_INTRO} characters")

    contacts = src.get("contact_fields")
    contacts = contacts if isinstance(contacts, (list, tuple)) else []
    chosen = []
    for key in contacts:
        key = _s(key).lower()
        if key not in CONTACT_TYPES:
            errors.append(f"Unknown contact field {key!r} (use name, email or phone)")
        elif key not in chosen:
            chosen.append(key)
    out["contact_fields"] = [k for k in CONTACT_TYPES if k in chosen]
    if not out["contact_fields"]:
        errors.append("Choose at least one contact field (name, email or phone)")

    questions = src.get("questions")
    questions = questions if isinstance(questions, (list, tuple)) else []
    clean_q: list[dict[str, Any]] = []
    for i, q in enumerate(questions, start=1):
        q = q if isinstance(q, dict) else {}
        label = _s(q.get("label"))
        kind = _s(q.get("type")).lower() or SHORT
        if not label:
            errors.append(f"Question {i}: text is required (remove the question if it is not needed)")
        elif len(label) > MAX_LABEL:
            errors.append(f"Question {i}: text is longer than {MAX_LABEL} characters")
        if kind not in QUESTION_KINDS:
            errors.append(f"Question {i}: type must be short answer or multiple choice")
            kind = SHORT
        item: dict[str, Any] = {"label": label, "type": kind}
        if kind == CHOICE:
            opts = [_s(o) for o in (q.get("options") if isinstance(q.get("options"), (list, tuple)) else [])]
            opts = [o for o in opts if o]
            if len(opts) < 2:
                errors.append(f"Question {i}: a multiple choice question needs at least 2 options")
            if len(opts) > MAX_OPTIONS:
                errors.append(f"Question {i}: more than {MAX_OPTIONS} options")
            if len(set(o.casefold() for o in opts)) != len(opts):
                errors.append(f"Question {i}: options must differ")
            if any(len(o) > MAX_OPTION for o in opts):
                errors.append(f"Question {i}: an option is longer than {MAX_OPTION} characters")
            item["options"] = opts
        clean_q.append(item)
    if len(clean_q) > MAX_QUESTIONS:
        errors.append(f"At most {MAX_QUESTIONS} custom questions")
    out["questions"] = clean_q

    out["privacy_url"] = str(src.get("privacy_url") or "").strip()
    if not out["privacy_url"]:
        errors.append("Privacy policy URL is required")
    elif not is_https_url(out["privacy_url"]):
        errors.append("Privacy policy URL must start with https://")
    out["privacy_link_text"] = _s(src.get("privacy_link_text")) or DEFAULT_LINK_TEXT
    if len(out["privacy_link_text"]) > MAX_LINK_TEXT:
        errors.append(f"Privacy link text is longer than {MAX_LINK_TEXT} characters")

    out["thank_you_title"] = _s(src.get("thank_you_title")) or DEFAULT_THANKS_TITLE
    out["thank_you_text"] = _multiline(src.get("thank_you_text"))
    if not out["thank_you_text"]:
        errors.append("Thank-you screen text is required")
    elif len(out["thank_you_text"]) > MAX_THANKS:
        errors.append(f"Thank-you screen text is longer than {MAX_THANKS} characters")
    out["website_url"] = str(src.get("website_url") or "").strip()
    if out["website_url"] and not is_https_url(out["website_url"]):
        errors.append("Website link on the thank-you screen must start with https://")
    out["button_text"] = _s(src.get("button_text")) or DEFAULT_BUTTON_TEXT

    locale = _s(src.get("locale")).upper().replace("-", "_") or DEFAULT_LOCALE
    if not _LOCALE_RE.match(locale):
        errors.append("Locale must look like EN_US")
    out["locale"] = locale
    return out, errors


def content_hash(tpl: dict[str, Any]) -> str:
    """Короткий хэш СОДЕРЖАНИЯ (без id и имени): другое содержание -> другая форма."""
    body = {k: tpl.get(k) for k in sorted(tpl) if k not in ("id", "name")}
    raw = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:6]


def form_name(tpl: dict[str, Any]) -> str:
    """Имя формы на Странице: «<шаблон> · ACL <хэш>». Ключ идемпотентности."""
    return f"{tpl['name']} · ACL {content_hash(tpl)}"


def expected_questions(tpl: dict[str, Any]) -> list[dict[str, Any]]:
    """Вопросы для Graph: контакты, затем пользовательские (CUSTOM)."""
    out: list[dict[str, Any]] = [{"type": CONTACT_TYPES[k]} for k in tpl["contact_fields"]]
    for i, q in enumerate(tpl["questions"], start=1):
        item: dict[str, Any] = {"type": "CUSTOM", "key": f"question_{i}", "label": q["label"]}
        if q["type"] == CHOICE:
            item["options"] = [{"key": f"q{i}_option_{j}", "value": v} for j, v in enumerate(q["options"], start=1)]
        out.append(item)
    return out


def form_params(tpl: dict[str, Any]) -> dict[str, Any]:
    """Тело `POST /{page}/leadgen_forms`: только то, что нужно шаблону. Без `tos_accepted`."""
    params: dict[str, Any] = {
        "name": form_name(tpl),
        "questions": expected_questions(tpl),
        "question_page_custom_headline": tpl["headline"],
        "privacy_policy": {"url": tpl["privacy_url"], "link_text": tpl["privacy_link_text"]},
        "locale": tpl["locale"],
    }
    if tpl.get("intro"):
        params["context_card"] = {"title": tpl["headline"], "style": "PARAGRAPH_STYLE",
                                  "content": [tpl["intro"]]}
    thanks: dict[str, Any] = {"title": tpl["thank_you_title"], "body": tpl["thank_you_text"]}
    if tpl.get("website_url"):
        thanks.update({"button_type": "VIEW_WEBSITE", "button_text": tpl["button_text"],
                       "website_url": tpl["website_url"]})
    else:
        # button_type обязателен; без ссылки на сайт - «без кнопки» (Meta: NONE).
        thanks["button_type"] = "NONE"
    params["thank_you_page"] = thanks
    return params


# ───────────────────────── хранилище ─────────────────────────


#: Метки автора и времени (added_by, added_at, updated_by, updated_at) лежат рядом
#: с содержанием, но validate их отбрасывает - на хэш формы они не влияют.
COPY_NAME = "form_templates_library.json"
KIND = "form_templates"

_CONTACT_LABEL = {"name": "Name", "email": "Email", "phone": "Phone"}


class TemplateError(ValueError):
    """Шаблон не сохранён; `errors` - список по-английски."""

    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


class LibraryUnavailable(TemplateError):
    """Общая библиотека недоступна: ничего не сохранено."""


def library_path() -> Path:
    """Локальный файл шаблонов машины (до командной библиотеки и без неё)."""
    from config.paths import get_local_config_dir

    return Path(get_local_config_dir()) / "form_templates.json"


def _copy_path() -> Path:
    return library_path().with_name(COPY_NAME)


def _tl():
    from src.analysis_agent import team_library

    return team_library


def _team() -> bool:
    return _tl().configured()


@contextmanager
def _transaction():
    from config.paths import machine_file_lock

    with _lock:
        with machine_file_lock(library_path().with_suffix(".lock")):
            yield


def _empty() -> dict[str, Any]:
    return {"version": 1, "templates": {}, "deleted": {}}


def _atomic_json(path: Path, data: dict[str, Any]) -> None:
    from config.paths import _atomic_write

    _atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8"))


def _lower_keys(snap: dict[str, Any]) -> dict[str, Any]:
    """Ключи записей - id в нижнем регистре (как всегда читал validate); при совпадении первая запись."""
    for part in ("templates", "deleted"):
        fixed: dict[str, Any] = {}
        for key, rec in snap[part].items():
            fixed.setdefault(key.strip().lower() if isinstance(key, str) else key, rec)
        snap[part] = fixed
    # Побеждает активная запись: удалённая с тем же ключом не показывается.
    for key in [k for k in snap["deleted"] if k in snap["templates"]]:
        del snap["deleted"][key]
    return snap


def snapshot() -> dict[str, Any]:
    """Локальные шаблоны машины в формате репозитория - для первого наполнения."""
    with _transaction():
        snap, _ = _local_read()
    return {"version": 1, "templates": snap["templates"], "deleted": snap["deleted"]}


def _name_of(rec: Any) -> str:
    return _s(rec.get("name")) if isinstance(rec, dict) else ""


# ── копия библиотеки на машине ──

def _read_copy() -> dict[str, Any]:
    try:
        data = json.loads(_copy_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _empty()
    if not isinstance(data, dict) or not isinstance(data.get("templates"), dict):
        return _empty()
    if not isinstance(data.get("deleted"), dict):
        data["deleted"] = {}
    return _lower_keys(data)


def _apply_library(lib: dict[str, Any]) -> None:
    """Свежая версия библиотеки становится копией для чтения на этой машине."""
    with _transaction():
        _atomic_json(_copy_path(), lib)


def _register() -> None:
    _tl().on_update(KIND, _apply_library, lambda: _read_copy().get("library_synced_at", ""))


_register()


# ── локальный файл машины ──

def _local_read(strict: bool = False) -> tuple[dict[str, Any], list[Any]]:
    """Локальный файл -> (снимок {templates: {id: запись}, deleted}, строки без id).

    Строки не чистятся: битые и без id возвращаются как есть, чтобы запись
    файла не стёрла их. `strict` - нечитаемый файл не заменяется пустым.
    """
    try:
        data = json.loads(library_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        data = {}
    except (OSError, ValueError) as exc:
        if strict:
            raise OSError("The local form template file could not be read") from exc
        data = {}
    snap = _empty()
    extras: list[Any] = []
    rows = data.get("templates") if isinstance(data, dict) else None
    for row in rows if isinstance(rows, list) else []:
        rid = row.get("id") if isinstance(row, dict) else None
        rid = rid.strip().lower() if isinstance(rid, str) else rid
        if isinstance(rid, str) and rid and rid not in snap["templates"]:
            snap["templates"][rid] = row
        else:
            extras.append(row)
    deleted = data.get("deleted") if isinstance(data, dict) else None
    if isinstance(deleted, dict):
        snap["deleted"] = deleted
    snap["_extras"] = extras      # для проверки имён; в файл не пишется
    return _lower_keys(snap), extras


def _local_write(snap: dict[str, Any], extras: list[Any]) -> None:
    rows = sorted(snap["templates"].values(), key=lambda r: _name_of(r).casefold()) + list(extras)
    _atomic_json(library_path(), {"templates": rows, "deleted": snap["deleted"]})


def _local_rows() -> list[Any]:
    with _transaction():
        snap, extras = _local_read()
    return list(snap["templates"].values()) + extras


# ── чтение ──

def pull(force: bool = False) -> dict[str, Any]:
    return _tl().pull(KIND, force=force)


def library_status() -> dict[str, Any]:
    return _tl().status(KIND)


def _snapshot(force: bool = False) -> dict[str, Any]:
    """Снимок для чтения: копия библиотеки, а без библиотеки - файл машины."""
    if _team():
        pull(force=force)
        with _transaction():
            return _read_copy()
    with _transaction():
        return _local_read()[0]


def load() -> list[dict[str, Any]]:
    """Годные к публикации шаблоны. Битые записи не исчезают, но и не отдаются."""
    out = []
    for key, rec in _snapshot()["templates"].items():
        clean, errors = validate(rec)
        if not errors and clean["id"] == key:
            out.append(clean)
    out.sort(key=lambda t: t["name"].casefold())
    return out


def get(template_id: str) -> dict[str, Any] | None:
    tid = str(template_id or "").strip().lower()
    return next((t for t in load() if t["id"] == tid), None)


def summary(tpl: dict[str, Any]) -> str:
    contacts = ", ".join(_CONTACT_LABEL[k] for k in tpl.get("contact_fields") or [] if k in _CONTACT_LABEL)
    n = len(tpl.get("questions") or [])
    return f"{contacts or 'No contact fields'} · " + (f"{n} question{'s' if n != 1 else ''}" if n else "no questions")


def _row(key: str, rec: Any) -> dict[str, Any]:
    clean, errors = validate(rec)
    if not errors and clean["id"] != key:
        errors = ["The template id does not match its library entry"]
    meta = rec if isinstance(rec, dict) else {}
    if _ID_RE.match(key):
        clean["id"] = key
    return {
        "id": key, "name": clean["name"] or key or "(no name)", "locale": clean["locale"], "summary": summary(clean),
        "author": str(meta.get("updated_by") or meta.get("added_by") or ""),
        "changed_at": str(meta.get("updated_at") or meta.get("added_at") or ""),
        "added_by": str(meta.get("added_by") or ""), "updated_by": str(meta.get("updated_by") or ""),
        "invalid": bool(errors), "errors": errors, "template": clean,
    }


def recently_deleted(snap: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    snap = snap if snap is not None else _snapshot()
    rows = []
    for key, rec in snap["deleted"].items():
        row = _row(key, rec.get("data"))
        row.update({"deleted_by": str(rec.get("deleted_by") or ""), "deleted_at": str(rec.get("deleted_at") or "")})
        rows.append(row)
    rows.sort(key=lambda r: r["deleted_at"], reverse=True)
    return rows[:100]


def listing(force: bool = False) -> dict[str, Any]:
    """Всё, что нужно вкладке Forms: строки (с invalid), удалённые, состояние, перенос."""
    snap = _snapshot(force)
    rows = [_row(k, r) for k, r in snap["templates"].items()]
    if not _team():
        # Строки локального файла без id: показываем (invalid), не трогаем.
        with _transaction():
            extras = _local_read()[1]
        for rec in extras:
            row = _row("", rec)
            row["errors"] = row["errors"] + ["The row has no id, so it cannot be edited here"]
            row["invalid"] = True
            row["id"] = ""
            rows.append(row)
    rows.sort(key=lambda r: r["name"].casefold())
    return {"rows": rows, "deleted": recently_deleted(snap), "library": library_status(),
            "migration": migration_status(snap)}


# ── правки: в репозиторий библиотеки или, без неё, в файл машины ──

def _mutate(change, message: str):
    tl = _tl()
    inner = change

    def change(lib):                       # noqa: F811 - обёртка: ключи всегда в нижнем регистре
        _lower_keys(lib)
        return inner(lib)

    if tl.configured():
        try:
            return tl.transact(KIND, change, message)[1]
        except tl.WriteUncertain as exc:
            raise LibraryUnavailable([str(exc)]) from exc
        except tl.LibraryError as exc:
            raise LibraryUnavailable([f"{exc} Nothing was saved."]) from exc
    with _transaction():
        snap, extras = _local_read(strict=True)
        result = change(snap)
        _local_write(snap, extras)
        return result


def _taken(lib: dict[str, Any], name: str, own: str = "") -> bool:
    own = own.lower()
    rows = [(k, r) for k, r in lib["templates"].items()] + [(None, r) for r in lib.get("_extras", [])]
    return any(k != own and _name_of(r).casefold() == name.casefold() for k, r in rows)


def _free_id(lib: dict[str, Any]) -> str:
    while True:
        tid = uuid.uuid4().hex[:12]
        if tid not in lib["templates"] and tid not in lib["deleted"]:
            return tid


def _stamp(rec: dict[str, Any], was: Any = None) -> dict[str, Any]:
    """Метки автора и времени поверх содержания шаблона."""
    tl = _tl()
    was = was if isinstance(was, dict) else {}
    if was.get("added_at") or was.get("added_by"):
        rec["added_by"], rec["added_at"] = str(was.get("added_by") or ""), str(was.get("added_at") or "")
        rec["updated_by"], rec["updated_at"] = tl.author(), tl.now()
    else:
        rec["added_by"], rec["added_at"] = tl.author(), tl.now()
    return rec


def save(raw: Any) -> dict[str, Any]:
    """Сохранить шаблон (новый - с новым id; с id - заменить). Ошибки - `TemplateError`."""
    clean, errors = validate(raw)
    if errors:
        raise TemplateError(errors)

    def change(lib):
        tid = clean["id"]
        if tid and tid in lib["deleted"] and tid not in lib["templates"]:
            raise TemplateError(["This template was deleted. Restore it from Recently deleted instead of editing it"])
        if _taken(lib, clean["name"], tid):
            raise TemplateError([f"A template named {clean['name']!r} already exists"])
        if tid not in lib["templates"] and len(lib["templates"]) >= MAX_TEMPLATES:
            raise TemplateError([f"At most {MAX_TEMPLATES} templates"])
        tid = tid or _free_id(lib)
        out = {**clean, "id": tid}
        was = lib["templates"].get(tid)
        # Поля записи, которых редактор не знает (поле из будущей версии), едут дальше.
        merged = {**(was if isinstance(was, dict) else {}), **out}
        lib["templates"][tid] = _stamp(merged, was)
        return out

    return _mutate(change, f"Save form template {clean['name']}")


def delete(template_id: str) -> bool:
    """Убрать шаблон в Recently deleted. Формы на Страницах не трогаются никогда."""
    tid = str(template_id or "").strip()

    def change(lib):
        key = tid if tid in lib["templates"] else tid.lower()
        if key not in lib["templates"]:
            return False
        lib["deleted"][key] = {"data": lib["templates"].pop(key),
                               "deleted_by": _tl().author(), "deleted_at": _tl().now()}
        return True

    return bool(tid) and _mutate(change, f"Delete form template {tid}")


def restore(template_id: str) -> dict[str, Any]:
    tid = str(template_id or "").strip()

    def change(lib):
        rec = lib["deleted"].get(tid)
        if rec is None:
            raise TemplateError(["This template is not in Recently deleted. It may have been restored on another machine"])
        data = dict(rec["data"])
        name = _name_of(data)
        if name and _taken(lib, name, tid):
            raise TemplateError([f"A template named {name!r} already exists. Rename or delete it first"])
        if len(lib["templates"]) >= MAX_TEMPLATES:
            raise TemplateError([f"At most {MAX_TEMPLATES} templates"])
        data["updated_by"], data["updated_at"] = _tl().author(), _tl().now()
        del lib["deleted"][tid]
        lib["templates"][tid] = data
        return validate(data)[0]

    return _mutate(change, f"Restore form template {tid}")


def duplicate(template_id: str) -> dict[str, Any]:
    tid = str(template_id or "").strip().lower()

    def change(lib):
        if tid not in lib["templates"]:
            raise TemplateError(["Template not found. It may have been removed on another machine"])
        clean, errors = validate(lib["templates"][tid])
        if errors:
            raise TemplateError(["Fix the template before duplicating it"] + errors)
        name = clean["name"]
        for n in range(1, 1000):
            suffix = " (copy)" if n == 1 else f" (copy {n})"
            name = clean["name"][:MAX_NAME - len(suffix)].rstrip() + suffix
            if not _taken(lib, name):
                break
        if len(lib["templates"]) >= MAX_TEMPLATES:
            raise TemplateError([f"At most {MAX_TEMPLATES} templates"])
        new = {**clean, "id": _free_id(lib), "name": name}
        lib["templates"][new["id"]] = _stamp(dict(new))
        return new

    return _mutate(change, f"Duplicate form template {tid}")


# ── перенос локальных шаблонов в общую библиотеку ──

def _plan_migration(local_rows: list[Any], lib: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Что из локального файла можно загрузить, и что и почему пропускается."""
    in_lib: dict[str, str | None] = {}
    for rec in lib["templates"].values():
        clean, errors = validate(rec)
        in_lib[_name_of(rec).casefold()] = None if errors else content_hash(clean)
    adds: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    used = set(lib["templates"]) | set(lib["deleted"])
    gone = {_name_of(rec.get("data")).casefold() for rec in lib["deleted"].values() if isinstance(rec, dict)}
    for row in local_rows:
        clean, errors = validate(row)
        name = clean["name"] or _name_of(row) or "(no name)"
        key = name.casefold()
        if errors:
            skipped.append({"name": name, "reason": "Invalid template: " + errors[0]})
        elif key in in_lib:
            same = in_lib[key] == content_hash(clean)
            skipped.append({"name": name, "reason": "Already in the team library" if same else
                            "A template with this name and different content is already in the team library"})
        elif key in gone:
            skipped.append({"name": name, "reason": "Deleted in the team library — restore it from Recently deleted instead"})
        elif len(lib["templates"]) + len(adds) >= MAX_TEMPLATES:
            skipped.append({"name": name, "reason": f"The team library is full ({MAX_TEMPLATES} templates)"})
        else:
            if not clean["id"] or clean["id"] in used:
                clean["id"] = ""
                while not clean["id"] or clean["id"] in used:
                    clean["id"] = uuid.uuid4().hex[:12]
            used.add(clean["id"])
            adds.append(clean)
            in_lib[key] = content_hash(clean)     # второй локальный с тем же именем уже не загрузится
    return adds, skipped


def migration_status(snap: dict[str, Any] | None = None) -> dict[str, Any]:
    """Сколько локальных шаблонов можно предложить загрузить в общую библиотеку."""
    if not _team():
        return {"available": False, "uploadable": 0, "skipped": []}
    adds, skipped = _plan_migration(_local_rows(), snap if snap is not None else _snapshot())
    return {"available": True, "uploadable": len(adds), "skipped": skipped}


def migrate_local() -> dict[str, Any]:
    """Загрузить локальные шаблоны одним коммитом. Локальный файл остаётся нетронутым."""
    if not _team():
        raise TemplateError(["The team library is not connected on this machine"])
    rows = _local_rows()
    tl = _tl()

    def change(lib):
        adds, skipped = _plan_migration(rows, lib)
        for clean in adds:
            lib["templates"][clean["id"]] = {**clean, "added_by": tl.author(), "added_at": tl.now()}
        return {"uploaded": len(adds), "skipped": skipped}

    return _mutate(change, "Upload local form templates")
