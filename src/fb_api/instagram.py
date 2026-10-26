"""Page-linked and Page-backed Instagram identity (ACL-1896, ACL-2221).

Discovery is read-only. Preparation is a separate, owner-authorized publish step.
Meta SDK evidence and limitations: docs/system/PROJECT.md.
"""
from __future__ import annotations

import re
import json
import os
from pathlib import Path
from dataclasses import dataclass

from src.fb_api.graph import GraphError

IG_KEY = "instagram_user_id"
#: Id аккаунта - только цифры (тот же вид, что у id формы и видео).
IG_ID_RE = re.compile(r"^[0-9]{5,25}$")

FOUND, NONE, UNKNOWN, PREPARE = "found", "none", "unknown", "prepare"
EDGE = "page_backed_instagram_accounts"
PREPARE_NOTE = ("Use the Facebook Page to represent the business on Instagram. "
                "Its Page-backed Instagram identity will be prepared and verified when you publish; "
                "this check creates nothing.")

MSG_NO_INSTAGRAM = ("This Facebook Page has no Instagram account, and the ad is set for Instagram placements. "
                    "Connect an Instagram account to the Page, or choose Manual Facebook-only placements.")
MSG_UNKNOWN = ("Meta did not confirm an Instagram account for this Facebook Page ({why}), and the ad is set "
               "for Instagram placements. Check the plan again; nothing was created.")

#: Поля Страницы в порядке предпочтения: подключённый аккаунт, бизнес-аккаунт, Page-backed.
PAGE_FIELDS = (("connected_instagram_account", "page settings"),
               ("instagram_business_account", "business conversion"),
               ("connected_page_backed_instagram_account", "Page-backed"))
PAGE_FIELDS_PARAM = ",".join(f"{name}{{id,username}}" for name, _ in PAGE_FIELDS)
#: Сколько чтений Graph стоит одна идентичность (1 - Страница, 2 - кабинет, если у Страницы пусто).
READS_PER_PAGE = 2
PREPARE_CALLS = 4


@dataclass
class IgIdentity:
    status: str          # found | none | unknown
    ig_id: str = ""
    source: str = ""
    detail: str = ""

    @property
    def message(self) -> str:
        if self.status == PREPARE:
            return PREPARE_NOTE
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
        if item is None:
            continue
        got = str(item.get("id") or "") if isinstance(item, dict) else ""
        if not IG_ID_RE.fullmatch(got):
            return IgIdentity(UNKNOWN, detail=f"{name} has no usable id")
        if got == page_id:
            return IgIdentity(UNKNOWN, detail="Meta returned the Facebook Page id instead of an Instagram identity")
        if label == "Page-backed":
            try:
                if _intent(page_id):
                    return read_page_backed(client, page_id)
            except (OSError, ValueError) as exc:
                return IgIdentity(UNKNOWN, detail=str(exc))
        return IgIdentity(FOUND, got, label)
    return read_page_backed(client, page_id)


def intent_path(page_id: str) -> Path:
    from config.paths import get_local_config_dir
    if not re.fullmatch(r"[0-9]{1,25}", page_id):
        raise ValueError("The Facebook Page id is invalid")
    return Path(get_local_config_dir()) / "instagram_preparations" / (page_id + ".json")


def _intent(page_id: str) -> dict | None:
    path = intent_path(page_id)
    try:
        row = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    if not isinstance(row, dict) or row.get("state") != "pending":
        raise ValueError("unreadable Instagram preparation record")
    return row


def read_page_backed(client, page_id: str) -> IgIdentity:
    """One complete Page edge, never an unrelated ad-account identity."""
    try:
        body = client.get(f"/{page_id}/{EDGE}", {"fields": "id", "limit": 2})
        rows = body.get("data")
        if not isinstance(rows, list) or not isinstance(body.get("paging", {}), dict):
            raise ValueError("unreadable Page-backed response")
        if body.get("paging", {}).get("next"):
            raise ValueError("the Page-backed list is incomplete")
        ids = {r["id"] for r in rows if isinstance(r, dict) and isinstance(r.get("id"), str)
               and IG_ID_RE.fullmatch(r["id"]) and r["id"] != page_id}
        if len(ids) != len(rows) or len(ids) > 1:
            raise ValueError("the Page-backed identity is invalid or ambiguous")
        pending = _intent(page_id)
        if ids:
            got = next(iter(ids))
            if pending and pending.get("id") and pending["id"] != got:
                raise ValueError("the prepared identity does not match this Page")
            return IgIdentity(FOUND, got, "Page-backed")
        if pending:
            raise ValueError("a previous Facebook Page identity preparation is unconfirmed; "
                             "check the Page in Ads Manager before trying again")
        return IgIdentity(PREPARE, source="Facebook Page")
    except (GraphError, ValueError, OSError) as exc:
        return IgIdentity(UNKNOWN, detail=str(exc))


def prepare_identity(client, page_id: str, *, before_write=lambda: None) -> IgIdentity:
    """Exactly one POST per durable Page intent, followed by same-Page readback.

    Exclusive file creation covers different profiles sharing the Page. A crash,
    timeout, rejection or unconfirmed result leaves the record; empty subsequent
    reads never authorize another POST. Existing identities can reconcile it.
    """
    current = read_page_backed(client, page_id)
    if current.status != PREPARE:
        return current
    before_write()
    path = intent_path(page_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as fh:
            json.dump({"state": "pending"}, fh)
            fh.flush()
            os.fsync(fh.fileno())
    except FileExistsError:
        return read_page_backed(client, page_id)
    try:
        before_write()
    except BaseException:
        path.unlink()  # This invocation owns the intent and has not dispatched.
        raise
    body = client.post(f"/{page_id}/{EDGE}", {}, instagram_page=page_id)
    got = body.get("id")
    if not isinstance(got, str) or not IG_ID_RE.fullmatch(got) or got == page_id:
        return IgIdentity(UNKNOWN, detail="Meta did not return a usable Facebook Page Instagram identity")
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as fh:
        json.dump({"state": "pending", "id": got}, fh)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(temporary, path)
    confirmed = read_page_backed(client, page_id)
    if confirmed.status != FOUND or confirmed.ig_id != got:
        return IgIdentity(UNKNOWN, detail="Meta did not confirm the prepared identity on this Facebook Page")
    return confirmed
