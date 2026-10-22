"""Страницы Facebook аккаунта: кабинет (`promote_pages`) и собственные Страницы профиля (ACL-2105).

Ads Manager предлагает не только Страницы, привязанные к кабинету, но и те, которыми профиль
сам управляет с правом рекламы. Робот раньше читал только `promote_pages` и на таких
аккаунтах не создавал объявления. Список читается; отдельный validate-only creative
подтверждает связку кабинет/Страница без создания объектов.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

SOURCE_ACCOUNT = "ad account"
SOURCE_PROFILE = "profile"

#: Права на Странице, с которыми можно рекламировать (полный доступ включает рекламу).
ADVERTISE_TASKS = frozenset({"ADVERTISE", "MANAGE", "FULL_CONTROL"})
#: Страницы нового формата отдают те же права с префиксом PROFILE_PLUS_ (ACL-2110).
TASK_PREFIX = "PROFILE_PLUS_"


def normalized_tasks(tasks: Any) -> set[str]:
    """Права Страницы без префикса нового формата: `PROFILE_PLUS_X` считается как `X`."""
    if not isinstance(tasks, (list, tuple, set, frozenset)):
        return set()
    names = {str(t).strip().upper() for t in tasks}
    return names | {n[len(TASK_PREFIX):] for n in names if n.startswith(TASK_PREFIX)}


def has_advertise_rights(tasks: Any) -> bool:
    """Есть ли право рекламировать (классические и PROFILE_PLUS_ имена; неизвестные игнорируются)."""
    return bool(ADVERTISE_TASKS & normalized_tasks(tasks))


@dataclass
class MergedPages:
    rows: list[dict[str, str]] = field(default_factory=list)  # {id, name, source}, кабинет первым
    account_count: int = 0
    profile_count: int = 0          # Страницы профиля с правом рекламы, которых нет в кабинете
    profile_read: bool = False      # `/me/accounts` читался вообще
    profile_error: str = ""
    truncated: bool = False         # какой-то из списков прочитан не целиком
    account_truncated: bool = False

    def counts(self) -> dict[str, Any]:
        return {"account": self.account_count, "profile": self.profile_count,
                "profile_read": self.profile_read, "total": len(self.rows)}


def _digits(value: Any) -> str:
    text = str(value or "").strip()
    return text if text.isdigit() else ""


def account_rows(client: Any, act: str, *, max_pages: int = 5) -> tuple[list[dict], bool]:
    """Страницы кабинета (`promote_pages`) и признак «прочитано не целиком»."""
    path = f"/{act}/promote_pages"
    rows = client.get_all(path, {"fields": "id,name", "limit": 100}, max_pages=max_pages)
    return rows, path in (getattr(client, "truncated", None) or [])


def profile_rows(client: Any, *, max_pages: int = 3) -> tuple[list[dict], bool]:
    """Собственные Страницы профиля с правом рекламы (`/me/accounts`) и признак неполноты."""
    path = "/me/accounts"
    rows = client.get_all(path, {"fields": "id,name,tasks", "limit": 100}, max_pages=max_pages)
    keep = []
    for row in rows:
        if has_advertise_rights(row.get("tasks")):
            keep.append(row)
    return keep, path in (getattr(client, "truncated", None) or [])


def merge(account: list[dict], profile: list[dict]) -> list[dict[str, str]]:
    """Кабинет первым; повтор Страницы из профиля не добавляется."""
    out: dict[str, dict[str, str]] = {}
    for source, rows in ((SOURCE_ACCOUNT, account), (SOURCE_PROFILE, profile)):
        for row in rows:
            pid = _digits(row.get("id"))
            if pid and pid not in out:
                out[pid] = {"id": pid, "name": str(row.get("name") or pid), "source": source}
    return list(out.values())


def read_profile_into(client: Any, merged: MergedPages) -> None:
    """Дочитать профильные Страницы в уже прочитанный список. Остановки Graph не глотаются."""
    from src.fb_api.graph import GraphError

    merged.profile_read = True
    try:
        extra, cut = profile_rows(client)
    except GraphError as exc:
        # Чтение профиля не вышло: остаёмся с кабинетом, причину помним для телеметрии.
        merged.profile_error = type(exc).__name__
        return
    known = {r["id"] for r in merged.rows}
    merged.rows = merge([{"id": r["id"], "name": r["name"]} for r in merged.rows], extra)
    merged.profile_count = len([r for r in merged.rows if r["id"] not in known])
    merged.truncated = merged.truncated or cut


def read_merged(client: Any, act: str, *, always_profile: bool, account_max_pages: int = 5) -> MergedPages:
    """Страницы кабинета; профильные дочитываются, если кабинет пуст или `always_profile`."""
    rows, cut = account_rows(client, act, max_pages=account_max_pages)
    merged = MergedPages(rows=merge(rows, []), truncated=cut, account_truncated=cut)
    merged.account_count = len(merged.rows)
    if always_profile or not merged.rows:
        read_profile_into(client, merged)
    return merged


def record_counts(merged: MergedPages, *, where: str, started: float | None = None, account: str = "") -> None:
    """Телеметрия: сколько Страниц дал каждый источник. Журнал не должен ломать чтение."""
    try:
        from src.fb_ads_rpa import run_telemetry as _rt

        diag = {"where": where, "account_pages": merged.account_count, "profile_pages": merged.profile_count,
                "profile_read": merged.profile_read, "profile_error": merged.profile_error}
        if account:
            diag["account"] = account
        _rt.record("api_pages_sources", True,
                   duration_s=round(time.monotonic() - started, 1) if started is not None else 0.0, diag=diag)
    except Exception:  # noqa: BLE001
        pass


def check_usable(client: Any, page_id: str, name: str = "", *, act: str = "", link: str = "") -> str:
    """Meta must accept this Page in a validate-only creative for the actual account."""
    from src.fb_api.graph import GraphError, WritesBlocked

    try:
        got = client.validate_page(act, page_id, link)
    except GraphError as exc:
        return f"Meta did not prove the Page is usable for this ad account ({exc})"
    except WritesBlocked as exc:
        return f"Page validation is blocked by an unresolved earlier write ({exc})"
    except ValueError as exc:
        return str(exc)
    if not isinstance(got, dict) or got.get("success") is not True or "error" in got or "id" in got:
        return "Meta did not explicitly confirm the Page for this ad account"
    return ""
