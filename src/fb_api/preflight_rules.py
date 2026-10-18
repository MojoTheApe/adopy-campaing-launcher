"""Pre-flight audit билдера: одна таблица правил для сервера и страницы (ACL-1819).

Каждая проверка описана ОДИН раз в `RULES`: id, подпись, серьёзность, поле, подсказка,
сторона (`client` — считает страница на каждую правку; `server` — нужен кабинет Meta,
считает `/publish-plan` через создателя кампаний) и уровень редактора. Таблица уезжает
на страницу как JSON (`export_rules`), а файл `preflight_rules.json` рядом проверяется
тестом на совпадение — расхождение таблицы и страницы невозможно.

Клиентские правила — чистые функции от «входа аудита» (`AuditInput`, см. ниже); точно
такие же написаны на JS в `report.html` (`cbPreflight`), и тест паритета гоняет одни и те
же фикстуры через обе реализации. Серверные правила не повторяют логику создателя:
`server_verdicts` зовёт его же куски (`resolve_page`, `resolve_pixel`, `TargetingResolver`,
`_check_forms`, чтение файлов, `_existing_differences`) и превращает их отказы в вердикты.

Аудит ничего не пишет в Meta: клиент пробного запуска создан без права записи, а здесь
нет ни одного POST.

Вердикт: {"rule": id, "status": ok|warning|error|unknown, "detail": str, "ads": [1-based]}.
`unknown` не блокирует: проверку не удалось выполнить (валюты нет в таблице, кабинет не
ответил) — об этом сказано на строке, а решение за человеком.
"""
from __future__ import annotations

import copy
import json
import re
import struct
from pathlib import Path
from typing import Any

#: Серьёзность правила — потолок: правило со severity=warning никогда не блокирует.
ERROR, WARNING, OK, UNKNOWN = "error", "warning", "ok", "unknown"

MAX_HEADLINE = 40
MAX_PRIMARY_VISIBLE = 125
AGE_MIN, AGE_MAX = 18, 65

#: Допустимые CTA по цели кампании и месту конверсии (ACL-1767). Единый список - в
#: `write_policy` (CTA_WEBSITE / CTA_FORM): тот же набор разрешает и политика записи.
#: Ключ `<цель>_instant_form` - список для лид-формы.
from src.fb_api import write_policy as _wp  # noqa: E402

ALLOWED_CTA = {
    "leads": _wp.CTA_WEBSITE,
    "leads_instant_form": _wp.CTA_FORM,
}
DEFAULT_OBJECTIVE = "leads"
#: Версия iOS/Android: «14» или «14.0»; старт набора: «YYYY-MM-DDTHH:MM» (ACL-1767).
_OS_MIN_RE = r"^[0-9]{1,2}(\.[0-9]{1,2})?$"
_START_RE = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}$"

#: Минимальный дневной бюджет Meta по валюте кабинета, в единицах валюты (не в центах).
#: Meta не публикует полной таблицы по валютам (Marketing API: «bid and budget are at the
#: ad account currency's minimum denomination»), документирован лишь минимум в долларах
#: для показов — $1.00 в день. Поэтому в таблице ТОЛЬКО то, что подтверждено; валюта вне
#: таблицы даёт вердикт `unknown`, а не выдуманное число. Дополнять — по справке Ads Manager.
MIN_DAILY_BUDGET: dict[str, float] = {"USD": 1.00}
MIN_DAILY_BUDGET_SOURCE = ("Meta: minimum daily budget for impressions billing is 1.00 USD; "
                           "other currencies are not listed here and stay 'unknown'.")

#: Видео: потолок длины в секундах (Meta: до 241 минуты); дольше минуты Stories/Reels обрежут.
VIDEO_MAX_SECONDS = 241 * 60
VIDEO_STORY_SECONDS = 60
#: Вертикаль: соотношение сторон w/h не больше этого — считаем вертикальным (9:16 = 0.5625).
VERTICAL_MAX_RATIO = 0.6

_LINK_RE = r"^https://[^\s/?#:]+\.[^\s/?#:]+(:\d+)?([/?#]\S*)?$"
_TRACK_RE = r"\?[^#\s]*[^=&#\s]="

JSON_PATH = Path(__file__).with_name("preflight_rules.json")


#: Разделы карточки (ACL-1990): карточка показывает разделы с одним статусом, а не
#: двадцать строк. Порядок = порядок разделов. «meta» — сама проверка плана с Meta:
#: в нём одна строка `plan_check`, которую страница добавляет сама (правила в таблице нет).
TOPICS: list[dict[str, str]] = [
    {"id": "account", "label": "Account & access"},
    {"id": "audience", "label": "Audience & targeting"},
    {"id": "budget", "label": "Budget"},
    {"id": "destination", "label": "Link & Instant Form"},
    {"id": "creative", "label": "Creatives & texts"},
    {"id": "names", "label": "Names & duplicates"},
    {"id": "meta", "label": "Meta plan check"},
]
TOPIC_IDS = [t["id"] for t in TOPICS]

