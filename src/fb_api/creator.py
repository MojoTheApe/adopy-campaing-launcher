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
3. Кампания (PAUSED) → группа(ы) → креатив → объявление (ACTIVE под кампанией на паузе, ACL-2114).
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
import re
import time
import uuid
from datetime import datetime, timezone
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from src.fb_api import error_catalog as _catalog
from src.fb_api import form_create as _fc
from src.fb_api import form_locale as _fl
from src.fb_api import form_templates as _ft
from src.fb_api import forms as _forms
from src.fb_api import targeting_resolver as _tr
from src.fb_api import instagram as _ig
from src.fb_api import write_policy as _wpol

from src.fb_api import pages as _pg
from src.fb_api.graph import GraphClient, GraphError, GraphStop, WritesBlocked, WriteUnknown, normalize_act

log = logging.getLogger(__name__)

PAUSED = "PAUSED"
#: ACL-2114: наборы и объявления создаются ACTIVE под кампанией на паузе - показа нет, пока человек
#: не включит кампанию. Только после чтения, подтвердившего PAUSED у кампании (`_confirm_campaign_paused`).
ACTIVE = "ACTIVE"
CHILD_STATUS = ACTIVE
#: Что человек читает в плане, в «Check plan» и в итоге (ACL-2114).
STATUS_NOTE = "Campaign PAUSED — ad sets and ads are active and start when you turn the campaign on"
OBJECTIVE = "OUTCOME_LEADS"
OPTIMIZATION_GOAL = "OFFSITE_CONVERSIONS"
BILLING_EVENT = "IMPRESSIONS"
DESTINATION = "WEBSITE"
BID_STRATEGY = "LOWEST_COST_WITHOUT_CAP"
CUSTOM_EVENT = "LEAD"
CTA_TYPE = "LEARN_MORE"
#: ACL-1767: допустимые кнопки (единый список - в политике записи) и значения новых полей.
CTA_WEBSITE = _wpol.CTA_WEBSITE
CTA_FORM = _wpol.CTA_FORM
GENDER_IDS = {"men": 1, "women": 2}
GENDER_LABELS = {"men": "Men", "women": "Women"}
DEVICE_CHOICES = ("mobile", "desktop")
OS_CHOICES = ("all", "ios", "android")
_OS_MIN_RE = re.compile(r"^[0-9]{1,2}(\.[0-9]{1,2})?$")
_START_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}$")
#: Multi-advertiser ads (ACL-1767): всегда явно выключены.
MULTI_ADS_OPT_OUT = {"enroll_status": "OPT_OUT"}

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
#: Видео до этого размера уходит одним запросом (как раньше); больше - кусками (ACL-1891).
VIDEO_SINGLE_MAX_BYTES = 200 * 1024 * 1024
#: Потолок видео на пути кусками: в памяти только кусок. Meta сама принимает до 4 ГБ.
VIDEO_MAX_BYTES = _wpol.UPLOAD_FILE_MAX
VIDEO_CHUNK_MAX = _wpol.UPLOAD_CHUNK_MAX
#: Вызовов на загрузку кусками сверх самих кусков: start, finish, запас на сверку.
CALLS_UPLOAD_FIXED = 3

#: Запас вызовов на всё остальное в кампании при проверке бюджета кусков (ACL-1891).
CALLS_RESERVE_OTHER = 40


def _disk_size(path: str) -> int:
    try:
        return Path(path).stat().st_size
    except (OSError, ValueError):
        return 0


