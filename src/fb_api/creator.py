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
import math
import os
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from src.fb_api import error_catalog as _catalog
from src.fb_api import plain_errors as _plain
from src.fb_api import facts_cache as _facts_cache
from src.fb_api import form_cover_store as _cover_store
from src.fb_api import form_create as _fc
from src.fb_api import form_locale as _fl
from src.fb_api import form_templates as _ft
from src.fb_api import forms as _forms
from src.fb_api import targeting_resolver as _tr
from src.fb_api import instagram as _ig
from src.fb_api import write_policy as _wpol
from src.fb_api import team_time as _tt

from src.fb_api import creative_recovery as _cr
from src.fb_api import image_convert as _imgconv
from src.fb_api import name_receipt as _nr
from src.fb_api import operation_journal as _oj
from src.fb_api import pages as _pg
from src.fb_api.telemetry import mask_id as _mask_id
from src.fb_api.graph import FormPhotoRefused, GraphClient, GraphError, GraphStop, PageFailed, WritesBlocked, WriteUnknown, normalize_act
from src.fb_api.graph import REJECTION_SUBCODES as _REJECTION_SUBCODES
from src.fb_api.graph import FILE_REFUSAL_SUBCODES as _FILE_REFUSAL_SUBCODES

log = logging.getLogger(__name__)

PAUSED = "PAUSED"
#: ACL-2114: наборы и объявления создаются ACTIVE под кампанией на паузе - показа нет, пока человек
#: не включит кампанию. Только после чтения, подтвердившего PAUSED у кампании (`_confirm_campaign_paused`).
ACTIVE = "ACTIVE"
CHILD_STATUS = ACTIVE
#: Паузы (с) между повторными чтениями метки объекта, которого Meta ещё «не показывает» (только чтения).
#: Каждое чтение идёт через клиента и считается в потолке вызовов (CallBudget останавливает громко).
LABEL_RECHECK_WAITS = (2.0, 4.0, 6.0)
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
#: ACL-1443: тип бюджета кампании.
BUDGET_DAILY, BUDGET_LIFETIME = "daily", "lifetime"
DAY_NAMES = ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat")
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

#: Прежний фиксированный запас на остальную кампанию в Check plan (ACL-1891); с ACL-2370 не используется -
#: остаток считает `estimate_create_calls`. Имя оставлено для старых тестов.
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
CALLS_PREPARE_EXTRA_READS = 4   # ACL-2244: page / profile-page checks and similar reads of `_prepare` outside the targeting reserve


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
        # ACL-2571: one upload of the template's picture to the campaign's Page per such template.
        total += len({a.form_template["id"] for x in plan.ad_sets for a in x.ads
                      if a.form_template and a.form_template.get("cover_local_sha256")})
    return total


#: ACL-2370: запас на повторы кусков (ACL-2000, докачка со смещения): 5% кусков, не меньше двух вызовов.
CHUNK_RETRY_SLACK_FRACTION = 0.05
CHUNK_RETRY_SLACK_MIN = 2


def video_files_of(plan: "CampaignPlan") -> list[str]:
    """Видеофайлы плана без повторов: главный и вертикальный (Stories / Reels) каждого видео-объявления.

    Один путь в нескольких объявлениях грузится ОДИН раз: `_video` ищет видео в кабинете по заголовку
    (тег + md5), и второе объявление находит уже загруженное. Поэтому считаем по уникальному пути.
    """
    seen: dict[str, None] = {}
    for ad_set in plan.ad_sets:
        for ad in ad_set.ads:
            if ad.kind == "video":
                seen.setdefault(ad.path, None)
            if ad.vertical_path and ad.vertical_kind == "video":
                seen.setdefault(ad.vertical_path, None)
    return list(seen)


def estimate_upload_calls(plan: "CampaignPlan", size_of: Callable[[str], int] | None = None) -> int:
    """Сколько вызовов Graph уйдёт на загрузку больших видео кусками (ACL-2370), с запасом на повторы.

    Файлы до 200 МБ уходят одним запросом и сюда не входят (их несут 8 опросов обработки в
    `estimate_create_calls`). Верхняя оценка: проверка плана читает исходный файл, а запуск может
    сжать его до загрузки.
    """
    size_of = size_of or _disk_size
    chunks = sum(upload_calls(size_of(path)) for path in video_files_of(plan))
    if not chunks:
        return 0
    return chunks + max(CHUNK_RETRY_SLACK_MIN, math.ceil(CHUNK_RETRY_SLACK_FRACTION * chunks))


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
#: ACL-2000: кусок, который страница не успела принять (`no_file: ... did not answer`), повторяется с тем же
#: смещением и той же сессией: до 3 раз, паузы 10/20/40 с. Дальше - стоп с прежним сообщением.
CHUNK_RETRY_BACKOFF_S = (10.0, 20.0, 40.0)
#: Пауза режется на доли, чтобы Stop и сторож тишины слышали жизнь задания.
CHUNK_RETRY_SLICE_S = 5.0
#: Страниц списка в ПОВТОРНЫХ чтениях сверки загрузки (поиск до загрузки и первое чтение - полные).
UPLOAD_RECONCILE_PAGES = 3
#: Сколько страниц (по 100) свежих креативов кабинета смотреть в поиске своего.
CREATIVE_LOOKUP_PAGES = 3
#: An unanswered creative-send receipt blocks for 15 minutes; after that the full listing decides.
STALE_WARNING = "An earlier creative with other media was found and not reused; a new creative was created."
STALE_WARNING_REUSED = ("An earlier creative with other media was found and not reused; "
                        "a matching creative was used instead.")
ORDER_WARNING = "The creative carries the same files in another order; accepted."
RECEIPT_UNKNOWN_S = 900
#: Тексты и вертикальный файл (ACL-1774).
MAX_TEXTS = _wpol.ASSET_FEED_MAX_TEXTS
LABEL_MAIN = "acl_main"
LABEL_VERTICAL = "acl_vertical"
#: Соотношение сторон w/h: основной файл — 1:1 или 4:5, Stories / Reels — 9:16 или 1:1 (ACL-2084), допуск 1,5 %.
MAIN_RATIOS = (1.0, 0.8)
VERTICAL_RATIOS = (9 / 16, 1.0)
RATIO_TOLERANCE = 0.015
#: Площадки вертикального файла (Stories и Reels); остальные из ручного списка — основному файлу.
VERTICAL_FACEBOOK = ("story", "facebook_reels")
VERTICAL_INSTAGRAM = ("story", "reels")
#: Значения-заглушки для проверки политикой до записи (настоящие приходят после загрузки).
PLACEHOLDER_HASH = "0" * 32
PLACEHOLDER_VIDEO_ID = "100000"
#: Цель кампании из билдера, которую умеет API-путь (как у RPA: Leads → сайт).
SUPPORTED_OBJECTIVES = frozenset({"leads"})

#: ACL-2250: повтор имени получает счётчик в конце: `<имя> copy <n>` (у кампании и всех её наборов).
COPY_WORD = " copy "
#: Остановки выбора имени: в Check plan это ERROR правила имени, а не сбой плана.
#: ACL-2583 / ACL-2578 (owner decision): a read by id that keeps failing is never a stop. It is retried (about 30 s in
#: total: one try plus three pauses), and if Meta still does not answer the publish continues as a NEW operation under a
#: distinct `<name> copy <n>` name. A possible duplicate is accepted; the same name is never reused.
TRANSIENT_READ_WAITS_S = (10.0, 10.0, 10.0)
MSG_CONTINUED_AS_COPY = ('Meta did not answer when ACL checked the earlier campaign of this publish, so this publish '
                         'continues as a new campaign named "{name}". If the earlier campaign is still in Ads Manager, '
                         "it is left as it is.")
MSG_CAMPAIGN_REWRITTEN = ('The earlier attempt to create the campaign "{old}" got no answer from Meta, and ACL could not '
                          'find it afterwards. This publish created a new campaign named "{name}". A possible '
                          'duplicate "{old}" (PAUSED) may exist in Ads Manager; it is left as it is.')
MSG_CAMPAIGN_UNKNOWN_STOP = ('Meta did not answer the campaign write several times in a row, and ACL could not find the '
                             'campaign "{name}" afterwards. Nothing else was written. Check the campaigns of this '
                             'ad account in Ads Manager (possible PAUSED duplicates) before publishing again.')
MAX_CAMPAIGN_REWRITES = 5
NAME_STOPS = ("name_taken", "name_unsure", "name_too_long", "name_receipt")
#: Столько строк даёт поиск кампаний по подстроке (2 страницы по 100): при полной выдаче
#: копий за её пределами могло не быть видно - тогда ищем точнее.
CAMPAIGN_SEARCH_ROWS = 200
#: Консервативная защита от слишком длинного имени кампании и набора (точный предел Meta не проверен:
#: для наборов в источниках 400 или 100). Единственная константа предела - ACL-2250, D6.
CAMPAIGN_NAME_MAX = 400

#: Что таблица показывает (архив и удалённое не считаем «существующим»).
LIVE_STATES = ["ACTIVE", "PAUSED", "PENDING_REVIEW", "DISAPPROVED", "PREAPPROVED",
               "PENDING_BILLING_INFO", "CAMPAIGN_PAUSED", "ADSET_PAUSED", "IN_PROCESS",
               "WITH_ISSUES"]


from src.fb_ads_rpa.draft import MAX_AD_SETS, MAX_AD_SETS_ERROR  # noqa: E402
from src.fb_ads_rpa.draft import share_first_headline as _share_first_headline  # noqa: E402

#: Id-пустышка формы для проверки политики и показа плана (формы ещё нет; 5-25 цифр).
FORM_ID_PLACEHOLDER = "0000000"


def _wp_form_edge() -> str:
    from src.fb_api import write_policy as _wp

    return _wp.FORM_EDGE


class PlanError(ValueError):
    """Черновик нельзя создать через API; к Meta ничего не писали."""

    #: Список проблем для дашборда (как у RPA, ACL-1437); у обычного отказа плана пуст.
    problems: tuple = ()
    #: ACL-2392: key of `plain_errors.TABLE` when the raise site knows the class; empty -> classify by text.
    key: str = ""

    def __init__(self, *args: Any, key: str = ""):
        super().__init__(*args)
        if key:
            self.key = key


class RulesViolation(PlanError):
    """Задание нарушает реестр правил Meta (ACL-1805): стоп до первой записи."""

    def __init__(self, message: str, problems: list[dict]):
        super().__init__(message, key="meta_rule")
        self.problems = problems


