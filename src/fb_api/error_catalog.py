"""ACL-1786: коды ошибок Meta -> короткое понятное сообщение и следующий шаг для байера.

Автономный модуль: graph.py не правим (его переписывает ACL-1761), наборы кодов
импортируем только на чтение. Таблица заполнена ТОЛЬКО тем, что сказано на
официальных страницах Meta (documented=True, source_url) либо тем, что мы
реально видели (documented=False, source_url = файл в репо). Неизвестный код
ничем не заменяем: возвращаем сырой текст Meta.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from src.fb_api.graph import POLICY_CODES, RATE_CODES, TOKEN_CODES

CATEGORIES = ("token", "rate_limit", "policy", "account", "permission", "validation", "transient", "other")

ERR_REF = "https://developers.facebook.com/documentation/ads-commerce/marketing-api/error-reference"
GRAPH_ERR = "https://developers.facebook.com/docs/graph-api/guides/error-handling"
RATE_REF = "https://developers.facebook.com/documentation/ads-commerce/marketing-api/overview/rate-limiting"
GRAPH_PY = "src/fb_api/graph.py"  # код есть в наборах graph.py, но страницы Meta его не описывают


@dataclass(frozen=True)
class Entry:
    message_en: str
    action_en: str
    retryable: bool
    category: str
    source_url: str          # страница Meta или путь файла в репо (для observed)
    documented: bool = True  # False — только наблюдали / принято в graph.py


@dataclass(frozen=True)
class Explanation:
    code: int | None
    subcode: int | None
    message_en: str
    action_en: str
    retryable: bool
    category: str
    source_url: str
    known: bool
    raw_message: str = ""

    def line(self) -> str:
        """Одна строка для списка проблем дашборда (ACL-1437)."""
        if not self.known:
            return self.raw_message or "Meta refused the request (no details given)."
        text = f"{self.message_en} {self.action_en}"
        if self.code == 2238055 and self.message_en.startswith("Meta uses this code") and self.raw_message:
            text += f" Meta said: {self.raw_message}"
        return text


_WAIT = "Wait a few minutes and try again."
_SLOW = "Wait, then retry; if it keeps happening, send fewer requests."
_FIX_PERM = "Check that this account has the needed permission for the ad account or Page."
_FIX_FIELD = "Fix the value in the campaign settings and publish again."
_REJECTED = "Update the ad to meet Meta's policies and create a new ad."


def _e(msg, action, retry, cat, src=ERR_REF, documented=True):
    return Entry(msg, action, retry, cat, src, documented)


CATALOG: dict[tuple[int, int | None], Entry] = {
    # --- токен / сессия ---
    (102, None): _e("The Meta session is no longer valid.", "Sign in to the account again, then retry.", False, "token", GRAPH_ERR),
    (190, None): _e("The Meta access token is invalid or has expired.", "Reopen the account profile and sign in again, then retry.", False, "token", GRAPH_ERR),
    (190, 459): _e("The account needs to fix an issue on facebook.com (checkpoint).", "Open the account in its browser profile, resolve the prompt on facebook.com, then retry.", False, "account", GRAPH_ERR),
    (190, 463): _e("The login status or access token has expired.", "Sign in to the account again, then retry.", False, "token", GRAPH_ERR),
    (190, 464): _e("The account needs to fix an issue on facebook.com (unconfirmed user).", "Open the account in its browser profile, resolve the prompt on facebook.com, then retry.", False, "account", GRAPH_ERR),
    (190, 467): _e("The access token is invalid or has been revoked.", "Sign in to the account again, then retry.", False, "token", GRAPH_ERR),
    # --- лимиты ---
    (4, None): _e("Meta says the app made too many requests.", _SLOW, True, "rate_limit", GRAPH_ERR),
    (4, 1504022): _e("Meta says there have been too many calls from this app.", "Wait a bit and try again; send fewer requests.", True, "rate_limit", RATE_REF),
    (4, 1504039): _e("Meta says there have been too many calls from this app.", "Wait a bit and try again; send fewer requests.", True, "rate_limit", RATE_REF),
    (17, None): _e("Meta says the user request limit was reached.", _SLOW, True, "rate_limit", GRAPH_ERR),
    (17, 2446079): _e("The ad account request limit was reached.", "Space out requests and retry later.", True, "rate_limit", RATE_REF),
    (17, 1885172): _e("An account spending limit can be changed only 10 times per day.", "Space out spending limit changes through the day.", False, "rate_limit", RATE_REF),
    (613, None): _e("Calls to this API have exceeded the rate limit.", "Wait and retry; if it keeps happening, contact Meta support.", True, "rate_limit", RATE_REF),
    (613, 1487742): _e("Too many calls from this ad account.", "Wait a bit and try again.", True, "rate_limit", RATE_REF),
    (613, 5044001): _e("The ad account exceeded the allowed rate of changes.", "Wait and retry; spread changes over time.", True, "rate_limit", RATE_REF),
    (613, 1487632): _e("An ad set budget can be changed only 4 times per hour.", "Wait before changing the budget again.", True, "rate_limit", RATE_REF),
    (613, 1487225): _e("The ad-level request limit was reached.", "Make fewer changes to the ad.", True, "rate_limit", RATE_REF),
    (341, None): _e("Meta says the application limit was reached.", _SLOW, True, "rate_limit", GRAPH_ERR),
    # --- политика ---
    (368, None): _e("Meta temporarily blocked this action for a policy violation.", "Do not retry now; wait and check the account in Meta Business Support.", False, "policy", GRAPH_ERR),
    (1404078, None): _e("Meta temporarily blocked this action.", "Wait before trying again.", False, "policy"),
    (2859015, None): _e("Meta temporarily blocked this action.", "Wait before trying again.", False, "policy"),
    (1404163, None): _e("This account is no longer allowed to advertise on Meta.", "Check the account status in Meta Business Support.", False, "account"),
    (2708008, None): _e("The account is not authorized to run ads about social issues.", "Complete Meta's ad authorization for this account.", False, "policy"),
    (2490427, None): _e("Meta rejected this ad in review and it is disabled.", _REJECTED, False, "policy"),
    (2490468, None): _e("Meta rejected this ad in review and it is disabled.", _REJECTED, False, "policy"),
    # --- общие ---
    (1, None): _e("Meta returned an unknown error (possibly a temporary issue).", "Wait and retry once; if it repeats, check the request.", True, "transient", GRAPH_ERR),
    (1, 99): _e("Meta returned an unknown error (can happen when the level is set to ad set).", "Retry; if it repeats, check the reporting level.", True, "transient"),
    (2, None): _e("Meta had a temporary service problem.", _WAIT, True, "transient", GRAPH_ERR),
    (3, None): _e("The app lacks a capability or permission for this call.", _FIX_PERM, False, "permission", GRAPH_ERR),
    (10, None): _e("Permission was not granted or has been removed.", _FIX_PERM, False, "permission"),
    (100, None): _e("Meta rejected a parameter as invalid.", _FIX_FIELD, False, "validation"),
    (100, 33): _e("Meta rejected the request: the token is not a system user of this object.", _FIX_PERM, False, "permission"),
    (100, 1487694): _e("A targeting category you selected is no longer available.", "Pick another targeting option and publish again.", False, "validation"),
    (100, 1752129): _e("This task combination is not supported for the user role.", _FIX_PERM, False, "permission"),
    (100, 3858258): _e("Meta could not download the image.", "Check that the image URL is public and not blocked, then retry.", True, "validation"),
    (104, None): _e("Meta says the request signature is incorrect.", "Report this to the ACL team.", False, "other"),
    (200, None): _e("Meta denied permission for this action.", _FIX_PERM, False, "permission"),
    (200, 1870034): _e("Custom Audience terms have not been accepted.", "Accept the Custom Audience terms in Ads Manager, then retry.", False, "permission"),
    (200, 1870047): _e("The audience is too small to remove users from it.", "Leave the audience as is.", False, "validation"),
    (294, None): _e("Managing ads requires the ads_management permission.", _FIX_PERM, False, "permission"),
    (506, None): _e("Duplicate posts cannot be published consecutively.", "Change the content and try again.", False, "validation", GRAPH_ERR),
    (1609005, None): _e("Meta could not read data from the provided link.", "Check the URL and try again.", True, "validation", GRAPH_ERR),
    (2606, None): _e("Meta cannot show a preview of this ad.", "Check the ad creative.", False, "validation"),
    (2607, None): _e("The currency is invalid for use with ads.", "Check the ad account currency.", False, "account"),
    (2615, None): _e("Invalid call to update this ad account.", "Check the ad account settings.", False, "validation"),
    (2654, None): _e("Meta failed to create the custom audience.", "Check the audience settings and retry.", False, "validation"),
    (2654, 1713092): _e("No write permission for this ad account.", "Ask the account admin to grant you access.", False, "permission"),
    (5000, None): _e("Meta returned an unknown error code.", "Retry once; if it repeats, report it.", True, "other"),
    (1340029, None): _e("Deleting a Dynamic Creative ad is not allowed.", "Delete it through its parent ad set.", False, "validation"),
    (1373054, None): _e("No call-to-action type could be parsed.", "Check the call-to-action button setting.", False, "validation"),
    (1487007, None): _e("This ad cannot be edited: it is part of a scheduled ad set.", "Edit the ad set instead.", False, "validation"),
    (1487033, None): _e("The campaign end date must be in the future.", "Set a later end date and publish again.", False, "validation"),
    (1487056, None): _e("This ad set was deleted; only its name can be edited.", "Duplicate it to make changes.", False, "validation"),
    (1487472, None): _e("This content cannot be promoted in an ad.", "Use a different post or creative.", False, "validation"),
    (1487566, None): _e("This campaign was deleted; only its name can be edited.", "Duplicate it to make changes.", False, "validation"),
    (1487678, None): _e("The app's operating system does not match the ad set targeting.", "Fix the platform targeting.", False, "validation"),
    (1487831, None): _e("The product set cannot be loaded; it may not exist.", "Check the catalog and product set.", False, "validation"),
    (1487929, None): _e("The object you are promoting is ambiguous.", "Specify exactly one promoted object.", False, "validation"),
    (1487990, None): _e("The ad account has too many archived ads.", "Delete some archived ads, then retry.", False, "account"),
    (1815199, None): _e("The ad account has no access to this Instagram account.", "Connect the Instagram account to the ad account.", False, "permission"),
    (1815629, None): _e("All ad asset values must be unique.", "Remove duplicate assets.", False, "validation"),
    (1815694, None): _e("The user does not have permission for this action.", _FIX_PERM, False, "permission"),
    (1870065, None): _e("The audience contains a disabled audience.", "Remove the disabled audience.", False, "validation"),
    (1870088, None): _e("Connection targeting is being deprecated.", "Remove connection targeting to publish.", False, "validation"),
    (1870090, None): _e("Custom Audience terms must be accepted.", "Accept the Custom Audience terms in Ads Manager.", False, "permission"),
    (1870092, None): _e("Meta Business Tools terms must be accepted.", "Accept the Meta Business Tools terms in Ads Manager.", False, "permission"),
    (1870165, None): _e("The audience uses targeting options that can no longer be used (for example for under-18 audiences).", "Adjust the age or targeting.", False, "validation"),
    (1870199, None): _e("Location targeting behavior has changed.", "Remove the location types field.", False, "validation"),
    (1870219, None): _e("Only employer exclusions are allowed in account control.", "Remove other exclusions.", False, "validation"),
    (1870220, None): _e("Too many employer exclusions.", "Reduce the number of exclusions.", False, "validation"),
    (1885029, None): _e("The selected Page does not match the Page of the promoted object.", "Use the same Page.", False, "validation"),
    (1885088, None): _e("This ad is archived and cannot be edited.", "Duplicate it to make changes.", False, "validation"),
    (1885183, None): _e("The creative post was created by an app in development mode.", "Make the app public (live), then retry.", False, "validation"),
    (1885204, None): _e("The chosen optimization needs automatic bidding.", "Set the bid strategy to automatic.", False, "validation"),
    (1885272, None): _e("The budget is too low.", "Raise the budget and publish again.", False, "validation"),
    (1885557, None): _e("The ad promotes a post that is unavailable (deleted or unpublished).", "Pick an available post.", False, "validation"),
    (1885621, None): _e("Set either an ad set budget or a campaign budget, not both.", "Keep one budget level.", False, "validation"),
    (1885650, None): _e("The budget is below the required minimum.", "Raise the budget.", False, "validation"),
    (2238055, None): _e("Meta uses this code for two different problems (Instagram identity or minimum budget).", "Check the Instagram account settings and the budget minimum, then publish again.", False, "validation"),
    (2446149, None): _e("Both instagram_user_id and instagram_actor_id were passed.", "Pass only one Instagram identity.", False, "validation"),
    (2446173, None): _e("A target rule label does not refer to asset labels.", "Fix the label names.", False, "validation"),
    (2446289, None): _e("The post you selected for the ad is not available.", "Pick another post or check permissions.", False, "validation"),
    (2446307, None): _e("The campaign spend cap is below the minimum.", "Raise the spend cap.", False, "validation"),
    (2446347, None): _e("An ad using an existing post needs use_existing_post set to true.", "Set the flag.", False, "validation"),
    (2446383, None): _e("The campaign objective requires an external website URL.", "Add a website URL.", False, "validation"),
    (2446394, None): _e("The ad set uses detailed targeting options that are no longer available.", "Pick other targeting options.", False, "validation"),
    (2446509, None): _e("The campaign destination type is not valid.", "Choose a valid destination.", False, "validation"),
    (2446580, None): _e("Components and child_attachments cannot be used together.", "Use only one.", False, "validation"),
    (2446712, None): _e("Store visits optimization is no longer available.", "Choose another optimization goal.", False, "validation"),
    (2446867, None): _e("The per-country campaign limit was reached.", "Use a standard conversion setup.", False, "validation"),
    (2446880, None): _e("The WhatsApp number connected to the Page was disconnected.", "Reconnect the number.", False, "validation"),
    (2490085, None): _e("The 191x100 crop key will no longer be available.", "Use a supported crop.", False, "validation"),
    (2490155, None): _e("The post for this ad is not available.", "Check that it exists and you have access.", False, "validation"),
    (2490372, None): _e("A shop destination must be chosen.", "Choose a shop destination.", False, "validation"),
    (3858064, None): _e("The campaign has options that cannot be used for under-18 audiences.", "Adjust targeting.", False, "validation"),
    (3858082, None): _e("The Standard Enhancements enrollment status is required.", "Provide the enroll status.", False, "validation"),
    (3858152, None): _e("The ad set needs beneficiary and payer information.", "Add beneficiary/payer info.", False, "validation"),
    (3867105, None): _e("This content cannot be used for a partnership ad.", "Use other content.", False, "validation"),
    (3910001, None): _e("The ad account is experiencing trouble.", "Try again later.", True, "transient"),
}

# Коды из наборов graph.py: страницы Meta описывают 80000/80003/80004/80014; остальные
# (32, 80001, 80002, 80005-80013) там не названы — общее сообщение по классу,
# источник — классификация в graph.py (documented=False), ничего не выдумываем.
_UNDOC_RATE = _e("Meta is limiting requests (rate limit).", _SLOW, True, "rate_limit", GRAPH_PY, False)
for _c in sorted(RATE_CODES):
    if _c in (80000, 80003, 80004, 80014):
        CATALOG[(_c, None)] = _e("Too many calls from this ad account.", "Wait a bit and try again; scale back changes to the ad account.", True, "rate_limit", RATE_REF)
    elif (_c, None) not in CATALOG:
        CATALOG[(_c, None)] = _UNDOC_RATE

# Видели у себя, страница кода не описывает: текст в живом Ads Manager (data-surface ...:100).
OBSERVED_PATTERNS: list[tuple[re.Pattern, Entry]] = [
    (re.compile(r"lead_gen_form_id\]? must be a valid Lead Gen", re.I),
     _e("The selected instant form is not valid for this ad.",
        "Pick an existing Lead Gen form in the ad and publish again.",
        False, "validation",
        "src/fb_ads_rpa/meta_dom.py (Param call_to_action[value][lead_gen_form_id] must be a valid Lead Gen Data id)",
        False)),
]


# Meta в error-reference даёт код 2238055 в двух строках с разным смыслом: выбираем по тексту.
_AMBIG_2238055 = [
    (re.compile(r"budget.*at least|minimum.?budget|must be at least", re.I),
     _e("The campaign budget is below the required minimum.", "Raise the budget to at least the minimum Meta states and publish again.", False, "validation")),
    (re.compile(r"instagram_(user|actor)_id", re.I),
     _e("Both instagram_user_id and instagram_actor_id were passed.", "Pass only one Instagram identity.", False, "validation")),
]


def explain(code: int | None, subcode: int | None = None, raw_message: str = "") -> Explanation:
    """Подкод точнее кода; нет в таблице — сырой текст Meta как есть."""
    entry = None
    # точный наблюдённый текст точнее общего кода (например, 100 «invalid parameter»)
    for pat, e in OBSERVED_PATTERNS:
        if raw_message and pat.search(raw_message):
            entry = e
            break
    if entry is None and 2238055 in (code, subcode) and raw_message:
        for pat, e in _AMBIG_2238055:
            if pat.search(raw_message):
                entry = e
                break
    if entry is None and code is not None and subcode is not None:
        entry = CATALOG.get((code, subcode))
    if entry is None and subcode is not None:
        entry = CATALOG.get((subcode, None))  # Meta шлёт 1404078 и т. п. подкодом: он точнее общего кода (ACL-1805)
    if entry is None and code is not None:
        entry = CATALOG.get((code, None))
    if entry is None:
        return Explanation(code, subcode, "", "", False, "other", "", False, raw_message)
    return Explanation(code, subcode, entry.message_en, entry.action_en, entry.retryable,
                       entry.category, entry.source_url, True, raw_message)


def problem_line(err: BaseException) -> str:
    """Строка проблемы из GraphError/GraphStop (читает .code/.subcode)."""
    return explain(getattr(err, "code", None), getattr(err, "subcode", None), str(err)).line()


__all__ = ["CATALOG", "Entry", "Explanation", "explain", "problem_line", "CATEGORIES",
           "TOKEN_CODES", "RATE_CODES", "POLICY_CODES"]
