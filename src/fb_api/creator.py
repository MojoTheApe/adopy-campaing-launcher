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
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable

from src.fb_api import form_create as _fc
from src.fb_api import form_templates as _ft
from src.fb_api import forms as _forms
from src.fb_api import targeting_resolver as _tr
from src.fb_api.graph import GraphClient, GraphError, GraphStop, WritesBlocked, WriteUnknown, normalize_act

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
#: Сверка после неизвестной загрузки ФАЙЛА (ACL-1899): видео могло долететь позже.
#: Паузы перед каждым чтением = RECONCILE_WAIT_S × эти множители (5, 10, 20, 30, 30, 30 с ≈ 2 мин);
#: каждое чтение идёт через клиента и считается в потолке вызовов кампании.
UPLOAD_RECONCILE_FACTORS = (1, 2, 4, 6, 6, 6)
#: Не чаще раза в секунду показываем процент загрузки файла.
UPLOAD_PROGRESS_EVERY_S = 1.0
#: Страниц списка в ПОВТОРНЫХ чтениях сверки загрузки (поиск до загрузки и первое чтение - полные).
UPLOAD_RECONCILE_PAGES = 3
#: Сколько страниц (по 100) свежих креативов кабинета смотреть в поиске своего.
CREATIVE_LOOKUP_PAGES = 3
#: Цель кампании из билдера, которую умеет API-путь (как у RPA: Leads → сайт).
SUPPORTED_OBJECTIVES = frozenset({"leads"})

#: ACL-1902: имя кампании занято - пробуем NAME, NAME_2 ... NAME_20 (всего столько кандидатов).
NAME_SUFFIX_MAX = 20
#: Столько строк даёт поиск кампаний по подстроке (2 страницы по 100): при полной выдаче
#: суффиксов за её пределами могло не быть видно - тогда ищем каждое имя отдельно.
CAMPAIGN_SEARCH_ROWS = 200
#: Не больше стольких своих кампаний с похожими именами сверяем с билдером (потолок чтений).
NAME_SCAN_MAX_OURS = 6
#: Консервативная защита от слишком длинного имени (точный предел Meta не проверен).
CAMPAIGN_NAME_MAX = 400

#: Что таблица показывает (архив и удалённое не считаем «существующим»).
LIVE_STATES = ["ACTIVE", "PAUSED", "PENDING_REVIEW", "DISAPPROVED", "PREAPPROVED",
               "PENDING_BILLING_INFO", "CAMPAIGN_PAUSED", "ADSET_PAUSED", "IN_PROCESS",
               "WITH_ISSUES"]


#: Id-пустышка формы для проверки политики и показа плана (формы ещё нет; 5-25 цифр).
FORM_ID_PLACEHOLDER = "0000000"


def _wp_form_edge() -> str:
    from src.fb_api import write_policy as _wp

    return _wp.FORM_EDGE


class PlanError(ValueError):
    """Черновик нельзя создать через API; к Meta ничего не писали."""


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
    #: Шаблон формы (ACL-1776): форму создаёт робот при публикации; `form_id` заполняется тогда же.
    form_template: dict | None = None


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
    """«a, b [AND] c» → [["a","b"],["c"]] — то же деление, что у RPA.

    ACL-1933: « or » и « and » внутри группы = запятая («A or B» → два интереса, без
    подтверждения разбиения). Разделитель групп [AND] в скобках не затрагивается.
    """
    import re

    groups = []
    for chunk in re.split(r"\[AND\]|\|", raw or "", flags=re.IGNORECASE):
        chunk = re.sub(r"\s+(?:or|and)\s+", ",", chunk, flags=re.IGNORECASE)
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


def plan_from_draft(draft: Any, template_loader: Callable[[str], dict | None] | None = None) -> CampaignPlan:
    """`CampaignDraft` (после `enrich_campaign_drafts`) → план. Ошибки — `PlanError`.

    `template_loader` - поиск шаблона формы по id (ACL-1776); по умолчанию - локальная библиотека.
    """
    load_template = template_loader or _ft.get
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
            template_id = (str(getattr(ad, "lead_form_template", "") or "").strip() if plan_set.instant else "")
            if template_id and plan_set.ads[-1].form_id:
                problems.append(f"Ad {i}: choose either an existing form or a template, not both")
            elif template_id:
                tpl = load_template(template_id)
                if tpl is None:
                    problems.append(f"Ad {i}: the form template was not found in the Forms library")
                else:
                    clean, errs = _ft.validate(tpl)
                    if errs:
                        problems.append(f"Ad {i}: form template {clean.get('name') or template_id!r} is not valid: "
                                        + "; ".join(errs))
                    else:
                        plan_set.ads[-1].form_template = clean
            elif plan_set.instant and not plan_set.ads[-1].form_id:
                problems.append(f"Ad {i}: no Instant Form selected (pick one of the Page's forms "
                                "or a template)")
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


