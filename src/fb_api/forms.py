"""Формы лидов Страницы: только чтение (ACL-1775).

Читаем `GET /{page_id}/leadgen_forms` (id, name, status, locale, created_time)
тем же `GraphClient` и той же сессией профиля, что и остальные чтения. Формы
здесь НЕ создаются (это ACL-1776) и условия Lead Ads не принимаются никогда.

Сможет ли токен, снятый в профиле, прочитать формы, заранее неизвестно:
Meta обычно требует для этого права `leads_retrieval`/`pages_manage_ads` и
токен Страницы. Поэтому есть чистая проба возможности `forms_capability`:
по ответу (строки или ошибка Graph) она говорит, что именно произошло, и
человек видит понятный текст, а не сырую ошибку.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from src.fb_api.graph import GraphClient, GraphError

FORMS_FIELDS = "id,name,status,locale,created_time"
#: В объявление идёт только форма со статусом ACTIVE; пустой или незнакомый
#: статус - не годится (ревью m1). Остальных в выборе нет.
USABLE_STATUS = "ACTIVE"
PAGE_ID_RE = re.compile(r"^[0-9]{1,25}$")

MSG_UNREADABLE = "This Page's forms can't be read with the current session"
MSG_TERMS = ("This Page has not accepted the Lead Ads terms. Accept them in Meta "
             "(the robot never accepts terms), then refresh the forms")
MSG_NO_PAGE = "No Page is set for this account, so its forms can't be listed"
MSG_EMPTY = "This Page has no Instant Forms yet. Create one in Meta, then refresh the forms"

#: Состояния чтения.
OK, EMPTY, UNREADABLE, TERMS, NO_PAGE = "ok", "empty", "unreadable", "terms", "no_page"


@dataclass(frozen=True)
class PageForm:
    id: str
    name: str
    status: str = ""
    locale: str = ""
    created_time: str = ""

    @property
    def usable(self) -> bool:
        return self.status.strip().upper() == USABLE_STATUS

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "status": self.status, "locale": self.locale,
                "created_time": self.created_time}


@dataclass
class FormsRead:
    """Итог чтения форм Страницы (или причина, почему прочитать нельзя)."""

    state: str
    page_id: str = ""
    forms: list[PageForm] = field(default_factory=list)
    message: str = ""
    #: Код ответа Meta при отказе — для разбора; в текст человеку не идёт.
    code: int | None = None

    @property
    def ok(self) -> bool:
        return self.state in (OK, EMPTY)

    @property
    def usable(self) -> list[PageForm]:
        return [f for f in self.forms if f.usable]

    def by_id(self) -> dict[str, PageForm]:
        return {f.id: f for f in self.forms}

    def as_dict(self) -> dict[str, Any]:
        return {"state": self.state, "ok": self.ok, "page_id": self.page_id, "message": self.message,
                "forms": [f.as_dict() for f in self.usable]}


def parse_forms(rows: list[dict]) -> list[PageForm]:
    out: list[PageForm] = []
    for row in rows or []:
        fid = str((row or {}).get("id") or "").strip()
        if not fid:
            continue
        out.append(PageForm(id=fid, name=str(row.get("name") or "").strip() or fid,
                            status=str(row.get("status") or "").strip(),
                            locale=str(row.get("locale") or "").strip(),
                            created_time=str(row.get("created_time") or "").strip()))
    return out


def mentions_terms(text: str) -> bool:
    low = (text or "").lower()
    return "terms" in low or "tos" in low.split() or "leadgen_tos" in low or "terms_of_service" in low


def forms_capability(page_id: str, rows: list[dict] | None, error: GraphError | None = None, *,
                     terms_accepted: bool | None = None) -> FormsRead:
    """Чистая проба возможности: что значит этот ответ Graph для выбора формы.

    `rows` — строки списка форм (None, если чтение упало с `error`).
    `terms_accepted` — значение `leadgen_tos_accepted` Страницы, если его
    удалось прочитать (None — неизвестно). Сети не трогает.
    """
    if not PAGE_ID_RE.fullmatch(str(page_id or "")):
        return FormsRead(NO_PAGE, message=MSG_NO_PAGE)
    if error is not None:
        if mentions_terms(str(error)) or terms_accepted is False:
            return FormsRead(TERMS, page_id=page_id, message=MSG_TERMS, code=error.code)
        return FormsRead(UNREADABLE, page_id=page_id, message=MSG_UNREADABLE, code=error.code)
    forms = parse_forms(rows or [])
    if not forms:
        if terms_accepted is False:
            return FormsRead(TERMS, page_id=page_id, message=MSG_TERMS)
        return FormsRead(EMPTY, page_id=page_id, message=MSG_EMPTY)
    return FormsRead(OK, page_id=page_id, forms=forms)


def page_terms_accepted(client: GraphClient, page_id: str) -> bool | None:
    """`leadgen_tos_accepted` Страницы: True/False, None — прочитать не удалось."""
    try:
        got = client.get(f"/{page_id}", {"fields": "leadgen_tos_accepted"})
    except GraphError:
        return None
    value = got.get("leadgen_tos_accepted") if isinstance(got, dict) else None
    return value if isinstance(value, bool) else None


def read_page_forms(client: GraphClient, page_id: str) -> FormsRead:
    """Формы Страницы: одно чтение списка (+ одно — условия, только если список пуст/недоступен).

    Остановки клиента (`GraphStop`: лимит, checkpoint, выход из сессии) не
    глотаются: решает человек. Ошибки Meta — `GraphError` — превращаются в
    понятный итог.
    """
    page_id = str(page_id or "").strip()
    if not PAGE_ID_RE.fullmatch(page_id):
        return forms_capability(page_id, [])
    try:
        rows = client.get_all(f"/{page_id}/leadgen_forms", {"fields": FORMS_FIELDS, "limit": 100},
                              max_pages=3)
    except GraphError as exc:
        return forms_capability(page_id, None, exc, terms_accepted=page_terms_accepted(client, page_id))
    first = forms_capability(page_id, rows)
    if first.state == EMPTY:
        return forms_capability(page_id, rows, terms_accepted=page_terms_accepted(client, page_id))
    return first
