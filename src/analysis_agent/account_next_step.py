"""ACL-123: что человеку делать с отключённым (ограниченным) кабинетом.

Вход — то, что дашборд уже знает о кабинете: слово состояния и его текст.
Причину читаем из текста Graph-чтения (`... reason ADS_INTEGRITY_POLICY)`,
см. `account_spend_api.map_status`). Страничное чтение причины не знает —
тогда говорим именно это. Диагноз не выдумываем и восстановление не
обещаем: только куда смотреть и кому писать.
"""
from __future__ import annotations

import re

_REASON_RE = re.compile(r"\breason\s+([A-Z][A-Z0-9_]+|\d+)\b")

_WHERE = "Open this account in Business Manager > Account Quality"

#: Причина Meta → шаг. Ключи — имена из `account_spend_api._DISABLE_REASONS`.
_BY_REASON = {
    "ADS_INTEGRITY_POLICY": f"Meta disabled this account for an ads policy violation. {_WHERE} to see the violation and whether an appeal is offered.",
    "ADS_IP_REVIEW": f"Meta disabled this account after an intellectual property review. {_WHERE} to see the details and whether an appeal is offered.",
    "RISK_PAYMENT": "Meta disabled this account over a payment risk. Check Billing & payments for this account and ask the account provider before changing the payment method.",
    "GRAY_ACCOUNT_SHUT_DOWN": f"Meta shut this account down as a suspicious (gray) account. {_WHERE}; if there is no appeal, ask the account provider for a replacement.",
    "ADS_AFC_REVIEW": f"Meta disabled this account in an ads integrity review. {_WHERE} to see whether an appeal is offered.",
    "BUSINESS_INTEGRITY_RAR": f"Meta disabled this account in a business integrity review. {_WHERE}, and check the Business Manager it belongs to.",
    "PERMANENT_CLOSE": "Meta closed this account permanently. Do not launch from it; ask the account provider for a replacement.",
    "UNUSED_RESELLER_ACCOUNT": "Meta closed this account because it was unused. Ask the account provider for a replacement.",
    "UNUSED_ACCOUNT": "Meta closed this account because it was unused. Ask the account provider for a replacement.",
    "UMBRELLA_AD_ACCOUNT": "This is an umbrella account that Meta disabled. Ask the account provider which account to use instead.",
    "BUSINESS_MANAGER_INTEGRITY_POLICY": f"Meta disabled this account because of its Business Manager. {_WHERE} and check the Business Manager status; ask the account provider.",
    "MISREPRESENTED_AD_ACCOUNT": f"Meta disabled this account for misrepresentation. {_WHERE} to see whether an appeal is offered.",
    "AOAB_DESHARE_LEGAL_ENTITY": "Meta disabled this account after it was unshared from its legal entity. Ask the account provider.",
    "CTX_THREAD_REVIEW": f"Meta disabled this account in a review. {_WHERE} to see whether an appeal is offered.",
    "COMPROMISED_AD_ACCOUNT": "Meta marked this account as compromised. Do not use it; tell the account provider so it can be secured or replaced.",
}

_BY_STATUS = {
    "disabled": f"{_WHERE} to see what Meta says and whether an appeal is offered. If nothing is offered, ask the account provider.",
    "restricted": "Check Billing & payments and Account Quality for this account, fix what Meta lists there, or ask the account provider.",
    "review": "Meta is reviewing this account. Wait for the result and do not launch from it meanwhile; ask the account provider if it stays in review.",
}


def reason_of(status_text: str) -> str:
    """Причина из текста состояния или пусто."""
    m = _REASON_RE.search(status_text or "")
    return m.group(1) if m else ""


def next_step(status: str, status_text: str = "") -> str:
    """Один шаг по-английски для человека; пусто, если делать нечего."""
    status = (status or "").strip().lower()
    base = _BY_STATUS.get(status)
    if base is None:
        return ""
    if status != "disabled":
        return base
    reason = reason_of(status_text)
    if not reason:
        return "The reason was not reported by this read. " + base
    known = _BY_REASON.get(reason)
    if known:
        return known
    return (f"Meta gave a reason this tool does not recognise ({reason}). The cause is unknown; "
            + base[0].lower() + base[1:])