class TargetingUnresolved(PlanError):
    """Термин библиотеки не найден точно или ждёт подтверждения человека (ACL-1771).

    `items` — непонятые термины/части: {term, state: pending|unresolved, candidates,
    suggested?, reason}. Текст начинается так же, как раньше («Detailed targeting not found…»).
    """

    def __init__(self, items: list[dict]):
        self.items = items
        unknown = [i for i in items if i.get("state") == "unknown"]
        lost = [i for i in items if i.get("state") not in ("pending", "unknown")]
        asks = [i for i in items if i.get("state") == "pending"]

        def cands(i: dict) -> str:
            rows = i.get("candidates") or []
            return ("; candidates: " + ", ".join(f"{c['name']} [{c['type']}]" for c in rows[:5])
                    if rows else "; no candidates")

        msg: list[str] = []
        if lost:
            msg.append("Detailed targeting not found by Meta: "
                       + " | ".join(f"{i['term']}{cands(i)}" + (f" ({i['reason']})" if i.get("reason") else "")
                                    for i in lost))
        if asks:
            msg.append("Detailed targeting needs your confirmation: "
                       + " | ".join(f"picked '{i['suggested']['name']}' [{i['suggested']['type']}] for '{i['term']}'?"
                                    for i in asks) + " Confirm a candidate in Check plan.")
        if unknown:
            # Причина из резолвера: «Meta отказала в поиске X» или «нажмите Check plan ещё раз».
            msg.extend(dict.fromkeys(i.get("reason") or _tr.UNKNOWN_MESSAGE for i in unknown))
        super().__init__(". ".join(msg))


class TargetingResolver:
    """Гео, языки, интересы → id Meta. Кэш на задание; только чтения."""

    def __init__(self, client: GraphClient, store: _tr.ConfirmedStore | None = None):
        self.client = client
        self._geo: dict[str, str] = {}
        self._locale: dict[str, int] = {}
        #: Детальный таргетинг (ACL-1771): свой поиск, кэш подтверждённых и бюджет вызовов.
        self.terms = _tr.TermResolver(client, store)

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
        """Группы имён → `flexible_spec` (interests/behaviors/work_*/education_* с id).

        Точное имя берётся само; всё остальное — `TargetingUnresolved` с кандидатами и
        с «picked X for Y?» (ACL-1771): ничего не подставляется молча.
        """
        terms = [n for g in groups for n in g]
        self.terms.prefetch(terms)
        results = {n: self.terms.resolve(n) for n in terms}
        bad: list[dict] = []
        for r in results.values():
            if r["state"] in ("exact", "confirmed", "split"):
                continue
            bad += r.get("bad_parts") or [r]
        if bad:
            raise TargetingUnresolved(bad)
        spec = []
        for g in groups:
            entry: dict[str, list] = {}
            for n in g:
                for hit in results[n]["picks"]:
                    bucket = entry.setdefault(hit["type"], [])
                    item = {"id": hit["id"], "name": hit["name"]}
                    if item not in bucket:
                        bucket.append(item)
            spec.append(entry)
        return spec

    def targeting_report(self) -> list[dict]:
        """Что нашлось/ждёт подтверждения по всем терминам задания (для Check plan)."""
        return self.terms.report()

    def begin_draft(self) -> None:
        self.terms.begin_draft()

    def refusal_warnings(self) -> list[str]:
        """Необязательные поиски, в которых Meta отказала по правам (ACL-1920): предупреждения."""
        return self.terms.refusals()


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
    #: Имя, под которым кампания опубликована (ACL-1902); `campaign_name` остаётся именем из билдера.
    published_as: str = ""
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
    #: ACL-1771: как найдены термины таргетинга и что ждёт подтверждения (для Check plan).
    targeting: list = field(default_factory=list)

    @property
    def mismatches(self) -> list[dict]:
        return list(self.readback.get("mismatches") or [])


