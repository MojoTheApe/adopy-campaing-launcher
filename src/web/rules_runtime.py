"""Rules T3 (ACL-1816): рабочая проводка движка оценки к дешборду.

Единственное место, где движок правил встречается с кэшем отчёта, описью
кампаний и целями My Goals. Все чтения локальные: книга из памяти процесса,
последний сохранённый снимок описи, `goals.json`. Meta не вызывается, профили не
открываются. Триггеры только ставят задание в очередь единственного потока.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any

from src.analysis_agent.rules.engine import RulesEngine
from src.analysis_agent.rules.gather import Sources
from src.analysis_agent.rules.store import RulesStore

log = logging.getLogger(__name__)

_LOCK = threading.Lock()
_ENGINE: RulesEngine | None = None
_INSTALLED = False


def _workbook() -> tuple[Any, Any, Any]:
    """Книга BI из кэша отчёта. Не собирает её: нет тёплой книги - нет оценки."""
    from src.web import cache

    entry = cache._CACHE.get("entry")  # noqa: SLF001 - готовый снимок кэша, читаем без сборки
    if not entry:
        return None, None, None
    built = cache.cache_built_at()
    stamp = None
    if built:
        try:
            stamp = datetime.fromisoformat(built).astimezone(timezone.utc).isoformat(timespec="seconds")
        except ValueError:
            stamp = None
    return entry[2], stamp, entry[0]


def _snapshot(account: str):
    from src.analysis_agent import campaign_inventory as ci

    return ci.get_snapshot(account)


def _aliases(account: str, country: str) -> dict[str, str]:
    """`{страна: имя аккаунта в книге}` для страны правила и других стран, где аккаунт есть в книге."""
    from src.analysis_agent.analyze_fb_stats import alias_matches
    from src.web import app as web

    wb = _workbook()
    df = wb[0]
    if df is None:
        return {}
    out: dict[str, str] = {}
    countries = [country] + [c for c in df["Country Final"].dropna().unique() if c != country]
    for c in countries:
        try:
            alias = web.workbook_alias_for(df, c, account)
            if c == country or df[(df["Country Final"] == c) & alias_matches(df["Account Alias"], alias)].shape[0]:
                out[c] = alias
        except Exception:  # noqa: BLE001
            log.exception("Alias lookup failed for %s / %s", account, c)
    return out


def _goals(cm: str, countries: Any) -> list[dict]:
    from src.web import goals

    return goals.for_cm(cm, countries)


def sources() -> Sources:
    return Sources(workbook=_workbook, snapshot=_snapshot, aliases=_aliases, goals=_goals)


def get_engine() -> RulesEngine:
    global _ENGINE
    with _LOCK:
        if _ENGINE is None:
            _ENGINE = RulesEngine(RulesStore(), sources())
        return _ENGINE


def _on_workbook(key: Any) -> None:
    get_engine().enqueue("workbook", str(key))


def _on_snapshot(done: Any) -> None:
    get_engine().enqueue("snapshot", done.account)


def install() -> None:
    """Подписаться на новую книгу и на удачное чтение описи. Повторный вызов безвреден."""
    global _INSTALLED
    from src.analysis_agent import campaign_inventory as ci
    from src.web import cache

    with _LOCK:
        if _INSTALLED:
            return
        _INSTALLED = True
    cache.add_build_listener(_on_workbook)
    ci.add_result_listener(_on_snapshot)