def upload_calls(size: int) -> int:
    """Вызовов на загрузку видео кусками (ACL-1891); 0 - файл уходит одним запросом."""
    if size <= VIDEO_SINGLE_MAX_BYTES:
        return 0
    return -(-size // VIDEO_CHUNK_MAX) + CALLS_UPLOAD_FIXED

#: Ожидание обработки видео — с потолком по времени и по числу вызовов.
VIDEO_WAIT_S = 300.0
VIDEO_POLL_S = 15.0
VIDEO_MAX_POLLS = 20

#: ACL-1768: потолок вызовов Graph на одну кампанию растёт с планом. Замерено на FakeMeta
#: (картинки, с предпроверкой): 14 + 5 за набор + 4 за объявление (10 наборов x 3 = 184).
#: Берём с запасом; видео добавляет опросы обработки, Instant Form - чтения форм Страницы.
CALLS_BASE = 20
CALLS_PER_AD_SET = 7
CALLS_PER_AD = 5
CALLS_PER_VIDEO_POLLS = 8       # опросов обработки на одно видео: медленная обработка (6-8 опросов) не должна упираться в бюджет;
                                # больше 250 вызовов на план - отказ заранее («разбейте»), а не остановка на полпути
CALLS_INSTANT_FORM = 10


def estimate_targeting_reads(plan: "CampaignPlan") -> int:
    """Запас на чтения таргетинга (ACL-1771) по ВСЕМ наборам: они идут из того же бюджета, что и записи.

    Как потолок задания у резолвера: база + шаг на каждую различную строку (целую и каждую её часть),
    но не выше `JOB_READ_ABS_CAP`. Без интересов - 0.
    """
    seen: set[str] = set()
    for ad_set in plan.ad_sets:
        for group in ad_set.interest_groups:
            for term in group:
                seen.add(_tr.norm(term))
                seen.update(_tr.norm(p) for p in _tr.split_term(term))
    seen.discard("")
    if not seen:
        return 0
    return min(_tr.JOB_READ_ABS_CAP, _tr.JOB_READ_BASE + _tr.JOB_READ_PER_TERM * len(seen))


def estimate_create_calls(plan: "CampaignPlan") -> int:
    """Сколько вызовов Graph нужно на ЗАПИСИ кампании целиком (первый запуск, с запасом).

    Чтения таргетинга не входят: их запас - `estimate_targeting_reads`, а `_prepare` сверяет остаток
    бюджета именно с этим числом уже после чтений.
    """
    total = CALLS_BASE + CALLS_PER_AD_SET * len(plan.ad_sets)
    for ad_set in plan.ad_sets:
        total += CALLS_PER_AD * len(ad_set.ads)
        total += CALLS_PER_VIDEO_POLLS * sum(1 for a in ad_set.ads if a.kind == "video")
    if any(x.instant for x in plan.ad_sets):
        total += CALLS_INSTANT_FORM
    return total


#: Пауза перед сверкой неизвестной записи: запрос мог ещё лететь (ревью, minor 3).
RECONCILE_WAIT_S = 5.0
#: Сверка после неизвестной загрузки ФАЙЛА (ACL-1899): видео могло долететь позже.
#: Паузы перед каждым чтением = RECONCILE_WAIT_S × эти множители (5, 10, 20, 30, 30, 30 с ≈ 2 мин);
#: каждое чтение идёт через клиента и считается в потолке вызовов кампании.
UPLOAD_RECONCILE_FACTORS = (1, 2, 4, 6, 6, 6)
#: Не чаще раза в секунду показываем процент загрузки файла.
UPLOAD_PROGRESS_EVERY_S = 1.0
#: Как часто (с) загрузка кусками пишет шаг «uploaded X/Y MB», чтобы панель не краснила её как зависшую (ACL-1998).
UPLOAD_STEP_EVERY_S = 12.0
#: Страниц списка в ПОВТОРНЫХ чтениях сверки загрузки (поиск до загрузки и первое чтение - полные).
UPLOAD_RECONCILE_PAGES = 3
#: Сколько страниц (по 100) свежих креативов кабинета смотреть в поиске своего.
CREATIVE_LOOKUP_PAGES = 3
#: Тексты и вертикальный файл (ACL-1774).
MAX_TEXTS = _wpol.ASSET_FEED_MAX_TEXTS
LABEL_MAIN = "acl_main"
LABEL_VERTICAL = "acl_vertical"
#: Соотношение сторон w/h: основной файл — 1:1 или 4:5, вертикальный — 9:16 (допуск 1,5 %).
MAIN_RATIOS = (1.0, 0.8)
VERTICAL_RATIO = 9 / 16
RATIO_TOLERANCE = 0.015
#: Площадки вертикального файла (Stories и Reels); остальные из ручного списка — основному файлу.
VERTICAL_FACEBOOK = ("story", "facebook_reels")
VERTICAL_INSTAGRAM = ("story", "reels")
#: Значения-заглушки для проверки политикой до записи (настоящие приходят после загрузки).
PLACEHOLDER_HASH = "0" * 32
PLACEHOLDER_VIDEO_ID = "100000"
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


from src.fb_ads_rpa.draft import MAX_AD_SETS, MAX_AD_SETS_ERROR  # noqa: E402

#: Id-пустышка формы для проверки политики и показа плана (формы ещё нет; 5-25 цифр).
FORM_ID_PLACEHOLDER = "0000000"


def _wp_form_edge() -> str:
    from src.fb_api import write_policy as _wp

    return _wp.FORM_EDGE


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
    #: Тип кнопки (ACL-1767); по умолчанию LEARN_MORE, как всегда.
    cta: str = CTA_TYPE
    #: ACL-1774: все тексты по порядку (первый — `body`), заголовки и описания без повторов.
    bodies: list[str] = field(default_factory=list)
    titles: list[str] = field(default_factory=list)
    descriptions: list[str] = field(default_factory=list)
    #: ACL-1774: необязательный вертикальный файл Stories/Reels (того же вида, что основной).
    vertical_path: str = ""
    vertical_kind: str = ""

    @property
    def multi_text(self) -> bool:
        """Несколько текстов любого вида: объявление идёт как Dynamic Creative (ACL-1774)."""
        return len(self.bodies) > 1 or len(self.titles) > 1 or len(self.descriptions) > 1

    @property
    def uses_feed(self) -> bool:
        """Нужна лента ассетов (`asset_feed_spec`): несколько текстов или вертикальный файл.

        Один текст и один файл идут прежним `object_story_spec` — байт в байт как раньше.
        """
        return self.multi_text or bool(self.vertical_path)
    #: Шаблон формы (ACL-1776): форму создаёт робот при публикации; `form_id` заполняется тогда же.
    form_template: dict | None = None
    #: ACL-2180: «Form locale: ES_LA (Spanish, Colombia)», если шаблон с locale Auto (иначе пусто).
    form_locale_note: str = ""


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
    #: ACL-1767. Пусто = как раньше (все полы / все устройства / старт при включении).
    gender: str = ""                 # "men" | "women"
    languages: list[str] = field(default_factory=list)   # дополнительные языки к языку кампании
    device: str = ""                 # "mobile" | "desktop"
    os_min: str = ""                 # минимальная версия iOS/Android: "14" или "14.0"
    start_time: str = ""             # "YYYY-MM-DDTHH:MM" по часовому поясу кабинета
    #: ACL-1768: номер набора и поля уровня набора; пусто = как у кампании.
    index: int = 1                   # 1-based порядковый номер набора в кампании
    language: str = ""               # язык набора вместо языка кампании
    pixel_id: str = ""               # пиксель набора вместо пикселя кампании

    @property
    def instant(self) -> bool:
        return self.destination == DEST_FORM

    @property
    def dynamic(self) -> bool:
        """Набор с объявлением из нескольких текстов - Dynamic Creative (ACL-1774)."""
        return any(a.multi_text for a in self.ads)


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


def media_size(kind: str, data: bytes) -> tuple[int, int] | None:
    """Ширина и высота файла по заголовку (PNG/GIF/JPEG, mp4/mov); не прочитали — None."""
    from src.fb_api import preflight_rules as _pf

    if kind == "video":
        info = _pf.video_info(data)
        if info and info.get("width") and info.get("height"):
            return int(info["width"]), int(info["height"])
        return None
    return _pf.image_size(data)


def _near(ratio: float, target: float) -> bool:
    return abs(ratio - target) / target <= RATIO_TOLERANCE


def pair_problems(index: int, kind: str, main: bytes, vertical: bytes) -> list[str]:
    """Основной файл 1:1 или 4:5 и вертикальный 9:16 — проверка ДО любой загрузки (ACL-1774).

    Не прочитали размер — тоже отказ: пару, которую нельзя проверить, Meta обрежет молча.
    """
    out: list[str] = []
    m, v = media_size(kind, main), media_size(kind, vertical)
    hint = "use JPG/PNG for images or MP4/MOV for videos so the size can be checked"
    if not m or not m[1]:
        out.append(f"Ad {index}: the size of the main file cannot be read; {hint}")
    elif not any(_near(m[0] / m[1], r) for r in MAIN_RATIOS):
        out.append(f"Ad {index}: the main file is {m[0]}x{m[1]}; with a Stories / Reels file "
                   "the main file must be 1:1 or 4:5")
    if not v or not v[1]:
        out.append(f"Ad {index}: the size of the Stories / Reels file cannot be read; {hint}")
    elif not _near(v[0] / v[1], VERTICAL_RATIO):
        out.append(f"Ad {index}: the Stories / Reels file is {v[0]}x{v[1]}; it must be 9:16 "
                   "(for example 1080x1920)")
    return out


def _plan_cta(ad: Any, plan_set: AdSetPlan, index: int, problems: list[str]) -> str:
    cta = str(getattr(ad, "cta", "") or "").strip().upper() or CTA_TYPE
    allowed = CTA_FORM if plan_set.instant else CTA_WEBSITE
    if cta not in allowed:
        problems.append(f"Ad {index}: the button {cta!r} is not available for "
                        f"{'an Instant Form' if plan_set.instant else 'a website'} Leads ad")
    return cta


def _unique(items: list[str]) -> list[str]:
    """Без пустых и повторов (пробелы не в счёт), порядок сохраняется."""
    out: list[str] = []
    seen: set[str] = set()
    for x in items:
        key = "".join(str(x).split())
        if key and key not in seen:
            seen.add(key)
            out.append(str(x).strip())
    return out


def _same_media_file(main: str, vertical: str) -> bool:
    if not main or not vertical:
        return False
    try:
        return Path(main).resolve() == Path(vertical).resolve()
    except (OSError, RuntimeError, ValueError):
        return False


def _plan_texts_and_vertical(plan_ad: AdPlan, src: Any, plan_set: AdSetPlan, problems: list[str],
                             warnings: list[str] | None = None) -> None:
    """Тексты 1-5 и вертикальный файл объявления (ACL-1774). Только правила плана, без чтения файлов."""
    i = plan_ad.index
    variants = [dict(v) for v in (getattr(src, "text_variants", None) or []) if isinstance(v, dict)]
    body2 = str(getattr(src, "ad_body2", "") or "").strip()
    if body2 and not any(str(v.get("body") or "").strip() == body2 for v in variants):
        # Черновик собран мимо библиотеки: второй текст без заголовка и описания.
        variants.insert(0, {"body": body2, "headline": "", "description": ""})
    plan_ad.bodies = _unique([plan_ad.body] + [str(v.get("body") or "") for v in variants])
    plan_ad.titles = _unique([plan_ad.title] + [str(v.get("headline") or "") for v in variants])
    plan_ad.descriptions = _unique([plan_ad.description] + [str(v.get("description") or "") for v in variants])
    for label, items in (("primary texts", plan_ad.bodies), ("headlines", plan_ad.titles),
                         ("descriptions", plan_ad.descriptions)):
        if len(items) > MAX_TEXTS:
            problems.append(f"Ad {i}: up to {MAX_TEXTS} {label} per ad (got {len(items)})")
    for label, items, key in (("primary text", plan_ad.bodies, "bodies"), ("headline", plan_ad.titles, "titles"),
                              ("description", plan_ad.descriptions, "descriptions")):
        limit = _wpol.ASSET_FEED_TEXT_MAX[key]
        for n, text in enumerate(items, 1):
            if len(text) > limit:
                problems.append(f"Ad {i}: {label} {n} is {len(text)} characters long, the limit is {limit}")
    vertical = str(getattr(src, "vertical_path", "") or "").strip()
    same_file = _same_media_file(plan_ad.path, vertical)
    if same_file:
        vertical = ""
        if warnings is not None:
            warnings.append(f"Ad {i}: the same file was selected as main and Stories / Reels; "
                            "it will be used once as the main file.")
    if plan_ad.multi_text and vertical:
        # Живой прогон 2026-10-14: Meta отказала в ленте «несколько текстов + правила площадок»;
        # несколько текстов - это Dynamic Creative (без правил), вертикальный файл - только с одним текстом.
        problems.append(f"Ad {i}: " + _wpol.MSG_TEXTS_VERTICAL)
    if plan_ad.bodies and not plan_ad.body:
        problems.append(f"Ad {i}: choose Primary text 1 first, the other texts come after it")
    if not vertical and not same_file and str(getattr(src, "vertical_filename", "") or "").strip():
        problems.append(f"Ad {i}: the Stories / Reels file was not found")
    if vertical:
        plan_ad.vertical_path = vertical
        plan_ad.vertical_kind = _kind_of(vertical, "")
        if not plan_ad.path:
            problems.append(f"Ad {i}: a Stories / Reels file needs the main file too")
        elif plan_ad.vertical_kind != plan_ad.kind:
            problems.append(f"Ad {i}: the main file is {plan_ad.kind} and the Stories / Reels file is "
                            f"{plan_ad.vertical_kind}; use two images or two videos")
    if plan_ad.uses_feed and plan_set.instant:
        problems.append(f"Ad {i}: several texts and a Stories / Reels file work only with a Website ad set "
                        "for now (Instant Form with an asset feed is not verified on Meta yet); "
                        "use one text and one file, or switch the ad set to Website")


def _plan_audience(plan_set: AdSetPlan, src: Any, problems: list[str]) -> None:
    """Пол, языки, устройства, версия ОС и старт набора (ACL-1767). Незнакомое - отказ вслух."""
    name = plan_set.name
    gender = str(getattr(src, "gender", "") or "").strip().lower()
    if gender and gender not in GENDER_IDS:
        problems.append(f"Ad set {name!r}: unknown gender {gender!r} (use All, Men or Women)")
    else:
        plan_set.gender = gender
    device = str(getattr(src, "device", "") or "").strip().lower()
    if device and device not in DEVICE_CHOICES:
        problems.append(f"Ad set {name!r}: unknown devices value {device!r} (use All, Mobile only or Desktop only)")
    else:
        plan_set.device = device
    if plan_set.os.casefold() not in OS_CHOICES:
        problems.append(f"Ad set {name!r}: unknown operating system {plan_set.os!r} (use All, iOS or Android)")
    mobile_os = plan_set.os.casefold() in ("ios", "android")
    if mobile_os and device == "desktop":
        problems.append(f"Ad set {name!r}: {plan_set.os} is a mobile system and cannot be combined with desktop only")
    elif device == "desktop":
        # ACL-1767 review B2: создатель всегда шлёт Instagram, Stories и Reels (только мобильные),
        # поэтому «только десктоп» Meta отвергнет уже после создания кампании. Настоящее решение -
        # ручные плейсменты (ACL-1772); до тех пор отказ здесь, до любой записи.
        problems.append(f"Ad set {name!r}: Desktop only isn't possible with automatic placements "
                        "(Instagram, Stories and Reels need mobile). Pick All devices or Mobile, "
                        "or wait for manual placements.")
    os_min = str(getattr(src, "os_min", "") or "").strip()
    if os_min:
        if not mobile_os:
            problems.append(f"Ad set {name!r}: a minimum OS version needs the operating system iOS or Android")
        elif not _OS_MIN_RE.match(os_min):
            problems.append(f"Ad set {name!r}: minimum OS version {os_min!r} is not a number like 14 or 14.0")
        else:
            plan_set.os_min = os_min
            if not _wpol.os_version_ok(plan_set.os, os_min):
                problems.append(f"Ad set {name!r}: Meta does not list {plan_set.os} {os_min} as a minimum version "
                                f"(use one of: {', '.join(_wpol.OS_VERSIONS[plan_set.os.casefold()])})")
    seen: set[str] = set()
    for lang in getattr(src, "languages", None) or []:
        lang = str(lang).strip()
        if lang and lang.casefold() not in seen:
            seen.add(lang.casefold())
            plan_set.languages.append(lang)
    start = str(getattr(src, "start_time", "") or "").strip()
    if start:
        try:
            if not _START_RE.match(start):
                raise ValueError(start)
            datetime.strptime(start, "%Y-%m-%dT%H:%M")
            plan_set.start_time = start
        except ValueError:
            problems.append(f"Ad set {name!r}: start time {start!r} is not a date and time")


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
    warnings = [str(w) for w in (getattr(draft, "warnings", None) or [])]
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
    if len(ad_sets_src) > MAX_AD_SETS:
        problems.append(MAX_AD_SETS_ERROR)      # ACL-1768: потолок наборов
    budget = getattr(ad_sets_src[0], "daily_budget", None) if ad_sets_src else None
    if not budget or float(budget) <= 0:
        problems.append("Daily budget missing")
    ad_sets: list[AdSetPlan] = []
    ad_no = 0     # ACL-1768: номер объявления сквозной по кампании (как «Ad N» в билдере)
    for set_no, s in enumerate(ad_sets_src, start=1):
        plan_set = AdSetPlan(
            index=set_no,
            language=str(getattr(s, "language", "") or "").strip(),
            pixel_id=str(getattr(s, "pixel_id", "") or "").strip(),
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
        _plan_audience(plan_set, s, problems)
        if not s.ads:
            problems.append(f"No ads in ad set {plan_set.name!r}")
        for ad in s.ads:
            ad_no += 1
            i = ad_no
            path = str(getattr(ad, "creative_path", "") or "").strip()
            if not path:
                problems.append(f"Ad {i}: no creative file")
            plan_set.ads.append(AdPlan(
                index=i, name=ad.ad_name(), kind=_kind_of(path, getattr(ad, "creative_type", "")),
                path=path, title=str(ad.ad_title or "").strip(), body=str(ad.ad_body or "").strip(),
                description=str(ad.ad_link_description or "").strip(),
                form_id=(str(getattr(ad, "lead_form_id", "") or "").strip() if plan_set.instant else ""),
                cta=_plan_cta(ad, plan_set, i, problems)))
            _plan_texts_and_vertical(plan_set.ads[-1], ad, plan_set, problems, warnings)
            template_id = (str(getattr(ad, "lead_form_template", "") or "").strip() if plan_set.instant else "")
            if template_id and plan_set.ads[-1].form_id:
                problems.append(f"Ad {i}: choose either an existing form or a template, not both")
            elif template_id:
                tpl = load_template(template_id)
                if tpl is None:
                    problems.append(f"Ad {i}: the form template was not found in the team Forms library. If it was saved on this computer before the library was connected, upload it from the Forms tab")
                else:
                    # Сохранённый шаблон: язык вне закрытого списка допустим (ACL-2053).
                    clean, errs = _ft.validate(tpl, lenient_locale=True)
                    if errs:
                        problems.append(f"Ad {i}: form template {clean.get('name') or template_id!r} is not valid: "
                                        + "; ".join(errs))
                    else:
                        try:   # ACL-2180: Auto -> код из языка и страны, до любой записи
                            clean, note = _fl.resolve_template(
                                clean, str(getattr(draft, "language", "") or ""), str(draft.country or ""))
                        except _fl.LocaleRefused as exc:
                            problems.append(f"Ad {i}: {exc}")
                        else:
                            plan_set.ads[-1].form_template = clean
                            plan_set.ads[-1].form_locale_note = note
            elif plan_set.instant and not plan_set.ads[-1].form_id:
                problems.append(f"Ad {i}: no Instant Form selected (pick one of the Page's forms "
                                "or a template)")
        if plan_set.dynamic and len(plan_set.ads) > 1:
            problems.append(f"Ad set {plan_set.name!r}: " + _wpol.MSG_TEXTS_DYNAMIC_SET)
        ad_sets.append(plan_set)
    # Ревью M2: одинаковые имена схлопнули бы объявления (поиск, сверка, повтор).
    # ACL-1768: сравнение без учёта регистра и пробелов по краям - Meta ищет наборы по имени.
    set_names = [x.name.casefold() for x in ad_sets]
    for dup in sorted({x.name for x in ad_sets if set_names.count(x.name.casefold()) > 1}):
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
        warnings=warnings)


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
    #: Смещение кабинета от UTC СЕЙЧАС (только для сведения: на дату старта оно может быть другим).
    utc_offset_hours: float | None = None
    #: IANA-имя часового пояса кабинета (`timezone_name`, тот же ответ, что и смещение).
    #: Время старта переводится по нему, а не по текущему смещению (ACL-1767: переход на летнее время).
    timezone_name: str = ""
    page_names: dict[str, str] = field(default_factory=dict)
    # ACL-2105: откуда Страница ("ad account" / "profile"); профильные дочитываются лениво.
    page_sources: dict[str, str] = field(default_factory=dict)
    profile_loader: Callable[["AccountFacts"], None] | None = field(default=None, repr=False, compare=False)
    profile_loaded: bool = False

    def ensure_profile(self) -> None:
        """Дочитать Страницы профиля один раз (только чтение). Без загрузчика - ничего."""
        if self.profile_loaded or self.profile_loader is None:
            return
        self.profile_loaded = True
        self.profile_loader(self)

    def page_source_of(self, page_id: str) -> str:
        return self.page_sources.get(page_id, _pg.SOURCE_ACCOUNT)


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
    started = time.monotonic()
    merged = _pg.read_merged(client, act, always_profile=False)
    if merged.account_truncated:
        raise PlanError("Facebook Pages could not be fully loaded. Refresh pages before running the campaign.")
    pixels = client.get_all(f"/{act}/adspixels", {"fields": "id,name", "limit": 50}, max_pages=1)
    try:
        raw_off = acc.get("timezone_offset_hours_utc")
        offset = float(raw_off) if raw_off is not None else None
    except (TypeError, ValueError):
        offset = None
    facts = AccountFacts(act=act, currency=str(acc.get("currency") or "").upper(),
                         pixels=[str(p.get("id")) for p in pixels if p.get("id")],
                         pages=[], page_names={}, page_sources={},
                         utc_offset_hours=offset, timezone_name=str(acc.get("timezone_name") or "").strip())
    _apply_pages(facts, merged)
    facts.profile_loaded = merged.profile_read

    def load_profile(f: AccountFacts) -> None:
        # Страница черновика не нашлась среди страниц кабинета: смотрим Страницы профиля.
        again = _pg.read_merged(client, act, always_profile=True)
        if again.account_truncated:
            raise PlanError("Facebook Pages could not be fully loaded. Refresh pages before running the campaign.")
        _apply_pages(f, again)
        _pg.record_counts(again, where="creator", started=started)

    facts.profile_loader = load_profile
    _pg.record_counts(merged, where="creator", started=started)
    return facts


def _apply_pages(facts: AccountFacts, merged: "_pg.MergedPages") -> None:
    facts.pages = [r["id"] for r in merged.rows]
    facts.page_names = {r["id"]: r["name"] for r in merged.rows}
    facts.page_sources = {r["id"]: r["source"] for r in merged.rows}


MANUAL_PAGE_NOTICE = ("No Facebook Page is available. Only the campaign (PAUSED) and ad sets (active under it) will be created; "
                      "ads will not be created. Add a Page and ads in Ads Manager, or refresh Pages and retry "
                      "to complete the same campaign.")


def needs_manual_page(plan: CampaignPlan) -> bool:
    return not plan.page_id and plan.page_source == "manual completion"


def resolve_page(plan: CampaignPlan, facts: AccountFacts) -> list[str]:
    """Страница плана: на кабинете и одна определённая. Заметки; ошибки — `PlanError`."""
    notes = []
    if plan.page_id and plan.page_id not in facts.pages:
        facts.ensure_profile()      # ACL-2105: возможно, это Страница самого профиля
    elif not plan.page_id and not facts.pages:
        facts.ensure_profile()
    if plan.page_id:
        # Принадлежность проверяется и при пустом списке: пустой список — не «можно всё».
        if plan.page_id not in facts.pages:
            src = plan.page_source or "draft"
            raise PlanError(f"The page from the {src} ({plan.page_id}) is not among the pages "
                            f"this ad account can advertise from ({len(facts.pages)} found). "
                            "This account cannot advertise using that Page. Refresh pages and choose again.")
    elif facts.pages:
        plan.page_id = facts.pages[0]
        plan.page_source = "account default"
        notes.append("Automatic Facebook Page: the first available page of this ad account is used"
                     if facts.page_source_of(plan.page_id) == _pg.SOURCE_ACCOUNT else
                     "Automatic Facebook Page: the ad account has no Pages of its own; "
                     "the first Page of the profile (with advertise rights) is used")
    else:
        if any(s.instant for s in getattr(plan, 'ad_sets', [])):
            raise PlanError("Instant Forms require a Facebook Page. Refresh pages or choose a website destination.")
        plan.page_source = "manual completion"
        notes.append(MANUAL_PAGE_NOTICE)
    return notes


def resolve_pixel(plan: CampaignPlan, facts: AccountFacts) -> None:
    """Пиксель плана (только сайтовые наборы). Ошибки — `PlanError`."""
    if plan.ad_sets and all(s.instant for s in plan.ad_sets):
        # Instant Form: цель — форма Страницы, пиксель не нужен (ACL-1775).
        return
    for s in plan.ad_sets:
        # ACL-1768: пиксель набора, если задан, тоже должен быть на кабинете.
        if s.pixel_id and not s.instant and s.pixel_id not in facts.pixels:
            raise PlanError(f"The pixel of ad set {s.name!r} is not on this ad account.")
    if plan.pixel_id:
        # То же для пикселя. Читается первая страница (50): больше — безопасный отказ.
        if plan.pixel_id not in facts.pixels:
            src = plan.pixel_source or "draft"
            raise PlanError(f"The pixel from the {src} ({plan.pixel_id}) is not on this ad account "
                            f"({len(facts.pixels)} found). Fix the pixel instead of letting another one be picked.")
    elif all(s.pixel_id for s in plan.ad_sets if not s.instant):
        return
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
                                    for i in asks) + " Confirm a candidate in Check plan")
        if unknown:
            msg.append(_tr.UNKNOWN_MESSAGE)
        super().__init__(". ".join(msg))