@dataclass
class NameChoice:
    """Итог выбора имени кампании (ACL-1902): план с итоговым именем и меткой, до первой записи."""
    plan: CampaignPlan
    #: Своя совпавшая с билдером кампания (возобновление/повтор) - её переиспользуем; иначе None.
    existing: dict | None = None
    found_sets: list = field(default_factory=list)
    #: Пропущенные кандидаты: {"name", "reason": "different"|"foreign", "differences": [...]}.
    skipped: list = field(default_factory=list)

    @property
    def renamed(self) -> bool:
        return bool(self.skipped)


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
                 progress: Callable[[str], None] = lambda _s: None,
                 live_progress: Callable[[str], None] = lambda _s: None):
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
        #: Шаблоны форм плана -> что с ними будет (ACL-1776); читается в `_check_forms`.
        self._form_steps: dict[str, _fc.FormStep] = {}
        self._form_warnings: list[str] = []
        self.progress = progress
        #: Живой процент загрузки файла (ACL-1899): только на панель задания, без шагов и телеметрии.
        self.live_progress = live_progress

    # -- служебное
    def _step(self, out: Outcome, text: str) -> None:
        out.steps.append(text)
        self.progress(text)

    def _check_cancel(self, out: Outcome) -> None:
        if self.cancelled():
            raise CreationStopped("Stopped by the user", created=out.created, kind="cancelled")

    def _upload_progress(self, ad_index: int, kind: str, size: int) -> Callable[[float], None]:
        """Показ «ad 1: uploading video (180 MB), 1m 20s» - не чаще раза в секунду.

        Байтового прогресса нет (слушатель `xhr.upload` включает CORS-preflight): размер
        известен из складирования, время - от старта загрузки. Процент придёт с кусками (ACL-891).
        """
        last = [None]

        def show(elapsed_s: float) -> None:
            now = self.clock()
            if last[0] is not None and now - last[0] < UPLOAD_PROGRESS_EVERY_S:
                return
            last[0] = now
            secs = int(max(0, elapsed_s))
            if secs >= 3600:
                span = f"{secs // 3600}h {secs % 3600 // 60:02d}m"
            elif secs >= 60:
                span = f"{secs // 60}m {secs % 60:02d}s"
            else:
                span = f"{secs}s"
            self.live_progress(f"ad {ad_index}: uploading {kind} ({max(1, size // (1024 * 1024))} MB), {span}")
        return show

    def _write(self, out: Outcome, what: str, path: str, params: dict, find: Callable[[], str | None],
               *, files: dict | None = None, filename: str = "", id_of=lambda b: b.get("id"),
               on_ad: bool = False, form_page: str = "",
               upload: tuple[int, str] | None = None) -> str:
        """Одна запись; неизвестный результат — сверка `find()` чтением.

        `upload` = (номер объявления, «video»/«image») - запись с файлом: без срока,
        с показом процента и Stop; неизвестный результат сверяется несколько раз.
        """
        self._check_cancel(out)
        try:
            # `on_ad` — только креатив Instant Form при наборе ON_AD (ACL-1775);
            # `form_page` — создание формы на Странице плана, своя цепочка политики (ACL-1776).
            extra: dict[str, Any] = {}
            if on_ad:
                extra["on_ad"] = True
            if form_page:
                extra["form_page"] = form_page
            if upload and files:
                size = len(next(iter(files.values())))
                extra["progress"] = self._upload_progress(upload[0], upload[1], size)
                extra["cancelled"] = self.cancelled
            body = self.client.post(path, params, files=files, filename=filename, **extra)
        except WriteUnknown as exc:
            # `WritesBlocked` сюда не попадает: та запись не уходила (ревью M4).
            stopped = bool(upload) and self.cancelled()
            self._step(out, f"{what}: result unknown — reconciling by readback")
            # Файл мог долететь позже: несколько чтений с растущими паузами (≈ 2 мин).
            # После Stop загрузка оборвана — одно чтение без ожидания.
            if upload and stopped:
                # Тело могло уйти целиком, а Meta ещё обрабатывает: два чтения (5 и 20 с).
                waits = [self.reconcile_wait_s, self.reconcile_wait_s * 4]
            elif upload:
                waits = [self.reconcile_wait_s * f for f in UPLOAD_RECONCILE_FACTORS]
            else:
                waits = [self.reconcile_wait_s]
            found = None
            try:
                for n, wait in enumerate(waits):
                    # Stop посреди сверки: останавливаемся; метка `unreconciled` остаётся.
                    if upload and not stopped and self.cancelled():
                        raise CreationStopped(
                            f"Stopped by the user while checking the {what} result. Meta may still be "
                            "processing the upload: wait a few minutes before publishing again.",
                            created=out.created, kind="cancelled")
                    if wait > 0:
                        self.sleep(wait)
                    if upload and not stopped and self.cancelled():
                        raise CreationStopped(
                            f"Stopped by the user while checking the {what} result. Meta may still be "
                            "processing the upload: wait a few minutes before publishing again.",
                            created=out.created, kind="cancelled")
                    # Повторные чтения дешевле: свежий файл в первых страницах списка.
                    found = find(pages=UPLOAD_RECONCILE_PAGES) if (upload and n > 0) else find()
                    if found:
                        break
            except (GraphStop, GraphError) as rexc:
                raise CreationStopped(
                    f"{what}: the write result is unknown ({exc}) and the readback failed ({rexc}). "
                    "Nothing else was written; check Ads Manager, then publish again "
                    "(a re-run does not duplicate).", created=out.created, kind="unknown") from rexc
            if not found:
                if stopped:
                    raise CreationStopped("Stopped by the user during the upload; nothing was found "
                                          "in the ad account yet. Meta may still be processing the "
                                          "upload: wait a few minutes before publishing again.",
                                          created=out.created, kind="cancelled") from exc
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

    def _campaign_rows(self, act: str, name: str) -> list[dict]:
        """Живые кампании кабинета, чьё имя содержит `name` (одно чтение, максимум 2 страницы)."""
        return self.client.get_all(f"/{act}/campaigns", {
            "fields": "id,name,status,adlabels{name}",
            "filtering": [{"field": "name", "operator": "CONTAIN", "value": name}],
            "effective_status": LIVE_STATES, "limit": 100}, max_pages=2)

    def _name_rows(self, act: str, name: str, base_rows: list[dict], truncated: bool) -> list[dict]:
        """Строки с этим именем: из общего поиска, а если он мог обрезаться - точным запросом по имени.

        Точный запрос (EQUAL) тоже полный на 200 строках - тогда доверять нечему, стоп до записей.
        """
        if not truncated:
            return base_rows
        rows = self.client.get_all(f"/{act}/campaigns", {
            "fields": "id,name,status,adlabels{name}",
            "filtering": [{"field": "name", "operator": "EQUAL", "value": name}],
            "effective_status": LIVE_STATES, "limit": 100}, max_pages=2)
        if len(rows) >= CAMPAIGN_SEARCH_ROWS:
            raise CreationStopped(
                f"Cannot tell whether the campaign name {name!r} is free: the ad account has too many "
                "campaigns with that name. Nothing was created; rename the campaign in the builder.",
                kind="name_unsure")
        return rows

    def _ad_names_fit(self, cand: CampaignPlan, campaign_id: str) -> bool:
        """Для NAME_2...: кампания с чужими объявлениями (построена билдером с таким именем) - не наша."""
        have = {str(r.get("name") or "").strip() for r in self._children(campaign_id, "ads", "id,name")}
        want = {ad.name for s in cand.ad_sets for ad in s.ads}
        return have <= want

    def _resolve_name(self, plan: CampaignPlan, targetings: list[dict]) -> NameChoice:
        """ACL-1902: имя кампании до любой записи - и в боевом запуске, и в Check plan.

        Кандидаты NAME, NAME_2 ... NAME_20 (суффикс всегда к полному имени, чужие цифры не разбираем):
        нет живой кампании с таким именем - берём; наша (метка кандидата) и без расхождений с билдером -
        переиспользуем (возобновление не плодит дубль); наша, но другая, или чужая (без метки) - дальше.
        Метка считается от имени кандидата, поэтому у NAME_2 своя метка и свои креативы.
        Только чтения: один поиск по подстроке на все кандидаты + сверка найденных своих.
        """
        rows = self._campaign_rows(plan.act, plan.name)
        truncated = len(rows) >= CAMPAIGN_SEARCH_ROWS
        skipped: list[dict] = []
        scans = 0
        for n in range(1, NAME_SUFFIX_MAX + 1):
            name = plan.name if n == 1 else f"{plan.name}_{n}"
            if len(name) > CAMPAIGN_NAME_MAX:
                raise CreationStopped(
                    f"The campaign name {name!r} would be longer than the Meta limit of "
                    f"{CAMPAIGN_NAME_MAX} characters. Nothing was created; shorten the campaign name "
                    "in the builder.", kind="name_too_long")
            cand = plan if n == 1 else replace(plan, name=name, tag=campaign_tag(plan.act, name))
            hits = self._name_rows(plan.act, name, rows, truncated)
            same = [r for r in hits if str(r.get("name") or "").strip() == name]
            if not same:
                return NameChoice(cand, None, [None] * len(plan.ad_sets), skipped)
            ours = [r for r in same if cand.label in _labels(r)]
            if not ours:
                skipped.append({"name": name, "reason": "foreign", "differences": []})
                continue
            scans += 1
            if scans > NAME_SCAN_MAX_OURS:
                raise CreationStopped(
                    f"Too many earlier campaigns named like {plan.name!r} to check them all within the "
                    "call budget. Nothing was created; rename the campaign in the builder or remove "
                    "old ones in Ads Manager.", kind="name_scan_limit")
            campaign_id = str(ours[0]["id"])
            found_sets = self._find_sets(cand, campaign_id)
            bad = self._existing_differences(cand, targetings, campaign_id, found_sets)
            if not bad and n > 1 and not self._ad_names_fit(cand, campaign_id):
                skipped.append({"name": name, "reason": "other_ads", "differences": []})
                continue
            if not bad:
                return NameChoice(cand, ours[0], found_sets, skipped)
            skipped.append({"name": name, "reason": "different", "differences": bad})
        raise CreationStopped(
            f"All {NAME_SUFFIX_MAX} campaign names are taken: {plan.name!r} and {plan.name}_2 ... "
            f"{plan.name}_{NAME_SUFFIX_MAX} already exist on the ad account (not ours, or ours with different "
            "settings). Nothing was created; rename the campaign in the builder or remove the old ones "
            "in Ads Manager.", kind="name_taken")

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

        def find(pages: int = 1) -> str | None:
            rows = self.client.get_all(f"/{plan.act}/adimages", {"hashes": [md5], "fields": "hash"},
                                       max_pages=min(1, pages))
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
                          files={"filename": data}, filename=image_name(ad.path, data), id_of=hash_of,
                          upload=(ad.index, "image"))
        out.created.setdefault("images", []).append(got)
        self._step(out, f"ad {ad.index}: image uploaded")
        return got

    def _video(self, out: Outcome, plan: CampaignPlan, ad: AdPlan, data: bytes) -> tuple[str, str]:
        title = video_title(plan.tag, data)

        def find(pages: int = 10) -> str | None:
            rows = self.client.get_all(f"/{plan.act}/advideos", {"fields": "id,title", "limit": 100},
                                       max_pages=pages)
            hit = next((r for r in rows if str(r.get("title") or "") == title), None)
            return str(hit["id"]) if hit else None

        vid = find()
        if vid:
            self._step(out, f"ad {ad.index}: video already in the ad account")
        else:
            vid = self._write(out, f"ad {ad.index}: video upload", f"/{plan.act}/advideos",
                              {"name": title, "title": title}, find, files={"source": data},
                              filename=Path(ad.path).name, upload=(ad.index, "video"))
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
        getattr(self.resolver, "begin_draft", lambda: None)()   # предупреждения об отказах - по черновику
        out = Outcome(campaign_name=plan.name, warnings=list(plan.warnings))
        try:
            self._run(plan, out)
        except CreationStopped as exc:
            out.error, out.stop_kind = str(exc), exc.kind
        except PlanError as exc:
            out.error, out.stop_kind = str(exc) + " Nothing more was created.", "plan"
        except WritesBlocked as exc:
            out.error, out.stop_kind = f"Not written: {exc}", "blocked"
        except GraphStop as exc:
            out.error, out.stop_kind = f"Graph API stopped: {exc}", exc.kind
        except GraphError as exc:
            out.error, out.stop_kind = f"Meta refused: {exc}", "graph_error"
        except WriteUnknown as exc:
            out.error, out.stop_kind = f"Write result unknown: {exc}", "unknown"
        return out

    def _add_refusal_warnings(self, out: Outcome) -> None:
        fn = getattr(self.resolver, "refusal_warnings", None)
        for w in (fn() if fn else []):
            if w not in out.warnings:
                out.warnings.append(w)

    def _prepare(self, plan: CampaignPlan, out: Outcome) -> tuple[list[dict], dict[str, bytes]]:
        """Всё, что проверяется до записи: страница, пиксель, таргетинг, файлы."""
        out.warnings += resolve_page_pixel(plan, self.facts)
        try:
            targetings = [targeting_for(plan, s, self.resolver) for s in plan.ad_sets]
        finally:
            self._add_refusal_warnings(out)     # и при ошибке плана: человек видит, что Meta отказала
        self._step(out, "preflight: targeting resolved")
        self._page_forms = None
        self._form_steps = {}
        self._form_warnings = []
        if any(s.instant for s in plan.ad_sets):
            self._check_forms(plan)
            out.warnings += self._form_warnings
            self._step(out, "preflight: Instant Forms belong to the Page")
            for step in self._form_steps.values():
                self._step(out, f"preflight: form will be created: {step.form_name}" if step.action == "create"
                           else f"preflight: form found on the Page, will be reused: {step.form_name}")
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
        return targetings, files

    def _check_forms(self, plan: CampaignPlan) -> None:
        """Формы объявлений — формы ЭТОЙ Страницы (ACL-1775). Только чтение, до любой записи.

        Чужая, архивная или несуществующая форма — `PlanError`, к Meta ничего не ушло.
        """
        read = _forms.read_page_forms(self.client, plan.page_id)
        if not read.ok:
            raise PlanError(read.message + ".")
        known = read.by_id()
        templates: dict[str, dict] = {}
        for ad_set in plan.ad_sets:
            if not ad_set.instant:
                continue
            for ad in ad_set.ads:
                if ad.form_template:
                    templates.setdefault(ad.form_template["id"], ad.form_template)
                    continue
                form = known.get(ad.form_id)
                if form is None:
                    raise PlanError(f"Ad {ad.index}: the Instant Form {ad.form_id} is not a form of this "
                                    "Page; pick one of the Page's forms (Refresh forms).")
                if not form.usable:
                    raise PlanError(f"Ad {ad.index}: the Instant Form {form.name!r} is "
                                    f"{form.status.lower() or 'of unknown status'} (only ACTIVE forms can be "
                                    "used); pick another form.")
        self._form_steps = {}
        for tid, tpl in templates.items():
            existing, why = _fc.match_existing(read, tpl)
            if why:
                raise PlanError(why + ".")
            fname = _ft.form_name(tpl)
            if existing:
                _fc.clear_pending(plan.act, plan.page_id, fname)
            elif _fc.is_pending(plan.act, plan.page_id, fname):
                # Список только что перечитан и формы в нём нет: прошлая запись могла дойти.
                raise PlanError(_fc.MSG_PENDING + ".")
            self._form_steps[tid] = _fc.FormStep(template_id=tid, template_name=tpl["name"],
                                                 form_name=_ft.form_name(tpl), existing_id=existing)
        if any(step.action == "create" for step in self._form_steps.values()):
            # Условия Lead Ads: робот их не принимает никогда; без них - остановка до записи.
            if _forms.page_terms_accepted(self.client, plan.page_id) is False:
                raise PlanError(_fc.MSG_TERMS_STOP + ".")
            self._check_form_permission()
        self._page_forms = read

    def _check_form_permission(self) -> None:
        """Право `pages_manage_ads` у снятого токена: только предупреждение (ACL-1776).

        Токен Ads Manager - не обычное приложение, список прав может не отражать
        реальность, поэтому отсутствие права не блокирует, а предупреждает; окончательный
        ответ даст сама запись (отказ Meta -> понятный текст). Чужие токены не просим.
        """
        try:
            rows = self.client.get_all("/me/permissions", {"limit": 100}, max_pages=1)
        except GraphError:
            return
        granted = {str(r.get("permission")) for r in rows if str(r.get("status")) == "granted"}
        if rows and "pages_manage_ads" not in granted:
            self._form_warnings.append(_fc.MSG_NO_PERMISSION + " (this is a warning: Meta decides when the form is created)")

    @staticmethod
    def _planned(ad: AdPlan, placeholder: str) -> AdPlan:
        """Копия объявления для плана/проверки политики: у шаблонной формы id ещё нет."""
        if ad.form_template and not ad.form_id:
            return replace(ad, form_id=placeholder)
        return ad

    def _find_form_id(self, plan: CampaignPlan, tpl: dict) -> str | None:
        """Сверка чтением списка форм Страницы: id своей формы по точному имени."""
        read = _forms.read_page_forms(self.client, plan.page_id)
        if not read.ok:
            raise GraphError(read.message)
        existing, _why = _fc.match_existing(read, tpl)
        return existing or None

    def _verify_form(self, out: Outcome, plan: CampaignPlan, tpl: dict, form_id: str) -> None:
        """Форма ACTIVE, на нашей Странице, вопросы как в шаблоне; иначе стоп."""
        row = self.client.get(f"/{form_id}", {"fields": _fc.READBACK_FIELDS})
        bad = _fc.compare_form(tpl, plan.page_id, row)
        if bad:
            raise CreationStopped(f"The form {_ft.form_name(tpl)!r} was read back with differences ("
                                  + "; ".join(bad) + "). Nothing that depends on it was created.",
                                  created=out.created, kind="form_mismatch")

    def _ensure_form(self, out: Outcome, plan: CampaignPlan, ad: AdPlan) -> None:
        """Форма шаблона объявления: найти свою или создать ОДИН раз; `ad.form_id` - её id."""
        tpl = ad.form_template
        if not tpl or ad.form_id:
            return
        step = self._form_steps.get(tpl["id"])
        if step is None:
            raise PlanError("the form template was not checked before publishing; run Check plan")
        if not step.form_id:
            if step.existing_id:
                step.form_id = step.existing_id
                out.reused.setdefault("forms", []).append(step.form_id)
                self._step(out, f"form {step.form_name!r}: found on the Page, reused")
            else:
                try:
                    # Метка ДО записи: без неё повторный запуск может создать вторую форму.
                    _fc.record_pending(plan.act, plan.page_id, step.form_name)
                except OSError as exc:
                    raise CreationStopped(_fc.MSG_NO_STATE, created=out.created,
                                          kind="form_state") from exc
                try:
                    step.form_id = self._write(
                        out, f"form {step.form_name!r}", f"/{plan.page_id}/{_wp_form_edge()}",
                        _ft.form_params(tpl), lambda: self._find_form_id(plan, tpl),
                        form_page=plan.page_id)
                except GraphError as exc:
                    _fc.clear_pending(plan.act, plan.page_id, step.form_name)   # Meta отказала: формы нет
                    raise CreationStopped(_fc.refused_message(exc), created=out.created,
                                          kind="form_refused") from exc
                _fc.clear_pending(plan.act, plan.page_id, step.form_name)
                out.created.setdefault("forms", []).append(step.form_id)
                self._step(out, f"form {step.form_name!r}: created")
            self._verify_form(out, plan, tpl, step.form_id)
            self._step(out, f"form {step.form_name!r}: read back, ACTIVE, on the Page, questions match")
        ad.form_id = step.form_id

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
        done_forms: set[str] = set()
        for ad_set in plan.ad_sets:
            for ad in ad_set.ads:
                tpl = ad.form_template
                if tpl and tpl["id"] not in done_forms:
                    done_forms.add(tpl["id"])
                    try:
                        _wp.check_form_create(plan.page_id, "POST", f"/{plan.page_id}/{_wp.FORM_EDGE}",
                                              _ft.form_params(tpl), None, plan.page_id)
                    except _wp.WriteRefused as exc:
                        raise PlanError(f"form {tpl['name']!r}: {exc}") from exc
        for ad_set, targeting in zip(plan.ad_sets, targetings):
            ok("adsets", self.adset_params(plan, ad_set, "0", targeting), f"ad set {ad_set.name!r}")
            for ad in ad_set.ads:
                ad = self._planned(ad, FORM_ID_PLACEHOLDER)
                media = ({"video_id": "0", "thumb": "https://example.invalid/t.jpg"} if ad.kind == "video"
                         else {"image_hash": "0"})
                ok("adcreatives", {"name": f"{ad.name} #{ad.index} [{plan.label}]",
                                   "object_story_spec": self._story(plan, ad, ad_set, media),
                                   "degrees_of_freedom_spec": creative_features(),
                                   "adlabels": [{"name": plan.label}]}, f"ad {ad.index} creative",
                   on_ad=ad_set.instant)
                ok("ads", {"name": ad.name, "adset_id": "0", "creative": {"creative_id": "0"},
                           "status": PAUSED, "adlabels": [{"name": plan.label}]}, f"ad {ad.index}")

    def plan_only(self, plan: CampaignPlan) -> Outcome:
        """Пробный запуск (ревью M3): предпроверка и план, НИ ОДНОЙ записи.

        Клиент пробного запуска создаётся без права записи вовсе; здесь же
        `post` не зовётся ни разу.
        """
        self._creatives = None
        getattr(self.resolver, "begin_draft", lambda: None)()   # предупреждения об отказах - по черновику
        out = Outcome(campaign_name=plan.name, warnings=list(plan.warnings))
        differences: list | None = None
        files: dict | None = None
        renamed = name_error = ""
        requested, skipped = plan.name, []
        try:
            targetings, files = self._prepare(plan, out)
            choice = self._resolve_name(plan, targetings)
            plan, existing, skipped = choice.plan, choice.existing, choice.skipped
            out.published_as = plan.name
            self._check_planned(plan, targetings)
            differences = []
            if choice.renamed:
                renamed = _rename_message(requested, plan.name, skipped, future=True)
                out.warnings.append(renamed)
            sets = []
            for ad_set, targeting in zip(plan.ad_sets, targetings):
                ads = []
                for ad in ad_set.ads:
                    data = files[ad.path]
                    media = ({"video_id": "<uploaded video>", "thumb": "<video thumbnail>"} if ad.kind == "video"
                             else {"image_hash": "<uploaded image>"})
                    ad = self._planned(ad, "<form to be created>")
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
                                                     existing_differences=differences,
                                                     requested_name=requested if skipped else "")}
            self._step(out, "dry run: preflight passed, nothing was written")
        except CreationStopped as exc:
            out.error, out.stop_kind = str(exc), exc.kind
            if exc.kind == "name_taken":
                name_error = str(exc)
        except PlanError as exc:
            out.error, out.stop_kind = str(exc), "plan"
        except GraphStop as exc:
            out.error, out.stop_kind = f"Graph API stopped: {exc}", exc.kind
        except GraphError as exc:
            out.error, out.stop_kind = f"Meta refused: {exc}", "graph_error"
        out.targeting = self._targeting_report()
        self._add_refusal_warnings(out)
        self._audit(plan, out, existing=differences, checked=differences is not None, files=files,
                    renamed=renamed, name_error=name_error)
        return out

    def _targeting_report(self) -> list:
        fn = getattr(self.resolver, "targeting_report", None)
        try:
            items = list(fn()) if fn else []
            _tr.register_candidates(items)      # подтвердить можно только показанных кандидатов
            return items
        except Exception:  # noqa: BLE001 — отчёт вспомогательный
            return []

    def _audit(self, plan: CampaignPlan, out: Outcome, *, existing: list | None, checked: bool,
               files: dict | None, renamed: str = "", name_error: str = "") -> None:
        """Pre-flight audit (ACL-1819): независимые вердикты серверных правил, только чтения.

        Не идёт после остановки Graph (лимит/блокировка): новых чтений там быть не должно.
        Сбой самого аудита не ломает план: вердиктов просто нет.
        """
        if out.stop_kind not in ("", "plan", "graph_error", "name_taken"):
            return
        from src.fb_api import preflight_rules as _pf

        try:
            out.audit = _pf.server_verdicts(self, plan, existing=existing, existing_checked=checked, files=files,
                                       renamed=renamed, name_error=name_error)
        except Exception:  # noqa: BLE001 — аудит вспомогательный, план от него не зависит
            import logging

            logging.getLogger(__name__).exception("pre-flight audit failed for %r", plan.name)
            out.audit = []

    def plan_summary(self, plan: CampaignPlan, targetings: list[dict], files: dict[str, bytes], *,
                     existing: bool = False, existing_differences: list[dict] | None = None,
                     requested_name: str = "") -> dict:
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
                    if ad.form_template:
                        step = self._form_steps.get(ad.form_template["id"])
                        item["form_template"] = ad.form_template["name"]
                        if step is not None:
                            item["form_name"] = step.form_name
                            item["form_id"] = step.existing_id
                            item["form_action"] = step.action
                            item["form_note"] = (f"form will be created: {step.form_name}" if step.action == "create"
                                                 else f"form found on the Page, reused: {step.form_name}")
        summary = {
            "campaign": plan.name, "status": PAUSED, "objective": OBJECTIVE,
            "daily_budget": plan.daily_budget, "currency": self.facts.currency,
            "budget_minor": budget_minor(plan.daily_budget, self.facts.currency),
            "page_id": plan.page_id, "pixel_id": plan.pixel_id,
            "page_source": plan.page_source, "pixel_source": plan.pixel_source,
            "existing": existing, "existing_differences": list(existing_differences or []),
            "tag": plan.label, "ad_sets": sets,
        }
        if requested_name:
            # ACL-1902: имя из билдера занято - публикуем под выбранным (`campaign`), прежнее здесь.
            summary["requested_name"] = requested_name
        if self._form_steps:
            # Только у планов с шаблонами форм: сайтовый план не меняется ни байтом (ACL-1775).
            summary["forms"] = [st.as_dict() for st in self._form_steps.values()]
        return summary

    def _run(self, plan: CampaignPlan, out: Outcome) -> None:
        # 1. Всё, что можно проверить до записи.
        targetings, files = self._prepare(plan, out)

        # 2. Имя (ACL-1902: NAME, NAME_2 ...) и кампания (наша - по имени и метке).
        #    Найденные свои кампания/группы уже сверены с билдером внутри выбора имени - до записей.
        choice = self._resolve_name(plan, targetings)
        requested, plan = plan.name, choice.plan
        out.published_as = plan.name
        self._check_planned(plan, targetings)
        if choice.renamed:
            out.warnings.append(_rename_message(requested, plan.name, choice.skipped, future=False))
            self._step(out, f"campaign name {requested!r} is taken: using {plan.name!r}")
        existing, found_sets = choice.existing, choice.found_sets
        if existing:
            out.campaign_id = str(existing["id"])
            out.reused["campaign"] = out.campaign_id
            self._step(out, "campaign: found from a previous run")
            self._step(out, "existing campaign and ad sets match the builder")
        else:
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
                    # ACL-1776: форма из шаблона - ровно перед креативом, которому она нужна.
                    self._ensure_form(out, plan, ad)
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
                if ad.form_template and not ad.form_id:
                    # Объявление найдено с прошлого запуска: форма шаблона - та, что нашли по имени.
                    step = self._form_steps.get(ad.form_template["id"])
                    ad.form_id = (step.form_id or step.existing_id) if step else ""
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


def _rename_message(requested: str, chosen: str, skipped: list[dict], *, future: bool) -> str:
    """Почему кампания пойдёт под другим именем (ACL-1902); одна строка для плана, итога и аудита."""
    why = []
    for item in skipped:
        if item["reason"] == "foreign":
            why.append(f"{item['name']!r} is already used by another campaign")
        elif item["reason"] == "other_ads":
            why.append(f"{item['name']!r} is a campaign with other ads")
        else:
            parts = [f"{f['object']}: " + "; ".join(f["differences"]) for f in item["differences"]]
            why.append(f"{item['name']!r} (earlier campaign) is left as is; it already exists with different "
                       "settings (" + " | ".join(parts) + ")")
    head = "Will be published as" if future else "Published as"
    return f"{head} {chosen!r}: " + "; ".join(why) + "."


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
