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

Только чтения. Page-backed аккаунт сами не создаём (это запись; решение владельца).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from src.fb_api.graph import GraphError

IG_KEY = "instagram_user_id"
#: Id аккаунта - только цифры (тот же вид, что у id формы и видео).
IG_ID_RE = re.compile(r"^[0-9]{5,25}$")

FOUND, NONE, UNKNOWN = "found", "none", "unknown"

MSG_NO_INSTAGRAM = ("This Facebook Page has no Instagram account, and the ad is set for Instagram placements. "
                    "Connect an Instagram account to the Page, or wait for manual placements (ACL-1772) "
                    "to publish Facebook-only.")
MSG_UNKNOWN = ("Meta did not confirm an Instagram account for this Facebook Page ({why}), and the ad is set "
               "for Instagram placements. Check the plan again; nothing was created.")

#: Поля Страницы в порядке предпочтения: подключённый аккаунт, бизнес-аккаунт, Page-backed.
PAGE_FIELDS = (("connected_instagram_account", "page settings"),
               ("instagram_business_account", "business conversion"),
               ("connected_page_backed_instagram_account", "Page-backed"))
PAGE_FIELDS_PARAM = ",".join(f"{name}{{id,username}}" for name, _ in PAGE_FIELDS)
#: Сколько чтений Graph стоит одна идентичность (1 - Страница, 2 - кабинет, если у Страницы пусто).
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
        return IgIdentity(FOUND, got, label)
    # У Страницы ничего нет. Список кабинета - только для сведения (чей это аккаунт, неизвестно).
    try:
        rows = client.get_all(f"/{act}/instagram_accounts", {"fields": "id,username", "limit": 25}, max_pages=1)
    except GraphError as exc:
        return IgIdentity(UNKNOWN, detail=f"the ad account's Instagram accounts could not be read: {exc}")
    return IgIdentity(NONE, detail=f"the ad account lists {len(rows)} Instagram account(s), none linked to the Page")