class TargetingResolver:
    """Гео, языки, интересы → id Meta. Кэш на задание; только чтения."""

    def __init__(self, client: GraphClient, store: _tr.ConfirmedStore | None = None, *,
                 search_cache: "_tr.SearchCache | None" = None, cache_scope: str = ""):
        self.client = client
        self._geo: dict[str, str] = {}
        self._locale: dict[str, int] = {}
        #: Имя, под которым Meta нашла язык («Spanish (All)»): показываем в Check plan.
        self.locale_names: dict[str, str] = {}
        #: Детальный таргетинг (ACL-1771): свой поиск, кэш подтверждённых и бюджет вызовов.
        self.terms = _tr.TermResolver(client, store, cache=search_cache, scope=cache_scope)

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
        self.locale_names[lang] = str(pick.get("name") or lang)
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
            if r["state"] in ("exact", "confirmed"):
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


def targeting_for(plan: CampaignPlan, ad_set: AdSetPlan, resolver: TargetingResolver) -> dict:
    t: dict[str, Any] = {
        "geo_locations": {"countries": [resolver.country(plan.country)]},
        "age_min": ad_set.age_min,
        "age_max": ad_set.age_max,
        # Возраст и интересы — как в билдере; Meta не расширяет аудиторию сама.
        "targeting_automation": {"advantage_audience": 0},
    }
    loc = resolver.locale(ad_set.language or plan.language)
    locales = [loc] if loc is not None else []
    for extra in ad_set.languages:
        # Язык кампании остаётся первым; дополнительные - только точные совпадения /search.
        got = resolver.locale(extra)
        if got is not None and got not in locales:
            locales.append(got)
    if locales:
        t["locales"] = locales
    if ad_set.gender:
        t["genders"] = [GENDER_IDS[ad_set.gender]]
    if ad_set.interest_groups:
        t["flexible_spec"] = resolver.interests(ad_set.interest_groups)
    # Площадки — вручную, как у робота (не Advantage+ placements).
    t["publisher_platforms"] = list(PUBLISHER_PLATFORMS)
    t["facebook_positions"] = list(FACEBOOK_POSITIONS)
    t["instagram_positions"] = list(INSTAGRAM_POSITIONS)
    os_val = ad_set.os.casefold()
    if os_val in ("ios", "android"):
        # Как в билдере (решение по Android — за владельцем, пока как было).
        t["user_os"] = [user_os_value(os_val, ad_set.os_min)]
        t["device_platforms"] = ["mobile"]
    else:
        t["device_platforms"] = [ad_set.device] if ad_set.device else ["mobile", "desktop"]
    return t


