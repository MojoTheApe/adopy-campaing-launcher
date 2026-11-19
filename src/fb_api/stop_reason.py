"""ACL-2811: a stopped publish names Meta's own reason for the account, never "wait".

Когда публикация остановилась без своей причины (вкладка не отвечает, неизвестный отказ, таймаут),
берём ПОСЛЕДНИЙ известный сигнал Meta по этому кабинету: статус, который уже записали чтения
(`account_spend.get_status`: account_status / issues_info, например 31/3858385 «authenticate your account»).
Ни одного нового запроса к Meta, ни таймеров, ни опроса: только локальный файл статусов.
Остановка на одном кабинете читает сигнал только этого кабинета.
"""
from __future__ import annotations

from typing import Any

from src.fb_api import plain_errors as _plain

#: Failures without a Meta reason of their own: the account signal explains them better.
OVERRIDABLE = ("tab_unresponsive", "unknown", "graph_read_timeout", "graph_write_unconfirmed", "internal")
AUTH_SHORT = "Meta asks to authenticate this account"
AUTH_NEXT = "open the profile in Multilogin, pass the check, then Publish again"
BLOCKED_NEXT = "check it in Ads Manager, then Publish again"


def latest_signal(account: str) -> dict[str, str]:
    """The last status stored for this account (`{}` when none, or the store cannot be read). No Meta call."""
    if not account:
        return {}
    try:
        from src.analysis_agent import account_spend as _as

        got = _as.get_status(account)
        return got if isinstance(got, dict) else {}
    except Exception:  # noqa: BLE001 - the signal is secondary, the stop text must still come out
        return {}


def reason_failure(signal: dict[str, Any], base: Any = None) -> Any | None:
    """`Failure` from a stored status that names a reason; None when Meta named none (clear / unknown)."""
    status = str((signal or {}).get("status") or "")
    text = str((signal or {}).get("status_text") or "")
    if status in ("", "clear"):
        return None
    try:
        from src.analysis_agent.account_spend_api import AUTH_RESTRICTED_TEXT
    except Exception:  # noqa: BLE001
        AUTH_RESTRICTED_TEXT = ""
    auth = bool(text) and text == AUTH_RESTRICTED_TEXT
    key = "auth_required" if auth else "account_blocked"
    short = AUTH_SHORT if auth else (f"Meta reports a problem with this ad account: {text}" if text
                                     else "Meta reports a problem with this ad account")
    details = dict(getattr(base, "details", None) or {})
    details["account_signal"] = {"status": status, "text": text, "at": str((signal or {}).get("at") or "")}
    return _plain.Failure(key, short, AUTH_NEXT if auth else BLOCKED_NEXT, left=list(getattr(base, "left", None) or []),
                          severity="hold" if auth else "blocking", retry="after_fix", details=details,
                          stage=str(getattr(base, "stage", "") or ""))


def explain(failure: Any, account: str) -> Any:
    """`failure`, or the account's own Meta reason in its place when the failure has none. Never raises."""
    if failure is None or getattr(failure, "key", "") not in OVERRIDABLE:
        return failure
    return reason_failure(latest_signal(account), failure) or failure


def not_started_text(account: str, stop_kind: str) -> str:
    """Line of a campaign that was never begun because an earlier one stopped on this account."""
    found = reason_failure(latest_signal(account))
    if found is not None:
        return f"Not started: {found.line()}. Nothing was created for it."
    return f"Not started: Graph API stopped earlier ({stop_kind}). Nothing was created for it."
