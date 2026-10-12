"""Rules T3 (ACL-1816): минимальный API только для чтения оценок.

* `GET /rules/evaluations?rule=…` - журнал оценок (последние записи);
* `POST /rules/evaluate-now` - ставит оценку в очередь и сразу отвечает.

Оба - только при `ACL_FB_BACKEND=api` и привязке сервера к loopback, как команды
над кампаниями. Ничего не пишут в Meta и не открывают профили.
"""
from __future__ import annotations

from typing import Any

from src.web import campaign_commands as _guards

DEFAULT_LIMIT = 50
MAX_LIMIT = 500


def _guard() -> tuple[dict, int] | None:
    if not _guards.api_enabled():
        return {"error": "Rules need the Graph API backend (ACL_FB_BACKEND=api).", "code": "rpa_backend"}, 409
    if not _guards.bound_to_loopback():
        return {"error": "Rules are only available when the dashboard listens on this computer alone (127.0.0.1).",
                "code": "not_loopback"}, 403
    return None


def list_evaluations(rule_id: str, limit: Any = None, *, store: Any = None) -> tuple[dict, int]:
    refused = _guard()
    if refused:
        return refused
    if store is None:
        from src.analysis_agent.rules.store import RulesStore
        store = RulesStore()
    try:
        n = max(1, min(MAX_LIMIT, int(limit))) if limit not in (None, "") else DEFAULT_LIMIT
    except (TypeError, ValueError):
        n = DEFAULT_LIMIT
    rows = [r for r in store.evaluations() if not rule_id or r.get("rule_id") == rule_id]
    return {"ok": True, "rule": rule_id, "evaluations": rows[-n:]}, 200


def evaluate_now(body: dict, *, engine: Any = None) -> tuple[dict, int]:
    refused = _guard()
    if refused:
        return refused
    rule_id = str((body or {}).get("rule_id") or "").strip()
    if engine is None:
        from src.web import rules_runtime
        engine = rules_runtime.get_engine()
    queued = engine.enqueue("manual", rule_id or None)
    return {"ok": True, "queued": queued, "rule_id": rule_id, "status": engine.status()}, 202