def user_os_value(os_val: str, os_min: str = "") -> str:
    """«iOS» / «Android», с версией - «iOS_ver_14.0_and_above» (Graph «Targeting», user_os)."""
    name = "iOS" if os_val.casefold() == "ios" else "Android"
    if not os_min:
        return name
    ver = os_min if "." in os_min else os_min + ".0"
    return f"{name}_ver_{ver}_and_above"


def plan_has_instagram(targetings: list[dict]) -> bool:
    """В каком-либо наборе плана есть площадки Instagram (ACL-1896; ACL-1772 сделает их выбираемыми)."""
    return any("instagram" in (t.get("publisher_platforms") or []) and bool(t.get("instagram_positions"))
               for t in targetings)


def plan_has_instagram_placements(plan: "CampaignPlan") -> bool:
    """То же без чтения таргетинга: пока площадки зашиты в создателе, они всегда с Instagram."""
    return "instagram" in PUBLISHER_PLATFORMS and bool(INSTAGRAM_POSITIONS)


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


class LargeFile:
    """Большое видео на диске (ACL-1891): в памяти не держится, читаются только куски.

    Ведёт себя как bytes ровно настолько, насколько нужно предпроверке: длина, индекс и срез
    (заголовки mp4 читаются малыми срезами). Срез больше потолка куска - отказ, чтобы случайный
    `data[:]` не втянул файл в память.
    """

    HASH_BLOCK = 1024 * 1024

    def __init__(self, path: str, size: int,
                 read_range: Callable[[str, int, int], bytes] | None = None):
        self.path = path
        self.size = int(size)
        self._read = read_range or self._read_disk
        self._md5: str | None = None
        #: Самый большой единичный кусок, прочитанный с диска (для проверки памяти).
        self.max_read = 0

    @staticmethod
    def _read_disk(path: str, offset: int, n: int) -> bytes:
        with open(path, "rb") as fh:
            fh.seek(offset)
            return fh.read(n)

    def __len__(self) -> int:
        return self.size

    def __bool__(self) -> bool:
        return self.size > 0

    def read_range(self, offset: int, n: int) -> bytes:
        if n < 0 or n > max(VIDEO_CHUNK_MAX, self.HASH_BLOCK) or offset < 0:
            raise ValueError("read is larger than one chunk")
        out = self._read(self.path, offset, n) if n else b""
        self.max_read = max(self.max_read, len(out))
        return out

    def __getitem__(self, key):
        if isinstance(key, slice):
            start, stop, step = key.indices(self.size)
            if step != 1:
                raise ValueError("only plain slices")
            return self.read_range(start, max(0, stop - start))
        index = key + self.size if key < 0 else key
        if not 0 <= index < self.size:
            raise IndexError(key)
        return self.read_range(index, 1)[0]

    def md5(self) -> str:
        if self._md5 is None:
            h = hashlib.md5()  # noqa: S324 - только имя/поиск, не защита
            offset = 0
            while offset < self.size:
                block = self.read_range(offset, min(self.HASH_BLOCK, self.size - offset))
                if not block:
                    raise OSError("file ended while hashing")
                h.update(block)
                offset += len(block)
            self._md5 = h.hexdigest()
        return self._md5