_TOPIC_OF = {
    "account_active": "account",
    "page_ok": "account",
    "pixel_ok": "account",
    "form_active": "destination",
    "form_chosen": "destination",
    "link_https": "destination",
    "link_tracking": "destination",
    "country_set": "audience",
    "country_resolves": "audience",
    "language_resolves": "audience",
    "interests_resolve": "audience",
    "age_range": "audience",
    "media_chosen": "creative",
    "media_readable": "creative",
    "media_limits": "creative",
    "media_vertical": "creative",
    "primary_text": "creative",
    "headline": "creative",
    "headline_len": "creative",
    "primary_len": "creative",
    "cta_allowed": "creative",
    "budget_set": "budget",
    "budget_min": "budget",
    "ad_names_unique": "names",
    "existing_diffs": "names",
    # Правила из соседних задач (ACL-1896, 1776, 1771, 1774, 1767, 1768): на TEST9095 они стоят
    # вместе с этим PR, и без темы `_r()` падал KeyError — Check plan ломался целиком.
    "instagram_identity": "account",
    "form_template_privacy": "destination",
    "form_template_contacts": "destination",
    "form_terms": "destination",
    "interests_confirmed": "audience",
    "vertical_needs_main": "creative",
    "vertical_ratio": "creative",
    "texts_limit": "creative",
    "texts_vertical": "creative",
    "texts_dynamic": "creative",
    "devices_consistent": "audience",
    "start_time_valid": "budget",
    "adset_names_unique": "names",
    "adset_has_ads": "names",
}


def _r(rid: str, label: str, severity: str, side: str, hint: str, *, sel: str = "", level: str = "campaign",
       applies: str = "always", unit: str = "campaign") -> dict[str, Any]:
    return {"id": rid, "label": label, "severity": severity, "side": side, "hint": hint,
            "field": {"sel": sel, "level": level}, "applies": applies, "unit": unit, "topic": _TOPIC_OF.get(rid, "meta")}


