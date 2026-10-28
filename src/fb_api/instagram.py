"""Instagram-идентичность объявления (ACL-1896).

Живой прогон Doc120 (2026-10-14): кампания, набор и креатив созданы на паузе, затем Meta
отказала объявлению: Graph 100/1772103 «Instagram Account Is Missing». Создатель всегда
шлёт площадки Instagram (лента, Stories, Reels), а у Страницы нет подключённого
Instagram-аккаунта, и в креативе не было Instagram-идентичности.

Что подтверждено справочником Meta (developers.facebook.com, 2026-10-14):
- `object_story_spec.instagram_user_id` - строка из цифр, «аккаунт Instagram, от имени которого
  публикуется объявление». Поле `instagram_actor_id` в справочнике не упомянуто - не шлём.
- `GET /act_X/instagram_accounts` - рёбро только для чтения, список IGUser кабинета.
- Поля узла Page (чтение): `connected_instagram_account` («подключён через настройки Страницы»),
  `instagram_business_account` («привязан при переходе на бизнес-аккаунт Instagram»),
  `connected_page_backed_instagram_account` («связанный Page-backed аккаунт Instagram»).
  Рёбер Страницы `instagram_accounts` / `page_backed_instagram_accounts` в справочнике нет (404).

Что требует живой проверки (см. docs/system/PROJECT.md): отдаёт ли токен профиля эти поля Страницы;
принимает ли Meta именно этот id как `instagram_user_id`; возвращает ли чтение креатива поле
внутри `object_story_spec` или на верхнем уровне креатива.

Только чтения. Page-backed аккаунт сами не создаём (ACL-2221: нативная кнопка «Use Facebook Page» - внутренняя
GraphQL-мутация Ads Manager, публичное ребро POST отклонено; создаёт владелец, затем Check plan снова).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from src.fb_api.graph import GraphError

IG_KEY = "instagram_user_id"
#: Id аккаунта - только цифры (тот же вид, что у id формы и видео).
IG_ID_RE = re.compile(r"^[0-9]{5,25}$")

FOUND, NONE, UNKNOWN = "found", "none", "unknown"

#: ACL-2221: the identity is created by the owner with the native Ads Manager button, never by us.
MSG_NO_INSTAGRAM = ("This Page has no Instagram identity. In Ads Manager press 'Use Facebook Page' "
                    "for this Page, then run Check plan again.")
MSG_UNKNOWN = ("Meta did not confirm an Instagram account for this Facebook Page ({why}), and the ad is set "
               "for Instagram placements. Check the plan again; nothing was created.")

#: Поля Страницы в порядке предпочтения: подключённый аккаунт, бизнес-аккаунт, Page-backed.
PAGE_FIELDS = (("connected_instagram_account", "page settings"),
               ("instagram_business_account", "business conversion"),
               ("connected_page_backed_instagram_account", "Page-backed"))
PAGE_FIELDS_PARAM = ",".join(f"{name}{{id,username}}" for name, _ in PAGE_FIELDS)
#: ACL-2221: read-only list of the Page-backed identities of the Page (second read, only if the Page fields are empty).
PAGE_BACKED_EDGE = "page_backed_instagram_accounts"
#: Сколько чтений Graph стоит одна идентичность (1 - Страница, 2 - её Page-backed список, если поля пусты).
READS_PER_PAGE = 2


@dataclass
class IgIdentity:
    status: str          # found | none | unknown
    ig_id: str = ""
    source: str = ""
    detail: str = ""

    @property
    def message(self) -> str:
        if self.status == NONE:
            return MSG_NO_INSTAGRAM
        return MSG_UNKNOWN.format(why=self.detail or "no answer")


def read_identity(client, act: str, page_id: str) -> IgIdentity:
    """Два дешёвых чтения (второе - только если у Страницы пусто). Остановки Graph (`GraphStop`) идут выше;
    отказ чтения - `unknown`, и он НЕ превращается в «есть аккаунт»."""
    try:
        row = client.get(f"/{page_id}", {"fields": PAGE_FIELDS_PARAM})
    except GraphError as exc:
        return IgIdentity(UNKNOWN, detail=f"the Page could not be read: {exc}")
    if not isinstance(row, dict):
        return IgIdentity(UNKNOWN, detail="the Page answer was not readable")
    for name, label in PAGE_FIELDS:
        item = row.get(name)
        if not item:
            continue
        got = str(item.get("id") or "") if isinstance(item, dict) else ""
        if not IG_ID_RE.fullmatch(got):
            return IgIdentity(UNKNOWN, detail=f"{name} has no usable id")
        if got == page_id:
            return IgIdentity(UNKNOWN, detail="Meta returned the Facebook Page id instead of an Instagram identity")
        return IgIdentity(FOUND, got, label)
    return read_page_backed(client, page_id)


def read_page_backed(client, page_id: str) -> IgIdentity:
    """ACL-2221: an already existing Page-backed identity of exactly this Page; read only.

    Creating one is the owner's click on the native "Use Facebook Page" button (an internal Ads Manager
    GraphQL mutation); the public POST edge is rejected on our transport, so we never write here.
    A complete empty list is `none`; failed, partial, malformed or ambiguous answers are `unknown`.
    Graph error 100 means the edge is not readable on this connection: the Page fields above are then
    the only evidence, so it reads as `none` (the owner is told to press the native button).
    """
    try:
        body = client.get(f"/{page_id}/{PAGE_BACKED_EDGE}", {"fields": "id", "limit": 2})
    except GraphError as exc:
        if exc.code == 100:
            return IgIdentity(NONE, detail="the Page-backed list is not readable on this connection")
        return IgIdentity(UNKNOWN, detail=f"the Page-backed Instagram accounts could not be read: {exc}")
    rows = body.get("data") if isinstance(body, dict) else None
    paging = body.get("paging") if isinstance(body, dict) else None
    if not isinstance(rows, list) or not (paging is None or isinstance(paging, dict)):
        return IgIdentity(UNKNOWN, detail="the Page-backed answer was not readable")
    if (paging or {}).get("next"):
        return IgIdentity(UNKNOWN, detail="the Page-backed list is incomplete")
    ids = {r["id"] for r in rows if isinstance(r, dict) and isinstance(r.get("id"), str)
           and IG_ID_RE.fullmatch(r["id"]) and r["id"] != page_id}
    if len(ids) != len(rows) or len(ids) > 1:
        return IgIdentity(UNKNOWN, detail="the Page-backed identity is invalid or ambiguous")
    if ids:
        return IgIdentity(FOUND, next(iter(ids)), "Page-backed")
    return IgIdentity(NONE, detail="the Page has no linked or Page-backed Instagram identity")
