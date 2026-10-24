"""Эндпоинты команд над кампаниями: превью, исполнение, статус (ACL-1800).

Только при `ACL_FB_BACKEND=api`; при `rpa` — чёткий отказ. Сессию (профиль →
вкладка → клиент Graph) даёт тот же `account_spend_api._session`, что и чтениям:
охлаждения (190/368/checkpoint), очередь мест по памяти и правило «чужой
профиль не останавливаем» — оттуда. Браузер для превью и для записи берётся
тем же замком профиля, что у чтений и публикации.

Логика — в `src/fb_api/commands.py`; здесь только проводка и коды ответов.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Any

from src.fb_api import commands as _cmd
from src.fb_api.graph import GraphError, GraphStop

BACKEND_ENV = "ACL_FB_BACKEND"
RPA_REFUSAL = ("Campaign commands need the Graph API backend (ACL_FB_BACKEND=api). "
               "This machine uses the RPA backend, so nothing was changed.")


def api_enabled() -> bool:
    from src.web import publish_api

    return publish_api.api_enabled()


def _failure(account: str, exc: BaseException) -> str:
    from src.analysis_agent import account_spend_api as _api

    return _api._failure(account, exc)[0]


def _refusal(exc: _cmd.CommandRefused) -> tuple[dict, int]:
    return {"error": str(exc), "code": exc.code}, exc.status


BIND_HOST_ENV = "ACL_BIND_HOST"
LOOPBACK_REFUSAL = ("Campaign commands are only available when the dashboard listens on this "
                    "computer alone (127.0.0.1). It is bound to a network address, so nothing "
                    "was changed.")


def bound_to_loopback() -> bool:
    """Адрес привязки сервера (его ставит `serve.py`) — loopback. Не известен — нет."""
    import ipaddress
    import os

    host = (os.environ.get(BIND_HOST_ENV) or "").strip().strip("[]")
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _guard() -> tuple[dict, int] | None:
    if not api_enabled():
        return {"error": RPA_REFUSAL, "code": "rpa_backend"}, 409
    if not bound_to_loopback():
        return {"error": LOOPBACK_REFUSAL, "code": "not_loopback"}, 403
    return None


def availability() -> dict:
    """Что страница может предлагать: включены ли команды и почему нет. Ничего не читает у Meta."""
    if not api_enabled():
        return {"available": False, "code": "rpa_backend", "reason": "Needs ACL_FB_BACKEND=api",
                "max_targets": 0}
    if not bound_to_loopback():
        return {"available": False, "code": "not_loopback",
                "reason": "Only when the dashboard listens on this computer alone (127.0.0.1)",
                "max_targets": 0}
    return {"available": True, "code": "", "reason": "",
            "max_targets": min(_cmd.MAX_TARGETS, _cmd.max_targets_for_ceiling())}


def run_preview(body: dict[str, Any]) -> tuple[dict, int]:
    refused = _guard()
    if refused:
        return refused
    account = str(body.get("account") or "")
    try:
        from src.analysis_agent import campaign_inventory
        saved = campaign_inventory.get_snapshot(account)
        baseline = {}
        if saved is not None:
            for level, rows in (("campaign", saved.campaigns), ("adset", saved.adsets), ("ad", saved.ads)):
                baseline.update({level + ":" + str(row["id"]): row for row in rows})
        kwargs = {"baseline": baseline} if baseline else {}
        op = _cmd.preview(_cmd.get_store(), _sessions(), account=account,
                          actor=body.get("actor"), command=str(body.get("command") or ""),
                          value=body.get("value"), targets=body.get("targets"), **kwargs)
    except _cmd.CommandRefused as exc:
        if exc.code == "stale":
            _refresh_stale({"account": account})
        return _refusal(exc)
    except (GraphStop, GraphError) as exc:
        return {"error": _failure(account, exc), "code": getattr(exc, "kind", "")}, 502
    except Exception as exc:  # noqa: BLE001 — профиль не открылся и т. п.
        return {"error": _failure(account, exc), "code": getattr(exc, "code", "")}, 503
    return op, 200


def run_execute(body: dict[str, Any]) -> tuple[dict, int]:
    refused = _guard()
    if refused:
        return refused
    try:
        op = _cmd.execute(_cmd.get_store(), _sessions(), revision_id=str(body.get("revision_id") or ""),
                          idempotency_key=str(body.get("idempotency_key") or ""),
                          after_write=_refresh_after_write, on_stale=_refresh_stale)
    except _cmd.CommandRefused as exc:
        return _refusal(exc)
    return op, 202 if op.get("state") in (_cmd.RUNNING, _cmd.VERIFYING) else 200


def _refresh_after_write(op: dict, sess: Any) -> None:
    from src.analysis_agent import account_refresh, campaign_inventory

    snap = campaign_inventory.read_in_session(op["account"], sess, trigger="action")
    account_refresh.queue().record_result(snap, trigger="action")


def _refresh_stale(op: dict) -> None:
    from src.analysis_agent import account_refresh

    q = account_refresh.queue()
    q.request(op["account"], "action", force=True)
    q.start()


def get_status(op_id: str) -> tuple[dict, int]:
    refused = _guard()
    if refused:
        return refused
    op = _cmd.status(_cmd.get_store(), op_id)
    if op is None:
        return {"error": "Unknown operation."}, 404
    return op, 200


def account_history(account: str, limit: int = 20) -> tuple[dict, int]:
    """Журнал команд одного аккаунта («What was changed here» под его строкой).

    Только чтение локального журнала, Meta не трогается и бэкенд не важен: журнал показывают и там, где команды выключены. Превью в журнал не
    попадает (`history()` их отсекает): нажатие без подтверждения ничего не меняло.
    """
    account = str(account or "").strip()
    if not account:
        return {"ok": False, "error": "account is required"}, 400
    ops = [o for o in _cmd.get_store().history(limit=500) if o.get("account") == account]
    keep = ("op_id", "state", "command", "value", "currency", "actor", "created_at", "summary", "message")
    return {"ok": True, "entries": [{k: o.get(k) for k in keep} for o in ops[:max(1, min(200, limit))]]}, 200


def _sessions():
    """Точка подмены в тестах: `open_session(account, commands)`."""
    return open_session


@contextmanager
def open_session(account: str, commands: bool):
    """Сессия под замком профиля (читатель браузера + очередь профиля), как у чтений.

    Профиль, который поднят не нами, не останавливается — это делает `_session`.
    """
    from src.analysis_agent import account_spend as _as
    from src.analysis_agent import account_spend_api as _api
    from src.fb_ads_rpa.operations import browser_operation, profile_lock

    with browser_operation.reading(), profile_lock(_as._profile_key(account)):
        with _api._session(account, commands=commands) as sess:
            yield sess
