"""Страницы и пиксели кабинета для выбора в билдере и запись выбора в карту (ACL-1770).

Чтение только по явному нажатию «Read Pages and pixels»: ни опроса, ни таймеров. Путь тот же, что у
`page_forms` (замок чтения профиля, сессия Ads Manager, клиент БЕЗ права записи). Читаются
`/promote_pages` и `/adspixels`.

Выбор человека сохраняется в карту аккаунтов этой машины (`page_id` / `pixel_id`), но только в пустую
графу: уже заданное значение карты не перезаписывается молча.
"""
from __future__ import annotations

import csv
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

#: Вызовов Graph на одно нажатие: страницы кабинета + пиксели.
READ_MAX_CALLS = 4

_ID = re.compile(r"^\d{1,25}$")


def _rows(rows: list[dict]) -> list[dict[str, str]]:
    return [{"id": str(r.get("id")), "name": str(r.get("name") or "")} for r in rows if r.get("id")]


def read_options_with(client: Any, act: str) -> dict[str, Any]:
    """Страницы и пиксели кабинета через готовый клиент. Только чтения."""
    pages = client.get_all(f"/{act}/promote_pages", {"fields": "id,name", "limit": 100}, max_pages=1)
    pixels = client.get_all(f"/{act}/adspixels", {"fields": "id,name", "limit": 50}, max_pages=1)
    return {"pages": _rows(pages), "pixels": _rows(pixels)}


def map_defaults(account_label: str) -> dict[str, str]:
    """Страница и пиксель из карты (пусто - не заданы)."""
    from src.analysis_agent import account_spend as _as

    entry = _as._entry_for(account_label)
    return {"page_id": str(getattr(entry, "page_id", "") or "") if entry is not None else "",
            "pixel_id": str(getattr(entry, "pixel_id", "") or "") if entry is not None else ""}


def _read_in_session(account_label: str) -> dict[str, Any]:
    from src.analysis_agent import account_spend_api as _api

    with _api._session(account_label) as sess:
        client = sess.client
        if client.allow_writes:
            raise AssertionError("the Page and pixel read must not be able to write")
        client.reset_budget(READ_MAX_CALLS)
        got = read_options_with(client, sess.act)
        if client.writes:
            raise AssertionError("the Page and pixel read must not write")
    return got


def read_options(account_label: str) -> dict[str, Any]:
    """Всегда словарь: `ok`, `pages`, `pixels`, `map` (значения карты), `message`."""
    from src.analysis_agent import account_spend as _as
    from src.analysis_agent import account_spend_api as _api

    started = time.monotonic()
    run = _as.serialized_browser(key=_as._profile_key, reading=True)(_read_in_session)
    error = ""
    try:
        out = dict(run(account_label))
        out["ok"] = True
    except Exception as exc:  # noqa: BLE001 - отказ профиля/страницы: текстом человеку
        error = _api._failure(account_label, exc)[0]
        out = {"ok": False, "pages": [], "pixels": []}
    out["message"] = error or _summary(out)
    out["map"] = map_defaults(account_label)
    out["account"] = account_label
    out["refreshed_at"] = datetime.now(timezone.utc).isoformat()
    _as._record("api_page_pixels_read", not error, time.monotonic() - started,
                {"account": account_label, "source": "api", "pages": len(out["pages"]),
                 "pixels": len(out["pixels"])})
    return out


def _summary(out: dict[str, Any]) -> str:
    p, x = len(out.get("pages") or []), len(out.get("pixels") or [])
    return f"{p} Page{'' if p == 1 else 's'}, {x} pixel{'' if x == 1 else 's'} on the ad account"


class SaveRefused(ValueError):
    """Выбор не записан; текст - человеку."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def save_choice(account_label: str, *, page_id: str = "", pixel_id: str = "",
                map_path: str | Path | None = None) -> dict[str, str]:
    """Записать выбранные Страницу/пиксель в карту этой машины.

    Пишет только в пустую графу; другое значение в карте - отказ (его меняют на странице аккаунтов).
    Возвращает записанное: `{"page_id": ..., "pixel_id": ...}` (только записанные ключи).
    """
    from config.paths import write_csv_atomic

    page_id, pixel_id = (page_id or "").strip(), (pixel_id or "").strip()
    for what, val in (("Page", page_id), ("pixel", pixel_id)):
        if val and not _ID.match(val):
            raise SaveRefused(f"The {what} id must be a number.")
    if not page_id and not pixel_id:
        raise SaveRefused("Nothing to save: choose a Page or a pixel.")
    if map_path is None:
        from config.paths import get_ad_account_map_path
        map_path = get_ad_account_map_path()
    path = Path(map_path)
    if not path.is_file():
        raise SaveRefused("This machine has no account map yet; connect the account first.", 404)
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fields = list(reader.fieldnames or [])
    name = (account_label or "").strip().casefold()
    hits = [r for r in rows if (r.get("account_name") or r.get("account") or "").strip().casefold() == name]
    if not hits:
        raise SaveRefused("The account is not in this machine's account map.", 404)
    if len(hits) != 1:
        raise SaveRefused("The account name is ambiguous in the map; use the complete account name.", 409)
    hit = hits[0]
    saved: dict[str, str] = {}
    for col, val, what in (("page_id", page_id, "Page"), ("pixel_id", pixel_id, "pixel")):
        if not val:
            continue
        have = (hit.get(col) or "").strip()
        if have and have != val:
            raise SaveRefused(f"The map already has a {what} ({have}) for this account; "
                              "change it on the Accounts page.", 409)
        if not have:
            if col not in fields:
                fields.append(col)
            hit[col] = val
            saved[col] = val
    if saved:
        try:
            write_csv_atomic(path, fields, rows)
        except PermissionError:
            raise SaveRefused(f"{path.name} is open elsewhere (e.g. Excel) - close it and retry.", 409)
    return saved