def _md5(data: "bytes | LargeFile") -> str:
    if isinstance(data, LargeFile):
        return data.md5()
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
    #: Что показать человеку списком (ACL-1437): title/impact/action/severity/raw.
    problems: list = field(default_factory=list)
    manual_required: list[str] = field(default_factory=list)
    #: ACL-2114: чтением подтверждено, что кампания PAUSED, - только тогда пишутся ACTIVE дети.
    campaign_confirmed_paused: bool = False

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
                 size_of: Callable[[str], int] | None = None,
                 read_range: Callable[[str, int, int], bytes] | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic,
                 video_wait_s: float = VIDEO_WAIT_S, video_poll_s: float = VIDEO_POLL_S,
                 video_max_polls: int = VIDEO_MAX_POLLS,
                 reconcile_wait_s: float | None = None,
                 progress: Callable[[str], None] = lambda _s: None,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 live_progress: Callable[[str], None] = lambda _s: None):
        self.client = client
        self.facts = facts
        self.resolver = resolver
        self.cancelled = cancelled
        self.read_file = read_file or (lambda p: Path(p).read_bytes())
        #: ACL-1891: размер файла на диске (None - неизвестен, читаем целиком как раньше) и чтение куска.
        self.size_of = size_of or (lambda p: _disk_size(p) or None)
        self.read_range = read_range
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
        #: Instagram-идентичность Страницы (ACL-1896): читается раз на Страницу за прогон.
        self._ig_cache: dict[str, _ig.IgIdentity] = {}
        #: id аккаунта Instagram, который уходит в креатив; пусто - Instagram-площадок нет.
        self._ig_id = ""
        #: Шаблоны форм плана -> что с ними будет (ACL-1776); читается в `_check_forms`.
        self._form_steps: dict[str, _fc.FormStep] = {}
        self._form_warnings: list[str] = []
        self.progress = progress
        self.now = now
        #: Живой процент загрузки файла (ACL-1899): только на панель задания, без шагов и телеметрии.
        self.live_progress = live_progress

    # -- Instagram-идентичность (ACL-1896)
    def instagram_identity(self, plan: CampaignPlan) -> _ig.IgIdentity:
        """Чтение (кэш на Страницу): только чтения; `unknown` не считается «есть»."""
        got = self._ig_cache.get(plan.page_id)
        if got is None:
            got = self._ig_cache[plan.page_id] = _ig.read_identity(self.client, plan.act, plan.page_id)
        return got

    def _require_instagram(self, plan: CampaignPlan, targetings: list[dict], out: Outcome) -> None:
        """Instagram-площадки без подтверждённого аккаунта Страницы - отказ ДО записи (Doc120)."""
        self._ig_id = ""
        if not plan.page_id or not plan_has_instagram(targetings):
            return
        got = self.instagram_identity(plan)
        if got.status != _ig.FOUND:
            raise PlanError(got.message)
        self._ig_id = got.ig_id
        self._step(out, f"preflight: Instagram account of the Page found ({got.source})")

    def with_instagram(self, story: dict) -> dict:
        """Креативу добавляется Instagram-идентичность, если она подтверждена (иначе ключа нет)."""
        return {**story, _ig.IG_KEY: self._ig_id} if self._ig_id else story

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
               on_ad: bool = False, children_active: bool = False, form_page: str = "",
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
            if children_active:
                extra["children_active"] = True
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

    def _confirm_campaign_paused(self, out: Outcome, plan: CampaignPlan) -> bool:
        """ACL-2114: перед ПЕРВОЙ записью набора/объявления - чтение: кампания точно PAUSED.

        Иначе стоп без записи: ACTIVE-дочерний объект под не-паузной кампанией начал бы
        тратить деньги. Подтверждение одно на запуск; найденные (reused) объекты сюда не
        попадают - их статус мы не пишем.
        """
        if out.campaign_confirmed_paused:
            return True
        row = self.client.get(f"/{out.campaign_id}", {"fields": "status"})
        got = row.get("status")
        if got != PAUSED:
            raise CreationStopped(
                f"The campaign {plan.name!r} is not confirmed PAUSED (Meta reports {got or 'no status'}). "
                "Stopped before creating any ad set or ad, which would start delivering. "
                "Nothing else was written; check the campaign in Ads Manager.",
                created=out.created, kind="campaign_not_paused")
        out.campaign_confirmed_paused = True
        return True

    def _verify_label(self, out: Outcome, what: str, object_id: str, label: str) -> None:
        """Метка на созданном объекте; нет — стоп до следующей записи (ревью B1b)."""
        row = self.client.get(f"/{object_id}", {"fields": "adlabels{name}"})
        if label not in _labels(row):
            raise CreationStopped(
                f"{what} was created but Meta did not keep the ACL tag ({label}) on it. "
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
    def _image_hash(self, out: Outcome, plan: CampaignPlan, ad: AdPlan, data: bytes, *,
                    path: str = "", role: str = "") -> str:
        md5 = _md5(data)
        path = path or ad.path
        who = f"ad {ad.index}{role}"

        def find(pages: int = 1) -> str | None:
            rows = self.client.get_all(f"/{plan.act}/adimages", {"hashes": [md5], "fields": "hash"},
                                       max_pages=min(1, pages))
            return md5 if any(str(r.get("hash")) == md5 for r in rows) else None

        if find():
            self._step(out, f"{who}: image already in the ad account")
            return md5
        def hash_of(body: dict) -> str | None:
            images = body.get("images") if isinstance(body, dict) else None
            for v in (images or {}).values():
                if isinstance(v, dict) and v.get("hash"):
                    return str(v["hash"])
            return None

        # Имя в библиотеке несёт md5 файла: по нему сверка узнаёт картинку и при повторе.
        got = self._write(out, f"{who}: image upload", f"/{plan.act}/adimages", {}, find,
                          files={"filename": data}, filename=image_name(path, data), id_of=hash_of,
                          upload=(ad.index, f"{role.strip()} image".strip()))
        out.created.setdefault("images", []).append(got)
        self._step(out, f"{who}: image uploaded")
        return got

    def _video(self, out: Outcome, plan: CampaignPlan, ad: AdPlan, data: bytes, *,
               path: str = "", role: str = "") -> tuple[str, str]:
        title = video_title(plan.tag, data)
        path = path or ad.path
        who = f"ad {ad.index}{role}"

        def find(pages: int = 10) -> str | None:
            rows = self.client.get_all(f"/{plan.act}/advideos", {"fields": "id,title", "limit": 100},
                                       max_pages=pages)
            hit = next((r for r in rows if str(r.get("title") or "") == title), None)
            return str(hit["id"]) if hit else None

        vid = find()
        if vid:
            self._step(out, f"{who}: video already in the ad account")
        elif isinstance(data, LargeFile):
            vid = self._video_chunked(out, plan, data, title, find, who)
            out.created.setdefault("videos", []).append(vid)
            self._step(out, f"{who}: video uploaded")
        else:
            vid = self._write(out, f"{who}: video upload", f"/{plan.act}/advideos",
                              {"name": title, "title": title}, find, files={"source": data},
                              filename=Path(path).name, upload=(ad.index, f"{role.strip()} video".strip()))
            out.created.setdefault("videos", []).append(vid)
            self._step(out, f"{who}: video uploaded")
        return vid, self._wait_video(out, ad, vid, who=who)

    # -- загрузка большого видео кусками (ACL-1891)
    @staticmethod
    def _offsets(body: dict, size: int, floor: int, what: str) -> tuple[int, int]:
        """Смещения из ответа Meta: цифры, не назад, не за концом файла. Иначе - стоп."""
        s_raw, e_raw = str(body.get("start_offset", "")), str(body.get("end_offset", ""))
        if not (s_raw.isdigit() and e_raw.isdigit() and len(s_raw) <= 13 and len(e_raw) <= 13):
            raise CreationStopped(f"{what}: Meta returned no usable upload offsets", kind="video_error")
        start, end = int(s_raw), int(e_raw)
        if start < floor or start > size or end < start or end > size:
            raise CreationStopped(f"{what}: Meta reported an upload offset outside the file "
                                  f"({start}-{end} of {size}); stopped before sending more",
                                  kind="video_error")
        return start, end

    def _upload_call(self, out: Outcome, what: str, path: str, params: dict, files: dict | None,
                     *, find: Callable[[], str | None] | None = None) -> dict:
        """Один вызов фазы загрузки. Неизвестный результат - стоп (кусок повторяет сам цикл)."""
        self._check_cancel(out)
        try:
            return self.client.post(path, params, files=files, filename="chunk" if files else "")
        except WriteUnknown as exc:
            self._step(out, f"{what}: result unknown")
            raise CreationStopped(
                f"{what}: the write result is unknown ({exc}). Publish again later: a finished video "
                "is found by its tag and is not uploaded twice.", created=out.created, kind="unknown") from exc

    def _video_chunked(self, out: Outcome, plan: CampaignPlan, big: "LargeFile", title: str,
                       find: Callable[[], str | None], who: str) -> str:
        """start -> transfer (куски от смещения Meta) -> finish. В памяти один кусок."""
        path = f"/{plan.act}/advideos"
        size = len(big)
        what = f"{who}: video upload"
        # Meta требует waterfall_id от токена Ads Manager (подкод 1363049); один на всю загрузку.
        waterfall = uuid.uuid4().hex
        started = self._upload_call(out, what, path, {"upload_phase": "start", "file_size": str(size),
                                                       "waterfall_id": waterfall}, None)
        sid, vid = str(started.get("upload_session_id") or ""), str(started.get("video_id") or "")
        if not (sid.isdigit() and vid.isdigit()):
            raise CreationStopped(f"{what}: Meta returned no upload session", created=out.created,
                                  kind="video_error")
        offset, end = self._offsets(started, size, 0, what)
        self._step(out, f"{who}: chunked upload started ({size // (1024 * 1024)} MB)")
        # Предохранитель от бесконечного цикла: не больше вдвое сверх числа кусков.
        guard = 2 * (-(-size // VIDEO_CHUNK_MAX)) + 4
        failures = 0
        # ACL-1998: большая загрузка идёт минутами; без шагов панель считает её зависшей (idle_s > 180 с).
        # Шаг «uploaded X/Y MB» - не чаще раза в UPLOAD_STEP_EVERY_S секунд, но не реже каждых 10% файла.
        mb_total = max(size // (1024 * 1024), 1)
        last_step_at, last_step_off = self.clock(), offset
        while offset < size:
            guard -= 1
            if guard < 0:
                raise CreationStopped(f"{what}: upload does not progress", created=out.created, kind="video_error")
            n = min(max(end - offset, 1), VIDEO_CHUNK_MAX, size - offset)
            chunk = big.read_range(offset, n)
            if len(chunk) != n:
                raise CreationStopped(f"{what}: the file changed while uploading", created=out.created,
                                      kind="video_error")
            params = {"upload_phase": "transfer", "upload_session_id": sid, "start_offset": str(offset),
                      "waterfall_id": waterfall}
            try:
                body = self._upload_call(out, what, path, params, {_wpol.UPLOAD_CHUNK_FIELD: chunk})
            except CreationStopped as exc:
                if exc.kind != "unknown" or failures >= 2:
                    raise
                # Неизвестно, дошёл ли кусок: тот же кусок с тем же смещением - запрос смещения у Meta.
                failures += 1
                self.client.reconciled(path)
                continue
            except GraphError as exc:
                # Meta отказала: назвала нужное смещение - продолжаем с него (не с нуля), иначе стоп.
                d = exc.details or {}
                if failures >= 2 or not str(d.get("start_offset", "")).isdigit():
                    raise
                failures += 1
                offset, end = self._offsets(d, size, offset, what)
                self._step(out, f"{who}: resuming the upload from offset {offset}")
                continue
            failures = 0
            offset, end = self._offsets(body, size, offset, what)
            now = self.clock()
            if offset < size and (now - last_step_at >= UPLOAD_STEP_EVERY_S
                                  or (offset - last_step_off) * 10 >= size):
                self._step(out, f"{who}: uploaded {offset // (1024 * 1024)}/{mb_total} MB")
                last_step_at, last_step_off = now, offset
        try:
            done = self._upload_call(out, what, path, {"upload_phase": "finish", "upload_session_id": sid,
                                                        "title": title, "name": title,
                                                        "waterfall_id": waterfall}, None)
        except CreationStopped as exc:
            if exc.kind != "unknown":
                raise
            # Результат finish неизвестен: сверка чтением по метке названия, второй раз не грузим.
            if self.reconcile_wait_s > 0:
                self.sleep(self.reconcile_wait_s)
            found = find()
            if not found:
                raise
            self.client.reconciled(path)
            self._step(out, f"{what}: reconciled - found by readback")
            return str(found)
        if done.get("success") is False:
            raise CreationStopped(f"{what}: Meta did not finish the upload", created=out.created,
                                  kind="video_error")
        return vid

    def _wait_video(self, out: Outcome, ad: AdPlan, vid: str, *, who: str = "") -> str:
        """Ждать обработку видео — не дольше `video_wait_s` и `video_max_polls` вызовов."""
        who = who or f"ad {ad.index}"
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
                raise CreationStopped(f"{who}: Meta could not process the video", created=out.created,
                                      kind="video_error")
            if polls >= self.video_max_polls or self.clock() + self.video_poll_s > deadline:
                raise CreationStopped(
                    f"{who}: the video is still processing after {int(self.video_wait_s)} s; "
                    "publish again later (nothing is duplicated)", created=out.created, kind="video_timeout")
            self.sleep(self.video_poll_s)
        self._step(out, f"{who}: video processed")
        thumbs = self.client.get_all(f"/{vid}/thumbnails", {"fields": "uri,is_preferred"}, max_pages=1)
        pick = next((t for t in thumbs if t.get("is_preferred")), thumbs[0] if thumbs else None)
        if not pick or not pick.get("uri"):
            raise CreationStopped(f"{who}: Meta returned no thumbnail for the video",
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
            params = {"name": ad_set.name, "campaign_id": campaign_id, "status": CHILD_STATUS,
                      "optimization_goal": OPTIMIZATION_GOAL_FORM, "billing_event": BILLING_EVENT,
                      "destination_type": DESTINATION_ON_AD,
                      "promoted_object": {"page_id": plan.page_id},
                      "targeting": targeting, "adlabels": [{"name": plan.label}]}
        else:
            params = {"name": ad_set.name, "campaign_id": campaign_id, "status": CHILD_STATUS,
                      "optimization_goal": OPTIMIZATION_GOAL, "billing_event": BILLING_EVENT,
                      "destination_type": DESTINATION,
                      "promoted_object": {"pixel_id": ad_set.pixel_id or plan.pixel_id,
                                          "custom_event_type": CUSTOM_EVENT},
                      "targeting": targeting, "adlabels": [{"name": plan.label}]}
        if ad_set.start_time:
            # ACL-1767: ключа нет, пока старт не задан - прежние планы не меняются.
            params["start_time"] = self.start_iso(ad_set)
        if ad_set.dynamic:
            # ACL-1774: несколько текстов - Dynamic Creative на уровне набора; ключа нет у прежних планов.
            params[_wpol.DYNAMIC_KEY] = True
        return params

    def _start_dt(self, ad_set: AdSetPlan) -> datetime:
        """Время старта набора как момент: введено по часовому поясу кабинета."""
        name = self.facts.timezone_name
        zone = None
        if name:
            try:
                zone = ZoneInfo(name)
            except (ZoneInfoNotFoundError, ValueError, OSError):
                zone = None
        if zone is None:
            # Не угадываем по текущему смещению: на дату старта оно может быть другим (летнее время).
            raise PlanError(f"Ad set {ad_set.name!r}: the time zone of the ad account "
                            f"({name or 'not returned by Meta'}) is not known here, so the start time "
                            "cannot be set. Clear the start time or try again.")
        return datetime.strptime(ad_set.start_time, "%Y-%m-%dT%H:%M").replace(tzinfo=zone)

    def start_iso(self, ad_set: AdSetPlan) -> str:
        """«2026-10-20T10:00:00+0200» - с явным смещением кабинета (Graph: ISO 8601)."""
        return self._start_dt(ad_set).strftime("%Y-%m-%dT%H:%M:%S%z")

    def creative_params(self, plan: CampaignPlan, ad: AdPlan, ad_set: AdSetPlan, media: dict, name: str) -> dict:
        """Запись креатива: одно описание на запись, предпроверку политикой и сверку (ACL-1767)."""
        if ad.uses_feed:
            # ACL-1774: несколько текстов и/или вертикальный файл - лента ассетов; рядом только страница.
            story: dict = {"page_id": plan.page_id}
            extra: dict = {"asset_feed_spec": self.feed_spec(plan, ad, media)}
        else:
            story, extra = self._story(plan, ad, ad_set, media), {}
        return {"name": name, "object_story_spec": self.with_instagram(story), **extra,
                "degrees_of_freedom_spec": creative_features(),
                # Multi-advertiser ads - явно ВЫКЛЮЧЕНЫ (решение владельца 2026-10-10).
                "contextual_multi_ads": dict(MULTI_ADS_OPT_OUT),
                "adlabels": [{"name": plan.label}]}

    @staticmethod
    def rule_positions() -> tuple[dict, dict]:
        """Площадки двух правил: вертикальный файл — Stories и Reels, основной — всё остальное."""
        vertical = {"publisher_platforms": list(PUBLISHER_PLATFORMS),
                    "facebook_positions": list(VERTICAL_FACEBOOK),
                    "instagram_positions": list(VERTICAL_INSTAGRAM)}
        main = {"publisher_platforms": list(PUBLISHER_PLATFORMS),
                "facebook_positions": [x for x in FACEBOOK_POSITIONS if x not in VERTICAL_FACEBOOK],
                "instagram_positions": [x for x in INSTAGRAM_POSITIONS if x not in VERTICAL_INSTAGRAM]}
        return main, vertical

    @staticmethod
    def feed_spec(plan: CampaignPlan, ad: AdPlan, media: dict) -> dict:
        """`asset_feed_spec` сайтового объявления: тексты (до 5) и один или два файла с метками.

        Один файл — без правил. Два — правило Stories/Reels на вертикальный и правило на остальные
        площадки на основной (Meta требует минимум два правила). Текстовые улучшения и Multi-advertiser
        выключены рядом, в самом креативе.
        """
        key = "videos" if ad.kind == "video" else "images"

        def item(m: dict, label: str | None) -> dict:
            row = ({"video_id": m["video_id"], "thumbnail_url": m["thumb"]} if ad.kind == "video"
                   else {"hash": m["image_hash"]})
            if label:
                row["adlabels"] = [{"name": label}]
            return row

        spec: dict[str, Any] = {
            "bodies": [{"text": t} for t in ad.bodies], "titles": [{"text": t} for t in ad.titles],
            key: [item(media, LABEL_MAIN if ad.vertical_path else None)],
            "link_urls": [{"website_url": plan.link}], "call_to_action_types": [ad.cta],
            "ad_formats": ["SINGLE_VIDEO" if ad.kind == "video" else "SINGLE_IMAGE"]}
        if ad.descriptions:
            spec["descriptions"] = [{"text": t} for t in ad.descriptions]
        if ad.vertical_path:
            spec[key].append(item(media["vertical"], LABEL_VERTICAL))
            label_key = "video_label" if ad.kind == "video" else "image_label"
            main_pos, vert_pos = CampaignCreator.rule_positions()
            spec["asset_customization_rules"] = [
                {"customization_spec": vert_pos, label_key: {"name": LABEL_VERTICAL}},
                {"customization_spec": main_pos, label_key: {"name": LABEL_MAIN}}]
        return spec

    @staticmethod
    def story_spec(plan: CampaignPlan, ad: AdPlan, media: dict, *, instant: bool = False) -> dict:
        if instant:
            # ACL-1775: CTA несёт id существующей формы; своей ссылки у объявления нет.
            cta = {"type": ad.cta, "value": {"lead_gen_form_id": ad.form_id}}
        else:
            cta = {"type": ad.cta, "value": {"link": plan.link}}
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
            return self.with_instagram(self.story_spec(plan, ad, media, instant=True))
        return self.with_instagram(self.story_spec(plan, ad, media))

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
        # ACL-2050: новый макет Advantage+ - Meta может молча включить автоплощадки. Поле не вернули
        # (старый API / нет прав) - не блокируем, а отмечаем в диагностике; ENABLED - блокирует.
        state = _advantage_info(camp).get("advantage_placement_state")
        if state is not None:
            check("advantage_placement_state", "DISABLED", state)
        return checks

    def _adset_checks(self, plan: CampaignPlan, ad_set: AdSetPlan, targeting: dict, row: dict,
                      campaign_id: str, *, reused: bool = False) -> list[dict]:
        obj = f"ad set {ad_set.name}"
        checks: list[dict] = []

        def check(f: str, e: Any, a: Any) -> None:
            self._mk_check(checks, obj, f, e, a)

        check("name", ad_set.name, row.get("name"))
        check("status", _child_status_expected(row.get("status"), reused), row.get("status"))
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
            check("pixel", ad_set.pixel_id or plan.pixel_id, str(po.get("pixel_id") or ""))
            check("conversion_event", CUSTOM_EVENT, po.get("custom_event_type"))
        # Оба направления: набор прошлого запуска мог быть динамическим, а объявление теперь с одним текстом.
        check("dynamic_creative", ad_set.dynamic,
              bool(row.get("is_dynamic_creative")) if ("is_dynamic_creative" in row or not ad_set.dynamic) else None)
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
        if targeting.get("genders") or t.get("genders"):
            check("gender", targeting.get("genders") or [], t.get("genders") or [])
        if ad_set.start_time:
            check("start_time", _utc(self.start_iso(ad_set)), _utc(row.get("start_time")))
        # Площадки вручную, как у робота (паритет RPA): платформы, места, устройства.
        check("publisher_platforms", targeting.get("publisher_platforms") or [],
              t.get("publisher_platforms") or [])
        check("facebook_positions", targeting.get("facebook_positions") or [],
              t.get("facebook_positions") or [])
        check("instagram_positions", targeting.get("instagram_positions") or [],
              t.get("instagram_positions") or [])
        check("devices", targeting.get("device_platforms") or [], t.get("device_platforms") or [])
        # ACL-2050: одна понятная строка, если Meta не сохранила ручные площадки (автоплощадки/другой список).
        # Порядок списков не важен (как в проверках выше): сводка нужна, только если набор реально отличается.
        want_pl, got_pl = placements_text(targeting), placements_text(t)
        if targeting.get("publisher_platforms") and any(
                not _same(targeting.get(k) or [], t.get(k) or [])
                for k in ("publisher_platforms", "facebook_positions", "instagram_positions", "device_platforms")):
            check("placements_summary", want_pl, got_pl)
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
            diffs = _describe_diffs(self._adset_checks(plan, ad_set, targeting, row, campaign_id, reused=True))
            if diffs:
                found.append({"object": f"ad set {ad_set.name!r}", "kind": "adset", "differences": diffs})
        return found

    def _find_sets(self, plan: CampaignPlan, campaign_id: str) -> list[dict | None]:
        return [self._find_child(campaign_id, "adsets", s.name) for s in plan.ad_sets]

    # -- весь путь
    def run(self, plan: CampaignPlan) -> Outcome:
        self._creatives = None
        self._profile_page_checks = {}
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

    def load_media(self, path: str, kind: str) -> "bytes | LargeFile":
        """Файл объявления: видео больше 200 МБ - `LargeFile` (куски с диска), остальное - bytes."""
        if kind == "video":
            size = self.size_of(path)
            if size and size > VIDEO_SINGLE_MAX_BYTES:
                return LargeFile(path, size, self.read_range)
        return self.read_file(path)

    def check_profile_page(self, plan: CampaignPlan) -> None:
        """Страница из профиля (не из кабинета) должна пройти проверку пригодности (ACL-2105).

        Один validate-only creative для кабинета/Страницы/ссылки; объектов не создаёт.
        Отказ или неизвестный ответ - PlanError до любых объектов/загрузок/форм.
        """
        pid = plan.page_id
        if not pid or self.facts.page_source_of(pid) != _pg.SOURCE_PROFILE:
            return
        name = self.facts.page_names.get(pid, pid)
        if plan.act != self.facts.act:
            raise PlanError(f"The Page {name!r} ({pid}) validation account differs from the verified ad account. "
                            "Nothing was created.")
        cache = self.__dict__.setdefault("_profile_page_checks", {})
        key = (plan.act, pid, plan.link)
        if key not in cache:
            cache[key] = _pg.check_usable(self.client, pid, self.facts.page_names.get(pid, ""),
                                          act=plan.act, link=plan.link)
        if cache[key]:
            raise PlanError(f"The Page {name!r} ({pid}) belongs to the profile, not to this ad account, and "
                            f"cannot be used for ads here: {cache[key]}. Choose another Page or add this Page "
                            "to the ad account in Business Manager. Nothing was created.")

    def _prepare(self, plan: CampaignPlan, out: Outcome, *,
                 writing: bool = True) -> tuple[list[dict], dict[str, bytes]]:
        """Всё, что проверяется до записи: страница, пиксель, таргетинг, файлы.

        `writing=False` - пробный запуск (Check plan): записей не будет, и бюджет на них не сверяется.
        """
        out.warnings += resolve_page_pixel(plan, self.facts)
        self.check_profile_page(plan)
        # ACL-1768: таргетинг ВСЕХ наборов разбирается до отказа - Check plan показывает все термины,
        # ждущие подтверждения, за один раз, а не по набору за круг.
        targetings = []
        unresolved: list[dict] = []
        for s in plan.ad_sets:
            try:
                targetings.append(targeting_for(plan, s, self.resolver))
            except TargetingUnresolved as exc:
                unresolved += [i for i in exc.items if i not in unresolved]
        if unresolved:
            raise TargetingUnresolved(unresolved)
        self._step(out, "preflight: targeting resolved")
        self._require_instagram(plan, targetings, out)
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
                    data = self.load_media(ad.path, ad.kind)
                except OSError as exc:
                    raise PlanError(f"ad {ad.index}: creative file cannot be read ({exc})") from exc
                limit = VIDEO_MAX_BYTES if ad.kind == "video" else IMAGE_MAX_BYTES
                if not data or len(data) > limit:
                    raise PlanError(f"ad {ad.index}: creative file is empty or larger than "
                                    f"{limit // (1024 * 1024)} MB")
                files[ad.path] = data
                if ad.vertical_path:
                    try:
                        vdata = self.load_media(ad.vertical_path, ad.vertical_kind)
                    except OSError as exc:
                        raise PlanError(f"ad {ad.index}: Stories / Reels file cannot be read ({exc})") from exc
                    vlimit = VIDEO_MAX_BYTES if ad.vertical_kind == "video" else IMAGE_MAX_BYTES
                    if not vdata or len(vdata) > vlimit:
                        raise PlanError(f"ad {ad.index}: Stories / Reels file is empty or larger than "
                                        f"{vlimit // (1024 * 1024)} MB")
                    bad = pair_problems(ad.index, ad.kind, data, vdata)
                    if bad:
                        raise PlanError("; ".join(bad))
                    files[ad.vertical_path] = vdata
        self._step(out, "preflight: creatives read")
        # ACL-1891: куски большого видео должны влезть в бюджет вызовов - иначе отказ до записи.
        budget = getattr(self.client, "max_calls", None)
        chunk_calls = sum(upload_calls(len(v)) for v in files.values() if isinstance(v, LargeFile))
        # ACL-1768: куски большого видео и записи ВСЕХ наборов берутся из одного бюджета (его размер
        # считается по плану, `publish_api.create_call_budget`; чтения таргетинга в него заложены).
        # Не хватает - отказ ДО первой записи. Check plan (`writing=False`) бюджет на записи не сверяет.
        if isinstance(budget, int) and chunk_calls:
            rest = estimate_create_calls(plan) if writing else CALLS_RESERVE_OTHER
            if chunk_calls + rest > budget:
                raise PlanError(f"Large videos need about {chunk_calls} Meta calls to upload in chunks "
                                f"and the rest of the campaign about {rest}, more than "
                                f"one run allows ({budget}). Use a smaller video or split the campaign. "
                                "Nothing was created.")
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
                # ACL-2114: ACTIVE у набора/объявления проверяется как при записи (кампания будет PAUSED).
                _wp.check(plan.act, "POST", f"/{plan.act}/{edge}", params, on_ad=on_ad,
                          children_active=edge in _wp.CHILD_EDGES)
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
            if ad_set.start_time and self._start_dt(ad_set) <= self.now():
                raise PlanError(f"Ad set {ad_set.name!r}: the start time {ad_set.start_time} "
                                "(ad account time zone) is not in the future")
            ok("adsets", self.adset_params(plan, ad_set, "0", targeting), f"ad set {ad_set.name!r}")
            if needs_manual_page(plan):
                continue  # No creative/ad write is planned until a Page is available.
            for ad in ad_set.ads:
                ad = self._planned(ad, FORM_ID_PLACEHOLDER)
                media = ({"video_id": PLACEHOLDER_VIDEO_ID, "thumb": "https://example.invalid/t.jpg"}
                         if ad.kind == "video" else {"image_hash": PLACEHOLDER_HASH})
                if ad.vertical_path:
                    media["vertical"] = dict(media)
                ok("adcreatives", self.creative_params(plan, ad, ad_set, media,
                                                       f"{ad.name} #{ad.index} [{plan.label}]"),
                   f"ad {ad.index} creative", on_ad=ad_set.instant)
                ok("ads", {"name": ad.name, "adset_id": "0", "creative": {"creative_id": "0"},
                           "status": CHILD_STATUS, "adlabels": [{"name": plan.label}]}, f"ad {ad.index}")

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
        self._profile_page_checks = {}
        out = Outcome(campaign_name=plan.name, warnings=list(plan.warnings))
        differences: list | None = None
        files: dict | None = None
        renamed = name_error = ""
        requested, skipped = plan.name, []
        try:
            targetings, files = self._prepare(plan, out, writing=False)
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
                for ad in ([] if needs_manual_page(plan) else ad_set.ads):
                    data = files[ad.path]
                    media = ({"video_id": "<uploaded video>", "thumb": "<video thumbnail>"} if ad.kind == "video"
                             else {"image_hash": "<uploaded image>"})
                    ad = self._planned(ad, "<form to be created>")
                    if ad.vertical_path:
                        media["vertical"] = dict(media)
                    ads.append({"name": ad.name, "status": CHILD_STATUS, "format": ad.kind,
                                "file": Path(ad.path).name, "bytes": len(data),
                                **({"vertical_file": Path(ad.vertical_path).name,
                                    "vertical_bytes": len(files.get(ad.vertical_path, b""))}
                                   if ad.vertical_path else {}),
                                "library_name": (video_title(plan.tag, data) if ad.kind == "video"
                                                 else image_name(ad.path, data)),
                                "creative": (self.creative_params(plan, ad, ad_set, media, ad.name)
                                             if ad.uses_feed else self._story(plan, ad, ad_set, media))})
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
            out.problems = list(exc.problems)
        except GraphStop as exc:
            out.error, out.stop_kind = f"Graph API stopped: {_catalog.problem_line(exc)}", exc.kind
            out.problems = [graph_problem(exc)]
        except GraphError as exc:
            out.error, out.stop_kind = f"Meta refused: {_catalog.problem_line(exc)}", "graph_error"
            out.problems = [graph_problem(exc)]
        out.targeting = self._targeting_report()
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

    def _audience_summary(self, row: dict, plan: CampaignPlan, ad_set: AdSetPlan, t: dict) -> None:
        """Новые поля набора в «Check plan»; пустые не добавляют ключей (ACL-1767)."""
        if ad_set.gender:
            row["gender"] = GENDER_LABELS[ad_set.gender]
        if ad_set.languages:
            names = self.resolver.locale_names
            row["languages_picked"] = [names.get(x, x) for x in [ad_set.language or plan.language, *ad_set.languages] if x]
        if ad_set.device:
            row["devices"] = f"{ad_set.device} only"
        if ad_set.os_min:
            row["os_min"] = (t.get("user_os") or [""])[0]
        if ad_set.start_time:
            row["start_time"] = self.start_iso(ad_set)

    @staticmethod
    def _texts_summary(ad: AdPlan, files: dict[str, bytes]) -> dict:
        """Тексты и вертикальный файл в «Check plan»; у прежнего объявления ключей нет (ACL-1774)."""
        if not ad.uses_feed:
            return {}
        row: dict[str, Any] = {"primary_texts": list(ad.bodies), "headlines": list(ad.titles),
                               "descriptions": list(ad.descriptions)}
        if ad.vertical_path:
            row["stories_reels_file"] = Path(ad.vertical_path).name
            row["stories_reels_bytes"] = len(files.get(ad.vertical_path, b""))
            row["placements_by_file"] = "Stories and Reels use the vertical file; Feed uses the main file"
        return row

    def plan_summary(self, plan: CampaignPlan, targetings: list[dict], files: dict[str, bytes], *,
                     existing: bool = False, existing_differences: list[dict] | None = None,
                     requested_name: str = "") -> dict:
        """Что увидит человек в «Check plan»: читаемые поля, без сырых спецификаций."""
        sets = []
        for ad_set, t in zip(plan.ad_sets, targetings):
            interests = [[i.get("name") or i.get("id") for bucket in g.values() if isinstance(bucket, list)
                          for i in bucket] for g in (t.get("flexible_spec") or [])]
            sets.append({
                "name": ad_set.name, "status": CHILD_STATUS,
                "countries": list((t.get("geo_locations") or {}).get("countries") or []),
                "language": ad_set.language or plan.language, "locales": list(t.get("locales") or []),
                "age": f"{ad_set.age_min}-{ad_set.age_max}",
                "interests": interests,
                "os": ad_set.os or "All",
                "placements": placements_text(t),
                "creative_enhancements": "off: " + ", ".join(CREATIVE_FEATURES_OPT_OUT),
                "optimization": f"{OPTIMIZATION_GOAL} · {CUSTOM_EVENT} on the website pixel",
                "ads": [{
                    "index": ad.index, "name": ad.name, "status": CHILD_STATUS, "format": ad.kind,
                    "file": Path(ad.path).name, "bytes": len(files.get(ad.path, b"")),
                    "headline": ad.title, "primary_text": ad.body, "description": ad.description,
                    "link": plan.link, "cta": ad.cta,
                    **self._texts_summary(ad, files),
                } for ad in ad_set.ads],
                # Multi-advertiser ads всегда выключены (ACL-1767).
                "multi_advertiser": "off",
            })
            if len(plan.ad_sets) > 1:
                sets[-1]["index"] = ad_set.index        # ACL-1768: у одного набора ключа нет, как раньше
            self._audience_summary(sets[-1], plan, ad_set, t)
            if ad_set.dynamic:
                sets[-1]["creative_mode"] = "Dynamic Creative (Meta mixes the texts; one ad in this ad set)"
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
                        if ad.form_locale_note:
                            item["form_locale"] = ad.form_locale_note
                        if step is not None:
                            item["form_name"] = step.form_name
                            item["form_id"] = step.existing_id
                            item["form_action"] = step.action
                            item["form_note"] = (f"form will be created: {step.form_name}" if step.action == "create"
                                                 else f"form found on the Page, reused: {step.form_name}")
        summary = {
            "campaign": plan.name, "status": PAUSED, "children_status": CHILD_STATUS,
            "status_note": STATUS_NOTE, "objective": OBJECTIVE,
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
        if needs_manual_page(plan):
            summary["manual_required"] = [MANUAL_PAGE_NOTICE]
            for row in summary["ad_sets"]:
                row["ads_not_created"] = True
                row["pending_ad_count"] = len(row["ads"])
                row["ads"] = []
        if plan.page_id and self.facts.page_source_of(plan.page_id) == _pg.SOURCE_PROFILE:
            # ACL-2105: только для Страниц профиля, чтобы сайтовый план остался прежним (золотой файл).
            summary["page_origin"] = _pg.SOURCE_PROFILE
        if self.facts.page_names.get(plan.page_id, plan.page_id) != plan.page_id:
            summary["page_name"] = self.facts.page_names[plan.page_id]
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
                    lambda: (self._find_child(out.campaign_id, "adsets", ad_set.name) or {}).get("id"),
                    children_active=self._confirm_campaign_paused(out, plan))
                out.created.setdefault("adsets", []).append(adset_id)
                self._verify_label(out, f"The ad set {ad_set.name!r}", adset_id, plan.label)
                self._step(out, f"ad set {ad_set.name!r}: created ACTIVE (campaign PAUSED), tag kept")
            out.adset_ids.append(adset_id)

            if needs_manual_page(plan):
                continue

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
                if ad.vertical_path:
                    # ACL-1774: вертикальный файл грузится так же, до креатива; записи - в `rec` для сверки.
                    vdata = files[ad.vertical_path]
                    if ad.kind == "video":
                        vvid, vthumb = self._video(out, plan, ad, vdata, path=ad.vertical_path,
                                                   role=" Stories / Reels file")
                        media["vertical"] = {"video_id": vvid, "thumb": vthumb}
                        rec["vertical_video_id"] = vvid
                    else:
                        media["vertical"] = {"image_hash": self._image_hash(
                            out, plan, ad, vdata, path=ad.vertical_path, role=" Stories / Reels file")}
                        rec["vertical_image_hash"] = media["vertical"]["image_hash"]
                # Номер объявления в имени креатива: два объявления не делят креатив (M2). Номер сквозной по
                # кампании (ACL-1768): добавленное в первый набор объявление сдвигает номера следующих наборов,
                # и креатив, оставшийся от неполного запуска под старым номером, будет создан заново.
                # Объявления при этом находятся по имени внутри набора, так что дублей не возникает.
                creative_name = f"{ad.name} #{ad.index} [{plan.label}]"
                if ad.uses_feed:
                    payload = self.creative_params(plan, ad, ad_set, media, "")
                    for video in payload["asset_feed_spec"].get("videos", []):
                        # Meta's thumbnail URL may expire; the video ID binds the asset.
                        video.pop("thumbnail_url", None)
                    fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True,
                                                           ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()[:12]
                    creative_name = f"{ad.name} #{ad.index} {fingerprint} [{plan.label}]"
                creative_id = self._find_creative(plan, creative_name)
                if creative_id:
                    self._step(out, f"ad {ad.index}: creative found from a previous run")
                else:
                    # ACL-1776: форма из шаблона - ровно перед креативом, которому она нужна.
                    self._ensure_form(out, plan, ad)
                    creative_id = self._write(
                        out, f"ad {ad.index}: creative", f"/{plan.act}/adcreatives",
                        self.creative_params(plan, ad, ad_set, media, creative_name),
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
                     "status": CHILD_STATUS, "adlabels": [{"name": plan.label}]},
                    lambda: (self._find_child(adset_id, "ads", ad.name) or {}).get("id"),
                    children_active=self._confirm_campaign_paused(out, plan))
                out.ad_ids.append(ad_id)
                out.created.setdefault("ads", []).append(ad_id)
                rec.update({"ad_id": ad_id, "creative_id": creative_id})
                out.ad_media[ad.index] = rec
                if not ad_label_checked:
                    self._verify_label(out, f"The ad {ad.index}", ad_id, plan.label)
                    ad_label_checked = True
                self._step(out, f"ad {ad.index}: created ACTIVE (campaign PAUSED)")

        # 4. Сверка.
        out.readback = self.readback(plan, out, targetings, files)
        if needs_manual_page(plan):
            out.manual_required = [MANUAL_PAGE_NOTICE]
            self._step(out, "Manual completion required: add a Facebook Page and ads in Ads Manager")
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

    def _feed_checks(self, check: Callable[[str, str, Any, Any], None], aobj: str, plan: CampaignPlan,
                     ad: AdPlan, creative: dict, rec: dict, files: dict[str, bytes]) -> list[tuple[str, str, str]]:
        """Сверка ленты ассетов (ACL-1774): все тексты как набор и привязка файлов к площадкам.

        Возвращает картинки, чей хэш не равен md5 нашего файла, — по ним ещё сверяется имя в библиотеке.
        """
        fs = creative.get("asset_feed_spec") or {}
        if not isinstance(fs, dict):
            fs = {}

        def texts(key: str) -> list[str]:
            return [str(x.get("text") or "") for x in (fs.get(key) or []) if isinstance(x, dict)]

        check(aobj, "primary_texts", list(ad.bodies), texts("bodies"))
        check(aobj, "headlines", list(ad.titles), texts("titles"))
        check(aobj, "descriptions", list(ad.descriptions), texts("descriptions"))
        ctas = fs.get("call_to_action_types") or [""]
        check(aobj, "call_to_action", ad.cta, ctas[0] if isinstance(ctas, list) and ctas else "")
        links = fs.get("link_urls") or [{}]
        check(aobj, "link", plan.link, (links[0] or {}).get("website_url") if isinstance(links, list) and links else "")
        key = "videos" if ad.kind == "video" else "images"
        check(aobj, "format", ad.kind, "video" if fs.get("videos") else ("image" if fs.get("images") else ""))
        id_key = "video_id" if ad.kind == "video" else "hash"
        rows = [x for x in (fs.get(key) or []) if isinstance(x, dict)]

        def label_of(row: dict) -> str:
            raw = row.get("adlabels")
            if isinstance(raw, dict):
                raw = raw.get("data")
            first = raw[0] if isinstance(raw, list) and len(raw) == 1 else None
            return str(first.get("name") or "") if isinstance(first, dict) else ""

        by_label = {label_of(r): str(r.get(id_key) or "") for r in rows}
        bound = {"main": by_label.get(LABEL_MAIN) if ad.vertical_path else (str(rows[0].get(id_key) or "") if rows else "")}
        bound["vertical"] = by_label.get(LABEL_VERTICAL, "")
        check(aobj, "file_count", 2 if ad.vertical_path else 1, len(rows))
        if ad.vertical_path:
            check(aobj, "media_labels", [LABEL_MAIN, LABEL_VERTICAL], [label_of(r) for r in rows])
        else:
            check(aobj, "placement_rules", [], fs.get("asset_customization_rules") or [])
        extra: list[tuple[str, str, str]] = []
        wants = [("main", "main_file", ad.path, rec.get("video_id") or rec.get("image_hash"))]
        if ad.vertical_path:
            wants.append(("vertical", "stories_reels_file", ad.vertical_path,
                          rec.get("vertical_video_id") or rec.get("vertical_image_hash")))
        for slot, name, path, made in wants:
            got = bound[slot] or ""
            check(aobj, name + "_binding", True, bool(got))
            if made:
                check(aobj, name, str(made), got)
            data = files.get(path, b"")
            if not (data and got):
                continue
            if ad.kind == "video":
                title = self.client.get(f"/{got}", {"fields": "title"}).get("title")
                check(aobj, name + "_content", video_title(plan.tag, data), title)
            elif got != _md5(data):
                extra.append((aobj, path, got))
        if ad.vertical_path:
            # Какие площадки получили какой файл: правило на метку должно нести ровно наши площадки.
            main_pos, vert_pos = self.rule_positions()
            label_key = "video_label" if ad.kind == "video" else "image_label"
            rules: dict[str, dict] = {}
            for r in fs.get("asset_customization_rules") or []:
                lab = r.get(label_key) if isinstance(r, dict) else None
                if isinstance(lab, dict) and lab.get("name"):
                    cs = r.get("customization_spec") or {}
                    rules[str(lab["name"])] = {k: sorted(cs.get(k) or []) for k in
                                               ("publisher_platforms", "facebook_positions", "instagram_positions")}
            for label, want in ((LABEL_VERTICAL, vert_pos), (LABEL_MAIN, main_pos)):
                check(aobj, "placements_" + ("stories_reels" if label == LABEL_VERTICAL else "main"),
                      {k: sorted(v) for k, v in want.items()}, rules.get(label, {}))
        return extra

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
        # ACL-2050: диагностика (не блокирует): что Meta сказала про автоаудиторию и правила ставок.
        info = _advantage_info(camp)
        diag: dict[str, Any] = {"advantage_state": info.get("advantage_state"),
                                "advantage_placement_state": info.get("advantage_placement_state"),
                                "advantage_audience_state": info.get("advantage_audience_state"),
                                "adsets_with_value_rules": 0}
        diag["placement_state_verified"] = bool(info.get("advantage_placement_state"))
        if not diag["placement_state_verified"]:
            out.warnings.append("Meta did not return advantage_placement_state; placement mode was not verified "
                                "(check in Ads Manager).")
        if info.get("advantage_audience_state") == "ENABLED":
            out.warnings.append("Meta reports advantage_audience_state=ENABLED at campaign level; the ad sets "
                                "were sent with Advantage+ audience off (verify in Ads Manager).")

        # ACL-1775: форма принадлежит Странице - читаем список форм ещё раз, уже после записи.
        page_forms = (_forms.read_page_forms(self.client, plan.page_id)
                      if any(s.instant for s in plan.ad_sets) else None)
        image_ads: list[tuple[str, str, str]] = []    # (объект, путь файла, hash в объявлении)
        for ad_set, adset_id, targeting in zip(plan.ad_sets, out.adset_ids, targetings):
            obj = f"ad set {ad_set.name}"
            row = self.client.get(f"/{adset_id}", {"fields": ADSET_FIELDS})
            checks += self._adset_checks(plan, ad_set, targeting, row, out.campaign_id,
                                         reused=adset_id in (out.reused.get("adsets") or []))
            if row.get("value_rules_applied") or row.get("value_rule_set_id"):
                diag["adsets_with_value_rules"] += 1
                out.warnings.append(f"Meta applied bid value rules to {obj}; the builder did not set them.")

            if needs_manual_page(plan):
                continue  # Readback covers only the objects this run promised to create.

            ads = self.client.get_all(f"/{adset_id}/ads", {
                "fields": "id,name,status,adset_id,adlabels{name},"
                          "creative{id,adlabels{name},instagram_user_id,object_story_spec,degrees_of_freedom_spec,"
                          "contextual_multi_ads,asset_feed_spec}",
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
                check(aobj, "status", _child_status_expected(got.get("status"), bool(rec.get("reused"))),
                      got.get("status"))
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
                check(aobj, "multi_advertiser", OPT_OUT,
                      (creative.get("contextual_multi_ads") or {}).get("enroll_status"))
                spec = creative.get("object_story_spec") or {}
                check(aobj, "page", plan.page_id, str(spec.get("page_id") or ""))
                if self._ig_id:
                    # ACL-1896: Instagram-идентичность прочитана обратно; Meta отдаёт её в спецификации
                    # или полем креатива (живая проверка скажет, где именно).
                    check(aobj, "instagram_user_id", self._ig_id,
                          str(spec.get(_ig.IG_KEY) or creative.get(_ig.IG_KEY) or ""))
                if ad.uses_feed:
                    # ACL-1774: тексты и файлы в ленте ассетов, а не в object_story_spec.
                    image_ads += self._feed_checks(check, aobj, plan, ad, creative, rec, files)
                    continue
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
                    check(aobj, "call_to_action", ad.cta, cta.get("type"))
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
                        image_ads.append((aobj, ad.path, h))
                    check(aobj, "call_to_action", ad.cta, cta.get("type"))
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
            for aobj, path, h in image_ads:
                check(aobj, "image_file", image_name(path, files[path]), names.get(h, ""))
        mismatches = [c for c in checks if not c["ok"]]
        return {"ok": not mismatches, "checked": len(checks), "checks": checks, "mismatches": mismatches,
                "diagnostics": diag}


# ACL-2050: advantage_state_info и value_rules_* - только чтение, для сверки площадок после записи.
CAMPAIGN_FIELDS = ("name,objective,status,daily_budget,bid_strategy,adlabels{name},"
                   "advantage_state_info{advantage_state,advantage_placement_state,advantage_audience_state}")
ADSET_FIELDS = ("name,status,campaign_id,optimization_goal,billing_event,destination_type,"
                "promoted_object,targeting,start_time,end_time,is_dynamic_creative,adlabels{name},"
                "value_rules_applied,value_rule_set_id")
_PLACEMENT_FIELDS = ("publisher_platforms", "facebook_positions", "instagram_positions", "devices")
#: Не показываем как «отличие от билдера»: имя и метку доказал поиск, остальное — сверка после записи.
_SKIP_DIFF = {"name", "tag", "campaign_id", "schedule", "placements_summary"}
_FIELD_TITLES = {"dynamic_creative": "Dynamic Creative", "geo": "countries", "age_min": "minimum age", "age_max": "maximum age",
                 "languages": "languages", "detailed_targeting": "detailed targeting",
                 "os": "operating system", "pixel": "pixel", "conversion_event": "conversion event",
                 "optimization_goal": "optimization goal", "billing_event": "billing event",
                 "destination_type": "destination", "daily_budget": "daily budget",
                 "promoted_page": "Page of the ad set", "gender": "gender", "start_time": "start time",
                 "bid_strategy": "bid strategy"}


def _utc(value: Any) -> str:
    """Момент времени в UTC (для сверки старта: Meta отдаёт другое смещение/формат)."""
    try:
        dt = datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%S%z")
    except ValueError:
        return str(value or "")
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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


def _advantage_info(camp: dict) -> dict:
    info = camp.get("advantage_state_info")
    return info if isinstance(info, dict) else {}


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


def _child_status_expected(actual: Any, reused: bool) -> str:
    """Ожидаемый `status` набора/объявления (ACL-2114): созданные этим запуском - ACTIVE.

    Найденные с прошлого запуска мы не трогали: ACTIVE и PAUSED оба верны (прошлые запуски
    создавали их PAUSED). `effective_status` не сверяем: под кампанией на паузе он
    CAMPAIGN_PAUSED - это ожидаемо, а не расхождение.
    """
    return actual if reused and actual in (ACTIVE, PAUSED) else CHILD_STATUS


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