#: Порядок = порядок строк в карточке. `applies`: always | website | instant_form.
RULES: list[dict[str, Any]] = [
    _r("account_active", "Ad account is active", ERROR, "server",
       "the profile has no access to the ad account, or the account is not active: check the Accounts page"),
    _r("page_ok", "Facebook Page", ERROR, "server",
       "choose a Facebook Page in this campaign or use Automatic; Refresh pages to retry loading"),
    _r("instagram_identity", "Instagram account of the Page", ERROR, "server",
       "the ad is set for Instagram placements: connect an Instagram account to the Facebook Page, "
       "or wait for manual placements (ACL-1772) to publish Facebook-only"),
    _r("pixel_ok", "Pixel for conversions", ERROR, "server",
       "website conversions need exactly one pixel on the ad account; set pixel_id on the Accounts page",
       applies="website"),
    _r("form_active", "Instant Form of the Page", ERROR, "server",
       "pick an ACTIVE form of this Page (Refresh forms)", sel=".cb-form-select", level="ad",
       applies="instant_form", unit="ad"),
    _r("form_chosen", "Instant Form chosen", ERROR, "client",
       "choose one of the Page's forms or a template in the ad", sel=".cb-form-select", level="ad",
       applies="instant_form", unit="ad"),
    _r("form_template_privacy", "Form template: privacy policy URL (https)", ERROR, "client",
       "the template needs a privacy policy URL that starts with https://", sel=".cb-form-template-select",
       level="ad", applies="instant_form", unit="ad"),
    _r("form_template_contacts", "Form template: a contact field", ERROR, "client",
       "the template needs at least one contact field (name, email or phone)",
       sel=".cb-form-template-select", level="ad", applies="instant_form", unit="ad"),
    _r("form_terms", "Lead Ads terms accepted on the Page", ERROR, "server",
       "accept the Lead Ads terms for this Page in Meta (the robot never accepts them), then check again",
       applies="instant_form"),
    _r("country_set", "Country", ERROR, "client", "pick the country in the account",
       sel="", level="campaign"),
    _r("country_resolves", "Country found in Meta targeting", ERROR, "server",
       "Meta found no exact match for the country: check its name or ISO code"),
    _r("language_resolves", "Language found in Meta targeting", ERROR, "server",
       "Meta found no exact match for the language: pick it from the list or clear the field",
       sel='[name$="_language"]', level="adset"),
    _r("interests_resolve", "Detailed targeting found in Meta", ERROR, "server",
       "Meta found no exact match for some terms: pick one of the candidates or fix the term in the library",
       sel=".cb-targeting-search", level="adset"),
    _r("interests_confirmed", "Detailed targeting picks confirmed", ERROR, "server",
       "Meta has only a near match for some terms: Confirm the pick in Check plan (it is remembered)",
       sel=".cb-targeting-search", level="adset"),
    _r("link_https", "Link https://", ERROR, "client", "enter the website URL, starting with https://",
       sel='input[name="tracking_link"]', level="campaign", applies="website"),
    _r("link_tracking", "Tracking parameters in the link", WARNING, "client",
       "the link has no ?key=value parameters: clicks may not be attributed",
       sel='input[name="tracking_link"]', level="campaign", applies="website"),
    _r("media_chosen", "Media chosen", ERROR, "client", "choose a file in each ad",
       sel=".cb-creative-search", level="ad", unit="ad"),
    _r("media_readable", "Media file exists and is readable", ERROR, "server",
       "the file is missing, empty or unreadable: choose another file",
       sel=".cb-creative-search", level="ad", unit="ad"),
    _r("media_limits", "Media within Meta limits", ERROR, "server",
       "file type, size (image 30 MB, video 1536 MB) or video length is over the limit",
       sel=".cb-creative-search", level="ad", unit="ad"),
    _r("media_vertical", "Vertical file for Stories and Reels", WARNING, "server",
       "no 9:16 file: without one Facebook crops the picture itself",
       sel=".cb-creative-search", level="ad", unit="ad"),
    _r("vertical_needs_main", "Main file next to the Stories / Reels file", ERROR, "client",
       "choose the main (1:1 or 4:5) file too, or clear the Stories / Reels file",
       sel=".cb-creative-search", level="ad", unit="ad"),
    _r("vertical_ratio", "Aspect ratios: main 1:1 or 4:5, Stories / Reels 9:16", ERROR, "server",
       "with a Stories / Reels file the main file must be 1:1 or 4:5, the vertical one 9:16, both images "
       "or both videos, and the size must be readable (JPG/PNG, MP4/MOV)",
       sel=".cb-vertical-search", level="ad", unit="ad"),
    _r("texts_limit", "Up to 5 texts per field", ERROR, "client",
       "an ad takes up to 5 primary texts, 5 headlines and 5 descriptions: remove the extra ones",
       sel=".cb-adtitle-search", level="ad", unit="ad"),
    _r("texts_vertical", "Several texts and a Stories / Reels file", ERROR, "client",
       _wp.MSG_TEXTS_VERTICAL, sel=".cb-vertical-search", level="ad", unit="ad"),
    _r("texts_dynamic", "Several texts per ad", ERROR, "client",
       _wp.MSG_TEXTS_DYNAMIC + ": use one text per ad (or one text with the Stories / Reels file)",
       sel=".cb-adtitle-search", level="ad", unit="ad"),
    _r("primary_text", "Primary text", ERROR, "client", "choose a primary text in each ad",
       sel=".cb-adtitle-search", level="ad", unit="ad"),
    _r("headline", "Headline", ERROR, "client", "the chosen text has no headline: pick another library entry",
       sel=".cb-adtitle-search", level="ad", unit="ad"),
    _r("headline_len", "Headline up to 40 characters", WARNING, "client",
       "a longer headline may be cut off", sel=".cb-adtitle-search", level="ad", unit="ad"),
    _r("primary_len", "Text visible in full (up to 125)", WARNING, "client",
       "after 125 characters the text is folded behind 'See more'",
       sel=".cb-adtitle-search", level="ad", unit="ad"),
    _r("cta_allowed", "CTA allowed for the objective", ERROR, "client",
       "choose a call to action that the objective accepts", sel="", level="ad", unit="ad"),
    _r("budget_set", "Daily budget", ERROR, "client", "enter a daily budget above zero",
       sel='[name$="_budget"]', level="campaign"),
    _r("budget_min", "Budget is at least Meta's minimum", ERROR, "server",
       "raise the daily budget to Meta's minimum for the account currency",
       sel='[name$="_budget"]', level="campaign"),
    _r("age_range", "Age 18-65", ERROR, "client", "age must be between 18 and 65, and min not above max",
       sel='[name$="_age"]', level="adset"),
    _r("devices_consistent", "Devices and OS version fit together", ERROR, "client",
       "iOS and Android are mobile only (not desktop only), and a minimum version needs iOS or Android "
       "and looks like 14 or 14.0", sel='[name$="_os"]', level="adset"),
    _r("start_time_valid", "Start time", ERROR, "client",
       "enter the start as date and time, not earlier than today (ad account time zone)",
       sel='[name$="_start_time"]', level="adset"),
    _r("ad_names_unique", "Ad names are unique", ERROR, "client",
       "give the ads different creatives or texts: their names must differ inside a campaign",
       sel=".cb-adtitle-search", level="ad", unit="ad"),
    _r("adset_names_unique", "Ad set names are unique", ERROR, "client",
       "give every ad set of the campaign its own name", sel=".cb-adset-name", level="adset"),
    _r("adset_has_ads", "Every ad set has an ad", ERROR, "client",
       "add an ad to the ad set or remove the ad set", sel="", level="ad"),
    _r("existing_diffs", "Campaign name is free, or gets a _2, _3 suffix", ERROR, "server",
       "if a different campaign already has this name, the robot publishes as NAME_2, NAME_3 ... "
       "(up to _20); an error only when all 20 names are taken: rename the campaign"),
]
RULE_BY_ID = {r["id"]: r for r in RULES}
CLIENT_IDS = [r["id"] for r in RULES if r["side"] == "client"]
SERVER_IDS = [r["id"] for r in RULES if r["side"] == "server"]


def export_rules() -> dict[str, Any]:
    """Всё, что нужно странице: правила и константы (без логики)."""
    return {
        "version": 1,
        "rules": RULES,
        "topics": TOPICS,
        "consts": {"max_headline": MAX_HEADLINE, "max_primary_visible": MAX_PRIMARY_VISIBLE,
                   "age_min": AGE_MIN, "age_max": AGE_MAX, "allowed_cta": {k: list(v) for k, v in ALLOWED_CTA.items()},
                   "default_objective": DEFAULT_OBJECTIVE, "link_re": _LINK_RE, "track_re": _TRACK_RE,
                   "os_min_re": _OS_MIN_RE, "start_re": _START_RE,
                   "max_texts": MAX_TEXTS},
    }


