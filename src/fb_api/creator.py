"""Кампания из черновика билдера через Graph API в профиле — на паузе (ACL-1761).

Вход — тот же `CampaignDraft`, что публикует RPA (`slots_to_campaign_drafts`
→ `enrich_campaign_drafts`). Выход — объекты Meta в статусе PAUSED и отчёт
сверки: каждое поле, прочитанное обратно, против намерения билдера.

Порядок:
1. Предпроверка (до единой записи): кабинет из карты есть в `/me/adaccounts`
   и активен (как ACL-1697), страница среди `promote_pages`, пиксель кабинета,
   гео/языки/интересы через `/search`, файлы креативов на диске.
2. Загрузка медиа: картинка — `/act_X/adimages` (сначала ищем по md5: хэш
   картинки у Meta — её md5), видео — `/act_X/advideos` с ограниченным
   ожиданием обработки.
3. Кампания → группа(ы) → креатив → объявление, всё PAUSED.
4. Сверка чтением, поле за полем; расхождение — в отчёт, ничего не включается.
5. Повторный запуск ничего не создаёт: у каждого объекта детерминированное
   имя и метка `acl-<tag>` (adlabels); перед созданием ищем существующий.

Каждая запись — один раз (`GraphClient.post`). Неизвестный результат —
стоп и сверка поиском по метке; нашли — продолжаем с найденным, не нашли —
стоп, повторный запуск безопасен.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from src.fb_api import error_catalog as _catalog
from src.fb_api import forms as _forms
from src.fb_api.graph import GraphClient, GraphError, GraphStop, WritesBlocked, WriteUnknown, normalize_act

log = logging.getLogger(__name__)

PAUSED = "PAUSED"
OBJECTIVE = "OUTCOME_LEADS"
OPTIMIZATION_GOAL = "OFFSITE_CONVERSIONS"
BILLING_EVENT = "IMPRESSIONS"
DESTINATION = "WEBSITE"
BID_STRATEGY = "LOWEST_COST_WITHOUT_CAP"
CUSTOM_EVENT = "LEAD"
CTA_TYPE = "LEARN_MORE"

#: Instant Form (ACL-1775): место показа — сама форма, цель — лиды, объект — Страница.
DEST_WEBSITE = "website"
DEST_FORM = "instant_form"
DESTINATION_ON_AD = "ON_AD"
OPTIMIZATION_GOAL_FORM = "LEAD_GENERATION"
#: `link` в link_data у лид-креатива обязателен для Graph, а у формы своего сайта нет.
#: Адрес-пустышка Meta (как в её примерах лид-креатива); открывается форма, не он.
INSTANT_FORM_LINK = "https://fb.me/"

#: Площадки — как у робота (ACL-1761, паритет с RPA). Источник правды —
#: `src/fb_ads_rpa/meta_dom.py`: `_PLACEMENT_CATEGORIES`, `_SUBITEMS_ON`,
#: `_SUBITEMS_OFF` (Feeds, Stories/Status/Reels, In-stream ads for reels — да;
#: Search results, Apps and sites — нет). Здесь — зеркало в значениях Graph API
#: (импортировать meta_dom нельзя: это код RPA и Playwright).
#: Включено: Facebook Feed, Facebook Stories, Facebook Reels, Facebook in-stream
#: reels; Instagram feed, Instagram Stories, Instagram Reels.
#: Выключено (просто не перечислено): профили, Marketplace, правая колонка,
#: Business Explore, уведомления, поиск, «Ads on Facebook Reels», Explore,
#: Audience Network, Messenger, Threads, WhatsApp.
PUBLISHER_PLATFORMS = ("facebook", "instagram")
FACEBOOK_POSITIONS = ("feed", "story", "facebook_reels", "instream_video")
INSTAGRAM_POSITIONS = ("stream", "story", "reels")
#: Подписи площадок для «Check plan».
POSITION_LABELS = {
    ("facebook", "feed"): "Facebook Feed", ("facebook", "story"): "Facebook Stories",
    ("facebook", "facebook_reels"): "Facebook Reels", ("facebook", "instream_video"): "Facebook in-stream reels",
    ("instagram", "stream"): "Instagram feed", ("instagram", "story"): "Instagram Stories",
    ("instagram", "reels"): "Instagram Reels",
}
#: Advantage+ creative — выключено, как у робота (`disable_text_variations` /
#: `disable_text_generation`, решение владельца 21.09). Имя функции Graph для
#: вариантов текста — `text_optimizations`; первая живая сверка это подтвердит
#: (неизвестный ключ — известный отказ #100, ничего не создаётся).
CREATIVE_FEATURES_OPT_OUT = ("text_optimizations",)
#: «Text generation» робота отдельного документированного ключа Graph не имеет
#: (справочник «Ad Creative Features Spec»: text_optimizations, text_translation,
#: description_automation, … — без text_generation). Не гадаем; сверка в Ads
#: Manager после живого запуска покажет, выключено ли и оно.
OPT_OUT = "OPT_OUT"

#: Валюты без копеек у Meta (смещение 1): бюджет в целых единицах.
ZERO_DECIMAL = frozenset({"CLP", "COP", "CRC", "HUF", "ISK", "IDR", "JPY", "KRW", "PYG", "TWD", "VND"})

IMAGE_EXT = frozenset({".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff", ".webp"})
VIDEO_EXT = frozenset({".mp4", ".mov", ".m4v", ".avi", ".wmv", ".mkv", ".webm"})
IMAGE_MAX_BYTES = 30 * 1024 * 1024
VIDEO_MAX_BYTES = 200 * 1024 * 1024

#: Ожидание обработки видео — с потолком по времени и по числу вызовов.
VIDEO_WAIT_S = 300.0
VIDEO_POLL_S = 15.0
VIDEO_MAX_POLLS = 20
#: Пауза перед сверкой неизвестной записи: запрос мог ещё лететь (ревью, minor 3).
RECONCILE_WAIT_S = 5.0
#: Сколько страниц (по 100) свежих креативов кабинета смотреть в поиске своего.
CREATIVE_LOOKUP_PAGES = 3
#: Цель кампании из билдера, которую умеет API-путь (как у RPA: Leads → сайт).
SUPPORTED_OBJECTIVES = frozenset({"leads"})

#: Что таблица показывает (архив и удалённое не считаем «существующим»).
LIVE_STATES = ["ACTIVE", "PAUSED", "PENDING_REVIEW", "DISAPPROVED", "PREAPPROVED",
               "PENDING_BILLING_INFO", "CAMPAIGN_PAUSED", "ADSET_PAUSED", "IN_PROCESS",
               "WITH_ISSUES"]


class PlanError(ValueError):
    """Черновик нельзя создать через API; к Meta ничего не писали."""

    #: Список проблем для дашборда (как у RPA, ACL-1437); у обычного отказа плана пуст.
    problems: tuple = ()


class RulesViolation(PlanError):
    """Задание нарушает реестр правил Meta (ACL-1805): стоп до первой записи."""

    def __init__(self, message: str, problems: list[dict]):
        super().__init__(message)
        self.problems = problems


class CreationStopped(RuntimeError):
    """Создание кампании остановлено; что уже создано — в `created`."""

    def __init__(self, message: str, *, created: dict | None = None, kind: str = "stopped"):
        super().__init__(message)
        self.created = dict(created or {})
        self.kind = kind


# ───────────────────────── план из черновика ─────────────────────────

def campaign_tag(act: str, campaign_name: str) -> str:
    """Детерминированная метка: кабинет + имя кампании (имя — ключ статистики)."""
    raw = f"{normalize_act(act)}|{(campaign_name or '').strip()}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:12]


@dataclass
class AdPlan:
    index: int
    name: str
    kind: str              # "image" | "video"
    path: str
    title: str
    body: str
    description: str
    #: Id существующей формы Страницы (только у Instant Form; иначе пусто).
    form_id: str = ""


@dataclass
class AdSetPlan:
    name: str
    age_min: int
    age_max: int
    os: str
    interest_groups: list[list[str]]
    ads: list[AdPlan] = field(default_factory=list)
    #: "website" (как всегда) или "instant_form" (ACL-1775).
    destination: str = DEST_WEBSITE

    @property
    def instant(self) -> bool:
        return self.destination == DEST_FORM


@dataclass
class CampaignPlan:
    name: str
    act: str
    tag: str
    country: str
    language: str
    link: str
    page_id: str
    pixel_id: str
    daily_budget: float
    ad_sets: list[AdSetPlan] = field(default_factory=list)
    #: Откуда страница и пиксель: draft / map / account default (ACL-1766).
    page_source: str = ""
    pixel_source: str = ""
    #: Предупреждения черновика (смешанный таргетинг/бюджет) — в итог (ревью, minor 7).
    warnings: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return f"acl-{self.tag}"


def _interest_groups(raw: str) -> list[list[str]]:
    """«a, b [AND] c» → [["a","b"],["c"]] — то же деление, что у RPA."""
    import re

    groups = []
    for chunk in re.split(r"\[AND\]|\|", raw or "", flags=re.IGNORECASE):
        terms = [t.strip() for t in chunk.split(",") if t.strip()]
        if terms:
            groups.append(terms)
    return groups


def _kind_of(path: str, declared: str) -> str:
    ext = Path(path).suffix.lower()
    if ext in VIDEO_EXT:
        return "video"
    if ext in IMAGE_EXT:
        return "image"
    return "video" if "video" in (declared or "").lower() else "image"


def _source(draft: Any, what: str) -> str:
    """Источник страницы/пикселя: enrich ставит map/draft; без него — draft, если значение есть."""
    got = str(getattr(draft, f"{what}_source", "") or "")
    if got:
        return got
    return "draft" if str(getattr(draft, f"{what}_id", "") or "").strip() else ""


def plan_from_draft(draft: Any) -> CampaignPlan:
    """`CampaignDraft` (после `enrich_campaign_drafts`) → план. Ошибки — `PlanError`."""
    problems: list[str] = list(getattr(draft, "errors", None) or [])
    raw_act = str(getattr(draft, "ad_account_id", "") or "").strip()
    try:
        act = normalize_act(raw_act)
    except ValueError:
        problems.append("ad_account_id is missing or malformed in the account map")
        act = ""
    objective = str(getattr(draft, "objective", "") or "Leads").strip()
    if objective.casefold() not in SUPPORTED_OBJECTIVES:
        problems.append(f"Objective {objective!r} is not supported by the API backend "
                        "(only Leads with a website or Instant Form conversion)")
    link = str(getattr(draft, "link", "") or "").strip()
    ad_sets_src = list(getattr(draft, "ad_sets", None) or [])
    # Ссылка нужна только сайтовым наборам: у Instant Form своей ссылки нет (ACL-1775).
    if not link and (not ad_sets_src or any(str(getattr(s, "destination", "") or DEST_WEBSITE) != DEST_FORM
                                            for s in ad_sets_src)):
        problems.append("Tracking link is empty; the API backend needs it for the ads")
    if not ad_sets_src:
        problems.append("No ad sets in the campaign")
    budget = getattr(ad_sets_src[0], "daily_budget", None) if ad_sets_src else None
    if not budget or float(budget) <= 0:
        problems.append("Daily budget missing")
    ad_sets: list[AdSetPlan] = []
    for s in ad_sets_src:
        plan_set = AdSetPlan(
            name=str(s.name or draft.name).strip(),
            age_min=int(getattr(s, "age_min", 18) or 18),
            age_max=int(getattr(s, "age_max", 65) or 65),
            os=str(getattr(s, "os", "All") or "All").strip(),
            interest_groups=_interest_groups(str(getattr(s, "targeting_label", "") or "")),
        )
        dest = str(getattr(s, "destination", "") or DEST_WEBSITE).strip().lower()
        if dest not in (DEST_WEBSITE, DEST_FORM):
            problems.append(f"Unknown conversion location {dest!r} in ad set {plan_set.name!r}")
        else:
            plan_set.destination = dest
        if not s.ads:
            problems.append(f"No ads in ad set {plan_set.name!r}")
        for i, ad in enumerate(s.ads, start=1):
            if str(getattr(ad, "ad_body2", "") or "").strip():
                problems.append(f"Ad {i}: Primary text 2 is not supported by the API backend yet "
                                "(remove it or publish with RPA)")
            path = str(getattr(ad, "creative_path", "") or "").strip()
            if not path:
                problems.append(f"Ad {i}: no creative file")
            plan_set.ads.append(AdPlan(
                index=i, name=ad.ad_name(), kind=_kind_of(path, getattr(ad, "creative_type", "")),
                path=path, title=str(ad.ad_title or "").strip(), body=str(ad.ad_body or "").strip(),
                description=str(ad.ad_link_description or "").strip(),
                form_id=(str(getattr(ad, "lead_form_id", "") or "").strip() if plan_set.instant else "")))
            if plan_set.instant and not plan_set.ads[-1].form_id:
                problems.append(f"Ad {i}: no Instant Form selected (pick one of the Page's forms)")
        ad_sets.append(plan_set)
    # Ревью M2: одинаковые имена схлопнули бы объявления (поиск, сверка, повтор).
    set_names = [x.name for x in ad_sets]
    for dup in sorted({n for n in set_names if set_names.count(n) > 1}):
        problems.append(f"Two ad sets are named {dup!r}; ad set names in a campaign must differ")
    for x in ad_sets:
        names = [a.name for a in x.ads]
        for dup in sorted({n for n in names if names.count(n) > 1}):
            idx = ", ".join(str(a.index) for a in x.ads if a.name == dup)
            problems.append(f"Ads {idx} would all be named {dup!r}; give them different creatives "
                            "or ad texts (names must differ inside a campaign)")
    if problems:
        raise PlanError("; ".join(problems))
    name = str(draft.name).strip()
    return CampaignPlan(
        name=name, act=act, tag=campaign_tag(act, name), country=str(draft.country or "").strip(),
        language=str(getattr(draft, "language", "") or "").strip(), link=link,
        page_id=str(getattr(draft, "page_id", "") or "").strip(),
        pixel_id=str(getattr(draft, "pixel_id", "") or "").strip(),
        daily_budget=float(budget), ad_sets=ad_sets,
        page_source=_source(draft, "page"), pixel_source=_source(draft, "pixel"),
        warnings=[str(w) for w in (getattr(draft, "warnings", None) or [])])


def budget_minor(amount: float, currency: str) -> int:
    """Бюджет в мелких единицах валюты кабинета (центах), как хочет Graph."""
    if (currency or "").upper() in ZERO_DECIMAL:
        return int(round(amount))
    return int(round(amount * 100))


# ───────────────────────── предпроверка кабинета ─────────────────────────

@dataclass
class AccountFacts:
    act: str
    currency: str
    pages: list[str]
    pixels: list[str]


def preflight_account(client: GraphClient, act: str) -> AccountFacts:
    """Кабинет — профиля и активен; валюта, страницы и пиксели. Только чтения."""
    act = normalize_act(act)
    digits = act.removeprefix("act_")
    mine = client.ad_accounts()
    row = next((r for r in mine if str(r.get("account_id") or r.get("id", "")).removeprefix("act_") == digits),
               None)
    if row is None:
        raise PlanError("This profile has no access to the ad account from the account map "
                        "(not in /me/adaccounts). Nothing was created.")
    if str(row.get("account_status")) != "1":
        raise PlanError(f"The ad account is not active (account_status {row.get('account_status')}). "
                        "Nothing was created.")
    acc = client.account(act)
    got = str(acc.get("account_id") or "").strip()
    if got and got != digits:
        raise PlanError("Graph API answered for a different ad account. Nothing was created.")
    pages = client.get_all(f"/{act}/promote_pages", {"fields": "id,name", "limit": 100}, max_pages=2)
    pixels = client.get_all(f"/{act}/adspixels", {"fields": "id,name", "limit": 50}, max_pages=1)
    return AccountFacts(act=act, currency=str(acc.get("currency") or "").upper(),
                        pages=[str(p.get("id")) for p in pages if p.get("id")],
                        pixels=[str(p.get("id")) for p in pixels if p.get("id")])


def resolve_page(plan: CampaignPlan, facts: AccountFacts) -> list[str]:
    """Страница плана: на кабинете и одна определённая. Заметки; ошибки — `PlanError`."""
    notes = []
    if plan.page_id:
        # Принадлежность проверяется и при пустом списке: пустой список — не «можно всё».
        if plan.page_id not in facts.pages:
            src = plan.page_source or "draft"
            raise PlanError(f"The page from the {src} ({plan.page_id}) is not among the pages "
                            f"this ad account can advertise from ({len(facts.pages)} found).")
    elif len(facts.pages) == 1:
        plan.page_id = facts.pages[0]
        plan.page_source = "account default"
        notes.append("page_id was empty in the account map; the only page of the ad account is used")
    else:
        raise PlanError(f"page_id is empty in the account map and the ad account has "
                        f"{len(facts.pages)} pages; set page_id")
    return notes


def resolve_pixel(plan: CampaignPlan, facts: AccountFacts) -> None:
    """Пиксель плана (только сайтовые наборы). Ошибки — `PlanError`."""
    if plan.ad_sets and all(s.instant for s in plan.ad_sets):
        # Instant Form: цель — форма Страницы, пиксель не нужен (ACL-1775).
        pass
    elif plan.pixel_id:
        # То же для пикселя. Читается первая страница (50): больше — безопасный отказ.
        if plan.pixel_id not in facts.pixels:
            src = plan.pixel_source or "draft"
            raise PlanError(f"The pixel from the {src} ({plan.pixel_id}) is not on this ad account "
                            f"({len(facts.pixels)} found). Fix the pixel instead of letting another one be picked.")
    elif len(facts.pixels) == 1:
        plan.pixel_id = facts.pixels[0]
        plan.pixel_source = "account default"
    else:
        raise PlanError(f"The ad account has {len(facts.pixels)} pixels/datasets; "
                        "the API backend needs exactly one")


def resolve_page_pixel(plan: CampaignPlan, facts: AccountFacts) -> list[str]:
    """Страница и пиксель плана. Возвращает предупреждения; ошибки — `PlanError`."""
    notes = resolve_page(plan, facts)
    resolve_pixel(plan, facts)
    return notes


# ───────────────────────── таргетинг через /search ─────────────────────────

def _country_names_for(code: str) -> list[str]:
    """Английские названия страны по ISO-коду из общей карты дешборда (не больше двух)."""
    from src.analysis_agent.country_names import COUNTRY_CODES
    return [n.title() for n, c in COUNTRY_CODES.items() if c == code][:2]


class TargetingResolver:
    """Гео, языки, интересы → id Meta. Кэш на задание; только чтения."""

    def __init__(self, client: GraphClient):
        self.client = client
        self._geo: dict[str, str] = {}
        self._locale: dict[str, int] = {}
        self._interest: dict[str, dict] = {}
        self._behaviors: list[dict] | None = None

    def country(self, value: str) -> str:
        key = (value or "").strip()
        if not key:
            raise PlanError("No country in the draft")
        if key.upper() in self._geo:
            return self._geo[key.upper()]
        up = key.upper()
        code = ""
        # Для двухбуквенного кода «q=AR» отдаёт десятки стран с «ar» в названии, и нужная может
        # выпасть из страницы, поэтому: сначала код с широкой выдачей, затем английские названия.
        # Совпадение только точное (country_code == код); близкое никогда не берём (ACL-1761).
        if len(key) == 2 and key.isalpha():
            queries = [(key, 100, 3)] + [(n, 25, 1) for n in _country_names_for(up)]
        else:
            queries = [(key, 10, 1)]
        for q, limit, pages in queries:
            rows = self.client.get_all("/search", {"type": "adgeolocation", "location_types": ["country"],
                                                   "q": q, "limit": limit}, max_pages=pages)
            for r in rows:
                cc = str(r.get("country_code") or r.get("key") or "").upper()
                if cc == up or str(r.get("name") or "").casefold() == key.casefold():
                    code = cc
                    break
            if code:
                break
        if not code:
            raise PlanError(f"Country {key!r} was not found by Meta targeting search")
        self._geo[key.upper()] = code
        return code

    def locale(self, language: str) -> int | None:
        lang = (language or "").strip()
        if not lang:
            return None
        if lang in self._locale:
            return self._locale[lang]
        rows = self.client.get_all("/search", {"type": "adlocale", "q": lang, "limit": 25}, max_pages=1)
        pick = None
        for want in (f"{lang} (All)", lang):
            pick = next((r for r in rows if str(r.get("name") or "").casefold() == want.casefold()), None)
            if pick:
                break
        if not pick:
            raise PlanError(f"Audience language {lang!r} was not found by Meta targeting search")
        self._locale[lang] = int(pick["key"])
        return self._locale[lang]

    def interests(self, groups: list[list[str]]) -> list[dict]:
        """Группы имён → `flexible_spec` (interests/behaviors с id)."""
        names = [n for g in groups for n in g if n.casefold() not in self._interest]
        if names:
            rows = self.client.get_all("/search", {"type": "adinterestvalid", "interest_list": names},
                                       max_pages=1)
            for r in rows:
                if r.get("valid") is not False and r.get("id"):
                    self._interest[str(r.get("name") or "").casefold()] = {
                        "bucket": "interests", "id": str(r["id"]), "name": str(r.get("name") or "")}
        missing = [n for g in groups for n in g if n.casefold() not in self._interest]
        if missing:
            if self._behaviors is None:
                self._behaviors = self.client.get_all(
                    "/search", {"type": "adTargetingCategory", "class": "behaviors", "limit": 1000},
                    max_pages=1)
            for b in self._behaviors:
                nm = str(b.get("name") or "").casefold()
                if nm and b.get("id") and nm not in self._interest:
                    self._interest[nm] = {"bucket": "behaviors", "id": str(b["id"]),
                                          "name": str(b.get("name") or "")}
            missing = [n for g in groups for n in g if n.casefold() not in self._interest]
        if missing:
            raise PlanError("Detailed targeting not found by Meta: " + ", ".join(missing))
        spec = []
        for g in groups:
            entry: dict[str, list] = {}
            for n in g:
                hit = self._interest[n.casefold()]
                entry.setdefault(hit["bucket"], []).append({"id": hit["id"], "name": hit["name"]})
            spec.append(entry)
        return spec


def targeting_for(plan: CampaignPlan, ad_set: AdSetPlan, resolver: TargetingResolver) -> dict:
    t: dict[str, Any] = {
        "geo_locations": {"countries": [resolver.country(plan.country)]},
        "age_min": ad_set.age_min,
        "age_max": ad_set.age_max,
        # Возраст и интересы — как в билдере; Meta не расширяет аудиторию сама.
        "targeting_automation": {"advantage_audience": 0},
    }
    loc = resolver.locale(plan.language)
    if loc is not None:
        t["locales"] = [loc]
    if ad_set.interest_groups:
        t["flexible_spec"] = resolver.interests(ad_set.interest_groups)
    # Площадки — вручную, как у робота (не Advantage+ placements).
    t["publisher_platforms"] = list(PUBLISHER_PLATFORMS)
    t["facebook_positions"] = list(FACEBOOK_POSITIONS)
    t["instagram_positions"] = list(INSTAGRAM_POSITIONS)
    os_val = ad_set.os.casefold()
    if os_val in ("ios", "android"):
        # Как в билдере (решение по Android — за владельцем, пока как было).
        t["user_os"] = ["iOS" if os_val == "ios" else "Android"]
        t["device_platforms"] = ["mobile"]
    else:
        t["device_platforms"] = ["mobile", "desktop"]
    return t


def placements_text(t: dict) -> str:
    """«manual: Facebook Feed, …» — что увидит человек в «Check plan»."""
    names = [POSITION_LABELS.get(("facebook", p), "Facebook " + p) for p in t.get("facebook_positions") or []]
    names += [POSITION_LABELS.get(("instagram", p), "Instagram " + p) for p in t.get("instagram_positions") or []]
    devices = t.get("device_platforms") or []
    tail = ", ".join(t["user_os"]) + " " if t.get("user_os") else ""
    return ("manual: " + ", ".join(names) + " · devices " + tail + "/".join(devices)) if names \
        else "automatic (Advantage+ placements)"


def creative_features() -> dict:
    """`degrees_of_freedom_spec`: улучшения Advantage+ creative — выключены."""
    return {"creative_features_spec": {f: {"enroll_status": OPT_OUT} for f in CREATIVE_FEATURES_OPT_OUT}}


# ───────────────────────── создание ─────────────────────────

def _labels(row: dict) -> set[str]:
    raw = row.get("adlabels")
    if isinstance(raw, dict):
        raw = raw.get("data")
    return {str(x.get("name") or "") for x in (raw or []) if isinstance(x, dict)}


def _md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()  # noqa: S324 — только имя/поиск, не защита


def image_name(path: str, data: bytes) -> str:
    """Имя картинки в библиотеке кабинета: `acl-<md5[:16]><ext>` (помощник его не меняет)."""
    ext = Path(path).suffix.lower()
    ext = ext if ext in IMAGE_EXT else ".jpg"
    return f"acl-{_md5(data)[:16]}{ext}"


def video_title(tag: str, data: bytes) -> str:
    return f"acl-{tag}-{_md5(data)[:12]}"


@dataclass
class Outcome:
    campaign_name: str
    created: dict = field(default_factory=dict)        # что создано ЭТИМ запуском
    reused: dict = field(default_factory=dict)         # что нашлось с прошлого запуска
    campaign_id: str = ""
    adset_ids: list[str] = field(default_factory=list)
    ad_ids: list[str] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    readback: dict = field(default_factory=dict)
    error: str = ""
    stop_kind: str = ""
    #: Объявление → что мы ему дали: creative_id, image_hash / video_id (для сверки, B2).
    ad_media: dict = field(default_factory=dict)
    #: Пробный запуск: что было бы создано (ни одной записи).
    plan: dict = field(default_factory=dict)
    #: Pre-flight audit (ACL-1819): вердикты серверных правил; пусто, если аудит не шёл.
    audit: list = field(default_factory=list)
    #: Что показать человеку списком (ACL-1437): title/impact/action/severity/raw.
    problems: list = field(default_factory=list)

    @property
    def mismatches(self) -> list[dict]:
        return list(self.readback.get("mismatches") or [])


class CampaignCreator:
    """Создать одну кампанию по плану — идемпотентно, PAUSED, со сверкой."""

    def __init__(self, client: GraphClient, facts: AccountFacts, resolver: TargetingResolver, *,
                 cancelled: Callable[[], bool] = lambda: False,
                 read_file: Callable[[str], bytes] | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic,
                 video_wait_s: float = VIDEO_WAIT_S, video_poll_s: float = VIDEO_POLL_S,
                 video_max_polls: int = VIDEO_MAX_POLLS,
                 reconcile_wait_s: float | None = None,
                 progress: Callable[[str], None] = lambda _s: None):
        self.client = client
        self.facts = facts
        self.resolver = resolver
        self.cancelled = cancelled
        self.read_file = read_file or (lambda p: Path(p).read_bytes())
        self.sleep = sleep
        self.clock = clock
        self.video_wait_s = video_wait_s
        self.video_poll_s = video_poll_s
        self.video_max_polls = video_max_polls
        self.reconcile_wait_s = RECONCILE_WAIT_S if reconcile_wait_s is None else reconcile_wait_s
        #: Имя → id наших креативов (список кабинета читается раз на кампанию).
        self._creatives: dict[str, str] | None = None
        #: Формы Страницы плана (читаются раз на кампанию, только у Instant Form).
        self._page_forms: _forms.FormsRead | None = None
        self.progress = progress

    # -- служебное
    def _step(self, out: Outcome, text: str) -> None:
        out.steps.append(text)
        self.progress(text)

    def _check_cancel(self, out: Outcome) -> None:
        if self.cancelled():
            raise CreationStopped("Stopped by the user", created=out.created, kind="cancelled")

    def _write(self, out: Outcome, what: str, path: str, params: dict, find: Callable[[], str | None],
               *, files: dict | None = None, filename: str = "", id_of=lambda b: b.get("id"),
               on_ad: bool = False) -> str:
        """Одна запись; неизвестный результат — сверка `find()` чтением."""
        self._check_cancel(out)
        try:
            # `on_ad` — только креатив Instant Form при наборе ON_AD (ACL-1775).
            body = (self.client.post(path, params, files=files, filename=filename, on_ad=True) if on_ad
                    else self.client.post(path, params, files=files, filename=filename))
        except WriteUnknown as exc:
            # `WritesBlocked` сюда не попадает: та запись не уходила (ревью M4).
            self._step(out, f"{what}: result unknown — reconciling by readback")
            if self.reconcile_wait_s > 0:
                self.sleep(self.reconcile_wait_s)
            try:
                found = find()
            except (GraphStop, GraphError) as rexc:
                raise CreationStopped(
                    f"{what}: the write result is unknown ({exc}) and the readback failed ({rexc}). "
                    "Nothing else was written; check Ads Manager, then publish again "
                    "(a re-run does not duplicate).", created=out.created, kind="unknown") from rexc
            if not found:
                raise CreationStopped(
                    f"{what}: the write result is unknown ({exc}) and nothing was found by readback. "
                    "Nothing else was written; publish again later (a re-run does not duplicate).",
                    created=out.created, kind="unknown") from exc
            self.client.reconciled(path)
            self._step(out, f"{what}: reconciled — found by readback")
            return str(found)
        got = id_of(body)
        if not got:
            # Ответ без id: неизвестно, создан ли объект — та же сверка.
            self.client.unreconciled = path
            found = find()
            if not found:
                raise CreationStopped(f"{what}: Graph answered without an id and nothing was found "
                                      "by readback.", created=out.created, kind="unknown")
            self.client.reconciled(path)
            return str(found)
        return str(got)

    # -- поиск существующего
    def _find_campaign(self, plan: CampaignPlan, *, after_unknown: bool = False) -> dict | None:
        """Наша кампания: точное имя + метка.

        `after_unknown` — сверка нашей же записи: до неё кампании с таким именем
        не было вовсе (иначе мы бы её не создавали), значит найденная — наша,
        даже если Meta не сохранил метку. Это и говорим прямо (ревью B1).
        """
        rows = self.client.get_all(f"/{plan.act}/campaigns", {
            "fields": "id,name,status,adlabels{name}",
            "filtering": [{"field": "name", "operator": "CONTAIN", "value": plan.name}],
            "effective_status": LIVE_STATES, "limit": 100}, max_pages=2)
        same = [r for r in rows if str(r.get("name") or "").strip() == plan.name]
        ours = [r for r in same if plan.label in _labels(r)]
        if ours:
            return ours[0]
        if same and after_unknown:
            raise CreationStopped(
                f"The campaign {plan.name!r} was created (PAUSED) but Meta did not keep the ACL tag "
                f"({plan.label}) on it. Stopped before any further write: without the tag a re-run "
                "could not recognise it. Check it in Ads Manager.", kind="label_missing")
        if same:
            raise CreationStopped(
                f"A campaign named {plan.name!r} already exists on the ad account and was not "
                "created by ACL. Nothing was created; rename the draft or remove the old campaign.",
                kind="name_taken")
        return None

    def _children(self, parent_id: str, edge: str, fields: str) -> list[dict]:
        return self.client.get_all(f"/{parent_id}/{edge}", {
            "fields": fields, "effective_status": LIVE_STATES, "limit": 100}, max_pages=2)

    def _find_child(self, parent_id: str, edge: str, name: str) -> dict | None:
        """Группа в НАШЕЙ кампании, объявление в НАШЕЙ группе — по точному имени.

        Метка ребёнку не нужна: владение доказывает родитель с меткой (ревью B1a).
        """
        rows = self._children(parent_id, edge, "id,name,status,adlabels{name}")
        return next((r for r in rows if str(r.get("name") or "").strip() == name), None)

    def _verify_label(self, out: Outcome, what: str, object_id: str, label: str) -> None:
        """Метка на созданном объекте; нет — стоп до следующей записи (ревью B1b)."""
        row = self.client.get(f"/{object_id}", {"fields": "adlabels{name}"})
        if label not in _labels(row):
            raise CreationStopped(
                f"{what} was created (PAUSED) but Meta did not keep the ACL tag ({label}) on it. "
                "Stopped before any further write so a re-run cannot duplicate anything. "
                "Check it in Ads Manager.", created=out.created, kind="label_missing")

    def _find_creative(self, plan: CampaignPlan, name: str, *, fresh: bool = False) -> str | None:
        """Наш креатив без объявления: точное имя + метка, сверка на нашей стороне.

        Живой запуск 5414 (job 2fb409d1): `/adcreatives` не принимает
        `filtering` по `name` с `CONTAIN` (#100). Поэтому — постраничный список
        свежих креативов кабинета (`CREATIVE_LOOKUP_PAGES` × 100, новые первыми)
        один раз на кампанию; `fresh` — перечитать (сверка неизвестной записи).
        Не нашли — значит, креатива нет среди свежих: лишний креатив без
        объявления безвреден, дубля объявления не бывает (объявления ищутся в
        нашей группе по имени).
        """
        if fresh or self._creatives is None:
            rows = self.client.get_all(f"/{plan.act}/adcreatives", {
                "fields": "id,name,adlabels{name}", "limit": 100}, max_pages=CREATIVE_LOOKUP_PAGES)
            self._creatives = {}
            for r in rows:
                if plan.label in _labels(r) and r.get("id"):
                    self._creatives.setdefault(str(r.get("name") or ""), str(r["id"]))
        return self._creatives.get(name)

    # -- медиа
    def _image_hash(self, out: Outcome, plan: CampaignPlan, ad: AdPlan, data: bytes) -> str:
        md5 = _md5(data)

        def find() -> str | None:
            rows = self.client.get_all(f"/{plan.act}/adimages", {"hashes": [md5], "fields": "hash"},
                                       max_pages=1)
            return md5 if any(str(r.get("hash")) == md5 for r in rows) else None

        if find():
            self._step(out, f"ad {ad.index}: image already in the ad account")
            return md5
        def hash_of(body: dict) -> str | None:
            images = body.get("images") if isinstance(body, dict) else None
            for v in (images or {}).values():
                if isinstance(v, dict) and v.get("hash"):
                    return str(v["hash"])
            return None

        # Имя в библиотеке несёт md5 файла: по нему сверка узнаёт картинку и при повторе.
        got = self._write(out, f"ad {ad.index}: image upload", f"/{plan.act}/adimages", {}, find,
                          files={"filename": data}, filename=image_name(ad.path, data), id_of=hash_of)
        out.created.setdefault("images", []).append(got)
        self._step(out, f"ad {ad.index}: image uploaded")
        return got

    def _video(self, out: Outcome, plan: CampaignPlan, ad: AdPlan, data: bytes) -> tuple[str, str]:
        title = video_title(plan.tag, data)

        def find() -> str | None:
            rows = self.client.get_all(f"/{plan.act}/advideos", {"fields": "id,title", "limit": 100},
                                       max_pages=10)
            hit = next((r for r in rows if str(r.get("title") or "") == title), None)
            return str(hit["id"]) if hit else None

        vid = find()
        if vid:
            self._step(out, f"ad {ad.index}: video already in the ad account")
        else:
            vid = self._write(out, f"ad {ad.index}: video upload", f"/{plan.act}/advideos",
                              {"name": title, "title": title}, find, files={"source": data},
                              filename=Path(ad.path).name)
            out.created.setdefault("videos", []).append(vid)
            self._step(out, f"ad {ad.index}: video uploaded")
        return vid, self._wait_video(out, ad, vid)

    def _wait_video(self, out: Outcome, ad: AdPlan, vid: str) -> str:
        """Ждать обработку видео — не дольше `video_wait_s` и `video_max_polls` вызовов."""
        deadline = self.clock() + self.video_wait_s
        polls = 0
        while True:
            self._check_cancel(out)
            row = self.client.get(f"/{vid}", {"fields": "status"})
            polls += 1
            status = row.get("status") if isinstance(row.get("status"), dict) else {}
            state = str(status.get("video_status") or "").lower()
            if state == "ready":
                break
            if state == "error":
                raise CreationStopped(f"ad {ad.index}: Meta could not process the video", created=out.created,
                                      kind="video_error")
            if polls >= self.video_max_polls or self.clock() + self.video_poll_s > deadline:
                raise CreationStopped(
                    f"ad {ad.index}: the video is still processing after {int(self.video_wait_s)} s; "
                    "publish again later (nothing is duplicated)", created=out.created, kind="video_timeout")
            self.sleep(self.video_poll_s)
        self._step(out, f"ad {ad.index}: video processed")
        thumbs = self.client.get_all(f"/{vid}/thumbnails", {"fields": "uri,is_preferred"}, max_pages=1)
        pick = next((t for t in thumbs if t.get("is_preferred")), thumbs[0] if thumbs else None)
        if not pick or not pick.get("uri"):
            raise CreationStopped(f"ad {ad.index}: Meta returned no thumbnail for the video",
                                  created=out.created, kind="video_error")
        return str(pick["uri"])

    # -- описание объектов (одно на запись и на сверку)
    def campaign_params(self, plan: CampaignPlan) -> dict:
        return {"name": plan.name, "objective": OBJECTIVE, "status": PAUSED,
                "special_ad_categories": [], "buying_type": "AUCTION",
                "daily_budget": budget_minor(plan.daily_budget, self.facts.currency),
                "bid_strategy": BID_STRATEGY, "adlabels": [{"name": plan.label}]}

    def adset_params(self, plan: CampaignPlan, ad_set: AdSetPlan, campaign_id: str, targeting: dict) -> dict:
        if ad_set.instant:
            # Instant Form (ACL-1775): показ в самой форме, цель — лиды, объект — Страница, без пикселя.
            return {"name": ad_set.name, "campaign_id": campaign_id, "status": PAUSED,
                    "optimization_goal": OPTIMIZATION_GOAL_FORM, "billing_event": BILLING_EVENT,
                    "destination_type": DESTINATION_ON_AD,
                    "promoted_object": {"page_id": plan.page_id},
                    "targeting": targeting, "adlabels": [{"name": plan.label}]}
        return {"name": ad_set.name, "campaign_id": campaign_id, "status": PAUSED,
                "optimization_goal": OPTIMIZATION_GOAL, "billing_event": BILLING_EVENT,
                "destination_type": DESTINATION,
                "promoted_object": {"pixel_id": plan.pixel_id, "custom_event_type": CUSTOM_EVENT},
                "targeting": targeting, "adlabels": [{"name": plan.label}]}

    @staticmethod
    def story_spec(plan: CampaignPlan, ad: AdPlan, media: dict, *, instant: bool = False) -> dict:
        if instant:
            # ACL-1775: CTA несёт id существующей формы; своей ссылки у объявления нет.
            cta = {"type": CTA_TYPE, "value": {"lead_gen_form_id": ad.form_id}}
        else:
            cta = {"type": CTA_TYPE, "value": {"link": plan.link}}
        if ad.kind == "video":
            data = {"video_id": media["video_id"], "image_url": media["thumb"], "title": ad.title,
                    "message": ad.body, "call_to_action": cta}
            if ad.description:
                data["link_description"] = ad.description
            return {"page_id": plan.page_id, "video_data": data}
        data = {"link": INSTANT_FORM_LINK if instant else plan.link, "message": ad.body,
                "name": ad.title, "image_hash": media["image_hash"], "call_to_action": cta}
        if ad.description:
            data["description"] = ad.description
        return {"page_id": plan.page_id, "link_data": data}

    def _story(self, plan: CampaignPlan, ad: AdPlan, ad_set: AdSetPlan, media: dict) -> dict:
        """Спецификация креатива; `instant` передаётся только Instant Form (сайт - как раньше)."""
        if ad_set.instant:
            return self.story_spec(plan, ad, media, instant=True)
        return self.story_spec(plan, ad, media)

    # -- общие проверки кампании и группы (сверка после записи и предпроверка найденных)
    @staticmethod
    def _mk_check(checks: list[dict], obj: str, fld: str, expected: Any, actual: Any) -> None:
        checks.append({"object": obj, "field": fld, "expected": expected, "actual": actual,
                       "ok": _same(expected, actual), "not_read": _not_read(expected, actual)})

    def _campaign_checks(self, plan: CampaignPlan, camp: dict) -> list[dict]:
        checks: list[dict] = []

        def check(f: str, e: Any, a: Any) -> None:
            self._mk_check(checks, "campaign", f, e, a)

        want = self.campaign_params(plan)
        check("name", want["name"], camp.get("name"))
        check("status", PAUSED, camp.get("status"))
        check("objective", OBJECTIVE, camp.get("objective"))
        check("daily_budget", str(want["daily_budget"]), str(camp.get("daily_budget") or ""))
        check("bid_strategy", BID_STRATEGY, camp.get("bid_strategy"))
        check("tag", plan.label, plan.label if plan.label in _labels(camp) else "")
        return checks

    def _adset_checks(self, plan: CampaignPlan, ad_set: AdSetPlan, targeting: dict, row: dict,
                      campaign_id: str) -> list[dict]:
        obj = f"ad set {ad_set.name}"
        checks: list[dict] = []

        def check(f: str, e: Any, a: Any) -> None:
            self._mk_check(checks, obj, f, e, a)

        check("name", ad_set.name, row.get("name"))
        check("status", PAUSED, row.get("status"))
        check("campaign_id", campaign_id, str(row.get("campaign_id") or ""))
        check("tag", plan.label, plan.label if plan.label in _labels(row) else "")
        po = row.get("promoted_object") or {}
        if ad_set.instant:
            # ACL-1775: Instant Form — ON_AD, LEAD_GENERATION, Страница в promoted_object.
            check("optimization_goal", OPTIMIZATION_GOAL_FORM, row.get("optimization_goal"))
            check("billing_event", BILLING_EVENT, row.get("billing_event"))
            check("destination_type", DESTINATION_ON_AD, row.get("destination_type"))
            check("promoted_page", plan.page_id, str(po.get("page_id") or ""))
        else:
            check("optimization_goal", OPTIMIZATION_GOAL, row.get("optimization_goal"))
            check("billing_event", BILLING_EVENT, row.get("billing_event"))
            check("destination_type", DESTINATION, row.get("destination_type"))
            check("pixel", plan.pixel_id, str(po.get("pixel_id") or ""))
            check("conversion_event", CUSTOM_EVENT, po.get("custom_event_type"))
        check("schedule", "starts on activation, no end date",
              "starts on activation, no end date" if not row.get("end_time") else
              f"ends {row.get('end_time')}")
        t = row.get("targeting") or {}
        check("geo", targeting["geo_locations"]["countries"], (t.get("geo_locations") or {}).get("countries"))
        check("age_min", targeting["age_min"], t.get("age_min"))
        check("age_max", targeting["age_max"], t.get("age_max"))
        # Локали: если Meta поле не вернула, это «Not read», а не «пусто».
        # None («Not read») только если локали ждали, а Meta их не вернула; иначе как раньше.
        want_locales = targeting.get("locales") or []
        check("languages", want_locales,
              None if want_locales and "locales" not in t else t.get("locales") or [])
        check("detailed_targeting", _flex_ids(targeting.get("flexible_spec")), _flex_ids(t.get("flexible_spec")))
        check("os", targeting.get("user_os") or [], t.get("user_os") or [])
        # Площадки вручную, как у робота (паритет RPA): платформы, места, устройства.
        check("publisher_platforms", targeting.get("publisher_platforms") or [],
              t.get("publisher_platforms") or [])
        check("facebook_positions", targeting.get("facebook_positions") or [],
              t.get("facebook_positions") or [])
        check("instagram_positions", targeting.get("instagram_positions") or [],
              t.get("instagram_positions") or [])
        check("devices", targeting.get("device_platforms") or [], t.get("device_platforms") or [])
        return checks

    # -- найденные кампания и группы против плана (до любой записи под ними)
    def _existing_differences(self, plan: CampaignPlan, targetings: list[dict], campaign_id: str,
                              found_sets: list[dict | None]) -> list[dict]:
        """Что в уже существующих кампании/группах не совпадает с билдером. Только чтение.

        Имя, метка, campaign_id и расписание не показываем: имя и метку доказал поиск,
        остальное сверит сверка после записи. Ничего не обновляется и не удаляется.
        """
        found: list[dict] = []
        camp = self.client.get(f"/{campaign_id}", {"fields": CAMPAIGN_FIELDS})
        diffs = _describe_diffs(self._campaign_checks(plan, camp))
        if diffs:
            found.append({"object": f"campaign {plan.name!r}", "kind": "campaign", "differences": diffs})
        for ad_set, targeting, hit in zip(plan.ad_sets, targetings, found_sets):
            if not hit:
                continue
            row = self.client.get(f"/{hit['id']}", {"fields": ADSET_FIELDS})
            diffs = _describe_diffs(self._adset_checks(plan, ad_set, targeting, row, campaign_id))
            if diffs:
                found.append({"object": f"ad set {ad_set.name!r}", "kind": "adset", "differences": diffs})
        return found

    def _find_sets(self, plan: CampaignPlan, campaign_id: str) -> list[dict | None]:
        return [self._find_child(campaign_id, "adsets", s.name) for s in plan.ad_sets]

    # -- весь путь
    def run(self, plan: CampaignPlan) -> Outcome:
        self._creatives = None
        out = Outcome(campaign_name=plan.name, warnings=list(plan.warnings))
        try:
            self._run(plan, out)
        except CreationStopped as exc:
            out.error, out.stop_kind = str(exc), exc.kind
        except PlanError as exc:
            out.error, out.stop_kind = str(exc) + " Nothing more was created.", "plan"
            out.problems = list(exc.problems)
        except WritesBlocked as exc:
            out.error, out.stop_kind = f"Not written: {exc}", "blocked"
        except GraphStop as exc:
            out.error, out.stop_kind = f"Graph API stopped: {_catalog.problem_line(exc)}", exc.kind
            out.problems = [graph_problem(exc)]
        except GraphError as exc:
            out.error, out.stop_kind = f"Meta refused: {_catalog.problem_line(exc)}", "graph_error"
            out.problems = [graph_problem(exc)]
        except WriteUnknown as exc:
            out.error, out.stop_kind = f"Write result unknown: {exc}", "unknown"
        return out

    def _prepare(self, plan: CampaignPlan, out: Outcome) -> tuple[list[dict], dict[str, bytes]]:
        """Всё, что проверяется до записи: страница, пиксель, таргетинг, файлы."""
        out.warnings += resolve_page_pixel(plan, self.facts)
        targetings = [targeting_for(plan, s, self.resolver) for s in plan.ad_sets]
        self._step(out, "preflight: targeting resolved")
        self._page_forms = None
        if any(s.instant for s in plan.ad_sets):
            self._check_forms(plan)
            self._step(out, "preflight: Instant Forms belong to the Page")
        files: dict[str, bytes] = {}
        for s in plan.ad_sets:
            for ad in s.ads:
                try:
                    data = self.read_file(ad.path)
                except OSError as exc:
                    raise PlanError(f"ad {ad.index}: creative file cannot be read ({exc})") from exc
                limit = VIDEO_MAX_BYTES if ad.kind == "video" else IMAGE_MAX_BYTES
                if not data or len(data) > limit:
                    raise PlanError(f"ad {ad.index}: creative file is empty or larger than "
                                    f"{limit // (1024 * 1024)} MB")
                files[ad.path] = data
        self._step(out, "preflight: creatives read")
        self._check_planned(plan, targetings)
        self._check_meta_rules(plan, targetings)
        self._step(out, "preflight: Meta rules registry")
        return targetings, files

    def _check_forms(self, plan: CampaignPlan) -> None:
        """Формы объявлений — формы ЭТОЙ Страницы (ACL-1775). Только чтение, до любой записи.

        Чужая, архивная или несуществующая форма — `PlanError`, к Meta ничего не ушло.
        """
        read = _forms.read_page_forms(self.client, plan.page_id)
        if not read.ok:
            raise PlanError(read.message + ".")
        known = read.by_id()
        for ad_set in plan.ad_sets:
            if not ad_set.instant:
                continue
            for ad in ad_set.ads:
                form = known.get(ad.form_id)
                if form is None:
                    raise PlanError(f"Ad {ad.index}: the Instant Form {ad.form_id} is not a form of this "
                                    "Page; pick one of the Page's forms (Refresh forms).")
                if not form.usable:
                    raise PlanError(f"Ad {ad.index}: the Instant Form {form.name!r} is "
                                    f"{form.status.lower() or 'of unknown status'} (only ACTIVE forms can be "
                                    "used); pick another form.")
        self._page_forms = read

    def _check_planned(self, plan: CampaignPlan, targetings: list[dict]) -> None:
        """Политика записи над КАЖДЫМ запланированным объектом — до первой записи (R2-B1).

        Отказ здесь — `PlanError` этой кампании: к Meta ничего не ушло, и клиент
        не останавливается для остальных кампаний задания. Пробный запуск
        проходит ту же проверку, что и настоящий.
        """
        from src.fb_api import write_policy as _wp

        def ok(edge: str, params: dict, what: str, on_ad: bool = False) -> None:
            try:
                _wp.check(plan.act, "POST", f"/{plan.act}/{edge}", params, on_ad=on_ad)
            except _wp.WriteRefused as exc:
                raise PlanError(f"{what}: {exc}") from exc

        ok("campaigns", self.campaign_params(plan), "campaign")
        for ad_set, targeting in zip(plan.ad_sets, targetings):
            ok("adsets", self.adset_params(plan, ad_set, "0", targeting), f"ad set {ad_set.name!r}")
            for ad in ad_set.ads:
                media = ({"video_id": "0", "thumb": "https://example.invalid/t.jpg"} if ad.kind == "video"
                         else {"image_hash": "0"})
                ok("adcreatives", {"name": f"{ad.name} #{ad.index} [{plan.label}]",
                                   "object_story_spec": self._story(plan, ad, ad_set, media),
                                   "degrees_of_freedom_spec": creative_features(),
                                   "adlabels": [{"name": plan.label}]}, f"ad {ad.index} creative",
                   on_ad=ad_set.instant)
                ok("ads", {"name": ad.name, "adset_id": "0", "creative": {"creative_id": "0"},
                           "status": PAUSED, "adlabels": [{"name": plan.label}]}, f"ad {ad.index}")

    def meta_rule_jobs(self, plan: CampaignPlan, targetings: list[dict]) -> list[tuple[str, dict]]:
        """По заданию на объявление - ровно те значения, что уйдут в POST (ACL-1805)."""
        camp = self.campaign_params(plan)
        jobs = []
        for ad_set, targeting in zip(plan.ad_sets, targetings):
            aset = self.adset_params(plan, ad_set, "0", targeting)
            for ad in ad_set.ads:
                media = ({"video_id": "0", "thumb": "x"} if ad.kind == "video" else {"image_hash": "0"})
                story = self._story(plan, ad, ad_set, media)
                data = story.get("video_data") or story.get("link_data") or {}
                cta = data.get("call_to_action") or {}
                jobs.append((f"ad set {ad_set.name!r}, ad {ad.index}", {
                    "objective": camp["objective"], "bid_strategy": camp["bid_strategy"],
                    "optimization_goal": aset["optimization_goal"], "billing_event": aset["billing_event"],
                    "destination_type": aset["destination_type"], "call_to_action": cta.get("type"),
                    "ad_title": ad.title, "ad_body": ad.body, "ad_link_description": ad.description or None,
                    "api_preview": {"campaign": {"special_ad_categories": camp["special_ad_categories"]}}}))
        return jobs

    def _check_meta_rules(self, plan: CampaignPlan, targetings: list[dict]) -> None:
        """Реестр правил Meta (ACL-1781) над точными значениями запроса - до первой записи.

        Нарушение - `RulesViolation`, к Meta ничего не ушло. Реестр не загрузился
        или проверка упала - пропускаем с записью в лог (кампанию это не блокирует).
        """
        try:
            from src.campaign_bridge import meta_rules

            ruleset = meta_rules.load_rules()
            found: dict[str, Any] = {}
            for where, job in self.meta_rule_jobs(plan, targetings):
                for v in meta_rules.check_job(job, ruleset):
                    found.setdefault(f"{v.rule_id}|{v.message_en}", (where, v))
        except Exception:
            log.exception("Meta rules check failed; the campaign is not blocked by it")
            return
        if not found:
            return
        problems = [{"title": v.message_en, "impact": "Meta would refuse this campaign.",
                     "action": "Fix the value in the campaign settings and publish again.",
                     "severity": "blocking", "step": "meta_rules",
                     "raw": f"{where}; rule {v.rule_id}; source: {v.source_url}"}
                    for where, v in found.values()]
        text = " ".join(f"Meta rules: {p['title']}" for p in problems)
        raise RulesViolation(text, problems)

    def plan_only(self, plan: CampaignPlan) -> Outcome:
        """Пробный запуск (ревью M3): предпроверка и план, НИ ОДНОЙ записи.

        Клиент пробного запуска создаётся без права записи вовсе; здесь же
        `post` не зовётся ни разу.
        """
        self._creatives = None
        out = Outcome(campaign_name=plan.name, warnings=list(plan.warnings))
        differences: list | None = None
        files: dict | None = None
        try:
            targetings, files = self._prepare(plan, out)
            existing = self._find_campaign(plan)
            differences = (self._existing_differences(plan, targetings, str(existing["id"]),
                                                      self._find_sets(plan, str(existing["id"])))
                           if existing else [])
            sets = []
            for ad_set, targeting in zip(plan.ad_sets, targetings):
                ads = []
                for ad in ad_set.ads:
                    data = files[ad.path]
                    media = ({"video_id": "<uploaded video>", "thumb": "<video thumbnail>"} if ad.kind == "video"
                             else {"image_hash": "<uploaded image>"})
                    ads.append({"name": ad.name, "status": PAUSED, "format": ad.kind,
                                "file": Path(ad.path).name, "bytes": len(data),
                                "library_name": (video_title(plan.tag, data) if ad.kind == "video"
                                                 else image_name(ad.path, data)),
                                "creative": self._story(plan, ad, ad_set, media)})
                sets.append({**self.adset_params(plan, ad_set, "<campaign>", targeting), "ads": ads})
            out.plan = {"campaign": self.campaign_params(plan),
                        "existing_campaign_id": str(existing["id"]) if existing else "",
                        "ad_sets": sets,
                        "summary": self.plan_summary(plan, targetings, files,
                                                     existing=bool(existing),
                                                     existing_differences=differences)}
            self._step(out, "dry run: preflight passed, nothing was written")
        except CreationStopped as exc:
            out.error, out.stop_kind = str(exc), exc.kind
        except PlanError as exc:
            out.error, out.stop_kind = str(exc), "plan"
            out.problems = list(exc.problems)
        except GraphStop as exc:
            out.error, out.stop_kind = f"Graph API stopped: {_catalog.problem_line(exc)}", exc.kind
            out.problems = [graph_problem(exc)]
        except GraphError as exc:
            out.error, out.stop_kind = f"Meta refused: {_catalog.problem_line(exc)}", "graph_error"
            out.problems = [graph_problem(exc)]
        self._audit(plan, out, existing=differences, checked=differences is not None, files=files)
        return out

    def _audit(self, plan: CampaignPlan, out: Outcome, *, existing: list | None, checked: bool,
               files: dict | None) -> None:
        """Pre-flight audit (ACL-1819): независимые вердикты серверных правил, только чтения.

        Не идёт после остановки Graph (лимит/блокировка): новых чтений там быть не должно.
        Сбой самого аудита не ломает план: вердиктов просто нет.
        """
        if out.stop_kind not in ("", "plan", "graph_error"):
            return
        from src.fb_api import preflight_rules as _pf

        try:
            out.audit = _pf.server_verdicts(self, plan, existing=existing, existing_checked=checked, files=files)
        except Exception:  # noqa: BLE001 — аудит вспомогательный, план от него не зависит
            import logging

            logging.getLogger(__name__).exception("pre-flight audit failed for %r", plan.name)
            out.audit = []

    def plan_summary(self, plan: CampaignPlan, targetings: list[dict], files: dict[str, bytes], *,
                     existing: bool = False, existing_differences: list[dict] | None = None) -> dict:
        """Что увидит человек в «Check plan»: читаемые поля, без сырых спецификаций."""
        sets = []
        for ad_set, t in zip(plan.ad_sets, targetings):
            interests = [[i.get("name") or i.get("id") for bucket in g.values() if isinstance(bucket, list)
                          for i in bucket] for g in (t.get("flexible_spec") or [])]
            sets.append({
                "name": ad_set.name, "status": PAUSED,
                "countries": list((t.get("geo_locations") or {}).get("countries") or []),
                "language": plan.language, "locales": list(t.get("locales") or []),
                "age": f"{ad_set.age_min}-{ad_set.age_max}",
                "interests": interests,
                "os": ad_set.os or "All",
                "placements": placements_text(t),
                "creative_enhancements": "off: " + ", ".join(CREATIVE_FEATURES_OPT_OUT),
                "optimization": f"{OPTIMIZATION_GOAL} · {CUSTOM_EVENT} on the website pixel",
                "ads": [{
                    "index": ad.index, "name": ad.name, "status": PAUSED, "format": ad.kind,
                    "file": Path(ad.path).name, "bytes": len(files.get(ad.path, b"")),
                    "headline": ad.title, "primary_text": ad.body, "description": ad.description,
                    "link": plan.link, "cta": CTA_TYPE,
                } for ad in ad_set.ads],
            })
            if ad_set.instant:
                # ACL-1775: вместо сайта - форма Страницы (имя и id); ключей сайта у такого набора нет.
                known = self._page_forms.by_id() if self._page_forms else {}
                row = sets[-1]
                row["destination"] = "Instant Form"
                row["optimization"] = f"{OPTIMIZATION_GOAL_FORM} · Instant Form of Page {plan.page_id}"
                for item, ad in zip(row["ads"], ad_set.ads):
                    item.pop("link", None)
                    form = known.get(ad.form_id)
                    item["form_id"] = ad.form_id
                    item["form_name"] = form.name if form else ""
        return {
            "campaign": plan.name, "status": PAUSED, "objective": OBJECTIVE,
            "daily_budget": plan.daily_budget, "currency": self.facts.currency,
            "budget_minor": budget_minor(plan.daily_budget, self.facts.currency),
            "page_id": plan.page_id, "pixel_id": plan.pixel_id,
            "page_source": plan.page_source, "pixel_source": plan.pixel_source,
            "existing": existing, "existing_differences": list(existing_differences or []),
            "tag": plan.label, "ad_sets": sets,
        }

    def _run(self, plan: CampaignPlan, out: Outcome) -> None:
        # 1. Всё, что можно проверить до записи.
        targetings, files = self._prepare(plan, out)

        # 2. Кампания (наша — по имени и метке).
        existing = self._find_campaign(plan)
        if existing:
            out.campaign_id = str(existing["id"])
            out.reused["campaign"] = out.campaign_id
            self._step(out, "campaign: found from a previous run")
            # Прежде чем что-то добавлять под найденную кампанию/группы — сверить их с планом.
            found_sets = self._find_sets(plan, out.campaign_id)
            bad = self._existing_differences(plan, targetings, out.campaign_id, found_sets)
            if bad:
                raise CreationStopped(_existing_message(bad), created=out.created, kind="existing_differs")
            self._step(out, "existing campaign and ad sets match the builder")
        else:
            found_sets = [None] * len(plan.ad_sets)
            out.campaign_id = self._write(
                out, "campaign", f"/{plan.act}/campaigns", self.campaign_params(plan),
                lambda: (self._find_campaign(plan, after_unknown=True) or {}).get("id"))
            out.created["campaign"] = out.campaign_id
            self._verify_label(out, f"The campaign {plan.name!r}", out.campaign_id, plan.label)
            self._step(out, "campaign: created PAUSED, tag kept")

        # 3. Группы и объявления (дети нашей кампании — по точному имени, B1a).
        creative_label_checked = ad_label_checked = False
        for ad_set, targeting, found in zip(plan.ad_sets, targetings, found_sets):
            if found:
                adset_id = str(found["id"])
                out.reused.setdefault("adsets", []).append(adset_id)
                self._step(out, f"ad set {ad_set.name!r}: found from a previous run")
            else:
                adset_id = self._write(
                    out, f"ad set {ad_set.name!r}", f"/{plan.act}/adsets",
                    self.adset_params(plan, ad_set, out.campaign_id, targeting),
                    lambda: (self._find_child(out.campaign_id, "adsets", ad_set.name) or {}).get("id"))
                out.created.setdefault("adsets", []).append(adset_id)
                self._verify_label(out, f"The ad set {ad_set.name!r}", adset_id, plan.label)
                self._step(out, f"ad set {ad_set.name!r}: created PAUSED, tag kept")
            out.adset_ids.append(adset_id)

            have = {str(r.get("name") or "").strip(): str(r["id"])
                    for r in self._children(adset_id, "ads", "id,name")}
            for ad in ad_set.ads:
                if ad.name in have:
                    out.ad_ids.append(have[ad.name])
                    out.reused.setdefault("ads", []).append(have[ad.name])
                    out.ad_media[ad.index] = {"ad_id": have[ad.name], "reused": True}
                    self._step(out, f"ad {ad.index}: found from a previous run")
                    continue
                data = files[ad.path]
                rec: dict[str, Any] = {}
                if ad.kind == "video":
                    vid, thumb = self._video(out, plan, ad, data)
                    media = {"video_id": vid, "thumb": thumb}
                    rec["video_id"] = vid
                else:
                    media = {"image_hash": self._image_hash(out, plan, ad, data)}
                    rec["image_hash"] = media["image_hash"]
                # Номер объявления в имени креатива: два объявления не делят креатив (M2).
                creative_name = f"{ad.name} #{ad.index} [{plan.label}]"
                creative_id = self._find_creative(plan, creative_name)
                if creative_id:
                    self._step(out, f"ad {ad.index}: creative found from a previous run")
                else:
                    creative_id = self._write(
                        out, f"ad {ad.index}: creative", f"/{plan.act}/adcreatives",
                        {"name": creative_name,
                         "object_story_spec": self._story(plan, ad, ad_set, media),
                         "degrees_of_freedom_spec": creative_features(),
                         "adlabels": [{"name": plan.label}]},
                        lambda: self._find_creative(plan, creative_name, fresh=True),
                        on_ad=ad_set.instant)
                    out.created.setdefault("creatives", []).append(creative_id)
                    if not creative_label_checked:
                        self._verify_label(out, f"The creative of ad {ad.index}", creative_id, plan.label)
                        creative_label_checked = True
                    self._step(out, f"ad {ad.index}: creative created")
                ad_id = self._write(
                    out, f"ad {ad.index}", f"/{plan.act}/ads",
                    {"name": ad.name, "adset_id": adset_id, "creative": {"creative_id": creative_id},
                     "status": PAUSED, "adlabels": [{"name": plan.label}]},
                    lambda: (self._find_child(adset_id, "ads", ad.name) or {}).get("id"))
                out.ad_ids.append(ad_id)
                out.created.setdefault("ads", []).append(ad_id)
                rec.update({"ad_id": ad_id, "creative_id": creative_id})
                out.ad_media[ad.index] = rec
                if not ad_label_checked:
                    self._verify_label(out, f"The ad {ad.index}", ad_id, plan.label)
                    ad_label_checked = True
                self._step(out, f"ad {ad.index}: created PAUSED")

        # 4. Сверка.
        out.readback = self.readback(plan, out, targetings, files)
        n = len(out.mismatches)
        self._step(out, "readback: all fields match" if not n else f"readback: {n} mismatch(es)")

    # ───────────────────────── сверка ─────────────────────────
    @staticmethod
    def _form_checks(check: Callable[[str, str, Any, Any], None], aobj: str, ad: AdPlan, cta: dict,
                     page_forms: _forms.FormsRead | None) -> None:
        """Instant Form в креативе: тот ли id и числится ли он среди форм Страницы (ACL-1775)."""
        check(aobj, "lead_gen_form_id", ad.form_id, str((cta.get("value") or {}).get("lead_gen_form_id") or ""))
        on_page = bool(page_forms and page_forms.ok and ad.form_id in page_forms.by_id())
        check(aobj, "form_on_page", True, on_page)

    def readback(self, plan: CampaignPlan, out: Outcome, targetings: list[dict],
                 files: dict[str, bytes] | None = None) -> dict:
        checks: list[dict] = []
        files = files or {}

        def check(obj: str, fld: str, expected: Any, actual: Any) -> None:
            checks.append({"object": obj, "field": fld, "expected": expected, "actual": actual,
                           "ok": _same(expected, actual), "not_read": _not_read(expected, actual)})

        def tag(obj: str, row: dict) -> None:
            check(obj, "tag", plan.label, plan.label if plan.label in _labels(row) else "")

        camp = self.client.get(f"/{out.campaign_id}", {"fields": CAMPAIGN_FIELDS})
        checks += self._campaign_checks(plan, camp)

        # ACL-1775: форма принадлежит Странице - читаем список форм ещё раз, уже после записи.
        page_forms = (_forms.read_page_forms(self.client, plan.page_id)
                      if any(s.instant for s in plan.ad_sets) else None)
        image_ads: list[tuple[str, AdPlan, str]] = []    # (объект, объявление, hash в объявлении)
        for ad_set, adset_id, targeting in zip(plan.ad_sets, out.adset_ids, targetings):
            obj = f"ad set {ad_set.name}"
            row = self.client.get(f"/{adset_id}", {"fields": ADSET_FIELDS})
            checks += self._adset_checks(plan, ad_set, targeting, row, out.campaign_id)

            ads = self.client.get_all(f"/{adset_id}/ads", {
                "fields": "id,name,status,adset_id,adlabels{name},"
                          "creative{id,adlabels{name},object_story_spec,degrees_of_freedom_spec}",
                "effective_status": LIVE_STATES, "limit": 100}, max_pages=2)
            by_name: dict[str, list[dict]] = {}
            for a in ads:
                by_name.setdefault(str(a.get("name") or "").strip(), []).append(a)
            for ad in ad_set.ads:
                aobj = f"ad {ad.index} ({ad.name})"
                rec = out.ad_media.get(ad.index) or {}
                rows = by_name.get(ad.name) or []
                check(aobj, "count", 1, len(rows))
                if not rows:
                    continue
                got = rows[0]
                if rec.get("ad_id"):
                    check(aobj, "ad_id", rec["ad_id"], str(got.get("id") or ""))
                check(aobj, "status", PAUSED, got.get("status"))
                check(aobj, "adset_id", adset_id, str(got.get("adset_id") or ""))
                tag(aobj, got)
                creative = got.get("creative") or {}
                if rec.get("creative_id"):
                    check(aobj, "creative_id", rec["creative_id"], str(creative.get("id") or ""))
                tag(aobj + " creative", creative)
                feats = ((creative.get("degrees_of_freedom_spec") or {}).get("creative_features_spec") or {})
                for feature in CREATIVE_FEATURES_OPT_OUT:
                    check(aobj, f"enhancement {feature}", OPT_OUT,
                          (feats.get(feature) or {}).get("enroll_status"))
                spec = creative.get("object_story_spec") or {}
                check(aobj, "page", plan.page_id, str(spec.get("page_id") or ""))
                data = files.get(ad.path, b"")
                if ad.kind == "video":
                    vd = spec.get("video_data") or {}
                    cta = vd.get("call_to_action") or {}
                    check(aobj, "format", "video", "video" if vd else "image")
                    vid = str(vd.get("video_id") or "")
                    if rec.get("video_id"):
                        check(aobj, "video_id", rec["video_id"], vid)
                    if data and vid:
                        title = self.client.get(f"/{vid}", {"fields": "title"}).get("title")
                        check(aobj, "video_file", video_title(plan.tag, data), title)
                    check(aobj, "call_to_action", CTA_TYPE, cta.get("type"))
                    if ad_set.instant:
                        self._form_checks(check, aobj, ad, cta, page_forms)
                    else:
                        check(aobj, "link", plan.link, (cta.get("value") or {}).get("link"))
                    check(aobj, "headline", ad.title, vd.get("title") or "")
                    check(aobj, "primary_text", ad.body, vd.get("message") or "")
                    check(aobj, "description", ad.description, vd.get("link_description") or "")
                else:
                    ld = spec.get("link_data") or {}
                    cta = ld.get("call_to_action") or {}
                    check(aobj, "format", "image", "image" if ld.get("image_hash") else "video")
                    h = str(ld.get("image_hash") or "")
                    if rec.get("image_hash"):
                        # Хэш — тот, что Meta вернула при загрузке (или нашла), не md5.
                        check(aobj, "image_hash", rec["image_hash"], h)
                    # R2-M1: хэш равен md5 нашего файла — содержимое доказано, имя в
                    # библиотеке (старая загрузка RPA или руками) не проверяем.
                    if data and h and h != _md5(data):
                        image_ads.append((aobj, ad, h))
                    check(aobj, "call_to_action", CTA_TYPE, cta.get("type"))
                    if ad_set.instant:
                        self._form_checks(check, aobj, ad, cta, page_forms)
                    else:
                        check(aobj, "cta_link", plan.link, (cta.get("value") or {}).get("link"))
                        check(aobj, "link", plan.link, ld.get("link"))
                    check(aobj, "headline", ad.title, ld.get("name") or "")
                    check(aobj, "primary_text", ad.body, ld.get("message") or "")
                    check(aobj, "description", ad.description, ld.get("description") or "")
        if image_ads:
            # Какой файл за хэшем: имя в библиотеке несёт md5 нашего файла.
            rows = self.client.get_all(f"/{plan.act}/adimages", {
                "hashes": sorted({h for _, _, h in image_ads}), "fields": "hash,name"}, max_pages=1)
            names = {str(r.get("hash")): str(r.get("name") or "") for r in rows}
            for aobj, ad, h in image_ads:
                check(aobj, "image_file", image_name(ad.path, files[ad.path]), names.get(h, ""))
        mismatches = [c for c in checks if not c["ok"]]
        return {"ok": not mismatches, "checked": len(checks), "checks": checks, "mismatches": mismatches}


CAMPAIGN_FIELDS = "name,objective,status,daily_budget,bid_strategy,adlabels{name}"
ADSET_FIELDS = ("name,status,campaign_id,optimization_goal,billing_event,destination_type,"
                "promoted_object,targeting,start_time,end_time,adlabels{name}")
_PLACEMENT_FIELDS = ("publisher_platforms", "facebook_positions", "instagram_positions", "devices")
#: Не показываем как «отличие от билдера»: имя и метку доказал поиск, остальное — сверка после записи.
_SKIP_DIFF = {"name", "tag", "campaign_id", "schedule"}
_FIELD_TITLES = {"geo": "countries", "age_min": "minimum age", "age_max": "maximum age",
                 "languages": "languages", "detailed_targeting": "detailed targeting",
                 "os": "operating system", "pixel": "pixel", "conversion_event": "conversion event",
                 "optimization_goal": "optimization goal", "billing_event": "billing event",
                 "destination_type": "destination", "daily_budget": "daily budget",
                 "promoted_page": "Page of the ad set",
                 "bid_strategy": "bid strategy"}


def graph_problem(exc: BaseException) -> dict:
    """Отказ Meta записью списка проблем дашборда: каталог ошибок (ACL-1786) или сырой текст."""
    x = _catalog.explain(getattr(exc, "code", None), getattr(exc, "subcode", None), str(exc))
    code = f"{x.code}/{x.subcode}" if x.subcode else str(x.code or "")
    raw = str(exc)
    if not x.known:
        return {"title": raw or "Meta refused the request (no details given).", "impact": "", "action": "",
                "severity": "blocking", "step": "graph_error", "raw": raw}
    return {"title": x.message_en, "impact": "", "action": x.action_en, "severity": "blocking",
            "step": "graph_error", "raw": (f"code {code}: " if code else "") + raw}


def _show(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(map(str, value)) if value else "none"
    return "none" if value in (None, "") else str(value)


def _describe_diffs(checks: list[dict]) -> list[str]:
    """Расхождения одного объекта человеческим языком (площадки — одной строкой)."""
    bad = [c for c in checks if not c["ok"] and c["field"] not in _SKIP_DIFF]
    out: list[str] = []
    place = [c for c in bad if c["field"] in _PLACEMENT_FIELDS]
    if place:
        manual = any(c["expected"] for c in place if c["field"] == "publisher_platforms")
        auto = not any(c["actual"] for c in place if c["field"] != "devices")
        if manual and auto:
            out.append("placements (Advantage+ vs manual list)")
        else:
            out.append("placements (" + "; ".join(
                f"{c['field'].replace('_', ' ')}: builder {_show(c['expected'])}, Meta {_show(c['actual'])}"
                for c in place) + ")")
    for c in bad:
        if c["field"] in _PLACEMENT_FIELDS:
            continue
        out.append(f"{_FIELD_TITLES.get(c['field'], c['field'])} "
                   f"(builder: {_show(c['expected'])}, Meta: {_show(c['actual'])})")
    return out


def _existing_message(found: list[dict]) -> str:
    parts = [f"Existing {f['object']} from an earlier run differs from the builder: "
             + "; ".join(f["differences"]) + "." for f in found]
    return (" ".join(parts) + " Nothing was written and nothing in Meta was changed. Delete or rename "
            "the old campaign in Ads Manager, or rename the campaign in the builder.")


def _flex_ids(spec: Any) -> list[list[str]]:
    out = []
    for group in spec or []:
        if not isinstance(group, dict):
            continue
        ids = sorted(str(i.get("id")) for bucket in group.values() if isinstance(bucket, list)
                     for i in bucket if isinstance(i, dict))
        out.append(ids)
    return sorted(out)


def _not_read(expected: Any, actual: Any) -> bool:
    """Meta поле не вернула (None), хотя от него что-то ждали → в отчёте «Not read»."""
    return actual is None and expected not in (None, "", [])


def _same(expected: Any, actual: Any) -> bool:
    if isinstance(expected, list) and isinstance(actual, list):
        try:
            return sorted(map(json.dumps, expected)) == sorted(map(json.dumps, actual))
        except TypeError:
            return expected == actual
    if isinstance(expected, int) and not isinstance(expected, bool):
        try:
            return int(actual) == expected
        except (TypeError, ValueError):
            return False
    return expected == actual
