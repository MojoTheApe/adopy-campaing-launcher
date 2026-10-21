"""Библиотека шаблонов Instant Form и их превращение в запрос Graph (ACL-1776).

Шаблон - переносимое содержание формы (заголовок, вопросы, политика, экран
«спасибо», язык). Сама форма на Странице создаётся роботом при публикации
(`POST /{page_id}/leadgen_forms`), по одному разу: имя формы несёт метку
робота и короткий хэш содержания, поэтому повторный запуск находит свою форму
по точному имени и не создаёт дубликат.

Условия Lead Ads (`tos_accepted`) здесь не встречаются вообще: ни в шаблоне,
ни в запросе. Страница, не принявшая условия, - остановка с просьбой к
человеку принять их в Meta.

Хранение - `form_templates.json` в машинной папке (`get_local_config_dir()`),
как у остальных библиотек: не в гите, у каждого байера своя.
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
MAX_INTRO_TITLE = 60
MAX_FOLLOW_UP_URL = 1000
MAX_TEMPLATES = 200

#: Контактные поля шаблона -> тип вопроса Graph.
#: ACL-2053: имя и фамилия отдельными полями (FIRST_NAME/LAST_NAME) рядом с полным именем.
CONTACT_TYPES: dict[str, str] = {"name": "FULL_NAME", "email": "EMAIL", "phone": "PHONE",
                                 "first_name": "FIRST_NAME", "last_name": "LAST_NAME"}
#: Тип пользовательского вопроса шаблона.
SHORT, CHOICE = "short_answer", "multiple_choice"
QUESTION_KINDS = (SHORT, CHOICE)

DEFAULT_LOCALE = "EN_US"
#: ACL-2053: языки Instant Form, которые принимает Graph при создании формы (enum `locale` в
#: справочнике Page/leadgen_forms). Язык задаёт лишь встроенные подписи Meta, не текст формы.
#: Зеркало - `FORM_LOCALES` в report.html (сверяет тест).
LOCALES: tuple[str, ...] = (
    "AR_AR", "CS_CZ", "DA_DK", "DE_DE", "EL_GR", "EN_GB", "EN_US", "ES_ES", "ES_LA", "FI_FI", "FR_FR",
    "HE_IL", "HI_IN", "HU_HU", "ID_ID", "IT_IT", "JA_JP", "KO_KR", "NB_NO", "NL_NL", "PL_PL", "PT_BR",
    "PT_PT", "RO_RO", "RU_RU", "SV_SE", "TH_TH", "TR_TR", "VI_VN", "ZH_CN", "ZH_HK", "ZH_TW",
)
#: Тип формы -> `is_optimized_for_quality` (More volume = false, Higher intent = true).
FORM_TYPES: dict[str, bool] = {"more_volume": False, "higher_intent": True}
DEFAULT_FORM_TYPE = "more_volume"
DEFAULT_LINK_TEXT = "Privacy Policy"
DEFAULT_THANKS_TITLE = "Thank you"
DEFAULT_BUTTON_TEXT = "Visit website"
#: Метка робота в имени формы: «<шаблон> · ACL <6 hex>».
ROBOT_NAME_RE = re.compile(r" · ACL [0-9a-f]{6}$")

_HTTPS_RE = re.compile(r"^https://[^\s/?#:]+\.[^\s/?#:]+(:\d+)?([/?#]\S*)?$", re.I)
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
    # Новые поля (ACL-2053) кладём в чистый шаблон ТОЛЬКО со значением не по умолчанию:
    # так у старых шаблонов содержание, хэш и имя формы остаются прежними.
    intro_title = _s(src.get("intro_title"))
    if len(intro_title) > MAX_INTRO_TITLE:
        errors.append(f"Intro title is longer than {MAX_INTRO_TITLE} characters")
    if intro_title:
        out["intro_title"] = intro_title

    contacts = src.get("contact_fields")
    contacts = contacts if isinstance(contacts, (list, tuple)) else []
    chosen = []
    for key in contacts:
        key = _s(key).lower()
        if key not in CONTACT_TYPES:
            errors.append(f"Unknown contact field {key!r} (use name, first_name, last_name, email or phone)")
        elif key not in chosen:
            chosen.append(key)
    # Порядок - как выбран (у сохранённых шаблонов он уже name, email, phone).
    out["contact_fields"] = chosen
    if not chosen:
        errors.append("Choose at least one contact field (name, first name, last name, email or phone)")
    if "name" in chosen and ("first_name" in chosen or "last_name" in chosen):
        errors.append("Choose either full name or first and last name, not both")
    if src.get("questions_first") is True or _s(src.get("questions_first")).lower() == "true":
        out["questions_first"] = True

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

    follow_up = str(src.get("follow_up_action_url") or "").strip()
    if follow_up:
        out["follow_up_action_url"] = follow_up
        if not is_https_url(follow_up):
            errors.append("Follow-up URL must start with https://")
        elif len(follow_up) > MAX_FOLLOW_UP_URL:
            errors.append(f"Follow-up URL is longer than {MAX_FOLLOW_UP_URL} characters")

    out["thank_you_title"] = _s(src.get("thank_you_title")) or DEFAULT_THANKS_TITLE
    out["thank_you_text"] = _multiline(src.get("thank_you_text"))
    # Экран «спасибо» у Graph необязателен; без текста его можно опустить только
    # при ссылке follow-up (живая форма байера так и устроена).
    if not out["thank_you_text"]:
        if not follow_up:
            errors.append("Thank-you screen text is required")
    elif len(out["thank_you_text"]) > MAX_THANKS:
        errors.append(f"Thank-you screen text is longer than {MAX_THANKS} characters")
    out["website_url"] = str(src.get("website_url") or "").strip()
    if out["website_url"] and not is_https_url(out["website_url"]):
        errors.append("Website link on the thank-you screen must start with https://")
    out["button_text"] = _s(src.get("button_text")) or DEFAULT_BUTTON_TEXT

    locale = _s(src.get("locale")).upper().replace("-", "_") or DEFAULT_LOCALE
    if locale not in LOCALES:
        errors.append(f"Unknown locale: {locale} (choose a language from the list)")
    out["locale"] = locale

    form_type = _s(src.get("form_type")).lower() or DEFAULT_FORM_TYPE
    if form_type not in FORM_TYPES:
        errors.append("Form type must be More volume or Higher intent")
    elif form_type != DEFAULT_FORM_TYPE:
        out["form_type"] = form_type
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
    """Вопросы для Graph: контакты, затем пользовательские (CUSTOM); `questions_first` - наоборот."""
    contacts: list[dict[str, Any]] = [{"type": CONTACT_TYPES[k]} for k in tpl["contact_fields"]]
    custom: list[dict[str, Any]] = []
    for i, q in enumerate(tpl["questions"], start=1):
        item: dict[str, Any] = {"type": "CUSTOM", "key": f"question_{i}", "label": q["label"]}
        if q["type"] == CHOICE:
            item["options"] = [{"key": f"q{i}_option_{j}", "value": v} for j, v in enumerate(q["options"], start=1)]
        custom.append(item)
    return custom + contacts if tpl.get("questions_first") else contacts + custom


def form_params(tpl: dict[str, Any]) -> dict[str, Any]:
    """Тело `POST /{page}/leadgen_forms`: только то, что нужно шаблону. Без `tos_accepted`."""
    params: dict[str, Any] = {
        "name": form_name(tpl),
        "questions": expected_questions(tpl),
        "question_page_custom_headline": tpl["headline"],
        "privacy_policy": {"url": tpl["privacy_url"], "link_text": tpl["privacy_link_text"]},
        "locale": tpl["locale"],
        # Настройки формы (ACL-2053). «Больше объёма» = false; показ только целевым зрителям.
        # `allow_organic_lead` не шлём: при создании Graph его не принимает (есть лишь
        # устаревший allow_organic_lead_retrieval), «без органики» задаёт block_display_*.
        "is_optimized_for_quality": FORM_TYPES[tpl.get("form_type") or DEFAULT_FORM_TYPE],
        "block_display_for_non_targeted_viewer": True,
    }
    if tpl.get("follow_up_action_url"):
        params["follow_up_action_url"] = tpl["follow_up_action_url"]
    if tpl.get("intro"):
        params["context_card"] = {"title": tpl.get("intro_title") or tpl["headline"],
                                  "style": "PARAGRAPH_STYLE", "content": [tpl["intro"]]}
    if tpl.get("thank_you_text") or tpl.get("website_url"):
        thanks: dict[str, Any] = {"title": tpl["thank_you_title"]}
        if tpl.get("thank_you_text"):
            thanks["body"] = tpl["thank_you_text"]
        if tpl.get("website_url"):
            thanks.update({"button_type": "VIEW_WEBSITE", "button_text": tpl["button_text"],
                           "website_url": tpl["website_url"]})
        else:
            # button_type обязателен; без ссылки на сайт - «без кнопки» (Meta: NONE).
            thanks["button_type"] = "NONE"
        params["thank_you_page"] = thanks
    return params


# ───────────────────────── хранилище ─────────────────────────

def library_path() -> Path:
    from config.paths import get_local_config_dir

    return Path(get_local_config_dir()) / "form_templates.json"


@contextmanager
def _transaction():
    from config.paths import machine_file_lock

    with _lock:
        with machine_file_lock(library_path().with_suffix(".lock")):
            yield


def _read(strict: bool = False) -> list[dict[str, Any]]:
    path = library_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError):
        if strict:
            raise
        return []
    rows = data.get("templates") if isinstance(data, dict) else None
    out = []
    for row in rows if isinstance(rows, list) else []:
        clean, errors = validate(row)
        if clean["id"] and not errors:
            out.append(clean)
    return out


def _write(rows: list[dict[str, Any]]) -> None:
    from config.paths import _atomic_write

    _atomic_write(library_path(), json.dumps({"templates": rows}, ensure_ascii=False, indent=2,
                                             sort_keys=True).encode("utf-8"))


def load() -> list[dict[str, Any]]:
    """Все шаблоны (битые строки и битый файл молча пропускаются: страница не падает)."""
    with _lock:
        return _read()


def get(template_id: str) -> dict[str, Any] | None:
    tid = str(template_id or "").strip().lower()
    return next((t for t in load() if t["id"] == tid), None)


class TemplateError(ValueError):
    """Шаблон не сохранён; `errors` - список по-английски."""

    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


def save(raw: Any) -> dict[str, Any]:
    """Сохранить шаблон (новый - с новым id; с id - заменить). Ошибки - `TemplateError`."""
    clean, errors = validate(raw)
    if errors:
        raise TemplateError(errors)
    with _transaction():
        rows = _read(strict=True)
        if not clean["id"]:
            clean["id"] = uuid.uuid4().hex[:12]
        same = [t for t in rows if t["name"].casefold() == clean["name"].casefold() and t["id"] != clean["id"]]
        if same:
            raise TemplateError([f"A template named {clean['name']!r} already exists"])
        rows = [t for t in rows if t["id"] != clean["id"]]
        if len(rows) >= MAX_TEMPLATES:
            raise TemplateError([f"At most {MAX_TEMPLATES} templates"])
        rows.append(clean)
        rows.sort(key=lambda t: t["name"].casefold())
        _write(rows)
    return clean


def delete(template_id: str) -> bool:
    """Убрать шаблон ИЗ БИБЛИОТЕКИ (локально). Формы на Страницах не трогаются никогда."""
    tid = str(template_id or "").strip().lower()
    with _transaction():
        rows = _read(strict=True)
        left = [t for t in rows if t["id"] != tid]
        if len(left) == len(rows):
            return False
        _write(left)
    return True