def export_json() -> str:
    return json.dumps(export_rules(), indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def load_json() -> dict[str, Any]:
    return json.loads(JSON_PATH.read_text(encoding="utf-8"))


# ───────────────────────── клиентские правила ─────────────────────────
#
# Вход аудита (одна кампания; ACL-1768: наборы - `ad_sets`, плоский вход = один набор):
#   {"destination": "website"|"instant_form", "objective": "leads", "link": str, "country": str,
#    "budget": number|None, "age_min": int, "age_max": int,
#    "ads": [{"name": str, "file": str, "form_id": str, "template": {"privacy_url": str, "contact_fields": [str]}|None, "body": str, "headline": str, "cta": str,
#             ACL-1774 (необязательные): "bodies": [str], "headlines": [str], "descriptions": [str],
#             "vertical_file": str}],
#    ACL-1767 (необязательные): "os": "All"|"iOS"|"Android", "device": ""|"mobile"|"desktop",
#    "os_min": str, "start_time": "YYYY-MM-DDTHH:MM", "now": "YYYY-MM-DDTHH:MM" (часы страницы)}

def _multi_text(a: dict) -> bool:
    """Больше одного разного текста/заголовка/описания (Meta: это Dynamic Creative, у нас не поддержан)."""
    return any(len(_distinct(a.get(k))) > 1 for k in ("bodies", "headlines", "descriptions"))


def _s(v: Any) -> str:
    return str(v if v is not None else "").strip()


def _v(rule: str, status: str, detail: str = "", ads: list[int] | None = None,
       sets: list[int] | None = None) -> dict[str, Any]:
    out = {"rule": rule, "status": status, "detail": detail, "ads": list(ads or [])}
    if sets:
        # ACL-1768: у кампании с несколькими наборами - какие наборы (1-based); иначе ключа нет.
        out["sets"] = list(sets)
    return out


#: Сколько текстов каждого вида допускает одно объявление (ACL-1774; справочник Graph).
MAX_TEXTS = _wp.ASSET_FEED_MAX_TEXTS


def _distinct(items: Any) -> list[str]:
    """Непустые тексты без повторов (пробелы не в счёт) — как на сервере (`creator._unique`)."""
    out: list[str] = []
    seen: set[str] = set()
    for x in items if isinstance(items, list) else []:
        key = "".join(_s(x).split())
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out


def applies(rule: dict[str, Any], destination: str) -> bool:
    a = rule["applies"]
    return a == "always" or a == destination


def audit_sets(inp: dict[str, Any]) -> list[dict[str, Any]]:
    """Наборы входа аудита. Старый плоский вход (без `ad_sets`) - один набор (ACL-1768)."""
    given = inp.get("ad_sets")
    if isinstance(given, list) and given:
        return given
    flat = {k: inp.get(k) for k in ("name", "destination", "age_min", "age_max", "os", "device", "os_min",
                                     "start_time", "ads")}
    return [flat]


def _dest_of(a_set: dict[str, Any]) -> str:
    return "instant_form" if _s(a_set.get("destination")) == "instant_form" else "website"


def campaign_dests(inp: dict[str, Any]) -> set[str]:
    """Какие места конверсии есть среди наборов: по ним правила показываются или нет."""
    return {_dest_of(x) for x in audit_sets(inp)}


def _numbered(inp: dict[str, Any]) -> list[tuple[int, int, dict[str, Any], dict[str, Any]]]:
    """(номер набора 1-based, сквозной номер объявления 1-based, набор, объявление)."""
    out = []
    n = 0
    for si, st in enumerate(audit_sets(inp), start=1):
        for ad in st.get("ads") or []:
            n += 1
            out.append((si, n, st, ad))
    return out


def _per_ad(rule: str, inp: dict, bad, status: str = ERROR, only_dest: str = "") -> dict[str, Any]:
    ads = [n for _si, n, st, ad in _numbered(inp)
           if (not only_dest or _dest_of(st) == only_dest) and bad(ad, st)]
    return _v(rule, status if ads else OK, ("Ad " + ", ".join(map(str, ads))) if ads else "", ads)


def _per_set(rule: str, inp: dict, check) -> dict[str, Any]:
    """Правило уровня набора: `check(набор)` -> код ошибки или пустая строка."""
    bad: list[tuple[int, str]] = []
    for si, st in enumerate(audit_sets(inp), start=1):
        code = check(st)
        if code:
            bad.append((si, code))
    if not bad:
        return _v(rule, OK)
    if len(audit_sets(inp)) == 1:
        return _v(rule, ERROR, bad[0][1])
    return _v(rule, ERROR, "Ad set " + ", ".join(str(i) for i, _ in bad), sets=[i for i, _ in bad])


def evaluate_client(inp: dict[str, Any]) -> list[dict[str, Any]]:
    """Клиентские вердикты в порядке таблицы. Зеркало `cbPreflight.evaluate` в report.html.

    ACL-1768: правила уровня набора считаются на КАЖДЫЙ набор (`inp["ad_sets"]`); плоский
    вход прежней формы - один набор, вердикты те же.
    """
    sets = audit_sets(inp)
    dests = campaign_dests(inp)
    out: list[dict[str, Any]] = []
    link = _s(inp.get("link"))
    link_ok = bool(re.match(_LINK_RE, link, re.I))
    for rule in RULES:
        if rule["side"] != "client" or not (rule["applies"] == "always" or rule["applies"] in dests):
            continue
        rid = rule["id"]
        if rid == "form_chosen":
            out.append(_per_ad(rid, inp, lambda a, st: not _s(a.get("form_id")) and not a.get("template"),
                               only_dest="instant_form"))
        elif rid == "form_template_privacy":
            out.append(_per_ad(rid, inp, lambda a, st: bool(a.get("template"))
                               and not re.match(_LINK_RE, _s((a.get("template") or {}).get("privacy_url")), re.I)))
        elif rid == "form_template_contacts":
            out.append(_per_ad(rid, inp, lambda a, st: bool(a.get("template"))
                               and not (a.get("template") or {}).get("contact_fields")))
        elif rid == "country_set":
            out.append(_v(rid, OK if _s(inp.get("country")) else ERROR))
        elif rid == "link_https":
            if not link:
                out.append(_v(rid, ERROR, "empty"))
            elif not link_ok:
                out.append(_v(rid, ERROR, "not_https"))
            else:
                out.append(_v(rid, OK))
        elif rid == "link_tracking":
            if not link_ok:
                out.append(_v(rid, UNKNOWN, "no_valid_link"))
            else:
                out.append(_v(rid, OK if re.search(_TRACK_RE, link) else WARNING))
        elif rid == "media_chosen":
            out.append(_per_ad(rid, inp, lambda a, st: not _s(a.get("file"))))
        elif rid == "vertical_needs_main":
            out.append(_per_ad(rid, inp, lambda a, st: bool(_s(a.get("vertical_file"))) and not _s(a.get("file"))))
        elif rid == "texts_limit":
            out.append(_per_ad(rid, inp, lambda a, st: any(
                len(_distinct(a.get(k))) > MAX_TEXTS for k in ("bodies", "headlines", "descriptions"))))
        elif rid == "texts_vertical":
            out.append(_per_ad(rid, inp, lambda a, st: _multi_text(a) and bool(_s(a.get("vertical_file")))))
        elif rid == "texts_dynamic":
            out.append(_per_ad(rid, inp, lambda a, st: _multi_text(a) and not _s(a.get("vertical_file"))))
        elif rid == "primary_text":
            out.append(_per_ad(rid, inp, lambda a, st: not _s(a.get("body"))))
        elif rid == "headline":
            out.append(_per_ad(rid, inp, lambda a, st: not _s(a.get("headline"))))
        elif rid == "headline_len":
            out.append(_per_ad(rid, inp, lambda a, st: len(_s(a.get("headline"))) > MAX_HEADLINE, WARNING))
        elif rid == "primary_len":
            out.append(_per_ad(rid, inp, lambda a, st: len(_s(a.get("body"))) > MAX_PRIMARY_VISIBLE, WARNING))
        elif rid == "cta_allowed":
            objective = _s(inp.get("objective")).lower() or DEFAULT_OBJECTIVE

            def cta_bad(a, st, objective=objective):
                key = objective + ("_instant_form" if _dest_of(st) == "instant_form" else "")
                return (_s(a.get("cta")) or "LEARN_MORE") not in ALLOWED_CTA.get(key, ())
            out.append(_per_ad(rid, inp, cta_bad))
        elif rid == "budget_set":
            b = inp.get("budget")
            try:
                good = b is not None and b != "" and float(b) > 0
            except (TypeError, ValueError):
                good = False
            out.append(_v(rid, OK if good else ERROR))
        elif rid == "age_range":
            def age_bad(st):
                try:
                    lo, hi = int(st.get("age_min")), int(st.get("age_max"))
                    return "" if AGE_MIN <= lo <= AGE_MAX and AGE_MIN <= hi <= AGE_MAX and lo <= hi else "range"
                except (TypeError, ValueError):
                    return "range"
            out.append(_per_set(rid, inp, age_bad))
        elif rid == "devices_consistent":
            def dev_bad(st):
                os_ = _s(st.get("os")).lower() or "all"
                dev, ver = _s(st.get("device")).lower(), _s(st.get("os_min"))
                mobile = os_ in ("ios", "android")
                if mobile and dev == "desktop":
                    return "mobile_os_desktop"
                if ver and not mobile:
                    return "version_needs_os"
                if ver and not re.fullmatch(_OS_MIN_RE, ver):
                    return "bad_version"
                return ""
            out.append(_per_set(rid, inp, dev_bad))
        elif rid == "start_time_valid":
            now = _s(inp.get("now"))

            def start_bad(st, now=now):
                stt = _s(st.get("start_time"))
                if not stt:
                    return ""
                if not re.fullmatch(_START_RE, stt):
                    return "format"
                if re.fullmatch(_START_RE, now) and stt[:10] < now[:10]:
                    return "past"
                return ""
            out.append(_per_set(rid, inp, start_bad))
        elif rid == "ad_names_unique":
            # Имена объявлений различаются внутри набора (так создатель ищет и сверяет их).
            bad: list[int] = []
            for si, st in enumerate(sets, start=1):
                nums = [(n, _s(ad.get("name"))) for s1, n, _s2, ad in _numbered(inp) if s1 == si]
                names = [nm for _n, nm in nums]
                dup = {nm for nm in names if nm and names.count(nm) > 1}
                bad += [n for n, nm in nums if nm in dup]
            out.append(_v(rid, ERROR if bad else OK, ("Ad " + ", ".join(map(str, bad))) if bad else "", bad))
        elif rid == "adset_names_unique":
            names = [_s(st.get("name")).casefold() for st in sets]
            bad_sets = [i + 1 for i, nm in enumerate(names) if nm and names.count(nm) > 1]
            out.append(_v(rid, ERROR if bad_sets else OK,
                          ("Ad set " + ", ".join(map(str, bad_sets))) if bad_sets else "", sets=bad_sets))
        elif rid == "adset_has_ads":
            empty = [i + 1 for i, st in enumerate(sets) if not (st.get("ads") or [])]
            out.append(_v(rid, ERROR if empty else OK, ("Ad set " + ", ".join(map(str, empty))) if empty else "",
                          sets=empty))
    return out


# ───────────────────────── размеры медиа (без декодера) ─────────────────────────

def image_size(data: bytes) -> tuple[int, int] | None:
    """Ширина и высота PNG/GIF/JPEG по заголовку; иначе None."""
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        return struct.unpack(">II", data[16:24])
    if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
        w, h = struct.unpack("<HH", data[6:10])
        return w, h
    if data[:2] == b"\xff\xd8":
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            length = struct.unpack(">H", data[i + 2:i + 4])[0]
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                h, w = struct.unpack(">HH", data[i + 5:i + 9])
                return w, h
            i += 2 + length
    return None


def _boxes(data: bytes, start: int, end: int):
    i = start
    while i + 8 <= end:
        size, kind = struct.unpack(">I4s", data[i:i + 8])
        head = 8
        if size == 1 and i + 16 <= end:
            size = struct.unpack(">Q", data[i + 8:i + 16])[0]
            head = 16
        elif size == 0:
            size = end - i
        if size < head:
            return
        yield kind, i + head, min(i + size, end)
        i += size


def video_info(data: bytes) -> dict[str, float] | None:
    """Длина (с) и размеры кадра mp4/mov по moov/mvhd и trak/tkhd; иначе None."""
    info: dict[str, float] = {}
    for kind, a, b in _boxes(data, 0, len(data)):
        if kind != b"moov":
            continue
        for k2, a2, b2 in _boxes(data, a, b):
            if k2 == b"mvhd" and b2 - a2 >= 20:
                ver = data[a2]
                if ver == 1 and b2 - a2 >= 32:
                    scale, dur = struct.unpack(">IQ", data[a2 + 20:a2 + 32])
                else:
                    scale, dur = struct.unpack(">II", data[a2 + 12:a2 + 20])
                if scale:
                    info["seconds"] = dur / scale
            elif k2 == b"trak":
                for k3, a3, b3 in _boxes(data, a2, b2):
                    if k3 == b"tkhd" and b3 - a3 >= 84 and "width" not in info:
                        off = 88 if data[a3] == 1 else 76
                        if a3 + off + 8 <= b3:
                            w, h = struct.unpack(">II", data[a3 + off:a3 + off + 8])
                            if w and h:
                                w, h = w >> 16, h >> 16
                                # Матрица поворота: a, b — первые два числа 16.16. Поворот на
                                # 90/270 градусов (a = 0, |b| = 1) меняет ширину и высоту местами.
                                mo = a3 + (52 if data[a3] == 1 else 40)
                                a, b = struct.unpack(">ii", data[mo:mo + 8])
                                if a == 0 and abs(b) == 0x10000:
                                    w, h = h, w
                                info["width"], info["height"] = w, h
    return info or None


# ───────────────────────── серверные правила ─────────────────────────

def min_budget_for(currency: str) -> float | None:
    return MIN_DAILY_BUDGET.get((currency or "").strip().upper())


def _item_text(item: dict) -> str:
    """Термин без находки: имя и кандидаты (или честное «кандидатов нет»)."""
    rows = item.get("candidates") or []
    cands = ("candidates: " + ", ".join(f"{c['name']} [{c['type']}]" for c in rows[:5])
             if rows else "no candidates")
    return f"'{item['term']}' not found ({cands})" + (f" - {item['reason']}" if item.get("reason") else "")


def server_verdicts(creator: Any, plan: Any, *, existing: list[dict] | None = None,
                    existing_checked: bool = False, files: dict[str, bytes] | None = None,
                    renamed: str = "", name_error: str = "") -> list[dict[str, Any]]:
    """Серверные вердикты кампании. Только чтения: ни одного POST (клиент без права записи).

    Каждая проверка независима — отказ одной не гасит остальные. Остановка Graph
    (лимит, блокировка) обрывает дальнейшие чтения: остальные вердикты `unknown`.
    """
    from src.fb_api import creator as cr
    from src.fb_api.graph import GraphError, GraphStop

    plan = copy.deepcopy(plan)
    dest = "instant_form" if plan.ad_sets and all(s.instant for s in plan.ad_sets) else "website"
    out: dict[str, dict[str, Any]] = {}
    state = {"stopped": ""}

    def put(rid: str, status: str, detail: str = "", ads: list[int] | None = None) -> None:
        out[rid] = _v(rid, status, detail, ads)

    def guarded(rid: str, fn) -> None:
        if state["stopped"]:
            put(rid, UNKNOWN, "Not checked: " + state["stopped"])
            return
        try:
            fn()
        except cr.PlanError as exc:
            put(rid, ERROR, str(exc))
        except GraphStop as exc:
            state["stopped"] = str(exc) or "Graph API stopped"
            put(rid, UNKNOWN, "Not checked: " + state["stopped"])
        except GraphError as exc:
            put(rid, UNKNOWN, f"Meta did not answer: {exc}")

    facts = creator.facts
    put("account_active", OK)
    guarded("page_ok", lambda: (cr.resolve_page(plan, facts),
                                put("page_ok", WARNING if cr.needs_manual_page(plan) else OK,
                                    cr.MANUAL_PAGE_NOTICE if cr.needs_manual_page(plan) else "")))

    def instagram() -> None:
        # ACL-1896: Instagram-площадки без аккаунта у Страницы Meta отвергает уже после создания кампании.
        # Сервер - единственный судья: ему нужны данные Meta. Отказ чтения - не «можно».
        if cr.needs_manual_page(plan) or not plan.page_id or not cr.plan_has_instagram_placements(plan):
            put("instagram_identity", OK, "not needed")
            return
        got = creator.instagram_identity(plan)
        if got.status == "found":
            put("instagram_identity", OK, f"Instagram account {got.ig_id} ({got.source})")
        else:
            # «Нет аккаунта» и «Meta не подтвердила» одинаково блокируют: молча пропускать нельзя.
            put("instagram_identity", ERROR, got.message)
    if "page_ok" in out and out["page_ok"]["status"] != ERROR:
        guarded("instagram_identity", instagram)
    else:
        put("instagram_identity", UNKNOWN, "Not checked: the Facebook Page is not settled")
    if any(not s.instant for s in plan.ad_sets):
        guarded("pixel_ok", lambda: (cr.resolve_pixel(plan, facts), put("pixel_ok", OK)))

    def forms() -> None:
        if getattr(creator, "_page_forms", None) is not None:
            put("form_active", OK)
            return
        creator._check_forms(plan)
        put("form_active", OK)
    if any(s.instant for s in plan.ad_sets):
        guarded("form_active", forms)

    def terms() -> None:
        # Условия Lead Ads (ACL-1776): нужны только если форму придётся создать. Только чтение.
        steps = getattr(creator, "_form_steps", None)
        templated = any(a.form_template for s in plan.ad_sets for a in s.ads)
        if not templated or (steps and all(st.action == "reuse" for st in steps.values())):
            put("form_terms", OK, "not needed")
            return
        from src.fb_api import forms as _forms

        got = _forms.page_terms_accepted(creator.client, plan.page_id)
        if got is False:
            put("form_terms", ERROR, _forms.MSG_TERMS)
        elif got is None:
            put("form_terms", UNKNOWN, "The Page's Lead Ads terms could not be read; Meta will tell when the form is created")
        else:
            put("form_terms", OK)
    if any(s.instant for s in plan.ad_sets):
        guarded("form_terms", terms)

    resolver = creator.resolver
    guarded("country_resolves", lambda: (resolver.country(plan.country), put("country_resolves", OK)))

    def languages() -> None:
        resolver.locale(plan.language)
        for s in plan.ad_sets:          # ACL-1767: дополнительные языки - тоже только точные совпадения
            if s.language:              # ACL-1768: свой язык набора
                resolver.locale(s.language)
            for extra in s.languages:
                resolver.locale(extra)
        put("language_resolves", OK)
    guarded("language_resolves", languages)

    def interests() -> None:
        for s in plan.ad_sets:
            if s.interest_groups:
                resolver.interests(s.interest_groups)
        put("interests_resolve", OK)
        put("interests_confirmed", OK)

    def interests_guard() -> None:
        # ACL-1771: непонятый термин — ошибка с кандидатами; близкое совпадение — ошибка с Confirm.
        try:
            interests()
        except cr.TargetingUnresolved as exc:
            lost = [i for i in exc.items if i.get("state") not in ("pending", "unknown")]
            asks = [i for i in exc.items if i.get("state") == "pending"]
            if any(i.get("state") == "unknown" for i in exc.items):
                # Бюджет чтений вышел: не угадываем. Блокирует план (plan_error), человек жмёт Check plan снова.
                why = dict.fromkeys(i.get("reason") or cr._tr.UNKNOWN_MESSAGE
                                    for i in exc.items if i.get("state") == "unknown")
                put("interests_resolve", UNKNOWN, ". ".join(why))
            elif lost:
                put("interests_resolve", ERROR,
                    "Detailed targeting not found by Meta: " + "; ".join(_item_text(i) for i in lost))
                out["interests_resolve"]["items"] = lost
            else:
                put("interests_resolve", OK)
            if asks:
                put("interests_confirmed", ERROR,
                    "; ".join(f"picked '{i['suggested']['name']}' [{i['suggested']['type']}] for '{i['term']}'?"
                              for i in asks))
                out["interests_confirmed"]["items"] = asks
                out["interests_confirmed"]["action"] = "confirm"
            else:
                put("interests_confirmed", OK)

    if state["stopped"]:
        for rid in ("interests_resolve", "interests_confirmed"):
            put(rid, UNKNOWN, "Not checked: " + state["stopped"])
    else:
        try:
            interests_guard()
        except cr.PlanError as exc:
            put("interests_resolve", ERROR, str(exc))
            put("interests_confirmed", OK)
        except GraphStop as exc:
            state["stopped"] = str(exc) or "Graph API stopped"
            for rid in ("interests_resolve", "interests_confirmed"):
                put(rid, UNKNOWN, "Not checked: " + state["stopped"])
        except GraphError as exc:
            for rid in ("interests_resolve", "interests_confirmed"):
                put(rid, UNKNOWN, f"Meta did not answer: {exc}")

    # Файлы: читаем с диска как создатель (или берём уже прочитанные).
    readable: list[int] = []
    problems: dict[str, list[tuple[int, str]]] = {"media_readable": [], "media_limits": []}
    ratio_bad: list[tuple[int, str]] = []
    vertical_bad: list[int] = []
    vertical_unknown: list[int] = []
    long_video: list[int] = []
    for s in plan.ad_sets:
        for ad in s.ads:
            ext = Path(ad.path).suffix.lower()
            if ext not in cr.IMAGE_EXT and ext not in cr.VIDEO_EXT:
                problems["media_limits"].append((ad.index, f"Ad {ad.index}: file type {ext or '(none)'} is not an image or video Meta accepts"))
            try:
                data = (files or {}).get(ad.path)
                if data is None:
                    data = creator.load_media(ad.path, ad.kind)
            except OSError as exc:
                problems["media_readable"].append((ad.index, f"Ad {ad.index}: creative file cannot be read ({exc})"))
                continue
            if not data:
                problems["media_readable"].append((ad.index, f"Ad {ad.index}: creative file is empty"))
                continue
            limit = cr.VIDEO_MAX_BYTES if ad.kind == "video" else cr.IMAGE_MAX_BYTES
            if len(data) > limit:
                problems["media_limits"].append((ad.index, f"Ad {ad.index}: file is larger than {limit // (1024 * 1024)} MB"))
            readable.append(ad.index)
            size = None
            if ad.vertical_path:
                # ACL-1774: пара файлов — читаем вертикальный, сверяем соотношения и вид.
                try:
                    vdata = (files or {}).get(ad.vertical_path)
                    if vdata is None:
                        vdata = creator.load_media(ad.vertical_path, ad.vertical_kind)
                except OSError as exc:
                    problems["media_readable"].append((ad.index, f"Ad {ad.index}: Stories / Reels file cannot be read ({exc})"))
                    vdata = b""
                vlimit = cr.VIDEO_MAX_BYTES if ad.vertical_kind == "video" else cr.IMAGE_MAX_BYTES
                if vdata and len(vdata) > vlimit:
                    problems["media_limits"].append((ad.index, f"Ad {ad.index}: Stories / Reels file is larger than {vlimit // (1024 * 1024)} MB"))
                if vdata and ad.vertical_kind != ad.kind:
                    ratio_bad.append((ad.index, f"Ad {ad.index}: the main file is {ad.kind} and the Stories / Reels file is {ad.vertical_kind}"))
                elif vdata:
                    ratio_bad += [(ad.index, m) for m in cr.pair_problems(ad.index, ad.kind, data, vdata)]
            if ad.kind == "video":
                vi = video_info(data)
                if vi and vi.get("seconds", 0) > VIDEO_MAX_SECONDS:
                    problems["media_limits"].append((ad.index, f"Ad {ad.index}: video is longer than 241 minutes"))
                elif vi and vi.get("seconds", 0) > VIDEO_STORY_SECONDS:
                    long_video.append(ad.index)
                if vi and vi.get("width") and vi.get("height"):
                    size = (int(vi["width"]), int(vi["height"]))
            else:
                size = image_size(data)
            if ad.vertical_path:
                pass                      # вертикальный файл есть: «обрежет сама» не про это объявление
            elif size and size[1]:
                if size[0] / size[1] > VERTICAL_MAX_RATIO:
                    vertical_bad.append(ad.index)
            else:
                vertical_unknown.append(ad.index)
    if ratio_bad:
        put("vertical_ratio", ERROR, "; ".join(m for _, m in ratio_bad), sorted({i for i, _ in ratio_bad}))
    else:
        put("vertical_ratio", OK)
    for rid in ("media_readable", "media_limits"):
        if problems[rid]:
            put(rid, ERROR, "; ".join(m for _, m in problems[rid]), [i for i, _ in problems[rid]])
        else:
            put(rid, OK)
    if long_video and "media_limits" in out and out["media_limits"]["status"] == OK:
        out["media_limits"] = _v("media_limits", WARNING,
                                 "Ad " + ", ".join(map(str, long_video)) + ": video is over 60 s; Stories and Reels may cut it",
                                 long_video)
    if vertical_bad:
        put("media_vertical", WARNING, "Ad " + ", ".join(map(str, vertical_bad)), vertical_bad)
    elif vertical_unknown:
        put("media_vertical", UNKNOWN, "Ad " + ", ".join(map(str, vertical_unknown)) + ": size not readable", vertical_unknown)
    else:
        put("media_vertical", OK)

    # Бюджет против минимума валюты кабинета.
    minimum = min_budget_for(facts.currency)
    if minimum is None:
        put("budget_min", UNKNOWN, f"Meta's minimum for {facts.currency or 'this currency'} is not in our table")
    elif float(plan.daily_budget) < minimum:
        put("budget_min", ERROR, f"{plan.daily_budget:g} {facts.currency} is below the minimum {minimum:g} {facts.currency}")
    else:
        put("budget_min", OK)

    if name_error:
        # ACL-1902: заняты все NAME ... NAME_20 - публикация остановилась бы до записи.
        put("existing_diffs", ERROR, name_error)
    elif not existing_checked:
        put("existing_diffs", UNKNOWN, "Not checked: fix the errors above first")
    elif renamed:
        # ACL-1902: имя занято другой кампанией - робот возьмёт свободное, это не блокирует публикацию.
        put("existing_diffs", WARNING, renamed)
    else:
        put("existing_diffs", OK)

    return [out[r["id"]] for r in RULES if r["side"] == "server" and r["id"] in out]


# ───────────────────────── итог ─────────────────────────

def summarize(verdicts: list[dict[str, Any]]) -> dict[str, int]:
    """Счётчики для значка: блокирует только `error`."""
    n = {ERROR: 0, WARNING: 0, OK: 0, UNKNOWN: 0}
    for v in verdicts:
        n[v["status"]] = n.get(v["status"], 0) + 1
    return n


def blocks(verdicts: list[dict[str, Any]]) -> bool:
    """Гейт Publish: любое `error`. `warning` и `unknown` не блокируют."""
    return any(v["status"] == ERROR for v in verdicts)