class CreationStopped(RuntimeError):
    """Создание кампании остановлено; что уже создано — в `created`."""

    def __init__(self, message: str, *, created: dict | None = None, kind: str = "stopped",
                 failure: Any = None, not_found: bool = False):
        super().__init__(message)
        self.created = dict(created or {})
        self.kind = kind
        #: ACL-1856: an unknown write that the readback looked for and did not find (not "could not look").
        self.not_found = not_found
        #: ACL-2392: the plain text for the buyer (`plain_errors.Failure`); None -> classified from the message.
        self.failure = failure


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
    #: ACL-1772: точный выбор набора для правил двух файлов и их сверки.
    placements: dict | None = None

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
    #: ACL-1443 (lifetime): конец и расписание; пусто = как у daily (ключей в параметрах нет).
    end_time: str = ""               # "YYYY-MM-DDTHH:MM" по часовому поясу кабинета
    schedule: list = field(default_factory=list)   # [{"days": [0..6], "start_minute", "end_minute"}]
    #: ACL-1448: пояс, в котором введены старт, конец и сетка (пояс команды); только у lifetime, у daily пусто
    #: и время по-прежнему читается в поясе кабинета.
    time_zone: str = ""
    #: ACL-1768: номер набора и поля уровня набора; пусто = как у кампании.
    index: int = 1                   # 1-based порядковый номер набора в кампании
    language: str = ""               # язык набора вместо языка кампании
    pixel_id: str = ""               # пиксель набора вместо пикселя кампании

    placements: dict | None = None

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
    #: ACL-1443: "daily" (как всегда) | "lifetime"; при lifetime сумма на весь срок - `lifetime_budget`,
    #: а `daily_budget` равен 0.
    budget_type: str = BUDGET_DAILY
    lifetime_budget: float = 0.0
    #: ACL-2250: метка имени из билдера (до счётчика): видео копии берётся из библиотеки по ней, без повторной загрузки.
    media_tag: str = ""

    @property
    def label(self) -> str:
        return f"acl-{self.tag}"

    @property
    def lifetime(self) -> bool:
        return self.budget_type == BUDGET_LIFETIME

    @property
    def budget_amount(self) -> float:
        """Сумма бюджета того типа, что задан (в валюте кабинета)."""
        return self.lifetime_budget if self.lifetime else self.daily_budget

    @property
    def has_schedule(self) -> bool:
        return any(s.schedule for s in self.ad_sets)

    @property
    def video_tag(self) -> str:
        return self.media_tag or self.tag


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
    """Основной файл 1:1 или 4:5, Stories / Reels 9:16 или 1:1 — проверка ДО любой загрузки (ACL-1774, ACL-2084).

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
    elif not any(_near(v[0] / v[1], r) for r in VERTICAL_RATIOS):
        out.append(f"Ad {index}: the Stories / Reels file is {v[0]}x{v[1]}; it must be 9:16 "
                   "(for example 1080x1920) or 1:1 (for example 1080x1080)")
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


def placement_targeting(placements: dict | None) -> dict:
    """None — прежний ручной список; Advantage+ — без ограничений платформ/позиций."""
    if placements is None:
        return {"publisher_platforms": list(PUBLISHER_PLATFORMS),
                "facebook_positions": list(FACEBOOK_POSITIONS),
                "instagram_positions": list(INSTAGRAM_POSITIONS)}
    if placements["mode"] == "advantage_plus":
        return {}
    out = {k: list(placements[k]) for k in ("facebook_positions", "instagram_positions") if placements[k]}
    out["publisher_platforms"] = [p for p in PUBLISHER_PLATFORMS if out.get(p + "_positions")]
    return out


def _plan_placements(src: Any, problems: list[str], name: str) -> dict | None:
    raw = getattr(src, "placements", None)
    if raw is None:
        return None
    error = f"Ad set {name!r}: Placement selection is invalid; choose Advantage+ or supported Manual positions"
    if not isinstance(raw, dict) or raw.get("mode") not in ("manual", "advantage_plus"):
        problems.append(error)
        return None
    if raw["mode"] == "advantage_plus":
        if set(raw) != {"mode"}:
            problems.append(error)
        return {"mode": "advantage_plus"}
    out = {"mode": "manual"}
    if set(raw) - {"mode", "facebook_positions", "instagram_positions"}:
        problems.append(error)
    for key, supported in (("facebook_positions", FACEBOOK_POSITIONS), ("instagram_positions", INSTAGRAM_POSITIONS)):
        values = raw.get(key, [])
        if not isinstance(values, list) or any(not isinstance(v, str) or v not in supported for v in values):
            problems.append(error)
            values = []
        out[key] = [v for v in supported if v in values]
    if not out["facebook_positions"] and not out["instagram_positions"]:
        problems.append(error)
    # Ограничение комбинации отдельно от необязательного вертикального файла.
    # https://developers.facebook.com/docs/marketing-api/audiences/reference/placement-targeting/
    if ('story' in out['facebook_positions'] and 'feed' not in out['facebook_positions']
            and 'story' not in out['instagram_positions']):
        problems.append(f"Ad set {name!r}: Facebook Stories placements require Facebook Feed or Instagram Stories; "
                        "add one of these placements or turn off Facebook Stories")
    return out


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
    elif device == "desktop" and placement_targeting(plan_set.placements) != {"publisher_platforms": ["facebook"], "facebook_positions": ["feed"]}:
        # ACL-1772: проверенная Desktop-комбинация — только ручной Facebook Feed.
        # Другие комбинации не объявляем поддержанными без проверки возможностей API.
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


def _plan_period(plan_set: AdSetPlan, src: Any, lifetime: bool, problems: list[str]) -> None:
    """Lifetime (ACL-1443): старт и конец набора обязательны, расписание - сетка окон. Ошибки вслух.

    При daily конец и расписание не принимаются (Meta: срок < 24 ч у daily отвергается, а расписание
    работает только с lifetime). Поля плана остаются пустыми - прежние планы не меняются.
    """
    name = plan_set.name
    end = str(getattr(src, "end_time", "") or "").strip()
    raw = getattr(src, "schedule", None) or []
    if lifetime:
        # ACL-1448: время вводят по поясу команды; пояс задаёт сервер, из формы он не приходит.
        plan_set.time_zone = _tt.team_timezone()
    if not lifetime:
        if end:
            problems.append(f"Ad set {name!r}: an end time needs a lifetime budget")
        if raw:
            problems.append(f"Ad set {name!r}: a schedule needs a lifetime budget")
        return
    if not plan_set.start_time:
        problems.append(f"Ad set {name!r}: a lifetime budget needs a start time")
    if not end:
        problems.append(f"Ad set {name!r}: a lifetime budget needs an end time")
    else:
        try:
            if not _START_RE.match(end):
                raise ValueError(end)
            end_dt = datetime.strptime(end, "%Y-%m-%dT%H:%M")
            plan_set.end_time = end
            if plan_set.start_time and end_dt <= datetime.strptime(plan_set.start_time, "%Y-%m-%dT%H:%M"):
                problems.append(f"Ad set {name!r}: the end time {end} must be after the start time {plan_set.start_time}")
        except ValueError:
            problems.append(f"Ad set {name!r}: end time {end!r} is not a date and time")
    if not raw:
        return
    if not isinstance(raw, list):
        problems.append(f"Ad set {name!r}: the schedule must be a list of windows")
        return
    if all(isinstance(w, dict) and not w.get("days") for w in raw):
        problems.append(f"Ad set {name!r}: the schedule is empty; select at least one hour or turn the schedule off")
        return
    for w in raw:
        why = _wpol.schedule_window_problem(w, with_tz=False)
        if why:
            problems.append(f"Ad set {name!r}: {why}")
            return
    plan_set.schedule = [{"days": sorted(w["days"]), "start_minute": w["start_minute"],
                          "end_minute": w["end_minute"]} for w in raw]


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
    lifetime = (str(getattr(ad_sets_src[0], "budget_type", "") or "").strip().lower() == BUDGET_LIFETIME
                if ad_sets_src else False)
    if not budget or float(budget) <= 0:
        problems.append("Lifetime budget missing" if lifetime else "Daily budget missing")
    # ACL-2406: заголовок и описание всей кампании задаёт Ad 1; у объявлений 2+ свои только тексты.
    _share_first_headline(draft)
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
        plan_set.placements = _plan_placements(s, problems, plan_set.name)
        _plan_audience(plan_set, s, problems)
        _plan_period(plan_set, s, lifetime, problems)
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
            planned_ad = plan_set.ads[-1]
            planned_ad.placements = plan_set.placements
            if plan_set.placements is not None:
                targets = placement_targeting(plan_set.placements)
                has_vertical = not targets or any(
                    x in targets.get("facebook_positions", []) for x in VERTICAL_FACEBOOK) or any(
                    x in targets.get("instagram_positions", []) for x in VERTICAL_INSTAGRAM)
                if has_vertical and not planned_ad.vertical_path:
                    warnings.append(f"Ad {i}: no separate vertical Stories / Reels file; Meta will crop the main file")
                if planned_ad.vertical_path:
                    main_pos, vert_pos = CampaignCreator.rule_positions(plan_set.placements)
                    if not targets:
                        problems.append(f"Ad {i}: Advantage+ placements with two files is not supported; use one file or Manual placements")
                    elif not main_pos or not vert_pos:
                        # Один тип площадок не требует непроверенных правил для пустой второй группы.
                        problems.append(f"Ad {i}: the current app mapping for a two-file pair needs both Feed and Stories / Reels placements; clear the unused file and use the required file as main")
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
                        if clean.get("cover_local_sha256") and not _cover_store.exists(clean["id"], clean["cover_local_sha256"]):
                            # ACL-2571: the picture file is on another computer. Never a wrong Page's photo:
                            # this form is created (or found) as the same template without a background.
                            warnings.append(f"Ad {i}: {_fc.MSG_COVER_ON_OTHER_COMPUTER.format(name=clean['name'])}")
                            clean = {k: v for k, v in clean.items() if k != "cover_local_sha256"}
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
        # ACL-2538: the same file with the same text twice is not a buyer error:
        # the later ads get " 2", " 3"... and Check plan shows a note.
        seen: dict[str, int] = {}
        for a in x.ads:
            if a.name not in seen:
                seen[a.name] = a.index
        used = set(seen)
        counts: dict[str, int] = {}
        for a in x.ads:
            first = seen[a.name]
            if a.index == first:
                continue
            base = a.name
            n = counts.get(base, 1)
            while True:
                n += 1
                cand = f"{base} {n}"
                if cand not in used:
                    break
            counts[base] = n
            used.add(cand)
            a.name = cand
            warnings.append(f"Ad {a.index} named {cand!r} because Ad {first} has the same creative and text")
    if problems:
        raise PlanError("; ".join(problems))
    name = str(draft.name).strip()
    return CampaignPlan(
        name=name, act=act, tag=campaign_tag(act, name), country=str(draft.country or "").strip(),
        language=str(getattr(draft, "language", "") or "").strip(), link=link,
        page_id=str(getattr(draft, "page_id", "") or "").strip(),
        pixel_id=str(getattr(draft, "pixel_id", "") or "").strip(),
        daily_budget=0.0 if lifetime else float(budget), ad_sets=ad_sets,
        page_source=_source(draft, "page"), pixel_source=_source(draft, "pixel"),
        warnings=warnings,
        **({"budget_type": BUDGET_LIFETIME, "lifetime_budget": float(budget)} if lifetime else {}))


def _minor_or_none(value: Any) -> int | None:
    """Сумма в мелких единицах из ответа Meta ("100" -> 100); нет или не число - None."""
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def minor_to_major(minor: int, currency: str) -> float:
    """Мелкие единицы -> единицы валюты кабинета (центы /100, ZERO_DECIMAL - как есть)."""
    return float(minor) if (currency or "").upper() in ZERO_DECIMAL else minor / 100


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
    #: Минимальный дневной бюджет кабинета в мелких единицах (`min_daily_budget` Meta); None - не прочитан (ACL-1443).
    min_daily_budget: int | None = None
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
        _log_account_state(act, None)
        raise PlanError("This profile has no access to the ad account from the account map "
                        "(not in /me/adaccounts). Nothing was created.")
    if str(row.get("account_status")) != "1":
        _log_account_state(act, row)
        raise PlanError(f"The ad account is not active (account_status {row.get('account_status')}). "
                        "Nothing was created.", key="account_blocked")
    acc = client.account(act)
    got = str(acc.get("account_id") or "").strip()
    if got != digits:      # ACL-2267: пустой account_id в ответе - тоже несовпадение
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
                         utc_offset_hours=offset, timezone_name=str(acc.get("timezone_name") or "").strip(),
                         min_daily_budget=_minor_or_none(acc.get("min_daily_budget")))
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
    _log_account_state(act, row, acc, facts)
    return facts


def _log_account_state(act: str, row: dict | None, acc: dict | None = None, facts=None) -> None:
    """Состояние кабинета в общий журнал (ACL-2347); новых запросов к Meta нет, сбой молчит."""
    try:
        from src.acl_event_log import emit
        from src.acl_event_log.events import account_payload

        emit("account.state", account_payload(act, row, acc, facts), account=act)
    except Exception:
        pass


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
                            "This account cannot advertise using that Page. Refresh pages and choose again.", key="page_unavailable")
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
            raise PlanError(f"The pixel of ad set {s.name!r} is not on this ad account.", key="pixel_unavailable")
    if plan.pixel_id:
        # То же для пикселя. Читается первая страница (50): больше — безопасный отказ.
        if plan.pixel_id not in facts.pixels:
            src = plan.pixel_source or "draft"
            raise PlanError(f"The pixel from the {src} ({plan.pixel_id}) is not on this ad account "
                            f"({len(facts.pixels)} found). Fix the pixel instead of letting another one be picked.", key="pixel_unavailable")
    elif all(s.pixel_id for s in plan.ad_sets if not s.instant):
        return
    elif len(facts.pixels) == 1:
        plan.pixel_id = facts.pixels[0]
        plan.pixel_source = "account default"
    else:
        raise PlanError(f"The ad account has {len(facts.pixels)} pixels/datasets; "
                        "the API backend needs exactly one", key="pixel_unavailable")


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
                 search_cache: "_tr.SearchCache | None" = None, cache_scope: str = "", facts: Any = None,
                 act: str = ""):
        self.client = client
        #: ACL-2267: долгий кэш разобранных терминов (страна, язык, интерес); по умолчанию - кэш машины.
        self._facts = facts if facts is not None else _facts_cache.default()
        #: Ключи долгого кэша, использованные этим резолвером: при отказе Meta по таргетингу сбрасываются.
        self._touched: set[str] = set()
        self._facts_hits = 0
        self._geo: dict[str, str] = {}
        self._locale: dict[str, int] = {}
        #: Имя, под которым Meta нашла язык («Spanish (All)»): показываем в Check plan.
        self.locale_names: dict[str, str] = {}
        #: Детальный таргетинг (ACL-1771): свой поиск, кэш подтверждённых и бюджет вызовов.
        self.terms = _tr.TermResolver(client, store, cache=search_cache, scope=cache_scope, facts=self._facts,
                                      act=act)
        #: ACL-1907: группа «X (All)» -> локали, на которые её раскрывает Meta (из того же поиска
        #: adlocale, без зашитых id). Пусто = состав неизвестен, сверка остаётся строгой.
        self.locale_groups: dict[int, dict[int, str]] = {}

    def country(self, value: str) -> str:
        key = (value or "").strip()
        if not key:
            raise PlanError("No country in the draft")
        if key.upper() in self._geo:
            return self._geo[key.upper()]
        up = key.upper()
        wkey = self._facts.term_key("country", _tr.norm(key))
        warm = self._facts.get_term(wkey)
        if warm and re.fullmatch(r"[A-Z]{2}", str(warm.get("code") or "")):
            self._touched.add(wkey)
            self._facts_hits += 1
            self._geo[up] = str(warm["code"])
            return self._geo[up]
        code = ""
        # Для двухбуквенного кода «q=AR» отдаёт десятки стран с «ar» в названии, и нужная может
        # выпасть из страницы, поэтому: сначала код с широкой выдачей, затем английские названия.
        # Совпадение только точное (country_code == код); близкое никогда не берём (ACL-1761).
        if len(key) == 2 and key.isalpha():
            queries = [(key, 100, 3)] + [(n, 25, 1) for n in _country_names_for(up)]
        else:
            queries = [(key, 10, 1)]
        for q, limit, pages in queries:
            rows = _tr.named_search("adgeolocation", lambda: self.client.get_all(
                "/search", {"type": "adgeolocation", "location_types": ["country"], "q": q, "limit": limit},
                max_pages=pages))
            for r in rows:
                cc = str(r.get("country_code") or r.get("key") or "").upper()
                if cc == up or str(r.get("name") or "").casefold() == key.casefold():
                    code = cc
                    break
            if code:
                break
        if not code:
            raise PlanError(f"Country {key!r} was not found by Meta targeting search", key="targeting_term")
        self._geo[key.upper()] = code
        self._facts.put_term(wkey, {"code": code})
        self._touched.add(wkey)
        return code

    def drop_cached(self) -> int:
        """Meta отвергла таргетинг: забыть всё, что этот резолвер взял из долгого кэша или положил в него."""
        keys = sorted(self._touched | self.terms.touched_keys)
        self._facts.drop_terms(keys)
        self._touched.clear()
        self.terms.touched_keys.clear()
        return len(keys)

    def facts_stats(self) -> dict:
        """Счётчики долгого кэша за задание (для телеметрии Check plan, без имён терминов)."""
        st = dict(self.terms.facts_stats)
        st["warm_hits"] += self._facts_hits
        st["targeting_reads"] = self.terms.reads
        return st

    def locale(self, language: str) -> int | None:
        lang = (language or "").strip()
        if not lang:
            return None
        if lang in self._locale:
            return self._locale[lang]
        wkey = self._facts.term_key("locale", _tr.norm(lang))
        warm = self._facts.get_term(wkey)
        if warm and re.fullmatch(r"[0-9]+", str(warm.get("id") or "")):
            self._touched.add(wkey)
            self._facts_hits += 1
            self._locale[lang] = int(warm["id"])
            self.locale_names[lang] = str(warm.get("name") or lang)
            groups = warm.get("groups") if isinstance(warm.get("groups"), dict) else {}
            if groups or self.locale_names[lang].casefold().endswith(" (all)"):
                # группа «X (All)» без известных членов тоже группа: без пометки сравнение станет нестрогим
                self.locale_groups[self._locale[lang]] = {int(k): str(v) for k, v in groups.items()
                                                          if re.fullmatch(r"[0-9]+", str(k))}
            return self._locale[lang]
        rows = _tr.named_search("adlocale", lambda: self.client.get_all(
            "/search", {"type": "adlocale", "q": lang, "limit": 25}, max_pages=1))
        pick = None
        for want in (f"{lang} (All)", lang):
            pick = next((r for r in rows if str(r.get("name") or "").casefold() == want.casefold()), None)
            if pick:
                break
        if not pick:
            raise PlanError(f"Audience language {lang!r} was not found by Meta targeting search", key="targeting_term")
        self._locale[lang] = int(pick["key"])
        self.locale_names[lang] = str(pick.get("name") or lang)
        pick_name = str(pick.get("name") or "")
        if pick_name.casefold().endswith(" (all)"):
            base = pick_name[:-len(" (All)")].strip().casefold()
            self.locale_groups[self._locale[lang]] = {
                int(r["key"]): str(r.get("name") or "") for r in rows
                if r.get("key") is not None and r is not pick
                and str(r.get("name") or "").strip().casefold() != pick_name.casefold()
                and (str(r.get("name") or "").strip().casefold() == base
                     or str(r.get("name") or "").strip().casefold().startswith(base + " ("))}
        self._facts.put_term(wkey, {"id": str(self._locale[lang]), "name": self.locale_names[lang],
                                    "groups": {str(k): v for k, v in
                                               self.locale_groups.get(self._locale[lang], {}).items()}})
        self._touched.add(wkey)
        return self._locale[lang]

    def interests(self, groups: list[list[str]]) -> list[dict]:
        """Группы имён → `flexible_spec` (interests/behaviors/work_*/education_* с id).

        Точное имя берётся само; всё остальное — `TargetingUnresolved` с кандидатами и
        с «picked X for Y?» (ACL-1771): ничего не подставляется молча.
        """
        terms = [n for g in groups for n in g]
        try:
            self.terms.prefetch(terms)
            results = {n: self.terms.resolve(n) for n in terms}
        except _tr.ConfirmedStoreUnreadable as exc:
            # ACL-2243: unknown store state must stop the plan, not resolve as if nothing was confirmed.
            raise PlanError(str(exc)) from exc
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

    def targeting_warnings(self) -> list[str]:
        """ACL-1920: необязательные поиски, в которых Meta отказала (имя поиска в тексте)."""
        return self.terms.warnings()


def _placement_devices(ad_set: AdSetPlan) -> list[str]:
    """Тот же фактический список устройств для API и агрегатной сверки режима площадок."""
    if ad_set.os.casefold() in ("ios", "android"):
        return ["mobile"]
    return [ad_set.device] if ad_set.device else ["mobile", "desktop"]


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
    t.update(placement_targeting(ad_set.placements))
    os_val = ad_set.os.casefold()
    if os_val in ("ios", "android"):
        # Как в билдере (решение по Android — за владельцем, пока как было).
        t["user_os"] = [user_os_value(os_val, ad_set.os_min)]
    t["device_platforms"] = _placement_devices(ad_set)
    return t


def user_os_value(os_val: str, os_min: str = "") -> str:
    """«iOS» / «Android», с версией - «iOS_ver_14.0_and_above» (Graph «Targeting», user_os)."""
    name = "iOS" if os_val.casefold() == "ios" else "Android"
    if not os_min:
        return name
    ver = os_min if "." in os_min else os_min + ".0"
    return f"{name}_ver_{ver}_and_above"


def plan_has_instagram(targetings: list[dict]) -> bool:
    """Любой автоматический или явно выбранный Instagram-набор требует проверки identity."""
    return any(not t.get("publisher_platforms") or "instagram" in t["publisher_platforms"]
               for t in targetings)


def plan_has_instagram_placements(plan: "CampaignPlan") -> bool:
    """До чтения таргетинга: автоматические площадки могут включать Instagram."""
    return plan_has_instagram([placement_targeting(s.placements) for s in plan.ad_sets])


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


def image_name(path: str, data: bytes, ext: str = "") -> str:
    """Имя картинки в библиотеке кабинета: `acl-<md5[:16]><ext>` (помощник его не меняет).

    `ext` (ACL-2575): расширение после локальной конвертации WebP -> PNG/JPEG; иначе - от исходного файла."""
    ext = ext or Path(path).suffix.lower()
    ext = ext if ext in IMAGE_EXT else ".jpg"
    return f"acl-{_md5(data)[:16]}{ext}"


_STORED_SUFFIX = re.compile(r"(?:_\d+)+$")


def stored_image_name(expected: str, actual: str) -> str:
    """ACL-2578: Meta may append `_<digits>` to the library name of an uploaded image (`acl-<md5>.png_105`).

    Uploading the same image twice can add the suffix again (`acl-<md5>.png_105_105`). It is still our file: the
    suffixes alone are not a difference. Any other name stays as Meta returned it.
    """
    text = str(actual or "")
    if text != expected and _STORED_SUFFIX.sub("", text, count=1) == expected:
        return expected
    return text


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
    creative_ids: list[str] = field(default_factory=list)
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
    #: ACL-2392: plain-English version of `error` (`plain_errors.Failure`); `error` stays raw for logs and gating.
    failure: Any = None
    #: ACL-2250: (кабинет, имя из билдера) квитанции, когда сверка дошла до конца без остановки; конец задания
    #: (`publisher.finish_name_records`) удаляет её. Пусто - квитанция остаётся, следующий запуск допишет эту же кампанию.
    name_receipt_done: tuple = ()
    #: ACL-2250: номер счётчика в имени (0 - имя из билдера); в строку телеметрии.
    copy_number: int = 0
    #: ACL-1856: сводка журнала операции (id, состояния этапов, подтверждённые id); пусто без журнала.
    operation: dict = field(default_factory=dict)

    @property
    def mismatches(self) -> list[dict]:
        return list(self.readback.get("mismatches") or [])


class _AliveUnknown(Exception):
    """ACL-2583: the journal's campaign could not be read after the retries; `copy_number` is its name's counter."""

    def __init__(self, copy_number: int, campaign_id: str) -> None:
        super().__init__(campaign_id)
        self.copy_number = copy_number
        self.campaign_id = campaign_id


class _SwitchCopy(Exception):
    """ACL-2583: the journal's operation already wrote (or may have written) the campaign under copy name `n`, but the
    name choice lags behind Meta's search and picked a lower counter. The run continues under copy `n`."""

    def __init__(self, copy_number: int) -> None:
        super().__init__("switch copy")
        self.copy_number = copy_number


class _CampaignRewrite(Exception):
    """ACL-2583: the campaign write's result stayed unknown and the readbacks found nothing. The name search is exactly
    what lags behind Meta, so the caller must not write the campaign again under the SAME name."""

    def __init__(self, copy_number: int) -> None:
        super().__init__("campaign rewrite")
        self.copy_number = copy_number


@dataclass
class NameChoice:
    """Итог выбора имени кампании (ACL-1902, ACL-2250): план с итоговым именем и меткой, до первой записи."""
    plan: CampaignPlan
    #: Своя кампания, которую дописываем (возобновление после обрыва) - её переиспользуем; иначе None.
    existing: dict | None = None
    found_sets: list = field(default_factory=list)
    #: Номер счётчика: 0 - имя из билдера, n - `<имя> copy <n>`.
    copy_number: int = 0
    #: Сколько кампаний этого имени уже было на кабинете (для текста «2 earlier»).
    earlier: int = 0
    #: Почему верхняя своя кампания не дописывается (расхождения с билдером) - в шаги, не в предупреждение.
    notes: list = field(default_factory=list)

    @property
    def renamed(self) -> bool:
        return self.copy_number > 0


def resume_verify_calls(stages: "list[tuple[str, dict]]") -> int:
    """Upper bound of the Graph reads `_verify_resume` makes for these confirmed stages."""
    kinds = [st["kind"] for _k, st in stages]
    reads = sum(1 for k in kinds if k in ("campaign", "creative", "image", "video"))
    sets, ads = kinds.count("adset"), kinds.count("ad")
    if sets or ads:
        reads += 2                           # the ad set listing (two pages at most)
    if ads:
        reads += 2 * (sets + 1)              # one ad listing per ad set (two pages at most)
    return reads


def plan_copy_number(plan: CampaignPlan, submitted_view: dict) -> int:
    """Copy number of the chosen plan against the name that was submitted (0 = the builder's own name)."""
    base = str(submitted_view.get("name") or "")
    prefix = f"{base}{COPY_WORD}"
    if plan.name.startswith(prefix) and plan.name[len(prefix):].isdigit():
        return int(plan.name[len(prefix):])
    return 0


def name_copy_number(name: str, base: str) -> int:
    """Copy number carried by `name` against the builder's `base` name (0 = the base itself or an unrelated name)."""
    prefix = f"{base}{COPY_WORD}"
    if base and name.startswith(prefix) and name[len(prefix):].isdigit():
        return int(name[len(prefix):])
    return 0


def copy_name(base: str, n: int) -> str:
    return base if n <= 0 else f"{base}{COPY_WORD}{n}"


def copy_plan(plan: CampaignPlan, n: int) -> CampaignPlan:
    """План с счётчиком `n` (ACL-2250): имя и метка кампании, имена всех наборов; объявления и формы прежние.

    `media_tag` остаётся меткой имени из билдера: видео копии берётся из библиотеки, а не грузится заново.
    Таргетинги идут по позиции набора, имена на них не влияют.
    """
    if n <= 0:
        return plan
    name = copy_name(plan.name, n)
    return replace(plan, name=name, tag=campaign_tag(plan.act, name), media_tag=plan.video_tag,
                   ad_sets=[replace(s, name=copy_name(s.name, n)) for s in plan.ad_sets])


def family_rows(base: str, rows: list[dict]) -> dict[int, list[dict]]:
    """{0: строки `base`, n: строки `base copy n`} - точное имя, с учётом регистра. `copy 0` и `copy 01` не в счёте."""
    fam: dict[int, list[dict]] = {}
    prefix = f"{base}{COPY_WORD}"
    for row in rows:
        name = str(row.get("name") or "").strip()
        if name == base:
            fam.setdefault(0, []).append(row)
        elif name.startswith(prefix):
            tail = name[len(prefix):]
            if tail.isascii() and tail.isdigit() and not tail.startswith("0"):
                fam.setdefault(int(tail), []).append(row)
    return fam


#: ACL-2338: an unknown write result with one of these reasons had no answer at all (for a POST without a file the
#: page aborts its own request at a deadline, so `network_timeout` means the first request is finished): after two empty
#: readbacks it is sent once more (a file upload never is). `cdp_timeout` (a tab frozen for the whole CDP wait: a queued
#: POST may still start later), `context_stale` and `page_closed` only name the cause and stop.
RESEND_REASONS = frozenset({"network", "network_timeout"})

#: ACL-1856: тексты остановок по журналу операции (English, для человека).
MSG_JOURNAL = ("Progress could not be saved on this computer, so nothing was written to Meta. "
               "Free some disk space or check the folder permissions, then publish again.")
MSG_UNEXPECTED = ("Something unexpected went wrong. What was already created stays in Meta (campaign PAUSED). "
                  "Publish again to continue; nothing will be created twice.")
MSG_MISMATCH = ('"{name}" was changed or removed in Ads Manager after it was created. '
                "Fix or delete it there, then publish again.")
MSG_OTHER_ACCOUNT = ('"{name}" belongs to a different ad account than this campaign. '
                     "Delete or move it, then publish again.")
MSG_VIDEO_NOT_READY = ("The video for {name} is still processing or was rejected by Meta. "
                       "Publish again once it is ready, or replace the video.")
MSG_BUSY = ("Another run is creating {what} right now. Wait for it to finish, then publish again. "
            "Nothing was created twice.")
MSG_SUPERSEDED = ("The saved progress of an earlier campaign with this name was set aside because that campaign "
                  "is no longer in Ads Manager; this is a new publish.")
MSG_SUPERSEDED_RENAMED = ("The earlier campaign was renamed in Ads Manager and is left as it is; "
                          "a new campaign is created for this publish.")
