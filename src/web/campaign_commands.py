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


def _guard() -> tuple[dict, int] | None:
    if not api_enabled():
        return {"error": RPA_REFUSAL, "code": "rpa_backend"}, 409
    return None


def run_preview(body: dict[str, Any]) -> tuple[dict, int]:
    refused = _guard()
    if refused:
        return refused
    account = str(body.get("account") or "")
    try:
        op = _cmd.preview(_cmd.get_store(), _sessions(), account=account,
                          actor=body.get("actor"), command=str(body.get("command") or ""),
                          value=body.get("value"), targets=body.get("targets"))
    except _cmd.CommandRefused as exc:
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
                          idempotency_key=str(body.get("idempotency_key") or ""))
    except _cmd.CommandRefused as exc:
        return _refusal(exc)
    return op, 202 if op.get("state") in (_cmd.RUNNING, _cmd.VERIFYING) else 200


def get_status(op_id: str) -> tuple[dict, int]:
    refused = _guard()
    if refused:
        return refused
    op = _cmd.status(_cmd.get_store(), op_id)
    if op is None:
        return {"error": "Unknown operation."}, 404
    return op, 200


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