#: Вид объекта по ребру записи (для журнала).
_EDGE_KIND = {"campaigns": "campaign", "adsets": "adset", "ads": "ad", "adcreatives": "creative",
              "adimages": "image", "advideos": "video"}


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
                 live_progress: Callable[[str], None] = lambda _s: None,
                 compress_video: Callable[[str], Any] | None = None,
                 journal: "_oj.OperationJournal | None" = None, request_key: str = "", slot: int = 0):
        self.client = client
        #: ACL-1856: журнал операции на диске; None - прежнее поведение (поиск по имени и метке, без файла).
        self.journal = journal
        #: ACL-1856: the submit key of the page (bound per campaign); the journal refuses its reuse for another plan.
        self.request_key = request_key
        #: ACL-1856: position of the draft in its submit; part of the operation identity (two identical drafts = two).
        self.slot = int(slot)
        self._op: "_oj.Operation | None" = None
        #: ACL-2000: сжатие тяжёлого видео перед загрузкой (тот же сжиматель, что у RPA-пути); None - общий.
        self.compress_video = compress_video
        #: ACL-2353: снимок ревью задания (имя кампании -> fp, Page, пиксель); None - без ревью.
        self.reviewed: dict | None = None
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
        #: Формы Страницы плана (читаются раз на кампанию, только у Instant Form).
        self._page_forms: _forms.FormsRead | None = None
        #: Instagram-идентичность Страницы (ACL-1896): читается раз на Страницу за прогон.
        self._ig_cache: dict[str, _ig.IgIdentity] = {}
        #: id аккаунта Instagram, который уходит в креатив; пусто - Instagram-площадок нет.
        self._ig_id = ""
        #: Шаблоны форм плана -> что с ними будет (ACL-1776); читается в `_check_forms`.
        self._form_steps: dict[str, _fc.FormStep] = {}
        self._form_warnings: list[str] = []
        #: ACL-2571: template id -> photo id of its local picture uploaded to this plan's Page (before any write).
        self._cover_ids: dict[str, str] = {}
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
    def _note_unknown_write(self, what: str, exc: WriteUnknown, began: float, *, first: bool,
                            second: bool | None = None, outcome: str = "") -> None:
        """ACL-2338: one `api_create_step` row for an in-page write timeout: reason, wait, both readbacks, CPU, tabs."""
        if not exc.reason:
            return
        try:
            from src.fb_ads_rpa import run_telemetry

            load_of = getattr(getattr(self.client, "transport", None), "tab_load", None)
            load = load_of() if callable(load_of) else {}
            if not outcome:
                if first:
                    outcome = "found_first_readback"
                else:
                    outcome = "found_second_readback" if second else "resend"
            run_telemetry.record("api_create_step", True, diag={
                "step": f"{what}: unknown write result", "reason": exc.reason,
                "waited_s": round(max(0.0, self.clock() - began), 1),
                "readback_1": first, "readback_2": None if first else bool(second),
                "outcome": outcome, "cpu_pct": load.get("cpu_pct"), "page_count": load.get("page_count")})
        except Exception:  # noqa: BLE001 - telemetry must not break a write
            pass

    def _step(self, out: Outcome, text: str) -> None:
        out.steps.append(text)
        self.progress(text)

    @staticmethod
    def _beat() -> None:
        """ACL-2000: задание живо без новой строки (сторож тишины)."""
        try:
            from src.fb_ads_rpa import run_telemetry

            run_telemetry.beat()
        except Exception:  # noqa: BLE001 - сердцебиение не должно ронять загрузку
            pass

    @staticmethod
    def _waiting(what: str) -> None:
        """Отметка для сторожа тишины: поток ждёт `what`, а не завис."""
        try:
            from src.fb_ads_rpa import run_telemetry

            run_telemetry.set_waiting(what)
        except Exception:  # noqa: BLE001 - отметка не должна ронять загрузку
            pass

    def _video_row(self, plan: CampaignPlan, who: str, data: "bytes | LargeFile", mode: str, began: float,
                   error: Exception | None = None) -> None:
        """Строка телеметрии на видео: размер, md5 и время (`mode`: found / whole / chunked).

        По md5 видно, грузится ли один и тот же файл снова. Содержимого файла и токена здесь нет.
        """
        try:
            from src.fb_ads_rpa import run_telemetry

            # Шаг `api_create_step`: другие имена шагов панель прогресса не знает.
            diag: dict[str, Any] = {"source": "api", "step": f"{who}: video upload timing",
                                    "metric": "video_upload", "mode": mode, "bytes": len(data),
                                    "md5": _md5(data)[:12],
                                    "upload_s": round(float(self.clock() - began), 1)}
            if error is not None:
                details = getattr(error, "details", None) or {}
                diag.update(error=type(error).__name__, kind=getattr(error, "kind", ""),
                            http_status=details.get("http_status"), fbtrace_id=details.get("fbtrace_id"))
            run_telemetry.record("api_create_step", error is None, campaign=plan.name, diag=diag)
        except Exception:  # noqa: BLE001 - телеметрия не должна ронять загрузку
            pass

    @staticmethod
    def _page_timeout(exc: BaseException) -> bool:
        """Страница не приняла кусок вовремя: файл не лёг в неё, к Meta ничего не ушло."""
        text = str(exc)
        return "(no_file" in text and ("did not answer" in text or "timed out" in text)

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

    # -- ACL-1856: журнал операции
    @staticmethod
    def _journal_event(event: str, **diag: Any) -> None:
        try:
            from src.fb_ads_rpa import run_telemetry

            run_telemetry.record(event, True, diag={"source": "api", **diag})
        except Exception:  # noqa: BLE001 - телеметрия не должна ронять запись
            pass

    def _journaled(self, out: Outcome, what: str, kind: str, send: Callable[[], str],
                   find: Callable[[], str | None], *, name: str = "", content_md5: str = "") -> str:
        """Протокол журнала вокруг одной записи (спека §2.1): `sent` ДО отправки, итог - после.

        Без журнала - просто `send()`. Этап, оставшийся `sent`/`unknown` с прошлого запуска, сначала ищется
        чтением: нашли - принимаем; не нашли - повторное чтение через ~30 с, затем запись заново (без стопа по времени).

        ACL-2583: only the CAMPAIGN name is protected. A campaign stage that is still not found after the second read
        raises `_CampaignRewrite` (the caller continues under the next "<name> copy N"), because the name search is the
        part of Meta that lags. Ad sets and ads written again inside our own campaign keep their names (a duplicate
        is acceptable; the owner rule protects campaign names).
        """
        op = self._op
        if op is None:
            return send()
        self._check_cancel(out)
        op.refresh()                  # another process may have moved this stage since the operation was opened
        old = op.stage(what)
        if (old and old["state"] == _oj.CONFIRMED and old["object_id"] and kind != "creative"
                and old.get("owner") and old["owner"] != op.owner):
            self._step(out, f"{what}: already created by another run")
            return str(old["object_id"])
        if old and old["state"] in (_oj.SENT, _oj.UNKNOWN):
            found = find()
            if found:
                self._step(out, f"{what}: found by readback from an earlier run")
                self._journal_event("unknown_adopted", what=what)
                self._journal_confirm(out, what, str(found))
                return str(found)
            # Looked and found nothing. ACL-2583 owner rule: no time-based stop. When the outcome is really unknown
            # (a stage left `sent`, or an unknown one the readback could not settle) read back once more after the
            # same ~30 s pause as a transient read; still nothing -> write again (a duplicate is acceptable, the
            # objects are PAUSED and the name is never reused). A campaign stage is never sent again under its name, settled or not.
            settled = old["state"] == _oj.UNKNOWN and old.get("note") == _oj.NOT_FOUND_NOTE
            if kind == "campaign" and settled:
                # "Settled as not found" is only the first run's own name search, which is the part of Meta that lags.
                raise _CampaignRewrite(int(op.data.get("copy_number") or 0))
            if not settled:
                self._step(out, f"{what}: result of the earlier write is unknown, reading it back once more")
                self._pause(sum(TRANSIENT_READ_WAITS_S))
                if hasattr(self.client, "max_calls"):
                    self.client.max_calls += 1
                found = find()
                if found:
                    self._step(out, f"{what}: found by the second readback")
                    self._journal_event("unknown_adopted", what=what)
                    self._journal_confirm(out, what, str(found))
                    return str(found)
                if kind == "campaign":
                    # Only the campaign NAME is protected (owner rule): the caller continues as a new operation under
                    # the next "<name> copy N". Ad sets and ads written again inside our own campaign keep their names.
                    raise _CampaignRewrite(int(op.data.get("copy_number") or 0))
                self._step(out, f"{what}: not found, writing it again (a duplicate is possible; objects are PAUSED)")
                self._journal_event("unknown_rewritten", what=what)
        try:
            op.begin(what, kind, name=name, content_md5=content_md5, supersede=(kind == "creative"))
        except _oj.StageBusy as exc:
            raise CreationStopped(MSG_BUSY.format(what=what), created=out.created, kind="busy") from exc
        except _oj.JournalError as exc:
            self._journal_event("journal_save_failed", what=what, phase="sent", error=type(exc).__name__)
            raise CreationStopped(MSG_JOURNAL, created=out.created, kind="journal") from exc
        try:
            got = send()
        except CreationStopped as exc:
            state = {"unknown": _oj.UNKNOWN, "cancelled": _oj.CANCELLED}.get(exc.kind, "")
            self._journal_mark(what, state, _oj.NOT_FOUND_NOTE if exc.not_found else exc.kind)
            raise
        except WritesBlocked:
            self._journal_mark(what, _oj.NOT_SENT, "blocked")      # политика не пустила: в Meta не уходило
            raise
        except GraphError as exc:
            self._journal_mark(what, _oj.REJECTED, type(exc).__name__)
            raise
        self._journal_confirm(out, what, str(got))
        return str(got)

    def _journal_confirm(self, out: Outcome, what: str, object_id: str) -> None:
        """Подтверждение после записи: объект уже в Meta, потеря строки журнала - только предупреждение."""
        try:
            self._op.confirm(what, object_id)
        except _oj.JournalError as exc:
            self._journal_event("journal_save_failed", what=what, phase="confirmed", error=type(exc).__name__)
            self._step(out, f"{what}: created, but the progress record could not be saved "
                            "(the next run finds it by its tag)")

    def _journal_mark(self, what: str, state: str, note: str = "") -> None:
        if not state:
            return
        try:
            self._op.mark(what, state, note)
        except _oj.JournalError:
            self._journal_event("journal_save_failed", what=what, phase=state)

    def _finish_operation(self, out: Outcome, state: str, note: str = "") -> None:
        if self._op is None:
            return
        try:
            self._op.finish(state, note)
        except _oj.JournalError:
            self._journal_event("journal_save_failed", what="operation", phase=state)

    def _snapshot_operation(self, out: Outcome) -> None:
        if self._op is not None:
            out.operation = self._op.summary()

    def _plan_view(self, plan: CampaignPlan) -> dict:
        """План для отпечатка: без предупреждений, пути файлов - только имена (кэш сжатого видео не меняет отпечаток)."""
        from dataclasses import asdict

        def norm(v: Any) -> Any:
            if isinstance(v, dict):
                return {k: (Path(x).name if k in ("path", "vertical_path") and isinstance(x, str) else norm(x))
                        for k, x in v.items()}
            if isinstance(v, (list, tuple)):
                return [norm(x) for x in v]
            return v
        return norm(asdict(plan))

    def _pause(self, left: float) -> None:
        """Pause through the injected `sleep` in 5 s slices, keeping the run alive (heartbeat)."""
        while left > 0:
            step = min(5.0, left)
            self.sleep(step)
            left -= step
            beat = getattr(self.client, "_beat", None)
            if callable(beat):
                beat()

    def _get_retrying(self, path: str, params: dict) -> dict:
        """Read by id; a transient failure (not "does not exist") is retried after short pauses (ACL-2583).

        Rate limits and other Graph stops are not GraphError: they leave at once and stay a stop. The last GraphError
        is raised when every try failed. The pause goes through the injected `sleep`, in slices that keep the run alive.
        """
        waits = TRANSIENT_READ_WAITS_S
        for attempt in range(len(waits) + 1):
            try:
                return self.client.get(path, params)
            except GraphError as exc:
                if _object_gone(exc) or attempt == len(waits):
                    raise
                if hasattr(self.client, "max_calls"):
                    self.client.max_calls += 1          # the retry read is part of this run's call budget
                self._pause(float(waits[attempt]))
        raise AssertionError("unreachable")  # pragma: no cover

    def _campaign_alive(self, campaign_id: str) -> "tuple[bool | None, str]":
        """(still in Ads Manager - readable and not deleted/archived, its live name). Read only.

        `None` = Meta did not answer after the retries: nothing is known about the campaign (ACL-2583).
        """
        try:
            row = self._get_retrying(f"/{campaign_id}", {"fields": "id,name,status,effective_status"})
        except GraphError as exc:
            # ACL-2583 (Doc284): "does not exist / missing permissions" (100/33) is also what Meta answers for a live
            # campaign it cannot load right now. For a campaign THIS draft recorded it never frees the name: unknown.
            self._log_read_decision("alive_unknown", campaign_id, exc)
            return None, ""
        if not isinstance(row, dict) or str(row.get("id") or campaign_id) != campaign_id:
            return None, ""
        gone = ({str(row.get("status") or "").upper(), str(row.get("effective_status") or "").upper()}
                & {"DELETED", "ARCHIVED"})
        if gone:
            self._log_read_decision("gone_read", campaign_id, None, status=",".join(sorted(gone)))
        return (not gone), str(row.get("name") or "")

    def _log_read_decision(self, decision: str, campaign_id: str, exc: "GraphError | None", **extra: Any) -> None:
        """ACL-2583: Graph code, subcode and message (ids masked) behind every gone/unknown decision about a campaign."""
        diag: dict[str, Any] = {"decision": decision, "campaign": _mask_id(campaign_id), **extra}
        if exc is not None:
            diag.update(graph_code=exc.code, graph_subcode=exc.subcode,
                        graph_message=str(exc).replace(campaign_id, _mask_id(campaign_id))[:300])
        log.warning("campaign read decision: %s", json.dumps(diag, ensure_ascii=False, default=str))
        self._journal_event("campaign_read_decision", **diag)

    def _open_operation(self, plan: CampaignPlan, out: Outcome, files: dict, *,
                        submitted: "tuple[dict, str] | None" = None, existing_id: str = "") -> str:
        """Open the journal before the first write of the run.

        The identity is the plan AS SUBMITTED (`submitted` = its view and tag, taken before name choice and any start
        move) plus the draft slot, so it does not depend on what Meta holds or on the clock. The name chosen for this
        run (`plan`, after the ACL-2250 choice) is kept in the record. An earlier record whose campaign is gone from
        Ads Manager (the read by id fails, or DELETED/ARCHIVED), or that published under another name, is set aside and a
        NEW operation starts. "Gone" is decided ONLY by that read by id, never by the name search missing the campaign.
        Returns the id of a live campaign with the recorded name that the name search did not list: the caller
        resumes into it (`_verify_resume` checks it) instead of creating a second one; otherwise "".
        """
        if self.journal is None:
            return ""
        adopt = ""
        view, requested_tag = submitted if submitted is not None else (self._plan_view(plan), plan.tag)
        md5s: dict[str, list[str]] = {}
        for s_ in plan.ad_sets:
            for ad in s_.ads:
                md5s[str(ad.index)] = [_md5(files[p]) if p in files else ""
                                       for p in (ad.path, ad.vertical_path) if p]
        fp = _oj.plan_fingerprint(view, md5s, self.slot)
        try:
            op = self.journal.open(plan.act, requested_tag, fp, str(view.get("name") or plan.name),
                                   request_id=self.request_key)
            camp = op.campaign_id()
            # ACL-2583: a name this operation itself recorded (campaign stage while unresolved, else the published
            # name) is never a buyer rename and never goes back to a lower counter: continue under that copy plan.
            cst0 = op.stage("campaign") or {}
            rec_name = (str(cst0.get("name") or "") if cst0.get("state") in (_oj.SENT, _oj.UNKNOWN)
                        else op.published_name) or ""
            rec_n = name_copy_number(rec_name, str(view.get("name") or ""))
            if rec_n > plan_copy_number(plan, view) and (camp or cst0.get("state") in (_oj.SENT, _oj.UNKNOWN)):
                raise _SwitchCopy(rec_n)
            same_name = not op.published_name or op.published_name == plan.name
            if camp and (not same_name or camp != existing_id):
                if same_name:
                    alive, live_name = self._campaign_alive(camp)
                    if alive is None:
                        # ACL-2583: Meta does not answer. Neither stop nor reuse of the name: the caller continues
                        # under a new copy name, which makes the new operation distinct from this one.
                        raise _AliveUnknown(int(op.data.get("copy_number") or 0), camp)
                    # ACL-2583 owner rule: a campaign alive by id with the recorded name is always ours; no time
                    # window. A campaign renamed in Ads Manager is taken over by the buyer (new operation).
                    if alive and (not live_name or live_name == op.published_name):
                        adopt = camp         # alive with the recorded name, only the name search misses it: resume
                if not adopt:
                    op = self.journal.supersede(op, request_id=self.request_key)
                    if same_name:
                        op.warnings.append(MSG_SUPERSEDED if not alive else MSG_SUPERSEDED_RENAMED)
            n_open = plan_copy_number(plan, view)
            cst = op.stage("campaign") or {}
            if cst.get("state") in (_oj.SENT, _oj.UNKNOWN):
                # ACL-2583: a name this operation may already have written is never reused - the counter only
                # goes up while the campaign stage is unresolved (name choice may lag behind Meta's search).
                n_open = max(n_open, int(op.data.get("copy_number") or 0),
                             name_copy_number(str(cst.get("name") or ""), str(view.get("name") or "")))
            op.set_published(plan.name, n_open)
        except _oj.RequestConflict as exc:
            raise CreationStopped(f"{exc} Nothing was created.", kind="request_conflict") from exc
        except _oj.JournalError as exc:
            self._journal_event("journal_save_failed", what="open", phase="open", error=type(exc).__name__)
            raise CreationStopped(MSG_JOURNAL, kind="journal") from exc
        self._op = op
        out.warnings.extend(op.warnings)
        self._snapshot_operation(out)
        return adopt

    @staticmethod
    def _snapshot_operation_of(op: "_oj.Operation", out: Outcome) -> None:
        out.operation = op.summary()

    def _verify_resume(self, plan: CampaignPlan, out: Outcome, campaign_id: str = "") -> None:
        """Подтверждённые этапы прошлых запусков - сверка чтением ДО первой записи (спека §2.1).

        Файл говорит, что проверить; верить ему без чтения нельзя. Расхождение = стоп `resume_mismatch`, ни одной записи.
        """
        op = self._op
        if op is None:
            return
        stages = op.confirmed_stages()
        if not stages:
            return
        acct = normalize_act(plan.act).removeprefix("act_")
        # The reads of this check come out of the budget of the run: the allowance follows the confirmed objects
        # (one read each, the ad set listing and one ad listing per ad set), not a flat number (ACL-1856).
        self.client.max_calls += resume_verify_calls(stages)

        def mismatch(name: str, text: str = MSG_MISMATCH) -> CreationStopped:
            self._snapshot_operation(out)
            return CreationStopped(text.format(name=name), created=out.created, kind="resume_mismatch")

        def read(oid: str, fields: str) -> dict | None:
            try:
                row = self.client.get(f"/{oid}", {"fields": fields})
            except GraphError:
                return None                      # объекта нет / не читается: расхождение, а не сбой запуска
            return row if isinstance(row, dict) and str(row.get("id") or oid) == oid else None

        def other_account(row: dict) -> bool:
            got = str(row.get("account_id") or "").removeprefix("act_")
            return bool(got) and got != acct

        adset_names: dict[str, str] = {}
        ad_names: dict[str, str] = {}
        for key, st in stages:
            oid, name, kind = st["object_id"], st["name"], st["kind"]
            if kind == "campaign":
                row = read(oid, "id,name,status,account_id,adlabels{name}")
                if row is None or str(row.get("name") or "").strip() != plan.name:
                    raise mismatch(name or plan.name)
                if other_account(row):
                    raise mismatch(name or plan.name, MSG_OTHER_ACCOUNT)
                if str(row.get("status") or "").upper() != PAUSED or plan.label not in _labels(row):
                    raise mismatch(name or plan.name)
            elif kind == "adset":
                adset_names[oid] = name
            elif kind == "ad":
                ad_names[oid] = name
            elif kind == "creative":
                row = read(oid, "id,name,account_id")
                if row is None:
                    raise mismatch(name or key)
                if other_account(row):
                    raise mismatch(name or key, MSG_OTHER_ACCOUNT)
            elif kind == "image":
                rows = self.client.get_all(f"/{plan.act}/adimages", {"hashes": [oid], "fields": "hash"}, max_pages=1)
                if not any(str(r.get("hash")) == oid for r in rows):
                    raise mismatch(key)
            elif kind == "video":
                row = read(oid, "id,title,status,account_id")
                if row is None or (name and row.get("title") is not None and str(row["title"]) != name):
                    raise mismatch(key)
                if other_account(row):
                    raise mismatch(key, MSG_OTHER_ACCOUNT)
                status = row.get("status") if isinstance(row.get("status"), dict) else {}
                if str(status.get("video_status") or "").lower() != "ready":
                    raise mismatch(key, MSG_VIDEO_NOT_READY)
        if adset_names or ad_names:
            # Кампания: найденная выбором имени, иначе подтверждённая в журнале (запись о ней могла не сохраниться).
            campaign_id = campaign_id or next((st["object_id"] for _k, st in stages if st["kind"] == "campaign"), "")
            seen_sets = {str(r["id"]): str(r.get("name") or "").strip()
                         for r in self._children(campaign_id, "adsets", "id,name")} if campaign_id else {}
            for oid, name in adset_names.items():
                if seen_sets.get(oid) != name:
                    raise mismatch(name)
            seen_ads: dict[str, str] = {}
            if ad_names:
                for set_id in seen_sets:
                    for r in self._children(set_id, "ads", "id,name"):
                        seen_ads[str(r["id"])] = str(r.get("name") or "").strip()
            for oid, name in ad_names.items():
                if seen_ads.get(oid) != name:
                    raise mismatch(name)
        self._journal_event("resume_verify", stages=len(stages), ok=True)
        self._step(out, f"earlier progress checked in Meta: {len(stages)} object(s) match")

    def _write(self, out: Outcome, what: str, *args: Any, **kwargs: Any) -> str:
        """ACL-2392: `_write_inner` plus the plain-words stage on whatever stops it ("while creating ad set ...")."""
        try:
            return self._write_inner(out, what, *args, **kwargs)
        except (CreationStopped, GraphError, GraphStop, WriteUnknown) as exc:
            if not getattr(exc, "stage", ""):
                exc.stage = _plain.stage_of(what)
            raise

    def _write_inner(self, out: Outcome, what: str, path: str, params: dict, find: Callable[[], str | None],
                     *, files: dict | None = None, **kw: Any) -> str:
        """Одна запись под журналом операции (ACL-1856); без журнала - прежняя `_write_raw`. Стадию для ошибки ставит `_write` (ACL-2392)."""
        if self._op is None:
            return self._write_raw(out, what, path, params, find, files=files, **kw)
        kind = _EDGE_KIND.get(path.rsplit("/", 1)[-1], "form")
        md5 = _md5(next(iter(files.values()))) if files else ""
        return self._journaled(
            out, what, kind, lambda: self._write_raw(out, what, path, params, find, files=files, **kw), find,
            name=str(params.get("name") or ""), content_md5=md5)

    def _write_raw(self, out: Outcome, what: str, path: str, params: dict, find: Callable[[], str | None],
                   *, files: dict | None = None, filename: str = "", id_of=lambda b: b.get("id"),
                   on_ad: bool = False, children_active: bool = False, form_page: str = "",
                   upload: tuple[int, str] | None = None, _resent: bool = False) -> str:
        """Одна запись; неизвестный результат — сверка `find()` чтением.

        ACL-2301: если ответа не было вовсе (`network`/`network_timeout`/`cdp_timeout`, ACL-2338) и сверка дважды не нашла
        объект, запись без файла отправляется ОДИН раз повторно. Записи с файлом и ответы Meta
        вроде 5xx/кода 1-2 по-прежнему только сверяются и стоп.

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
            began = self.clock()
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
                            "processing the upload; publish again to check it first.",
                            created=out.created, kind="cancelled")
                    if wait > 0:
                        self.sleep(wait)
                    if upload and not stopped and self.cancelled():
                        raise CreationStopped(
                            f"Stopped by the user while checking the {what} result. Meta may still be "
                            "processing the upload; publish again to check it first.",
                            created=out.created, kind="cancelled")
                    # Повторные чтения дешевле: свежий файл в первых страницах списка.
                    found = find(pages=UPLOAD_RECONCILE_PAGES) if (upload and n > 0) else find()
                    if found:
                        break
            except (GraphStop, GraphError) as rexc:
                self._note_unknown_write(what, exc, began, first=True, outcome=f"stop_readback_failed:{rexc.__class__.__name__}")
                raise CreationStopped(
                    f"{what}: the write result is unknown ({exc}) and the readback failed ({rexc}). "
                    "Nothing else was written; reconcile the existing object in Ads Manager before retrying.", created=out.created, kind="unknown") from rexc
            first_found = bool(found)
            if first_found:
                self._note_unknown_write(what, exc, began, first=True)
            if not found:
                if stopped:
                    raise CreationStopped("Stopped by the user during the upload; nothing was found "
                                          "in the ad account yet. Meta may still be processing the "
                                          "upload; publish again to check it first.",
                                          created=out.created, kind="cancelled") from exc
                if exc.reason in RESEND_REASONS and not files and not upload and not _resent:
                    # Ответа не было: проверяем ещё раз (запрос мог долететь позже) и только потом повторяем.
                    self._beat()
                    if self.reconcile_wait_s > 0:
                        # About 20 s: a slow proxy may deliver the first request late (a duplicate is the costly failure).
                        self.sleep(self.reconcile_wait_s * 4)
                    self._beat()
                    try:
                        found = find()
                    except (GraphStop, GraphError) as rexc:
                        self._note_unknown_write(what, exc, began, first=False,
                                                 outcome=f"stop_readback_failed:{rexc.__class__.__name__}")
                        raise CreationStopped(
                            f"{what}: the write result is unknown ({exc}) and the readback failed ({rexc}). "
                            "Nothing else was written; reconcile the existing object in Ads Manager before retrying.",
                            created=out.created, kind="unknown") from rexc
                    self._note_unknown_write(what, exc, began, first=False, second=bool(found))
                    if not found and what == "campaign" and self._op is not None:
                        # ACL-2583 owner rule: the same campaign name is never written twice. The name search lags, so
                        # the caller continues under the next "<name> copy N" (a duplicate is acceptable).
                        self.client.reconciled(path)
                        raise _CampaignRewrite(int(self._op.data.get("copy_number") or 0))
                    if not found:
                        self.client.reconciled(path)
                        self._step(out, f"{what}: not found by readback after {exc.reason} - sending once more")
                        return self._write_raw(out, what, path, params, find, files=files, filename=filename,
                                               id_of=id_of, on_ad=on_ad, children_active=children_active,
                                               form_page=form_page, upload=upload, _resent=True)
                else:
                    self._note_unknown_write(what, exc, began, first=True, outcome="stop_not_found")
                    # ACL-2392: a known Meta rejection (subcode 1870226) is CLASSIFIED as a rejection for the text
                    # only; the stop stays "unknown" so the gating (blocked job, unreconciled path) is unchanged.
                    rejection = (_plain.classify(exc, kind="unknown", ctx={"stage": _plain.stage_of(what)})
                                 if exc.subcode in _REJECTION_SUBCODES else None)
                    raise CreationStopped(
                        f"{what}: the write result is unknown ({exc}) and nothing was found by readback. "
                        "Nothing else was written; reconcile the existing object before a re-run.",
                        created=out.created, kind="unknown", failure=rejection,
                        not_found=not (files or upload)) from exc
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
                                      "by readback.", created=out.created, kind="unknown",
                                      not_found=not (files or upload))
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

    def _campaign_finder(self, plan: CampaignPlan, base_plan: CampaignPlan) -> Callable[[], str | None]:
        """Readback of the campaign write. ACL-2583: while the journal's campaign stage is sent/unknown it holds the
        name that was really written; that name is searched, not the current plan name."""
        def find() -> str | None:
            target = plan
            st = (self._op.stage("campaign") if self._op is not None else None) or {}
            rec = str(st.get("name") or "")
            if st.get("state") in (_oj.SENT, _oj.UNKNOWN) and rec and rec != plan.name:
                n = name_copy_number(rec, base_plan.name)
                if n > 0 or rec == base_plan.name:
                    target = copy_plan(base_plan, n)
            return (self._find_campaign(target, after_unknown=True) or {}).get("id")
        return find

    def _campaign_rows(self, act: str, name: str) -> list[dict]:
        """Живые кампании кабинета, чьё имя содержит `name` (одно чтение, максимум 2 страницы)."""
        return self.client.get_all(f"/{act}/campaigns", {
            "fields": "id,name,status,adlabels{name}",
            "filtering": [{"field": "name", "operator": "CONTAIN", "value": name}],
            "effective_status": LIVE_STATES, "limit": 100}, max_pages=2)

    def _family_rows(self, plan: CampaignPlan) -> list[dict]:
        """Живые кампании с именем семейства: один поиск по подстроке.

        Если выдача могла обрезаться (200 строк) - ещё два точных поиска: `<имя> copy ` и имя целиком.
        Они тоже полны на 200 строках - тогда доверять нечему, стоп до записей.
        """
        rows = self._campaign_rows(plan.act, plan.name)
        if len(rows) < CAMPAIGN_SEARCH_ROWS:
            return rows
        more = self._campaign_rows(plan.act, f"{plan.name}{COPY_WORD}")
        same = self.client.get_all(f"/{plan.act}/campaigns", {
            "fields": "id,name,status,adlabels{name}",
            "filtering": [{"field": "name", "operator": "EQUAL", "value": plan.name}],
            "effective_status": LIVE_STATES, "limit": 100}, max_pages=2)
        if len(more) >= CAMPAIGN_SEARCH_ROWS or len(same) >= CAMPAIGN_SEARCH_ROWS:
            raise CreationStopped(
                f"Cannot tell which copy number of the campaign {plan.name!r} is free: the ad account has too "
                "many campaigns with that name. Nothing was created; rename the campaign in the builder.",
                kind="name_unsure")
        seen: set[str] = set()
        out: list[dict] = []
        for row in [*same, *more]:
            if str(row.get("id")) not in seen:
                seen.add(str(row.get("id")))
                out.append(row)
        return out

    def _check_name_length(self, cand: CampaignPlan) -> None:
        """ACL-2250 (D6): настоящие итоговые имена кампании и каждого набора - до любой записи; не обрезаем."""
        for what, name in [("campaign", cand.name), *[("ad set", s.name) for s in cand.ad_sets]]:
            if len(name) > CAMPAIGN_NAME_MAX:
                raise CreationStopped(
                    f"The {what} name {name!r} would be longer than the Meta limit of {CAMPAIGN_NAME_MAX} "
                    "characters. Nothing was created; shorten the campaign name in the builder.",
                    kind="name_too_long")

    def _resume_state(self, cand: CampaignPlan, targetings: list[dict], campaign_id: str):
        """Наша кампания кандидата: (найденные наборы, неполная ли), либо None - дописывать нельзя.

        Нельзя, если в ней есть объявления не из плана или она расходится с билдером (в том числе включена):
        третий элемент - причины текстом. Неполная: нет набора плана или объявления плана под найденным набором.
        Без Страницы объявления не считаются. Всего одно чтение объявлений кампании: и «подходят», и «неполная».
        """
        found_sets = self._find_sets(cand, campaign_id)
        manual = needs_manual_page(cand)
        ads = self._children(campaign_id, "ads", "id,name,adset_id")
        want = set() if manual else {ad.name for s in cand.ad_sets for ad in s.ads}
        if {str(r.get("name") or "").strip() for r in ads} - want:
            return found_sets, False, ["it is a campaign with other ads"]
        bad = self._existing_differences(cand, targetings, campaign_id, found_sets, has_ads=bool(ads))
        if bad:
            return found_sets, False, [f"{f['object']}: " + "; ".join(f["differences"]) for f in bad]
        have: dict[str, set[str]] = {}
        for r in ads:
            have.setdefault(str(r.get("adset_id") or ""), set()).add(str(r.get("name") or "").strip())
        partial = any(h is None for h in found_sets) or (not manual and any(
            h is not None and any(ad.name not in have.get(str(h["id"]), set()) for ad in s.ads)
            for s, h in zip(cand.ad_sets, found_sets)))
        return found_sets, partial, []

    def _receipt_choice(self, plan: CampaignPlan, targetings: list[dict], receipt: dict
                        ) -> tuple[NameChoice | None, tuple[int, dict, list[str]] | None]:
        """Квитанция прошлого запуска: та же кампания дописывается, пока она жива, наша и не расходится с билдером.

        Возвращает (выбор, остаток). ACL-2583: «id не существует» (100/33) имя НЕ освобождает - как нет ответа. Не живая (прочитана DELETED/ARCHIVED), переименована - квитанция не
        используется, решает листинг. Остановка Graph (GraphStop) идёт наружу.
        ACL-2578: (1) чтение по id упало по другой причине (не «не существует») - 2-3 повтора за ~30 с; всё ещё без ответа -
        имя считается занятым (второй элемент без причин), идём новой кампанией со следующим `copy N`, не стоп; (2) живая кампания с ТОЧНЫМ именем занимает его, чья бы она ни была
        (метка не важна); (3) наша, но расходится с билдером - не дописывается. В (2) и (3) она возвращается вторым
        элементом (номер, строка, причины): имя занято, даже если листинг Meta её сейчас не показал.
        Не обнаружить: квитанции нет и листинг промахнулся - тогда о кампании ничего не известно.
        """
        n = receipt.get("n")
        if not isinstance(n, int) or n < 0:
            return None, None
        cand = copy_plan(plan, n)
        if receipt.get("name") != cand.name:
            return None, None
        campaign_id = str(receipt.get("campaign_id") or "")
        if campaign_id:
            try:
                row = self._get_retrying(f"/{campaign_id}", {"fields": "name,effective_status,adlabels{name}"})
            except GraphError as exc:
                # ACL-2583 (Doc284): "does not exist" (100/33) does not free the name either: the receipt names a
                # campaign this draft created, and Meta answers the same for a live one it cannot load. Only a row
                # read with a non-live status (below) lets the base name go.
                self._log_read_decision("receipt_unknown", campaign_id, exc)
                # ACL-2578 / owner decision: Meta still does not answer after the retries. The earlier campaign may
                # exist, so its name is held (never reused): the caller takes the next copy number, as a new campaign.
                return None, (n, {"id": campaign_id, "name": cand.name}, None)
            row = {**row, "id": campaign_id} if isinstance(row, dict) else None
        else:
            hits = family_rows(plan.name, self._family_rows(plan)).get(n, [])
            row = next((r for r in hits if cand.label in _labels(r)), None)
        if not row or str(row.get("name") or "").strip() != cand.name:
            return None, None
        if str(row.get("effective_status") or row.get("status") or "") not in LIVE_STATES:
            return None, None
        if cand.label not in _labels(row):
            return None, (n, row, ["it was not created by ACL (no ACL tag)"])
        found_sets, _partial, why = self._resume_state(cand, targetings, str(row["id"]))
        if why:
            return None, (n, row, why)
        return NameChoice(cand, row, found_sets, n, n), None

    def _resolve_name(self, plan: CampaignPlan, targetings: list[dict]) -> NameChoice:
        """ACL-2250 (ACL-1902): имя кампании до любой записи - и в боевом запуске, и в Check plan.

        1. Квитанция этого задания (кабинет + имя из билдера): дописываем ту же кампанию.
        2. Иначе одним поиском собираем семейство `X`, `X copy 1`, `X copy 2` ... Нет семейства - имя как есть.
           Верхний номер, если он наш и неполный (обрыв, квитанция потеряна) - дописываем. Иначе новая копия
           с номером «верхний + 1» (не первая дырка): занятый номер не возвращаем.
        Только чтения; подробно читается одна строка - верхняя.
        """
        base = plan.name
        receipt = self._read_name_receipt(plan)
        leftover = None
        if receipt:
            chosen, leftover = self._receipt_choice(plan, targetings, receipt)
            if chosen:
                return chosen
        fam = family_rows(base, self._family_rows(plan))
        if leftover:
            # ACL-2578: our own left-over campaign (known by id from the receipt) holds its name even when the
            # Meta listing does not show it (search lag): the new one must never take the same name.
            n_left, row_left, _why = leftover
            known = fam.setdefault(n_left, [])
            if str(row_left.get("id")) not in {str(r.get("id")) for r in known}:
                known.append(row_left)
        if not fam:
            self._check_name_length(plan)
            return NameChoice(plan, None, [None] * len(plan.ad_sets))
        top = max(fam)
        cand = copy_plan(plan, top)
        ours = next((r for r in fam[top] if cand.label in _labels(r)), None)
        notes: list[str] = []
        if ours:
            if (leftover and leftover[2] is not None and top == leftover[0]
                    and str(ours.get("id")) == str(leftover[1].get("id"))):
                found_sets, partial, why = None, False, leftover[2]        # already read by the receipt check
            else:
                found_sets, partial, why = self._resume_state(cand, targetings, str(ours["id"]))
            if partial and not why:
                return NameChoice(cand, ours, found_sets, top, len(fam) - 1)
            if why:
                notes.append(f"{cand.name!r} (earlier campaign) is left as is; it differs from the builder: "
                             + " | ".join(why))
        cand = copy_plan(plan, top + 1)
        self._check_name_length(cand)
        return NameChoice(cand, None, [None] * len(plan.ad_sets), top + 1, len(fam), notes)

    def _save_name_receipt(self, out: Outcome, requested: str, choice: NameChoice) -> None:
        """До первой записи: выбранное имя на диск, чтобы повтор после обрыва дописал ту же кампанию."""
        try:
            _nr.save(choice.plan.act, requested, choice.plan.name, choice.copy_number,
                     str(choice.existing["id"]) if choice.existing else "")
        except _nr.NameReceiptError as exc:
            raise CreationStopped(
                f"The local campaign name record could not be saved ({exc}). Nothing was created; "
                "check the folder of the ACL machine settings.", kind="name_receipt") from exc

    def _attach_name_receipt(self, out: Outcome, act: str, requested: str) -> None:
        try:
            _nr.attach_id(act, requested, out.campaign_id)
        except _nr.NameReceiptError as exc:
            raise CreationStopped(
                f"The local campaign name record could not be updated ({exc}). The campaign "
                f"{out.campaign_id} exists (PAUSED); nothing else was written.",
                created=out.created, kind="name_receipt") from exc

    def _read_name_receipt(self, plan: CampaignPlan) -> dict | None:
        try:
            return _nr.load(plan.act, plan.name)
        except _nr.NameReceiptError as exc:
            raise CreationStopped(
                f"The local campaign name record could not be read ({exc}). Nothing was created; "
                "check the folder of the ACL machine settings.", kind="name_receipt") from exc

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

    def _same_videos(self, plan, params, row) -> bool:
        """Repeat: Meta returned the earlier creative that differs only by video id (same file uploaded again).
        Accepted unless the lengths really differ; an unreadable video is accepted too (the rest matched)."""
        pairs = _cr.video_pairs(plan.act, params, row)
        if not pairs:
            return False
        try:
            for want_id, got_id in pairs:
                a = self.client.get(f"/{want_id}", {"fields": "length"})
                b = self.client.get(f"/{got_id}", {"fields": "length"})
                la = float(a["length"]) if isinstance(a, dict) and a.get("length") else None
                lb = float(b["length"]) if isinstance(b, dict) and b.get("length") else None
                if la is not None and lb is not None and la != lb:
                    return False      # different length is a different video
        except Exception:
            pass                      # video could not be read: everything else matched
        return True

    def _creative_content_ok(self, plan, params, row) -> bool:
        """ACL-2290: readback judges a creative by the acceptance rules used at publish time: an exact match,
        the same labelled files in another order (ACL-2284), or an earlier upload of the same video (ACL-2271).
        Any other difference stays a mismatch."""
        if not row:
            return False
        return bool(_cr.matches(plan.act, params, row)
                    or _cr.media_diff(plan.act, params, row) == "order"
                    or self._same_videos(plan, params, row))

    def _verify_label(self, out: Outcome, what: str, object_id: str, label: str) -> None:
        """Метка на созданном объекте; нет — стоп до следующей записи (ревью B1b)."""
        row = self.client.get(f"/{object_id}", {"fields": "adlabels{name}"})
        # ACL-2206 (Doc120 26.10, Doc122 22.10, Doc120 18.10): Meta иногда отдаёт метку не сразу после создания.
        # Перечитываем несколько раз (только GET), и лишь потом останавливаемся; запись не повторяется никогда.
        for wait in LABEL_RECHECK_WAITS:
            if label in _labels(row):
                break
            self.sleep(wait)
            row = self.client.get(f"/{object_id}", {"fields": "adlabels{name}"})
        if label not in _labels(row):
            raise CreationStopped(
                f"{what} was created but Meta did not keep the ACL tag ({label}) on it. "
                "Stopped before any further write. Check the existing object before retrying. "
                "Check it in Ads Manager.", created=out.created, kind="label_missing")

    def _creative_listing(self, plan: CampaignPlan, *, fresh: bool = False) -> list[dict]:
        """Only a complete, well-formed listing proves absence before dispatch."""
        if not fresh and getattr(self, "_recovery_rows", None) is not None:
            return self._recovery_rows
        rows, cursors = [], set()
        params = {"fields": _cr.FIELDS, "limit": 100}
        for page in range(CREATIVE_LOOKUP_PAGES):
            body = self.client.get(f"/{plan.act}/adcreatives", params)
            valid = (isinstance(body, dict) and isinstance(body.get("data"), list)
                     and all(isinstance(r, dict) and re.fullmatch(r"[0-9]{1,30}", str(r.get("id") or ""))
                             and isinstance(r.get("name"), str) for r in body["data"]))
            if not valid:
                raise CreationStopped("The creative lookup is unreadable. No new creative was sent.",
                                      kind="creative_unknown")
            rows.extend(body["data"])
            paging = body.get("paging", {})
            if not isinstance(paging, dict):
                raise CreationStopped("The creative pagination is unreadable. No new creative was sent.",
                                      kind="creative_unknown")
            if "next" in paging and paging["next"] is not None and not isinstance(paging["next"], str):
                raise CreationStopped("The creative pagination is unreadable. No new creative was sent.",
                                      kind="creative_unknown")
            if not paging.get("next"):
                self._recovery_rows = rows
                return rows
            cursor = (paging.get("cursors") or {}).get("after") if isinstance(paging.get("cursors", {}), dict) else None
            if (not isinstance(paging["next"], str) or not isinstance(cursor, str)
                    or not cursor or cursor in cursors or page + 1 == CREATIVE_LOOKUP_PAGES):
                raise CreationStopped("The creative lookup is incomplete. No new creative was sent.",
                                      kind="creative_unknown")
            cursors.add(cursor)
            params = dict(params, after=cursor)
        raise AssertionError("unreachable creative pagination")

    def _creative_candidates(self, plan: CampaignPlan, stem: str, params: dict,
                             *, fresh: bool = False) -> list[dict]:
        rows = self._creative_listing(plan, fresh=fresh)
        # Explicit ownership by another ACL campaign is not our orphan. A name
        # suffix alone is not ownership: untagged legacy creatives still reconcile.
        same = [r for r in rows if _cr.same_ad(r.get("name"), stem)
                and not any(label.startswith("acl-") and label != plan.label
                            for label in _labels(r))]
        # A creative of an earlier repeat that the listing returned only partly is not stopped on (that blocked
        # the repeat of the same campaign). It is read once by id: if the full read is complete it is judged like any
        # other row, so a creative that really exists and matches is still found and reused. Only a creative that
        # stays unreadable (or belongs to another account) is not a candidate.
        readable = []
        for r in same:
            if not (str(r.get("account_id") or "") == plan.act.removeprefix("act_") and _cr.complete(params, r)):
                r = self._reread_creative(r)
                if not (r and str(r.get("account_id") or "") == plan.act.removeprefix("act_")
                        and _cr.complete(params, r)):
                    continue
            readable.append(r)
        return [r for r in readable if _cr.matches(plan.act, params, r)]

    def _reread_creative(self, row: dict) -> dict | None:
        """Full read of one creative by id (GET only); None when it still cannot be read."""
        try:
            full = self.client.get(f"/{row['id']}", {"fields": _cr.FIELDS})
        except Exception:
            return None
        return full if isinstance(full, dict) and str(full.get("id") or "") == str(row["id"]) else None

    def _recoverable_creative(self, out: Outcome, plan: CampaignPlan, ad: AdPlan,
                             ad_set: AdSetPlan, media: dict, name: str) -> tuple[str, bool]:
        """Persist intent before dispatch; verify all recovered IDs against actual content."""
        params = self.creative_params(plan, ad, ad_set, media, name)
        stem = f"{ad.name} #{ad.index}"
        def candidates(*, fresh=False):
            rows = self._creative_candidates(plan, stem, params, fresh=fresh)
            # Every candidate passed the full content check (_cr.matches), so they are identical: take the oldest.
            # Several identical creatives are a trace of earlier repeats, not a reason to stop (ACL-2250).
            if len(rows) > 1:
                rows = sorted(rows, key=lambda r: int(r["id"]))
                self._step(out, f"{len(rows)} identical creatives already exist; using the oldest {rows[0]['id']}")
            return str(rows[0]["id"]) if rows else None
        try:
            with _cr.transaction(plan.act, name, params) as receipt:
                creative_id = receipt.object_id
                created = False
                resumed_accepted = bool(creative_id) and receipt.accepted_identical
                if not creative_id:
                    creative_id = candidates()
                    if creative_id:
                        receipt.save(creative_id)
                    elif receipt.data and receipt.age_s() < RECEIPT_UNKNOWN_S:
                        raise CreationStopped("An earlier creative write has an unknown result. "
                                              "No new creative was sent. Reconcile the existing object first.",
                                              kind="creative_unknown")
                    else:
                        self._check_cancel(out)
                        receipt.save()
                        creative_id = self._write(out, f"ad {ad.index}: creative",
                                                  f"/{plan.act}/adcreatives", params,
                                                  lambda: candidates(fresh=True), on_ad=ad_set.instant)
                        out.created.setdefault("creatives", []).append(creative_id)
                        out.creative_ids.append(creative_id)
                        receipt.save(creative_id)
                        created = True
                replaced = False

                def finish(cid: str, verified: bool) -> tuple[str, bool]:
                    # ACL-2284: the warning is added only once a replacement has passed the full content check,
                    # and it says "new" only when the creative really is one written in this run.
                    if replaced:
                        out.warnings.append(STALE_WARNING if cid in out.created.get("creatives", [])
                                            else STALE_WARNING_REUSED)
                    return cid, verified

                while True:
                    if creative_id not in out.creative_ids and not replaced:
                        out.creative_ids.append(creative_id)
                    if not created:
                        out.reused.setdefault("creatives", []).append(creative_id)
                    label_failure = None
                    # After a replacement write the tag alone proves nothing (the repeat has the same tag as the
                    # stale creative): the creative is always read back and judged by content below.
                    if created and not replaced and not self._missing_creative_tag:
                        try:
                            self._verify_label(out, f"The creative of ad {ad.index}", creative_id, plan.label)
                            return creative_id, False
                        except CreationStopped as exc:
                            if exc.kind != "label_missing":
                                raise
                            label_failure = exc
                            self._missing_creative_tag = True
                    row = self.client.get(f"/{creative_id}", {"fields": _cr.FIELDS})
                    kind = _cr.media_diff(plan.act, params, row)
                    content_ok = kind in ("same", "order")
                    if kind == "order" and ORDER_WARNING not in out.warnings:
                        out.warnings.append(ORDER_WARNING)
                    # ACL-2249: Meta answers a repeated write with the id of the EXISTING identical creative, which
                    # still carries the tag of an earlier run. Only for a creative written just now, and only when
                    # account, Page, form and the full requested content match, it is the ad's creative. The shared
                    # creative is never re-tagged and a creative found only by name/tag is never adopted.
                    foreign_tag = isinstance(row, dict) and any(
                        label.startswith("acl-") and label != plan.label for label in _labels(row))
                    # On resume the receipt (written at acceptance) carries the mark; a bare receipt does not.
                    identical_existing = bool(
                        (created or resumed_accepted) and foreign_tag and isinstance(row, dict)
                        and str(row.get("id") or "") == creative_id
                        and (content_ok or self._same_videos(plan, params, row)))
                    if identical_existing:
                        note = "Meta returned an existing identical creative (tag of an earlier run)"
                        if plan.media_tag and plan.media_tag != plan.tag:
                            # ACL-2250: у копии это ожидаемо (то же видео и тот же контент) - шаг, а не предупреждение.
                            self._step(out, note)
                        else:
                            out.warnings.append(note)
                        if created:
                            # An existing object, not a new one: report it as reused and remember the acceptance.
                            made = out.created.get("creatives", [])
                            if creative_id in made:
                                made.remove(creative_id)
                            if not made:
                                out.created.pop("creatives", None)
                            out.reused.setdefault("creatives", []).append(creative_id)
                            receipt.save(creative_id, accepted_identical=True)
                        return finish(creative_id, True)
                    if (isinstance(row, dict) and str(row.get("id") or "") == creative_id and not foreign_tag
                            and not content_ok and self._same_videos(plan, params, row)):
                        # Same creative (account, Page, form and all other content), but its video is an earlier
                        # upload of the same file: accepted. _same_videos refuses when the lengths are known and differ.
                        out.warnings.append("The creative points at an earlier upload of the same video; "
                                            "everything else matches, so it was accepted.")
                        if creative_id not in out.reused.get("creatives", []):
                            out.reused.setdefault("creatives", []).append(creative_id)
                        return finish(creative_id, True)
                    if (not isinstance(row, dict) or str(row.get("id") or "") != creative_id
                            or (foreign_tag and not identical_existing)
                            or not (identical_existing or content_ok)):
                        if (kind == "stale" and not replaced and (foreign_tag or not created)
                                and str(row.get("id") or "") == creative_id):
                            # ACL-2284: same account and identity, only the MEDIA differs: another file, not our
                            # creative that failed to confirm. Never reused; the requested content gets its own.
                            replaced = True
                            self._step(out, "An earlier creative with other media was found; "
                                            f"first difference: {_cr.first_diff(plan.act, params, row)}")
                            creative_id, created = self._replace_stale_creative(
                                out, plan, ad, ad_set, params, receipt, creative_id, candidates)
                            if not created:
                                return finish(creative_id, True)      # a matching creative existed elsewhere: reused
                            continue                          # judge the new creative like any written one
                        if label_failure:
                            raise label_failure
                        raise CreationStopped(f"Creative {creative_id} was found but its identity, account "
                                              "or content was not confirmed. No ad was created; check it first. "
                                              f"First difference: {_cr.first_diff(plan.act, params, row)}.",
                                              created=out.created, kind="creative_unverified")
                    if plan.label not in _labels(row):
                        out.warnings.append("Meta did not return the creative ACL tag; the existing creative "
                                            "was verified by its account, Page, form and full requested content.")
                    return finish(creative_id, True)
        except (OSError, ValueError) as exc:
            raise CreationStopped("Creative recovery could not be saved or read safely. "
                                  "Do not retry until the local receipt is repaired.",
                                  created=out.created, kind="creative_receipt") from exc

    def _replace_stale_creative(self, out: Outcome, plan: CampaignPlan, ad: AdPlan, ad_set: AdSetPlan,
                                params: dict, receipt, stale_id: str, candidates) -> tuple[str, bool]:
        """ACL-2284: the creative found for this ad carries other media (same account and identity).

        It is dropped from this run's records. A creative that matches in full and exists elsewhere in the
        listing is preferred over writing; otherwise exactly one new creative is written (GET-verified by
        the caller). Returns (creative id, written now)."""
        for bucket in (out.created, out.reused):
            ids = bucket.get("creatives", [])
            if stale_id in ids:
                ids.remove(stale_id)
            if "creatives" in bucket and not ids:
                bucket.pop("creatives")
        if stale_id in out.creative_ids:
            out.creative_ids.remove(stale_id)
        other = candidates(fresh=True)
        if other and other != stale_id:
            receipt.save(other)
            out.creative_ids.append(other)
            out.reused.setdefault("creatives", []).append(other)
            return other, False
        self._check_cancel(out)
        receipt.save()
        new_id = self._write(out, f"ad {ad.index}: creative", f"/{plan.act}/adcreatives", params,
                             lambda: candidates(fresh=True), on_ad=ad_set.instant)
        if new_id != stale_id:
            # Meta answering with the stale id again created nothing; the caller stops on its content.
            out.created.setdefault("creatives", []).append(new_id)
            out.creative_ids.append(new_id)
            receipt.save(new_id)
        return new_id, True

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
        try:
            got = self._write(out, f"{who}: image upload", f"/{plan.act}/adimages", {}, find,
                              files={"filename": data}, filename=image_name(path, data, self._converted_ext(path)),
                              id_of=hash_of, upload=(ad.index, f"{role.strip()} image".strip()))
        except GraphError as exc:
            if exc.subcode not in _FILE_REFUSAL_SUBCODES:
                raise
            # ACL-2575: Meta's definite answer (FileTypeNotSupported, not transient) is a refusal of THIS file,
            # not an unknown result: no "check Ads Manager" advice; the objects already created are listed.
            ad_label = f"{ad.index}{role}"
            failure = _plain.file_refused(ad_label, Path(path).name, exc)
            raise CreationStopped(
                f"Meta refused the file of ad {ad_label} ({Path(path).name}): {_plain.FILE_REFUSED_REASON}.",
                created=out.created, kind="graph_error", failure=failure) from exc
        out.created.setdefault("images", []).append(got)
        self._step(out, f"{who}: image uploaded")
        return got

    def _video(self, out: Outcome, plan: CampaignPlan, ad: AdPlan, data: bytes, *,
               path: str = "", role: str = "") -> tuple[str, str]:
        title = video_title(plan.video_tag, data)
        path = path or ad.path
        who = f"ad {ad.index}{role}"

        def find(pages: int = 10) -> str | None:
            rows = self.client.get_all(f"/{plan.act}/advideos", {"fields": "id,title", "limit": 100},
                                       max_pages=pages)
            hit = next((r for r in rows if str(r.get("title") or "") == title), None)
            return str(hit["id"]) if hit else None

        began = self.clock()
        vid = find()
        if vid:
            self._step(out, f"{who}: video already in the ad account")
            self._video_row(plan, who, data, "found", began)
        else:
            mode = "chunked" if isinstance(data, LargeFile) else "whole"
            began = self.clock()
            self._waiting("video_upload")
            try:
                if mode == "chunked":
                    vid = self._journaled(
                        out, f"{who}: video upload", "video",
                        lambda: self._video_chunked(out, plan, data, title, find, who), find,
                        name=title, content_md5=_md5(data))
                else:
                    vid = self._write(out, f"{who}: video upload", f"/{plan.act}/advideos",
                                      {"name": title, "title": title}, find, files={"source": data},
                                      filename=Path(path).name,
                                      upload=(ad.index, f"{role.strip()} video".strip()))
            except Exception as exc:
                self._video_row(plan, who, data, mode, began, exc)
                raise
            finally:
                self._waiting("")
            out.created.setdefault("videos", []).append(vid)
            self._step(out, f"{who}: video uploaded")
            self._video_row(plan, who, data, mode, began)
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
        guard = 2 * (-(-size // VIDEO_CHUNK_MAX)) + 4 + 3 * (-(-size // VIDEO_CHUNK_MAX))
        failures = 0
        stalls = 0
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
            except PageFailed as exc:
                # ACL-2000: страница не успела принять кусок (CDP-таймаут при складывании). Ничего не
                # отправлено - повторяем тот же кусок с последнего подтверждённого смещения в той же сессии.
                if not self._page_timeout(exc) or stalls >= len(CHUNK_RETRY_BACKOFF_S)                         or not self.client.resume_after_unsent(exc):
                    raise
                wait = CHUNK_RETRY_BACKOFF_S[stalls]
                stalls += 1
                self._step(out, f"{who}: the page did not answer at {offset // (1024 * 1024)}/{mb_total} MB; "
                                f"retrying the same chunk in {wait:.0f}s (attempt {stalls}/{len(CHUNK_RETRY_BACKOFF_S)})")
                waited = 0.0
                while waited < wait:
                    step = min(CHUNK_RETRY_SLICE_S, wait - waited)
                    self.sleep(step)
                    waited += step
                    self._beat()
                    self._check_cancel(out)
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
            stalls = 0
            self._beat()
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
        params = {"name": plan.name, "objective": OBJECTIVE, "status": PAUSED,
                  "special_ad_categories": [], "buying_type": "AUCTION"}
        if plan.lifetime:
            # ACL-1443: сумма на весь срок; расписание - pacing_type=day_parting (так хранит Meta).
            params["lifetime_budget"] = budget_minor(plan.lifetime_budget, self.facts.currency)
        else:
            params["daily_budget"] = budget_minor(plan.daily_budget, self.facts.currency)
        params["bid_strategy"] = BID_STRATEGY
        if plan.lifetime and plan.has_schedule:
            params["pacing_type"] = ["day_parting"]
        params["adlabels"] = [{"name": plan.label}]
        return params

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
        if ad_set.end_time:
            # ACL-1443: lifetime - конец набора с явным смещением; у daily ключа нет.
            params["end_time"] = self.end_iso(ad_set)
        if ad_set.schedule:
            params["adset_schedule"] = self.schedule_windows(ad_set)
        if ad_set.dynamic:
            # ACL-1774: несколько текстов - Dynamic Creative на уровне набора; ключа нет у прежних планов.
            params[_wpol.DYNAMIC_KEY] = True
        return params

    def _local_dt(self, ad_set: AdSetPlan, value: str, what: str) -> datetime:
        """Время набора как момент, смещение - на САМУ дату (ACL-1443).

        Lifetime (ACL-1448): введено по поясу команды (Израиль), `ad_set.time_zone`. Daily: по поясу кабинета.
        Несуществующее время (дыра перевода часов) - отказ; неоднозначное (час повторяется) - первое.
        """
        if ad_set.time_zone:
            local = _tt.localize(value, ad_set.time_zone)
            if local is None:
                raise PlanError(f"Ad set {ad_set.name!r}: the {what} time {value} does not exist in Israel time "
                                f"({ad_set.time_zone}): clocks skip that hour. Pick another time.")
            return local
        name = self.facts.timezone_name
        zone = _tt.load_zone(name)
        if zone is None:
            # Не угадываем по текущему смещению: на дату старта оно может быть другим (летнее время).
            raise PlanError(f"Ad set {ad_set.name!r}: the time zone of the ad account "
                            f"({name or 'not returned by Meta'}) is not known here, so the {what} time "
                            f"cannot be set. Clear the {what} time or try again.")
        naive = datetime.strptime(value, "%Y-%m-%dT%H:%M")
        local = naive.replace(tzinfo=zone, fold=0)
        if local.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) != naive:
            raise PlanError(f"Ad set {ad_set.name!r}: the {what} time {value} does not exist in the ad account "
                            f"time zone ({name}): clocks skip that hour. Pick another time.")
        return local

    @staticmethod
    def _zone_note(ad_set: AdSetPlan) -> str:
        return "Israel time" if ad_set.time_zone else "ad account time zone"

    def _start_dt(self, ad_set: AdSetPlan) -> datetime:
        """Время старта набора как момент: введено по часовому поясу кабинета."""
        return self._local_dt(ad_set, ad_set.start_time, "start")

    def _end_dt(self, ad_set: AdSetPlan) -> datetime:
        return self._local_dt(ad_set, ad_set.end_time, "end")

    #: ACL-2465: запас до старта, сдвинутого «на сейчас» (запись в Meta ещё идёт), и шаг округления.
    TODAY_START_MARGIN = timedelta(minutes=5)
    TODAY_START_STEP_MIN = 15

    def _moved_start(self, local_now: datetime, now: datetime) -> datetime:
        """Ближайшая четверть часа не раньше now + 5 минут (секунды округляются вверх; расчёт в UTC)."""
        t = now.astimezone(timezone.utc) + self.TODAY_START_MARGIN
        if t.second or t.microsecond:
            t = t.replace(second=0, microsecond=0) + timedelta(minutes=1)
        t += timedelta(minutes=(-t.minute) % self.TODAY_START_STEP_MIN)
        return t.astimezone(local_now.tzinfo)

    def _move_passed_start_of_today(self, ad_set: AdSetPlan) -> None:
        """Lifetime с первым днём «сегодня»: прошедший час старта сдвигается вперёд, а не стопорит публикацию.

        ACL-2465: форма задаёт старт сегодняшнего дня как «следующий целый час», а задание может ждать
        очереди (процессор, память) дольше - 17:51 отправлено со стартом 18:00, проверено в 18:06.
        Старт того же дня (по поясу команды), уже прошедший, становится ближайшей четвертью часа
        не раньше чем через 5 минут. Старт прошлых дней по-прежнему отказ; Daily не трогается.
        Сдвиг виден человеку (`_start_move_notes`), а повторный запуск сверяет такой старт мягко
        (`_moved_start_matches`): в плане остаётся исходный старт, в Meta уже сдвинутый.
        """
        if not ad_set.time_zone or not ad_set.end_time:
            return
        start, now = self._start_dt(ad_set), self.now()
        if start > now:
            return
        local_now = now.astimezone(start.tzinfo)
        if start.date() != local_now.date():
            return
        nxt = self._moved_start(local_now, now)
        new = nxt.strftime("%Y-%m-%dT%H:%M")
        log.info("ACL-2465: ad set %r start %s (%s) has passed; moved to %s", ad_set.name,
                 ad_set.start_time, ad_set.time_zone, new)
        if not hasattr(self, "_start_move_notes"):
            self._start_move_notes = []
        self._start_move_notes.append(
            f"Ad set {ad_set.name!r}: the start {ad_set.start_time[-5:]} ({self._zone_note(ad_set)}) had passed "
            f"while the job waited in the queue; moved to {new[-5:]}"
            + ("" if new[:10] == ad_set.start_time[:10] else f" on {new[:10]}") + ".")
        if not hasattr(self, "_moved_from"):
            self._moved_from = {}
        self._moved_from[id(ad_set)] = ad_set.start_time[-5:]
        ad_set.start_time = new

    def _drain_start_move_notes(self, out: Outcome) -> None:
        """Сдвиг старта - в предупреждения результата (запуск и Check plan)."""
        for note in getattr(self, "_start_move_notes", []):
            if note not in out.warnings:
                out.warnings.append(note)
        self._start_move_notes = []

    def _moved_start_matches(self, ad_set: AdSetPlan, meta_start: Any) -> bool:
        """Старт набора в Meta - это сдвинутый (ACL-2465) старт плана при повторе/продолжении?

        Первый запуск сдвинул прошедший старт сегодняшнего дня, Meta хранит сдвинутый, а билдер
        отдаёт прежний. Совпадением считается, только если ВСЁ верно: Lifetime в поясе команды;
        старт плана уже прошёл; старт Meta не раньше старта плана; он в тот же день пояса команды
        либо в первые минуты следующего (сдвиг через полночь); и не дальше, чем сдвиг мог уйти от
        «сейчас». Будущий старт плана и любая другая правка сравниваются строго.
        """
        if not ad_set.time_zone or not ad_set.end_time or not ad_set.start_time:
            return False
        try:
            plan_start = self._start_dt(ad_set)
            got = datetime.strptime(str(meta_start), "%Y-%m-%dT%H:%M:%S%z")
        except (ValueError, PlanError):
            return False
        now = self.now()
        if plan_start > now or got < plan_start:
            return False
        if got > now + self.TODAY_START_MARGIN + timedelta(minutes=self.TODAY_START_STEP_MIN):
            return False
        gl = got.astimezone(plan_start.tzinfo)
        day = plan_start.date()
        if gl.date() == day:
            return True
        return gl.date() == day + timedelta(days=1) and gl.hour == 0 and gl.minute <= (
            self.TODAY_START_MARGIN.seconds // 60 + self.TODAY_START_STEP_MIN)

    def start_iso(self, ad_set: AdSetPlan) -> str:
        """«2026-10-20T10:00:00+0200» - с явным смещением кабинета (Graph: ISO 8601)."""
        return self._start_dt(ad_set).strftime("%Y-%m-%dT%H:%M:%S%z")

    def end_iso(self, ad_set: AdSetPlan) -> str:
        return self._end_dt(ad_set).strftime("%Y-%m-%dT%H:%M:%S%z")

    def schedule_windows(self, ad_set: AdSetPlan) -> list[dict]:
        """Окна расписания набора для записи: пояс кабинета (`ADVERTISER`) добавляет бэкенд.

        ACL-1448: сетка в плане - по поясу команды; в окна кабинета её переводит `team_grid_to_account_windows`
        по разнице поясов на момент старта. Нецелая разница часов или неизвестный пояс кабинета - отказ.
        """
        if not ad_set.time_zone:
            return [{"days": list(w["days"]), "start_minute": w["start_minute"], "end_minute": w["end_minute"],
                     "timezone_type": _wpol.SCHEDULE_TZ} for w in ad_set.schedule]
        account = self.facts.timezone_name
        if _tt.load_zone(account) is None:
            raise PlanError(f"Ad set {ad_set.name!r}: the time zone of the ad account "
                            f"({account or 'not returned by Meta'}) is not known here, so the hours of the "
                            "schedule cannot be converted. Clear the schedule or try again.")
        try:
            return _tt.team_grid_to_account_windows(ad_set.schedule, ad_set.time_zone, account,
                                                    self._start_dt(ad_set))
        except _tt.ShiftError as exc:
            raise PlanError(f"Ad set {ad_set.name!r}: {exc}.") from exc

    def schedule_shift_warnings(self, plan: CampaignPlan) -> list[str]:
        """ACL-1448: предупреждения (не ошибки), если разница Израиль-кабинет меняется между стартом и концом."""
        account = self.facts.timezone_name
        if not plan.lifetime or _tt.load_zone(account) is None:
            return []
        found: list[str] = []
        for s in plan.ad_sets:
            if not (s.schedule and s.time_zone and s.start_time and s.end_time):
                continue
            try:
                note = _tt.shift_change_warning(s.time_zone, account, self._start_dt(s), self._end_dt(s))
            except PlanError:
                continue     # дыра часов покажет свой отказ в предпроверке
            if note and note not in found:
                found.append(note)
        return found

    def lifetime_days(self, plan: CampaignPlan) -> int | None:
        """Число суток срока (вверх) по самому короткому набору; нет сроков - None (ACL-1443)."""
        spans = [(self._end_dt(s) - self._start_dt(s)).total_seconds()
                 for s in plan.ad_sets if s.start_time and s.end_time]
        return max(1, math.ceil(min(spans) / 86400)) if spans else None

    def budget_floor_problem(self, plan: CampaignPlan) -> str:
        """Lifetime ниже минимума кабинета, посчитанного по ЦЕЛЫМ часам срока (ACL-1443). Нет факта - пусто.

        Минимум при записи мягче вердикта Check plan (тот считает сутки вверх): отказ только если
        бюджет меньше `min_daily_budget` x часы / 24, округлённого вверх в мелких единицах.
        """
        minimum = self.facts.min_daily_budget
        spans = [(self._end_dt(s) - self._start_dt(s)).total_seconds()
                 for s in plan.ad_sets if s.start_time and s.end_time]
        if not plan.lifetime or not minimum or not spans:
            return ""
        hours = max(1, int(min(spans) // 3600))
        need = max(minimum, -(-minimum * hours // 24))
        if budget_minor(plan.lifetime_budget, self.facts.currency) >= need:
            return ""
        cur = self.facts.currency
        return (f"The lifetime budget {plan.lifetime_budget:g} {cur} is below the ad account minimum "
                f"{minor_to_major(minimum, cur):g} {cur} a day x {hours} hours / 24 = "
                f"{minor_to_major(need, cur):g} {cur}")

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
    def rule_positions(placements: dict | None = None) -> tuple[dict, dict]:
        """Площадки двух правил: вертикальный файл — Stories и Reels, основной — всё остальное."""
        vertical = {"publisher_platforms": list(PUBLISHER_PLATFORMS),
                    "facebook_positions": list(VERTICAL_FACEBOOK),
                    "instagram_positions": list(VERTICAL_INSTAGRAM)}
        main = {"publisher_platforms": list(PUBLISHER_PLATFORMS),
                "facebook_positions": [x for x in FACEBOOK_POSITIONS if x not in VERTICAL_FACEBOOK],
                "instagram_positions": [x for x in INSTAGRAM_POSITIONS if x not in VERTICAL_INSTAGRAM]}
        if placements is not None:
            selected = placement_targeting(placements)
            for group in (main, vertical):
                for platform in PUBLISHER_PLATFORMS:
                    key = platform + "_positions"
                    group[key] = [x for x in group[key] if x in selected.get(key, [])]
                group["publisher_platforms"] = [p for p in PUBLISHER_PLATFORMS if group[p + "_positions"]]
            main = {k: v for k, v in main.items() if v} if main["publisher_platforms"] else {}
            vertical = {k: v for k, v in vertical.items() if v} if vertical["publisher_platforms"] else {}
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
            "titles": [{"text": t} for t in ad.titles],
            key: [item(media, LABEL_MAIN if ad.vertical_path else None)],
            "link_urls": [{"website_url": plan.link}], "call_to_action_types": [ad.cta],
            "ad_formats": ["SINGLE_VIDEO" if ad.kind == "video" else "SINGLE_IMAGE"]}
        if ad.bodies:  # ACL-2474: без основного текста ключ `bodies` не отправляем
            spec["bodies"] = [{"text": t} for t in ad.bodies]
        if ad.descriptions:
            spec["descriptions"] = [{"text": t} for t in ad.descriptions]
        if ad.vertical_path:
            spec[key].append(item(media["vertical"], LABEL_VERTICAL))
            label_key = "video_label" if ad.kind == "video" else "image_label"
            main_pos, vert_pos = CampaignCreator.rule_positions(ad.placements)
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
            data = {"video_id": media["video_id"], "image_url": media["thumb"], "call_to_action": cta}
            if ad.title:
                data["title"] = ad.title
            if ad.body:
                data["message"] = ad.body
            if ad.description:
                data["link_description"] = ad.description
            return {"page_id": plan.page_id, "video_data": data}
        data = {"link": INSTANT_FORM_LINK if instant else plan.link,
                "image_hash": media["image_hash"], "call_to_action": cta}
        # Основной текст и заголовок необязательны: пустое поле не отправляем.
        if ad.body:
            data["message"] = ad.body
        if ad.title:
            data["name"] = ad.title
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
    def _mk_check(checks: list[dict], obj: str, fld: str, expected: Any, actual: Any,
                  note: str = "") -> None:
        row = {"object": obj, "field": fld, "expected": expected, "actual": actual,
               "ok": _same(expected, actual), "not_read": _not_read(expected, actual)}
        if note:
            row["note"] = note
        checks.append(row)

    def _language_check(self, checks: list[dict], obj: str, want: list, got: Any) -> None:
        """ACL-1907: группа «Spanish (All)» равна набору локалей, на который её раскрыла Meta."""
        self._mk_check(checks, obj, "languages", want, got)
        groups = getattr(self.resolver, "locale_groups", None) or {}
        if not isinstance(got, list) or checks[-1]["ok"] or not any(g in groups for g in want):
            return
        norm: list = []
        unknown: list = []
        for loc in want:
            members = groups.get(loc) or {}
            if loc in groups and loc not in got:
                if members:
                    norm += list(members)
                    continue
                unknown.append(loc)
            norm.append(loc)
        if _same(sorted(set(norm)), sorted(set(got))):
            checks[-1]["ok"] = True
            checks[-1]["expanded"] = True
        elif unknown:
            checks[-1]["note"] = ("language group " + ", ".join(map(str, unknown)) + " compared strictly: "
                                  "Meta's search did not list the locales it expands into")

    def _campaign_checks(self, plan: CampaignPlan, camp: dict) -> list[dict]:
        checks: list[dict] = []

        def check(f: str, e: Any, a: Any) -> None:
            self._mk_check(checks, "campaign", f, e, a)

        want = self.campaign_params(plan)
        check("name", want["name"], camp.get("name"))
        check("status", PAUSED, camp.get("status"))
        check("objective", OBJECTIVE, camp.get("objective"))
        if plan.lifetime:
            # ACL-1443: проверяем тот тип бюджета, что задан; у daily прежние проверки без изменений.
            check("lifetime_budget", str(want["lifetime_budget"]), _amount(camp.get("lifetime_budget")))
            check("daily_budget", "0", _amount(camp.get("daily_budget")))
            if "pacing_type" in want:
                check("pacing_type", want["pacing_type"], camp.get("pacing_type") if "pacing_type" in camp else None)
        else:
            check("daily_budget", str(want["daily_budget"]), str(camp.get("daily_budget") or ""))
        check("bid_strategy", BID_STRATEGY, camp.get("bid_strategy"))
        check("tag", plan.label, plan.label if plan.label in _labels(camp) else "")
        # ACL-1772: агрегат ENABLED только когда ВСЕ наборы без ограничений площадок.
        # Ручной набор или device_platforms только mobile/desktop — ограничение: DISABLED.
        # Устройства — placement restriction по тому же первичному placement-targeting reference;
        # расчёт пользуется тем же helper, который строит фактический API payload.
        # Первичный источник: https://developers.facebook.com/docs/marketing-api/advantage-campaigns/
        # раздел Advantage+ placement state automation criteria. Сверка каждого набора остаётся ниже.
        # Нет поля — прежняя явная диагностика unverified; незнакомый/пустой ответ — расхождение.
        state = _advantage_info(camp).get("advantage_placement_state")
        if state is not None:
            automatic = bool(plan.ad_sets) and all(
                not placement_targeting(s.placements) and _placement_devices(s) == ["mobile", "desktop"]
                for s in plan.ad_sets)
            check("advantage_placement_state", "ENABLED" if automatic else "DISABLED", state)
        return checks

    def _schedule_check(self, checks: list[dict], obj: str, ad_set: AdSetPlan, row: dict) -> None:
        """Расписание набора как множество окон (день, начало, конец), без учёта порядка и группировки дней."""
        if not ad_set.schedule and not row.get("adset_schedule"):
            return
        want = _window_set(self.schedule_windows(ad_set))
        got = _window_set(row["adset_schedule"]) if "adset_schedule" in row else None
        self._mk_check(checks, obj, "adset_schedule", want, got)
        checks[-1]["show_diff"] = True

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
        if ad_set.end_time:
            # ACL-1443: lifetime - Meta должна вернуть тот же конец (сравнение в UTC, смещения разные).
            check("schedule", f"ends {_utc(self.end_iso(ad_set))}",
                  f"ends {_utc(row.get('end_time'))}" if row.get("end_time") else
                  "starts on activation, no end date")
            checks[-1]["show_diff"] = True
            self._schedule_check(checks, obj, ad_set, row)
        else:
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
        self._language_check(checks, obj, want_locales,
                             None if want_locales and "locales" not in t else t.get("locales") or [])
        check("detailed_targeting", _flex_ids(targeting.get("flexible_spec")), _flex_ids(t.get("flexible_spec")))
        check("os", targeting.get("user_os") or [], t.get("user_os") or [])
        if targeting.get("genders") or t.get("genders"):
            check("gender", targeting.get("genders") or [], t.get("genders") or [])
        if ad_set.start_time:
            if self._moved_start_matches(ad_set, row.get("start_time")):
                check("start_time", _utc(row.get("start_time")), _utc(row.get("start_time")))
            else:
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
                              found_sets: list[dict | None], *, has_ads: bool = True) -> list[dict]:
        """Что в уже существующих кампании/группах не совпадает с билдером. Только чтение.

        ACL-2578: `has_ads=False` (кампания-остаток без объявлений) - агрегат `advantage_placement_state` не
        сверяется: Meta считает его по набору с объявлениями, а площадки каждого набора сверяются отдельно ниже;
        сам агрегат проверит сверка после записи, когда объявления уже есть.

        Имя, метка, campaign_id и расписание не показываем: имя и метку доказал поиск,
        остальное сверит сверка после записи. Ничего не обновляется и не удаляется.
        """
        found: list[dict] = []
        camp = self.client.get(f"/{campaign_id}", {"fields": CAMPAIGN_FIELDS})
        self._budget_type_conflict(plan, camp)
        camp_checks = self._campaign_checks(plan, camp)
        if not has_ads:
            camp_checks = [c for c in camp_checks if c["field"] != "advantage_placement_state"]
        diffs = _describe_diffs(camp_checks)
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

    @staticmethod
    def _budget_type_conflict(plan: CampaignPlan, camp: dict) -> None:
        """ACL-1443: наша кампания с этим именем уже есть, но с бюджетом другого типа - отказ вслух.

        Тип бюджета у существующего объекта мы не меняем (Meta это не подтверждает), поэтому
        повтор не молчит и не уходит в _2: человек переименует кампанию.
        """
        daily, life = _amount(camp.get("daily_budget")) != "0", _amount(camp.get("lifetime_budget")) != "0"
        if plan.lifetime and daily and not life:
            have = "a daily budget"
        elif not plan.lifetime and life and not daily:
            have = "a lifetime budget"
        else:
            return
        raise CreationStopped(f"A campaign with this name already exists with {have} — rename the campaign. "
                              "Nothing was created.", kind="budget_type_taken")

    def _find_sets(self, plan: CampaignPlan, campaign_id: str) -> list[dict | None]:
        return [self._find_child(campaign_id, "adsets", s.name) for s in plan.ad_sets]

    def _reset_stale_facts(self, exc: BaseException) -> None:
        """ACL-2267: Meta отвергла таргетинг (по коду) - забыть термины задания из долгого кэша."""
        try:
            if "targeting" in _facts_cache.kinds_for_error(exc):
                drop = getattr(self.resolver, "drop_cached", None)
                n = drop() if drop is not None else 0
                if n:
                    log.info("facts cache: %s targeting entries dropped after a Meta refusal", n)
                    from src.fb_ads_rpa import run_telemetry

                    run_telemetry.record("api_facts_dropped", True, diag={"source": "api", "dropped": n,
                                                                          "reason": "meta_refused_targeting"})
        except Exception:  # noqa: BLE001
            log.debug("facts cache reset failed", exc_info=True)

    # -- весь путь
    def run(self, plan: CampaignPlan) -> Outcome:
        self._recovery_rows = None
        self._missing_creative_tag = False
        self._profile_page_checks = {}
        out = Outcome(campaign_name=plan.name, warnings=list(plan.warnings))
        self._op = None
        try:
            self._run(plan, out)
            self._finish_operation(out, _oj.OP_COMPLETE if not out.error else _oj.OP_STOPPED)
        except CreationStopped as exc:
            out.error, out.stop_kind = str(exc), exc.kind
            out.failure = self._failure(plan, out, exc, kind=exc.kind)
        except PlanError as exc:
            out.error, out.stop_kind = str(exc) + " Nothing more was created.", "plan"
            out.problems = list(exc.problems)
            out.failure = self._failure(plan, out, exc, kind="plan")
        except WritesBlocked as exc:
            out.error, out.stop_kind = f"Not written: {exc}", "blocked"
            out.failure = self._failure(plan, out, exc, kind="blocked")
        except GraphStop as exc:
            out.error, out.stop_kind = f"Graph API stopped: {_catalog.problem_line(exc)}", exc.kind
            out.error += self._left_in_meta(plan, out)
            out.problems = [graph_problem(exc)]
            out.failure = self._failure(plan, out, exc, kind=exc.kind)
        except GraphError as exc:
            self._reset_stale_facts(exc)
            out.error, out.stop_kind = f"Meta refused: {_catalog.problem_line(exc)}{_lookup_note(exc)}", "graph_error"
            out.problems = [graph_problem(exc)]
            out.failure = self._failure(plan, out, exc, kind="graph_error")
        except WriteUnknown as exc:
            out.error, out.stop_kind = f"Write result unknown: {exc}", "unknown"
            out.failure = self._failure(plan, out, exc, kind="unknown")
        except Exception as exc:  # noqa: BLE001 - ACL-1856: частичный результат не теряем из-за непредвиденного сбоя
            log.exception("campaign creation failed unexpectedly")
            out.error, out.stop_kind = MSG_UNEXPECTED, "unexpected"
            self._finish_operation(out, _oj.OP_FAILED, note=type(exc).__name__)
            self._snapshot_operation(out)
            return out
        if out.error:
            self._finish_operation(out, _oj.OP_STOPPED, note=out.stop_kind)
        self._snapshot_operation(out)
        return out

    def _check_reviewed_ids(self, plan: CampaignPlan) -> None:
        """ACL-2353: Page и пиксель, выбранные сейчас («Automatic», «account default»), совпадают с показанными
        в ревью. Иначе стоп ДО первой записи: человек одобрял другое."""
        if self.reviewed is None:
            return
        from src.web import publish_review

        problem = publish_review.ids_mismatch(self.reviewed, plan.name, page_id=plan.page_id,
                                              pixel_id=plan.pixel_id)
        if problem:
            raise CreationStopped(problem, kind="review_stale")

    @staticmethod
    def _failure(plan: CampaignPlan, out: Outcome, exc: BaseException, *, kind: str) -> Any:
        """ACL-2392: the plain-English version of a stop, with what it left in Meta (raw text only in `details`)."""
        stage = getattr(exc, "stage", "") or CampaignCreator._stage_from_outcome(out)
        failure = _plain.classify(exc, kind=kind, ctx={"stage": stage})
        if failure.key == "targeting_merged":
            planned = [str(n) for s_ in plan.ad_sets for g in s_.interest_groups for n in g]
            hits, note = _plain.offending_note(planned, str((failure.details or {}).get("error_user_msg") or ""))
            failure.details.update({"offending": hits, "offending_note": note})
        if out.campaign_id and not failure.left:
            failure.left = _plain.left_in_meta(
                campaign=True, adsets=len(out.adset_ids), ads=len(out.ad_ids), name=plan.name,
                empty_note=failure.key == "targeting_merged",
                not_created=[ad.index for s_ in plan.ad_sets for ad in s_.ads if ad.index not in out.ad_media])
        return failure

    @staticmethod
    def _stage_from_outcome(out: Outcome) -> str:
        """A stop outside a write: where the run was, from what it had created so far."""
        if not out.campaign_id:
            return "before the campaign was created"
        if not out.adset_ids:
            return "after the campaign was created, before any ad set"
        if not out.ad_ids:
            return "after the ad set was created, before any ad"
        return "after some ads were created"

    @staticmethod
    def _left_in_meta(plan: CampaignPlan, out: Outcome) -> str:
        """ACL-2301: what a Graph stop left in Meta and what was not reached (English only).

        Empty when nothing was created. The ads of the plan without an entry in `ad_media` were
        not created; the next campaigns of the job are reported "Not started" by the publisher.
        """
        if not out.campaign_id:
            return ""
        todo = [ad for s_ in plan.ad_sets for ad in s_.ads if ad.index not in out.ad_media]
        text = (" What was already created stays in Meta (campaign PAUSED); \"Publish again\" reuses it "
                "and does not create duplicates.")
        if todo:
            text += " Not created: " + ", ".join(f"ad {ad.index}" for ad in todo) + "."
        return text

    def _video_for_upload(self, out: Outcome, path: str, who: str) -> str:
        """ACL-2000: тяжёлое видео сжимается тем же ffmpeg-сжимателем, что и в RPA-пути (кэш на исходник).

        Возвращает путь файла, который уйдёт в Meta. Нет ffmpeg / сжатие не вышло - исходник и
        предупреждение (не стоп). Копия в кэше детерминирована, поэтому повтор шлёт те же байты и
        находит уже загруженное видео по названию (md5 отправленных байт).
        """
        from src.fb_ads_rpa import creative_compress as _cc

        limit = _cc.DEFAULT_LIMIT_MB
        raw = os.environ.get("ACL_VIDEO_COMPRESS_ABOVE_MB", "").strip()
        try:
            if raw and float(raw) > 0:
                limit = float(raw)
        except ValueError:
            pass
        fn = self.compress_video or (lambda p: _cc.ensure_uploadable(p, limit_mb=limit))
        try:
            prepared = fn(path)
        except Exception as exc:  # noqa: BLE001 - сжатие не должно останавливать задание
            if type(exc).__name__ == "JobCancelled":
                raise
            out.warnings.append(f"{who}: video compression failed ({type(exc).__name__}); uploading the original")
            return path
        if getattr(prepared, "compressed", False):
            self._step(out, f"{who}: video compressed {prepared.original_mb:.0f} MB -> {prepared.sent_mb:.1f} MB")
            return prepared.path
        if prepared.original_mb > limit:
            out.warnings.append(f"{who}: the video is {prepared.original_mb:.0f} MB and was not compressed "
                                f"({prepared.reason or 'unknown reason'}); uploading the original, "
                                "which can take a long time")
        return path

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
                            "Nothing was created.", key="page_unavailable")
        cache = self.__dict__.setdefault("_profile_page_checks", {})
        key = (plan.act, pid, plan.link)
        if key not in cache:
            cache[key] = _pg.check_usable(self.client, pid, self.facts.page_names.get(pid, ""),
                                          act=plan.act, link=plan.link)
        if cache[key]:
            raise PlanError(f"The Page {name!r} ({pid}) belongs to the profile, not to this ad account, and "
                            f"cannot be used for ads here: {cache[key]}. Choose another Page or add this Page "
                            "to the ad account in Business Manager. Nothing was created.", key="page_unavailable")

    def _converted_ext(self, path: str) -> str:
        return self.__dict__.get("_image_ext", {}).get(path, "")

    def _meta_image(self, out: Outcome, path: str, data: bytes, who: str) -> bytes:
        """ACL-2575: WebP/AVIF/HEIC -> PNG/JPEG in memory before any write (the file on disk is not touched).

        The conversion is listed in the warnings; the library name keeps the new extension (`_image_ext`).
        A file that cannot be converted stops the run here with its name and reason: nothing was created."""
        try:
            done = _imgconv.convert(path, data, IMAGE_MAX_BYTES, who)
        except _imgconv.ImageConversionError as exc:
            err = PlanError(str(exc) + " Nothing was created.", key="creative_file")
            err.file_name = Path(path).name
            raise err from exc
        if done is None:
            return data
        self.__dict__.setdefault("_image_ext", {})[path] = done.ext
        if done.note not in out.warnings:
            out.warnings.append(done.note)
        return done.data

    def _prepare(self, plan: CampaignPlan, out: Outcome, *,
                 writing: bool = True) -> tuple[list[dict], dict[str, bytes]]:
        """Всё, что проверяется до записи: страница, пиксель, таргетинг, файлы.

        `writing=False` - пробный запуск (Check plan): записей не будет, и бюджет на них не сверяется.
        """
        calls_at_start = getattr(self.client, "calls", 0)
        out.warnings += resolve_page_pixel(plan, self.facts)
        out.warnings += self.schedule_shift_warnings(plan)
        self.check_profile_page(plan)
        try:
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
        finally:
            self._note_targeting_warnings(out)
        self._step(out, "preflight: targeting resolved")
        self._require_instagram(plan, targetings, out)
        self._page_forms = None
        self._form_steps = {}
        self._form_warnings = []
        self._cover_ids = {}
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
                    src = (self._video_for_upload(out, ad.path, f"ad {ad.index}")
                           if writing and ad.kind == "video" else ad.path)
                    data = self.load_media(src, ad.kind)
                except OSError as exc:
                    raise PlanError(f"ad {ad.index}: creative file cannot be read ({exc})", key="creative_file") from exc
                limit = VIDEO_MAX_BYTES if ad.kind == "video" else IMAGE_MAX_BYTES
                if not data or len(data) > limit:
                    raise PlanError(f"ad {ad.index}: creative file is empty or larger than "
                                    f"{limit // (1024 * 1024)} MB", key="creative_file")
                if ad.kind != "video":
                    data = self._meta_image(out, ad.path, data, f"ad {ad.index}")
                files[ad.path] = data
                if ad.vertical_path:
                    try:
                        vsrc = (self._video_for_upload(out, ad.vertical_path, f"ad {ad.index} Stories / Reels")
                                if writing and ad.vertical_kind == "video" else ad.vertical_path)
                        vdata = self.load_media(vsrc, ad.vertical_kind)
                    except OSError as exc:
                        raise PlanError(f"ad {ad.index}: Stories / Reels file cannot be read ({exc})", key="creative_file") from exc
                    vlimit = VIDEO_MAX_BYTES if ad.vertical_kind == "video" else IMAGE_MAX_BYTES
                    if not vdata or len(vdata) > vlimit:
                        raise PlanError(f"ad {ad.index}: Stories / Reels file is empty or larger than "
                                        f"{vlimit // (1024 * 1024)} MB", key="creative_file")
                    if ad.vertical_kind != "video":
                        vdata = self._meta_image(out, ad.vertical_path, vdata, f"ad {ad.index} Stories / Reels file")
                    if len(vdata) == len(data) and _md5(vdata) == _md5(data):
                        # ACL-2084: копия того же файла под другим именем. Meta отказывает в ленте с одним
                        # хэшем под двумя метками («All ad asset values must be unique», живой прогон
                        # 2026-11-04), поэтому файл идёт один раз - как тот же путь (ACL-2211).
                        # md5 только при равной длине: обычная пара хэширование не оплачивает.
                        ad.vertical_path = ad.vertical_kind = ""
                        out.warnings.append(f"Ad {ad.index}: the same file was selected as main and Stories / Reels; "
                                            "it will be used once as the main file.")
                        continue
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
        # Не хватает - отказ ДО первой записи. ACL-2370: Check plan считает остаток той же формулой, что
        # и запуск (раньше - фиксированные 40, и проверка принимала то, что запуск потом отказывал).
        # Здесь куски без запаса на повторы: это страховка, размер после сжатия может отличаться.
        if isinstance(budget, int) and chunk_calls:
            rest = estimate_create_calls(plan)
            if chunk_calls + rest > budget:
                raise PlanError(f"Large videos need about {chunk_calls} Meta calls to upload in chunks "
                                f"and the rest of the campaign about {rest}, more than "
                                f"one run allows ({budget}). Use a smaller video or split the campaign. "
                                "Nothing was created.")
            if not writing:
                # ACL-2370: Check plan показывает, сколько вызовов съест большое видео (верхняя оценка).
                sizes = [len(v) for v in files.values() if isinstance(v, LargeFile)]
                n_chunks = sum(-(-n // VIDEO_CHUNK_MAX) for n in sizes)
                out.warnings.append(
                    f"Large video: about {chunk_calls} Meta calls to upload in {n_chunks} chunks of "
                    f"{VIDEO_CHUNK_MAX // (1024 * 1024)} MB, {chunk_calls + rest} of {budget} calls planned "
                    "for this run (upper bound; the file may be compressed before upload).")
        self._check_planned(plan, targetings, early=True)
        self._check_meta_rules(plan, targetings)
        self._step(out, "preflight: Meta rules registry")
        if writing:
            self._require_remaining_budget(plan, chunk_calls, calls_at_start)
        return targetings, files

    def _require_remaining_budget(self, plan: CampaignPlan, chunk_calls: int, calls_at_start: int = 0) -> None:
        """ACL-2244: stop before the first write when preparation ate into the calls the writes were sized with.

        The run budget is sized as writes + targeting reads + chunks (`publish_api.create_call_budget`), and
        `estimate_create_calls` is a deliberately generous upper bound of the writes. So the remaining calls are
        compared with that bound only when preparation spent MORE than the budget reserved for it (targeting
        reads, the page and form reads); ordinary preparation reads never refuse a campaign that publishes fine.
        An operator cap below the sized budget is not checked here: it stops midway, as always.
        """
        budget = getattr(self.client, "max_calls", None)
        used = getattr(self.client, "calls", 0)
        if not isinstance(budget, int) or not isinstance(used, int):
            return
        spent = used - calls_at_start
        reserved = estimate_targeting_reads(plan) + CALLS_PREPARE_EXTRA_READS
        if any(x.instant for x in plan.ad_sets):
            reserved += CALLS_INSTANT_FORM          # the form reads are spent here, and counted in `need` as well
        if spent <= reserved:
            return
        remaining = budget - used
        need = estimate_create_calls(plan) + chunk_calls
        if need > remaining:
            raise PlanError(f"The Meta call budget left for this run is too small for this campaign: it needs about "
                            f"{need} calls, {remaining} remain, so {need - remaining} are missing. "
                            "Nothing was created; split the campaign or try again.")

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
                                    "Page; pick one of the Page's forms (Refresh forms).", key="form_unavailable")
                if not form.usable:
                    raise PlanError(f"Ad {ad.index}: the Instant Form {form.name!r} is "
                                    f"{form.status.lower() or 'of unknown status'} (only ACTIVE forms can be "
                                    "used); pick another form.", key="form_unavailable")
        self._form_steps = {}
        for tid, tpl in templates.items():
            # ACL-2188: картинка чужой Страницы - стоп до поиска формы и до любой записи (строки, не числа).
            if tpl.get("cover_photo_id") and str(tpl.get("cover_page_id") or "") != str(plan.page_id):
                raise PlanError(_fc.MSG_COVER_OTHER_PAGE.format(name=tpl["name"]))
            existing, why = _fc.match_existing(read, tpl)
            if why:
                raise PlanError(why + ".")
            fname = _ft.form_name(tpl)
            if existing:
                self._refuse_existing_without_cover(plan, tpl, existing)
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

    def _refuse_existing_without_cover(self, plan: CampaignPlan, tpl: dict, existing_id: str) -> None:
        """ACL-2579: a same-name form without the template's background is NOT reused (stop before any write).

        The name carries the template hash, so a form of this name may exist without a background (made by a run
        where the picture was missing, or one Meta stored without it). Reusing it would publish ads with the wrong
        form. Only a readable form that shows no / another picture stops; an unreadable one keeps the old
        behaviour (reuse, with a note after the readback).
        """
        want = str(tpl.get("cover_photo_id") or "")
        sha = str(tpl.get("cover_local_sha256") or "")
        if not want and sha:
            want = _cover_store.cached_photo(plan.page_id, sha) or ""
        if not want and not sha:
            return
        if not want and not _cover_store.exists(tpl["id"], sha):
            return      # ACL-2571: the picture is on another computer: this run makes/reuses a form without it
        try:
            row = self.client.get(f"/{existing_id}", {"fields": _fc.COVER_ONLY_FIELDS})
        except GraphError:
            return
        if _fc.existing_cover_state(tpl, row, want) in ("missing", "different"):
            raise PlanError(_fc.MSG_COVER_MISSING_ON_EXISTING.format(name=_ft.form_name(tpl)))

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

    def _upload_form_covers(self, out: Outcome, plan: CampaignPlan) -> None:
        """ACL-2571: upload the local picture of every template whose form will be CREATED on this Page.

        Once per Page + picture hash: a cached photo id is reused without an upload. A refusal stops the
        campaign here, before any object is created, with Meta's own answer.
        """
        seen: dict[str, dict] = {}
        for ad_set in plan.ad_sets:
            for ad in ad_set.ads:
                if ad.form_template and not ad.form_id:
                    seen.setdefault(ad.form_template["id"], ad.form_template)
        for tid, tpl in seen.items():
            sha = str(tpl.get("cover_local_sha256") or "")
            step = self._form_steps.get(tid)
            if not sha or step is None or step.action != "create":
                continue
            cached = _cover_store.cached_photo(plan.page_id, sha)
            if cached:
                self._cover_ids[tid] = cached
                self._step(out, f"form {step.form_name!r}: background picture already on this Page, reused (no upload)")
                continue
            data = _cover_store.read(tid, sha)
            mime, bad = _cover_store.check_file(data or b"")
            if data is None or bad:
                raise CreationStopped(_fc.MSG_COVER_FILE_LOST.format(name=tpl["name"]) if data is None
                                      else f"{bad} Nothing was created.", created=out.created, kind="form_cover_refused")
            try:
                got = self.client.upload_form_photo(plan.page_id, data, mime=mime)
            except (GraphError, WriteUnknown, FormPhotoRefused) as exc:
                raise CreationStopped(_fc.MSG_COVER_UPLOAD_REFUSED.format(name=tpl["name"], why=str(exc)[:300]),
                                      created=out.created, kind="form_cover_refused") from exc
            try:
                _cover_store.remember_photo(plan.page_id, sha, got["photo_id"])
            except OSError:
                pass        # only a cache: the next publish to this Page uploads again
            self._cover_ids[tid] = got["photo_id"]
            self._step(out, f"form {step.form_name!r}: background picture uploaded to the Page")

    def _find_form_id(self, plan: CampaignPlan, tpl: dict) -> str | None:
        """Сверка чтением списка форм Страницы: id своей формы по точному имени."""
        read = _forms.read_page_forms(self.client, plan.page_id)
        if not read.ok:
            raise GraphError(read.message)
        existing, _why = _fc.match_existing(read, tpl)
        return existing or None

    def _verify_form(self, out: Outcome, plan: CampaignPlan, tpl: dict, form_id: str, reused: bool = False,
                     cover_id: str = "") -> None:
        """Форма ACTIVE, на нашей Странице, вопросы как в шаблоне; иначе стоп.

        Картинка (ACL-2188): у новой формы расхождение - стоп, у переиспользованной - заметка и дальше
        (фото могли удалить со Страницы, рабочая форма не должна блокировать каждую публикацию).
        """
        # ACL-2571: the Page photo id of a local picture is known only at publish; the name stays the template's.
        cov = dict(tpl, cover_photo_id=cover_id) if cover_id else tpl
        row = self.client.get(f"/{form_id}", {"fields": _fc.readback_fields(cov)})
        bad = _fc.compare_form(tpl, plan.page_id, row)
        cover = _fc.compare_cover(cov, row)
        if cover and reused and not bad:
            self._step(out, f"form {_ft.form_name(tpl)!r}: {_fc.MSG_COVER_UNCONFIRMED}")
            cover = []
        bad += cover
        logic = _fc.compare_logic(tpl, row)     # ACL-2178: a changed or missing condition always stops the run
        bad += logic
        if bad:
            tail = (" The form stays on the Page; the next run reuses it with a note."
                    if "background image" in bad and not reused else "")
            if logic and not reused:
                tail += " The form stays on the Page; it can't be edited or deleted by the robot."
            raise CreationStopped(f"The form {_ft.form_name(tpl)!r} was read back with differences ("
                                  + "; ".join(bad) + "). Nothing that depends on it was created." + tail,
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
                cover_id = self._cover_ids.get(tpl["id"], "")
                try:
                    step.form_id = self._write(
                        out, f"form {step.form_name!r}", f"/{plan.page_id}/{_wp_form_edge()}",
                        _ft.form_params(tpl, cover_id), lambda: self._find_form_id(plan, tpl),
                        form_page=plan.page_id)
                except GraphError as exc:
                    _fc.clear_pending(plan.act, plan.page_id, step.form_name)   # Meta отказала: формы нет
                    if cover_id and tpl.get("cover_local_sha256"):
                        # The cached photo may be gone from the Page: the next publish uploads again.
                        _cover_store.forget_photo(plan.page_id, tpl["cover_local_sha256"])
                    raise CreationStopped(_fc.refused_message(exc, dict(tpl, cover_photo_id=cover_id) if cover_id else tpl),
                                          created=out.created, kind="form_refused") from exc
                _fc.clear_pending(plan.act, plan.page_id, step.form_name)
                out.created.setdefault("forms", []).append(step.form_id)
                self._step(out, f"form {step.form_name!r}: created")
            self._verify_form(out, plan, tpl, step.form_id, reused=bool(step.existing_id),
                              cover_id=self._cover_ids.get(tpl["id"], ""))
            self._step(out, f"form {step.form_name!r}: read back, ACTIVE, on the Page, questions match")
        ad.form_id = step.form_id

    def _check_planned(self, plan: CampaignPlan, targetings: list[dict],
                       found_sets: list | None = None, *, early: bool = False) -> None:
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
        floor = self.budget_floor_problem(plan)
        if floor:
            raise PlanError(floor)
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
        found = list(found_sets or [])
        for pos, (ad_set, targeting) in enumerate(zip(plan.ad_sets, targetings)):
            # Набор из прошлого прогона уже стартовал: его старт в прошлом - не причина отказа.
            # `early` - вызов до поиска по имени (found_sets ещё неизвестны): старт проверит поздний вызов.
            reused = pos < len(found) and bool(found[pos])
            if ad_set.start_time and not early and not reused:
                self._move_passed_start_of_today(ad_set)
            if ad_set.start_time and not early and not reused and self._start_dt(ad_set) <= self.now():
                raise PlanError(f"Ad set {ad_set.name!r}: the start time {ad_set.start_time} "
                                f"({self._zone_note(ad_set)}) is not in the future")
            if ad_set.end_time:
                # ACL-1443: конец позже старта по настоящим моментам (смещения на свои даты) и в будущем.
                if ad_set.start_time and self._end_dt(ad_set) <= self._start_dt(ad_set):
                    was = getattr(self, "_moved_from", {}).get(id(ad_set), "")
                    moved = f" (moved from {was} because it had passed)" if was else ""
                    raise PlanError(f"Ad set {ad_set.name!r}: the end time {ad_set.end_time} must be "
                                    f"after the start time {ad_set.start_time}{moved} ({self._zone_note(ad_set)})")
                if self._end_dt(ad_set) <= self.now():
                    raise PlanError(f"Ad set {ad_set.name!r}: the end time {ad_set.end_time} "
                                    f"({self._zone_note(ad_set)}) is not in the future")
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
                    "link": (plan.link if ad.uses_feed else
                             data.get("link") or (cta.get("value") or {}).get("link")),
                    "placements": aset["targeting"],
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
        self._recovery_rows = None
        self._missing_creative_tag = False
        self._profile_page_checks = {}
        out = Outcome(campaign_name=plan.name, warnings=list(plan.warnings))
        differences: list | None = None
        files: dict | None = None
        renamed = name_error = ""
        requested, renamed_choice = plan.name, False
        try:
            targetings, files = self._prepare(plan, out, writing=False)
            choice = self._resolve_name(plan, targetings)
            plan, existing, renamed_choice = choice.plan, choice.existing, choice.renamed
            out.published_as = plan.name
            self._check_planned(plan, targetings, choice.found_sets)
            self._drain_start_move_notes(out)
            differences = []
            if choice.renamed:
                renamed = _rename_message(requested, plan.name, choice.earlier, future=True)
                out.warnings.append(renamed)
                for note in choice.notes:
                    self._step(out, note)
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
                                "library_name": (video_title(plan.video_tag, data) if ad.kind == "video"
                                                 else image_name(ad.path, data, self._converted_ext(ad.path))),
                                "creative": (self.creative_params(plan, ad, ad_set, media, ad.name)
                                             if ad.uses_feed else self._story(plan, ad, ad_set, media))})
                sets.append({**self.adset_params(plan, ad_set, "<campaign>", targeting), "ads": ads})
            out.plan = {"campaign": self.campaign_params(plan),
                        "existing_campaign_id": str(existing["id"]) if existing else "",
                        "ad_sets": sets,
                        "summary": self.plan_summary(plan, targetings, files,
                                                     existing=bool(existing),
                                                     existing_differences=differences,
                                                     requested_name=requested if renamed_choice else "")}
            self._step(out, "dry run: preflight passed, nothing was written")
        except CreationStopped as exc:
            out.error, out.stop_kind = str(exc), exc.kind
            out.failure = self._failure(plan, out, exc, kind=exc.kind)
            if exc.kind in NAME_STOPS:
                name_error = str(exc)
        except PlanError as exc:
            out.error, out.stop_kind = str(exc), "plan"
            out.problems = list(exc.problems)
            out.failure = self._failure(plan, out, exc, kind="plan")
        except GraphStop as exc:
            out.error, out.stop_kind = f"Graph API stopped: {_catalog.problem_line(exc)}", exc.kind
            out.problems = [graph_problem(exc)]
            out.failure = self._failure(plan, out, exc, kind=exc.kind)
        except GraphError as exc:
            self._reset_stale_facts(exc)
            out.error, out.stop_kind = f"Meta refused: {_catalog.problem_line(exc)}{_lookup_note(exc)}", "graph_error"
            out.problems = [graph_problem(exc)]
            out.failure = self._failure(plan, out, exc, kind="graph_error")
        out.targeting = self._targeting_report()
        self._audit(plan, out, existing=differences, checked=differences is not None, files=files,
                    renamed=renamed, name_error=name_error)
        return out

    def _note_targeting_warnings(self, out: Outcome) -> None:
        """ACL-1920: отказ Meta в необязательном поиске - предупреждение, а не ошибка (без дублей)."""
        fn = getattr(self.resolver, "targeting_warnings", None)
        try:
            for w in (fn() if fn else []):
                if w not in out.warnings:
                    out.warnings.append(w)
        except Exception:  # noqa: BLE001 — предупреждения вспомогательные
            pass

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
        if out.stop_kind not in ("", "plan", "graph_error", *NAME_STOPS):
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
        if ad_set.end_time:
            row["end_time"] = self.end_iso(ad_set)
        if ad_set.schedule:
            row["schedule"] = _window_set(self.schedule_windows(ad_set))
        if ad_set.time_zone:
            # ACL-1448: время введено по поясу команды; расписание выше уже в часах кабинета.
            row["time_zone"] = ad_set.time_zone

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
            "budget_minor": budget_minor(plan.budget_amount, self.facts.currency),
            "page_id": plan.page_id, "pixel_id": plan.pixel_id,
            "page_source": plan.page_source, "pixel_source": plan.pixel_source,
            "existing": existing, "existing_differences": list(existing_differences or []),
            "tag": plan.label, "ad_sets": sets,
        }
        if plan.lifetime:
            # ACL-1443: ключи только у lifetime-плана; суммарный итог - на весь срок.
            summary["budget_type"] = BUDGET_LIFETIME
            summary["lifetime_budget"] = plan.lifetime_budget
            del summary["daily_budget"]
            # Пояс и минимум кабинета - страница считает по ним сроки и минимум (минимум в мелких единицах);
            # у daily-плана этих ключей нет: он не меняется ни байтом.
            summary["timezone_name"] = self.facts.timezone_name
            summary["timezone_offset_hours_utc"] = self.facts.utc_offset_hours
            summary["min_daily_budget"] = self.facts.min_daily_budget
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
        # ACL-1856: the request as submitted - before name choice and before any start-time move.
        submitted = (self._plan_view(plan), plan.tag)
        targetings, files = self._prepare(plan, out)
        self._check_reviewed_ids(plan)
        # ACL-2571: the picture of a shared template goes to this Page BEFORE the first object is created.
        self._upload_form_covers(out, plan)

        # 2. Имя (ACL-1902: NAME, NAME_2 ...) и кампания (наша - по имени и метке).
        #    Найденные свои кампания/группы уже сверены с билдером внутри выбора имени - до записей.
        choice = self._resolve_name(plan, targetings)
        base_plan = plan
        requested, plan = plan.name, choice.plan
        out.published_as = plan.name
        out.copy_number = choice.copy_number
        self._check_planned(plan, targetings, choice.found_sets)
        self._drain_start_move_notes(out)
        self._save_name_receipt(out, requested, choice)
        # ACL-1856: журнал по итоговому плану (имя уже выбрано) и сверка прошлых этапов - до первой записи.
        existing_id = str(choice.existing["id"]) if choice.existing else ""
        for _attempt in range(4):
            try:
                adopt = self._open_operation(plan, out, files, submitted=submitted, existing_id=existing_id)
                break
            except (_SwitchCopy, _AliveUnknown) as sw:
                if isinstance(sw, _SwitchCopy):
                    # ACL-2583: the operation already wrote (or may have written) the campaign under a higher copy
                    # name than the lagging name choice: switch the rest of the run to that copy plan (labels,
                    # names, receipt), never back to the base name.
                    n = sw.copy_number
                else:
                    # ACL-2583 (owner decision): the earlier campaign of the journal cannot be read even after the
                    # retries. No stop: a new operation under a copy name that differs from the earlier one.
                    n = max(choice.copy_number, sw.copy_number) + 1
                plan = copy_plan(base_plan, n)
                self._check_name_length(plan)
                choice = NameChoice(plan, None, [None] * len(plan.ad_sets), n, choice.earlier + 1)
                out.published_as, out.copy_number, existing_id = plan.name, n, ""
                self._check_planned(plan, targetings, choice.found_sets)
                self._save_name_receipt(out, requested, choice)
                if isinstance(sw, _AliveUnknown):
                    out.warnings.append(MSG_CONTINUED_AS_COPY.format(name=plan.name))
        else:
            raise CreationStopped(MSG_CAMPAIGN_UNKNOWN_STOP.format(name=plan.name), created=out.created,
                                  kind="unknown")
        if adopt and not existing_id:
            # The name search missed our live campaign: resume into it, never create a second one (ACL-1856).
            found_sets, _partial, why = self._resume_state(plan, targetings, adopt)
            if why:
                self._snapshot_operation(out)
                raise CreationStopped(MSG_MISMATCH.format(name=plan.name), created=out.created,
                                      kind="resume_mismatch")
            choice = NameChoice(plan, {"id": adopt, "name": plan.name}, found_sets, choice.copy_number)
            existing_id = adopt
        self._verify_resume(plan, out, existing_id)
        if choice.renamed:
            out.warnings.append(_rename_message(requested, plan.name, choice.earlier, future=False))
            self._step(out, f"campaign name {requested!r} is taken: using {plan.name!r}")
            for note in choice.notes:
                self._step(out, note)
        existing, found_sets = choice.existing, choice.found_sets
        if existing:
            out.campaign_id = str(existing["id"])
            out.reused["campaign"] = out.campaign_id
            self._step(out, "campaign: found from a previous run")
            self._step(out, "existing campaign and ad sets match the builder")
        else:
            rewrites = 0
            while True:
                try:
                    out.campaign_id = self._write(
                        out, "campaign", f"/{plan.act}/campaigns", self.campaign_params(plan),
                        self._campaign_finder(plan, base_plan))
                    break
                except _CampaignRewrite as rw:
                    # ACL-2583: the campaign write is unknown and not found: never the same name again. Set the old
                    # operation aside and continue as a new one under the next copy name (bounded).
                    rewrites += 1
                    if rewrites > MAX_CAMPAIGN_REWRITES:
                        self._snapshot_operation(out)
                        raise CreationStopped(MSG_CAMPAIGN_UNKNOWN_STOP.format(name=plan.name),
                                              created=out.created, kind="unknown") from None
                    old_name = plan.name
                    n = max(choice.copy_number, rw.copy_number,
                            name_copy_number(old_name, base_plan.name)) + 1
                    plan = copy_plan(base_plan, n)
                    self._check_name_length(plan)
                    choice = NameChoice(plan, None, [None] * len(plan.ad_sets), n, choice.earlier + 1)
                    out.published_as, out.copy_number = plan.name, n
                    found_sets = choice.found_sets
                    self._check_planned(plan, targetings, found_sets)
                    self._save_name_receipt(out, requested, choice)
                    try:
                        self._op = self.journal.supersede(self._op, request_id=self.request_key)
                        self._op.set_published(plan.name, n)
                    except _oj.JournalError as exc:
                        self._journal_event("journal_save_failed", what="open", phase="supersede",
                                            error=type(exc).__name__)
                        raise CreationStopped(MSG_JOURNAL, created=out.created, kind="journal") from exc
                    out.warnings.append(MSG_CAMPAIGN_REWRITTEN.format(old=old_name, name=plan.name))
                    self._step(out, f"campaign: not found, creating a new one as {plan.name!r}")
                    self._journal_event("unknown_rewritten", what="campaign", renamed_to=plan.name)
                    self._snapshot_operation(out)
            out.created["campaign"] = out.campaign_id
            self._attach_name_receipt(out, plan.act, requested)
            self._verify_label(out, f"The campaign {plan.name!r}", out.campaign_id, plan.label)
            self._step(out, "campaign: created PAUSED, tag kept")

        # 3. Группы и объявления (дети нашей кампании — по точному имени, B1a).
        ad_label_checked = False
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
                rec["file_md5"] = _md5(data)  # ACL-2347: хэш уже считается для имён, второго чтения нет
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
                self._ensure_form(out, plan, ad)
                creative_id, recovered = self._recoverable_creative(
                    out, plan, ad, ad_set, media, creative_name)
                step = "creative created" if creative_id in out.created.get("creatives", []) else "creative found from a previous run"
                self._step(out, f"ad {ad.index}: {step}")
                if recovered:
                    rec["creative_content_verified"] = self.creative_params(plan, ad, ad_set, media, creative_name)
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
        # ACL-2250: сверка дошла до конца (расхождения - тоже конец: всё записано, дописывать нечего).
        out.name_receipt_done = (plan.act, requested)

    # ───────────────────────── сверка ─────────────────────────
    @staticmethod
    def _form_checks(check: Callable[[str, str, Any, Any], None], aobj: str, ad: AdPlan, cta: dict,
                     page_forms: _forms.FormsRead | None) -> None:
        """Instant Form в креативе: тот ли id и числится ли он среди форм Страницы (ACL-1775)."""
        check(aobj, "lead_gen_form_id", ad.form_id, str((cta.get("value") or {}).get("lead_gen_form_id") or ""))
        on_page = bool(page_forms and page_forms.ok and ad.form_id in page_forms.by_id())
        check(aobj, "form_on_page", True, on_page)

    def _video_file_checks(self, check: Callable[[str, str, Any, Any], None], aobj: str,
                           id_field: str, content_field: str, uploaded_id: str, actual_id: str,
                           tag: str, data: Any, identities: list[dict] | None = None) -> None:
        """ACL-2224: a creative may reference a copy, not the uploaded video ID.

        Verify the returned video's existing deterministic content title. A changed
        ID is acceptable only with that positive evidence; never infer equivalence
        from a label binding, a successful upload or an unreadable/missing title.
        Titles are application-generated file fingerprints, not downloaded bytes.
        """
        expected_title = video_title(tag, data) if data else ""
        actual_title = (self.client.get(f"/{actual_id}", {"fields": "title"}).get("title")
                        if data and actual_id else "")
        verified = bool(expected_title and actual_title == expected_title)
        if uploaded_id and (str(uploaded_id) == actual_id or not verified):
            check(aobj, id_field, str(uploaded_id), actual_id)
        if data:
            check(aobj, content_field, expected_title, actual_title)
        if identities is not None:
            identities.append({"object": aobj, "field": id_field,
                               "uploaded_id": str(uploaded_id or ""),
                               "creative_video_id": actual_id,
                               "content_verified": verified,
                               "verified_by": "content_title" if verified else "unverified"})

    def _feed_checks(self, check: Callable[[str, str, Any, Any], None], aobj: str, plan: CampaignPlan,
                     ad: AdPlan, creative: dict, rec: dict, files: dict[str, bytes],
                     identities: list[dict] | None = None) -> list[tuple[str, str, str]]:
        """Сверка ленты ассетов (ACL-1774): все тексты как набор и привязка файлов к площадкам.

        Возвращает картинки, чей хэш не равен md5 нашего файла, — по ним ещё сверяется имя в библиотеке.
        """
        fs = creative.get("asset_feed_spec") or {}
        if not isinstance(fs, dict):
            fs = {}

        def texts(key: str) -> list[str]:
            return [str(x.get("text") or "") for x in (fs.get(key) or []) if isinstance(x, dict)]

        def nonblank(values: list[str]) -> list[str]:
            # ACL-2290: Meta returns [' '] for "none"; whitespace-only values count as empty on both sides.
            return [v for v in values if str(v).strip()]

        check(aobj, "primary_texts", nonblank(list(ad.bodies)), nonblank(texts("bodies")))
        check(aobj, "headlines", nonblank(list(ad.titles)), nonblank(texts("titles")))
        check(aobj, "descriptions", nonblank(list(ad.descriptions)), nonblank(texts("descriptions")))
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
            data = files.get(path, b"")
            if ad.kind == "video":
                self._video_file_checks(check, aobj, name, name + "_content", made, got,
                                        plan.video_tag, data, identities)
            else:
                if made:
                    check(aobj, name, str(made), got)
                if data and got and got != _md5(data):
                    extra.append((aobj, path, got))
        if ad.vertical_path:
            # Какие площадки получили какой файл: правило на метку должно нести ровно наши площадки.
            main_pos, vert_pos = self.rule_positions(ad.placements)
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
                      {k: sorted(want.get(k) or []) for k in ("publisher_platforms", "facebook_positions", "instagram_positions")}, rules.get(label, {}))
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
                          "creative{id,account_id,adlabels{name},instagram_user_id,object_story_spec,degrees_of_freedom_spec,"
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
                if rec.get("creative_content_verified"):
                    cid = str(creative.get("id") or "")
                    row = creative if cid and cid == str(rec.get("creative_id")) else {}
                    # ACL-2290: same acceptance as at publish time (same file in another order, an earlier
                    # upload of the same video with equal/unreadable length); anything else stays a mismatch.
                    check(aobj + " creative", "verified_content", True,
                          self._creative_content_ok(plan, rec["creative_content_verified"], row))
                elif rec.get("reused") and plan.label not in _labels(creative):
                    # Existing ad is inside our verified campaign/ad set. Its exact
                    # creative reference + account and the field checks below prove
                    # identity even when creative metadata was not retained.
                    cid = str(creative.get("id") or "")
                    row = self.client.get(f"/{cid}", {"fields": "id,account_id"}) if cid else {}
                    bound = bool(cid and str(row.get("id") or "") == cid
                                 and str(row.get("account_id") or "") == plan.act.removeprefix("act_")
                                 and plan.label in _labels(got)
                                 and str(got.get("adset_id") or "") == adset_id)
                    check(aobj + " creative", "bound_to_owned_ad", True, bound)
                else:
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
                    image_ads += self._feed_checks(check, aobj, plan, ad, creative, rec, files,
                                                   diag.setdefault("video_identity", []))
                    continue
                data = files.get(ad.path, b"")
                if ad.kind == "video":
                    vd = spec.get("video_data") or {}
                    cta = vd.get("call_to_action") or {}
                    check(aobj, "format", "video", "video" if vd else "image")
                    vid = str(vd.get("video_id") or "")
                    self._video_file_checks(check, aobj, "video_id", "video_file",
                                            rec.get("video_id"), vid, plan.video_tag, data,
                                            diag.setdefault("video_identity", []))
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
                want_name = image_name(path, files[path], self._converted_ext(path))
                check(aobj, "image_file", want_name, stored_image_name(want_name, names.get(h, "")))
        mismatches = [c for c in checks if not c["ok"]]
        return {"ok": not mismatches, "checked": len(checks), "checks": checks, "mismatches": mismatches,
                "diagnostics": diag}


# ACL-2050: advantage_state_info и value_rules_* - только чтение, для сверки площадок после записи.
CAMPAIGN_FIELDS = ("name,objective,status,daily_budget,lifetime_budget,pacing_type,bid_strategy,adlabels{name},"
                   "advantage_state_info{advantage_state,advantage_placement_state,advantage_audience_state}")
ADSET_FIELDS = ("name,status,campaign_id,optimization_goal,billing_event,destination_type,"
                "promoted_object,targeting,start_time,end_time,adset_schedule,is_dynamic_creative,adlabels{name},"
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
                 "bid_strategy": "bid strategy", "lifetime_budget": "lifetime budget",
                 "pacing_type": "pacing", "schedule": "end time", "adset_schedule": "time-of-day schedule"}


def _amount(value: Any) -> str:
    """Сумма Meta как строка; нет поля, пусто и "0" - одно и то же: бюджета этого типа нет (ACL-1443)."""
    text = str(value or "").strip()
    return text if text and text != "0" else "0"


def _window_set(windows: Any) -> list[str]:
    """Окна расписания -> отсортированный список «Mon 09:00-12:00» по одному на день (порядок и группы дней не важны)."""
    out: set[str] = set()
    for w in windows if isinstance(windows, list) else []:
        try:
            a, b = int(w["start_minute"]), int(w["end_minute"])
            for d in w["days"]:
                out.add(f"{DAY_NAMES[int(d)]} {a // 60:02d}:{a % 60:02d}-{b // 60:02d}:{b % 60:02d}")
        except (KeyError, TypeError, ValueError, IndexError):
            out.add(f"unreadable {w!r}")
    return sorted(out)


def _utc(value: Any) -> str:
    """Момент времени в UTC (для сверки старта: Meta отдаёт другое смещение/формат)."""
    try:
        dt = datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%S%z")
    except ValueError:
        return str(value or "")
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _lookup_note(exc: BaseException) -> str:
    """ACL-1920: каталог ошибок прячет сырой текст, а какой поиск отказан - нужно человеку."""
    m = re.search(r"(?:search_type|lookup)=(\w+)", str(exc))
    return f" (search: {m.group(1)})" if m else ""


def graph_problem(exc: BaseException) -> dict:
    """Отказ Meta записью списка проблем дашборда: каталог ошибок (ACL-1786) или сырой текст."""
    x = _catalog.explain(getattr(exc, "code", None), getattr(exc, "subcode", None), str(exc))
    code = f"{x.code}/{x.subcode}" if x.subcode else str(x.code or "")
    raw = str(exc)
    if not x.known:
        plain = _plain.classify(exc)
        return {"title": plain.short, "impact": "", "action": plain.next_step,
                "severity": "blocking", "step": "graph_error", "raw": raw or "Meta refused the request (no details given)."}
    return {"title": x.message_en, "impact": "", "action": x.action_en, "severity": "blocking",
            "step": "graph_error", "raw": (f"code {code}: " if code else "") + raw}


def _object_gone(exc: GraphError) -> bool:
    """ACL-2578: Meta says the object does not exist (deleted); any other failed read leaves the question open."""
    return exc.code == 100 and (exc.subcode == 33 or "does not exist" in str(exc).lower())


def _advantage_info(camp: dict) -> dict:
    info = camp.get("advantage_state_info")
    return info if isinstance(info, dict) else {}


def _show(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(map(str, value)) if value else "none"
    return "none" if value in (None, "") else str(value)


def _describe_diffs(checks: list[dict]) -> list[str]:
    """Расхождения одного объекта человеческим языком (площадки — одной строкой)."""
    bad = [c for c in checks if not c["ok"] and (c["field"] not in _SKIP_DIFF or c.get("show_diff"))]
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
                   f"(builder: {_show(c['expected'])}, Meta: {_show(c['actual'])})"
                   + (f" - {c['note']}" if c.get("note") else ""))
    return out


def _rename_message(requested: str, chosen: str, earlier: int, *, future: bool) -> str:
    """Почему кампания пойдёт под именем со счётчиком (ACL-2250); короткая строка для плана, итога и аудита."""
    head = "Will be published as" if future else "Published as"
    return f"{head} {chosen!r}: this name is already on the ad account ({earlier} earlier)."


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
