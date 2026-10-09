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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from src.fb_api.graph import GraphClient, GraphError, GraphStop, WriteUnknown, normalize_act

PAUSED = "PAUSED"
OBJECTIVE = "OUTCOME_LEADS"
OPTIMIZATION_GOAL = "OFFSITE_CONVERSIONS"
BILLING_EVENT = "IMPRESSIONS"
DESTINATION = "WEBSITE"
BID_STRATEGY = "LOWEST_COST_WITHOUT_CAP"
CUSTOM_EVENT = "LEAD"
CTA_TYPE = "LEARN_MORE"

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

#: Что таблица показывает (архив и удалённое не считаем «существующим»).
LIVE_STATES = ["ACTIVE", "PAUSED", "PENDING_REVIEW", "DISAPPROVED", "PREAPPROVED",
               "PENDING_BILLING_INFO", "CAMPAIGN_PAUSED", "ADSET_PAUSED", "IN_PROCESS",
               "WITH_ISSUES"]


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


@dataclass
class AdSetPlan:
    name: str
    age_min: int
    age_max: int
    os: str
    interest_groups: list[list[str]]
    ads: list[AdPlan] = field(default_factory=list)


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


def plan_from_draft(draft: Any) -> CampaignPlan:
    """`CampaignDraft` (после `enrich_campaign_drafts`) → план. Ошибки — `PlanError`."""
    problems: list[str] = list(getattr(draft, "errors", None) or [])
    raw_act = str(getattr(draft, "ad_account_id", "") or "").strip()
    try:
        act = normalize_act(raw_act)
    except ValueError:
        problems.append("ad_account_id is missing or malformed in the account map")
        act = ""
    link = str(getattr(draft, "link", "") or "").strip()
    if not link:
        problems.append("Tracking link is empty; the API backend needs it for the ads")
    ad_sets_src = list(getattr(draft, "ad_sets", None) or [])
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
                description=str(ad.ad_link_description or "").strip()))
        ad_sets.append(plan_set)
    if problems:
        raise PlanError("; ".join(problems))
    name = str(draft.name).strip()
    return CampaignPlan(
        name=name, act=act, tag=campaign_tag(act, name), country=str(draft.country or "").strip(),
        language=str(getattr(draft, "language", "") or "").strip(), link=link,
        page_id=str(getattr(draft, "page_id", "") or "").strip(),
        pixel_id=str(getattr(draft, "pixel_id", "") or "").strip(),
        daily_budget=float(budget), ad_sets=ad_sets)


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


def resolve_page_pixel(plan: CampaignPlan, facts: AccountFacts) -> list[str]:
    """Страница и пиксель плана. Возвращает предупреждения; ошибки — `PlanError`."""
    notes = []
    if plan.page_id:
        if facts.pages and plan.page_id not in facts.pages:
            raise PlanError("The page from the account map cannot advertise from this ad account.")
    elif len(facts.pages) == 1:
        plan.page_id = facts.pages[0]
        notes.append("page_id was empty in the account map; the only page of the ad account is used")
    else:
        raise PlanError(f"page_id is empty in the account map and the ad account has "
                        f"{len(facts.pages)} pages; set page_id")
    if plan.pixel_id:
        if facts.pixels and plan.pixel_id not in facts.pixels:
            raise PlanError("The pixel of the draft is not on this ad account.")
    elif len(facts.pixels) == 1:
        plan.pixel_id = facts.pixels[0]
    else:
        raise PlanError(f"The ad account has {len(facts.pixels)} pixels/datasets; "
                        "the API backend needs exactly one")
    return notes


# ───────────────────────── таргетинг через /search ─────────────────────────

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
        rows = self.client.get_all("/search", {"type": "adgeolocation", "location_types": ["country"],
                                               "q": key, "limit": 10}, max_pages=1)
        code = ""
        for r in rows:
            cc = str(r.get("country_code") or r.get("key") or "").upper()
            if cc == key.upper() or str(r.get("name") or "").casefold() == key.casefold():
                code = cc
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
    os_val = ad_set.os.casefold()
    if os_val in ("ios", "android"):
        t["user_os"] = ["iOS" if os_val == "ios" else "Android"]
        t["device_platforms"] = ["mobile"]
    return t


# ───────────────────────── создание ─────────────────────────

def _labels(row: dict) -> set[str]:
    raw = row.get("adlabels")
    if isinstance(raw, dict):
        raw = raw.get("data")
    return {str(x.get("name") or "") for x in (raw or []) if isinstance(x, dict)}


def _md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()  # noqa: S324 — так Meta считает хэш картинки


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
        self.progress = progress

    # -- служебное
    def _step(self, out: Outcome, text: str) -> None:
        out.steps.append(text)
        self.progress(text)

    def _check_cancel(self, out: Outcome) -> None:
        if self.cancelled():
            raise CreationStopped("Stopped by the user", created=out.created, kind="cancelled")

    def _write(self, out: Outcome, what: str, path: str, params: dict, find: Callable[[], str | None],
               *, files: dict | None = None, filename: str = "", id_of=lambda b: b.get("id")) -> str:
        """Одна запись; неизвестный результат — сверка `find()` чтением."""
        self._check_cancel(out)
        try:
            body = self.client.post(path, params, files=files, filename=filename)
        except WriteUnknown as exc:
            self._step(out, f"{what}: result unknown — reconciling by readback")
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
            self.client.reconciled()
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
            self.client.reconciled()
            return str(found)
        return str(got)

    # -- поиск существующего
    def _find_campaign(self, plan: CampaignPlan) -> dict | None:
        rows = self.client.get_all(f"/{plan.act}/campaigns", {
            "fields": "id,name,status,adlabels{name}",
            "filtering": [{"field": "name", "operator": "CONTAIN", "value": plan.name}],
            "effective_status": LIVE_STATES, "limit": 100}, max_pages=2)
        same = [r for r in rows if str(r.get("name") or "").strip() == plan.name]
        ours = [r for r in same if plan.label in _labels(r)]
        if ours:
            return ours[0]
        if same:
            raise CreationStopped(
                f"A campaign named {plan.name!r} already exists on the ad account and was not "
                "created by ACL. Nothing was created; rename the draft or remove the old campaign.",
                kind="name_taken")
        return None

    def _children(self, parent_id: str, edge: str, fields: str) -> list[dict]:
        return self.client.get_all(f"/{parent_id}/{edge}", {
            "fields": fields, "effective_status": LIVE_STATES, "limit": 100}, max_pages=2)

    def _find_child(self, parent_id: str, edge: str, name: str, label: str) -> dict | None:
        rows = self._children(parent_id, edge, "id,name,status,adlabels{name}")
        return next((r for r in rows if str(r.get("name") or "").strip() == name
                     and label in _labels(r)), None)

    def _find_creative(self, plan: CampaignPlan, name: str) -> str | None:
        rows = self.client.get_all(f"/{plan.act}/adcreatives", {
            "fields": "id,name,adlabels{name}",
            "filtering": [{"field": "name", "operator": "CONTAIN", "value": name}],
            "limit": 50}, max_pages=1)
        hit = next((r for r in rows if str(r.get("name") or "") == name and plan.label in _labels(r)), None)
        return str(hit["id"]) if hit else None

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
        stage = self.client.transport.stage_file(data)

        def hash_of(body: dict) -> str | None:
            images = body.get("images") if isinstance(body, dict) else None
            for v in (images or {}).values():
                if isinstance(v, dict) and v.get("hash"):
                    return str(v["hash"])
            return None

        got = self._write(out, f"ad {ad.index}: image upload", f"/{plan.act}/adimages", {}, find,
                          files={"filename": stage}, filename=Path(ad.path).name, id_of=hash_of)
        out.created.setdefault("images", []).append(got)
        self._step(out, f"ad {ad.index}: image uploaded")
        return got

    def _video(self, out: Outcome, plan: CampaignPlan, ad: AdPlan, data: bytes) -> tuple[str, str]:
        title = f"acl-{plan.tag}-{_md5(data)[:12]}"

        def find() -> str | None:
            rows = self.client.get_all(f"/{plan.act}/advideos", {"fields": "id,title", "limit": 100},
                                       max_pages=1)
            hit = next((r for r in rows if str(r.get("title") or "") == title), None)
            return str(hit["id"]) if hit else None

        vid = find()
        if vid:
            self._step(out, f"ad {ad.index}: video already in the ad account")
        else:
            stage = self.client.transport.stage_file(data)
            vid = self._write(out, f"ad {ad.index}: video upload", f"/{plan.act}/advideos",
                              {"name": title, "title": title}, find, files={"source": stage},
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
        return {"name": ad_set.name, "campaign_id": campaign_id, "status": PAUSED,
                "optimization_goal": OPTIMIZATION_GOAL, "billing_event": BILLING_EVENT,
                "destination_type": DESTINATION,
                "promoted_object": {"pixel_id": plan.pixel_id, "custom_event_type": CUSTOM_EVENT},
                "targeting": targeting, "adlabels": [{"name": plan.label}]}

    @staticmethod
    def story_spec(plan: CampaignPlan, ad: AdPlan, media: dict) -> dict:
        cta = {"type": CTA_TYPE, "value": {"link": plan.link}}
        if ad.kind == "video":
            data = {"video_id": media["video_id"], "image_url": media["thumb"], "title": ad.title,
                    "message": ad.body, "call_to_action": cta}
            if ad.description:
                data["link_description"] = ad.description
            return {"page_id": plan.page_id, "video_data": data}
        data = {"link": plan.link, "message": ad.body, "name": ad.title,
                "image_hash": media["image_hash"], "call_to_action": cta}
        if ad.description:
            data["description"] = ad.description
        return {"page_id": plan.page_id, "link_data": data}

    # -- весь путь
    def run(self, plan: CampaignPlan) -> Outcome:
        out = Outcome(campaign_name=plan.name)
        try:
            self._run(plan, out)
        except CreationStopped as exc:
            out.error, out.stop_kind = str(exc), exc.kind
        except PlanError as exc:
            out.error, out.stop_kind = str(exc) + " Nothing more was created.", "plan"
        except GraphStop as exc:
            out.error, out.stop_kind = f"Graph API stopped: {exc}", exc.kind
        except GraphError as exc:
            out.error, out.stop_kind = f"Meta refused: {exc}", "graph_error"
        except WriteUnknown as exc:
            out.error, out.stop_kind = f"Write result unknown: {exc}", "unknown"
        return out

    def _run(self, plan: CampaignPlan, out: Outcome) -> None:
        # 1. Всё, что можно проверить до записи.
        out.warnings += resolve_page_pixel(plan, self.facts)
        targetings = [targeting_for(plan, s, self.resolver) for s in plan.ad_sets]
        self._step(out, "preflight: targeting resolved")
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

        # 2. Кампания.
        existing = self._find_campaign(plan)
        if existing:
            out.campaign_id = str(existing["id"])
            out.reused["campaign"] = out.campaign_id
            self._step(out, "campaign: found from a previous run")
        else:
            out.campaign_id = self._write(out, "campaign", f"/{plan.act}/campaigns",
                                          self.campaign_params(plan),
                                          lambda: (self._find_campaign(plan) or {}).get("id"))
            out.created["campaign"] = out.campaign_id
            self._step(out, "campaign: created PAUSED")

        # 3. Группы и объявления.
        for ad_set, targeting in zip(plan.ad_sets, targetings):
            found = self._find_child(out.campaign_id, "adsets", ad_set.name, plan.label)
            if found:
                adset_id = str(found["id"])
                out.reused.setdefault("adsets", []).append(adset_id)
                self._step(out, f"ad set {ad_set.name!r}: found from a previous run")
            else:
                adset_id = self._write(
                    out, f"ad set {ad_set.name!r}", f"/{plan.act}/adsets",
                    self.adset_params(plan, ad_set, out.campaign_id, targeting),
                    lambda: (self._find_child(out.campaign_id, "adsets", ad_set.name, plan.label) or {}).get("id"))
                out.created.setdefault("adsets", []).append(adset_id)
                self._step(out, f"ad set {ad_set.name!r}: created PAUSED")
            out.adset_ids.append(adset_id)

            have = {str(r.get("name") or "").strip(): str(r["id"])
                    for r in self._children(adset_id, "ads", "id,name,adlabels{name}")
                    if plan.label in _labels(r)}
            for ad in ad_set.ads:
                if ad.name in have:
                    out.ad_ids.append(have[ad.name])
                    out.reused.setdefault("ads", []).append(have[ad.name])
                    self._step(out, f"ad {ad.index}: found from a previous run")
                    continue
                data = files[ad.path]
                if ad.kind == "video":
                    vid, thumb = self._video(out, plan, ad, data)
                    media = {"video_id": vid, "thumb": thumb}
                else:
                    media = {"image_hash": self._image_hash(out, plan, ad, data)}
                creative_name = f"{ad.name} [{plan.label}]"
                creative_id = self._find_creative(plan, creative_name)
                if creative_id:
                    self._step(out, f"ad {ad.index}: creative found from a previous run")
                else:
                    creative_id = self._write(
                        out, f"ad {ad.index}: creative", f"/{plan.act}/adcreatives",
                        {"name": creative_name, "object_story_spec": self.story_spec(plan, ad, media),
                         "adlabels": [{"name": plan.label}]},
                        lambda: self._find_creative(plan, creative_name))
                    out.created.setdefault("creatives", []).append(creative_id)
                    self._step(out, f"ad {ad.index}: creative created")
                ad_id = self._write(
                    out, f"ad {ad.index}", f"/{plan.act}/ads",
                    {"name": ad.name, "adset_id": adset_id, "creative": {"creative_id": creative_id},
                     "status": PAUSED, "adlabels": [{"name": plan.label}]},
                    lambda: (self._find_child(adset_id, "ads", ad.name, plan.label) or {}).get("id"))
                out.ad_ids.append(ad_id)
                out.created.setdefault("ads", []).append(ad_id)
                self._step(out, f"ad {ad.index}: created PAUSED")

        # 4. Сверка.
        out.readback = self.readback(plan, out, targetings)
        n = len(out.mismatches)
        self._step(out, "readback: all fields match" if not n else f"readback: {n} mismatch(es)")

    # ───────────────────────── сверка ─────────────────────────
    def readback(self, plan: CampaignPlan, out: Outcome, targetings: list[dict]) -> dict:
        checks: list[dict] = []

        def check(obj: str, fld: str, expected: Any, actual: Any) -> None:
            checks.append({"object": obj, "field": fld, "expected": expected, "actual": actual,
                           "ok": _same(expected, actual)})

        camp = self.client.get(f"/{out.campaign_id}", {
            "fields": "name,objective,status,daily_budget,bid_strategy,adlabels{name}"})
        want = self.campaign_params(plan)
        check("campaign", "name", want["name"], camp.get("name"))
        check("campaign", "status", PAUSED, camp.get("status"))
        check("campaign", "objective", OBJECTIVE, camp.get("objective"))
        check("campaign", "daily_budget", str(want["daily_budget"]), str(camp.get("daily_budget") or ""))
        check("campaign", "bid_strategy", BID_STRATEGY, camp.get("bid_strategy"))
        check("campaign", "tag", plan.label, plan.label if plan.label in _labels(camp) else "")

        for ad_set, adset_id, targeting in zip(plan.ad_sets, out.adset_ids, targetings):
            obj = f"ad set {ad_set.name}"
            row = self.client.get(f"/{adset_id}", {
                "fields": "name,status,campaign_id,optimization_goal,billing_event,destination_type,"
                          "promoted_object,targeting,start_time,end_time"})
            check(obj, "name", ad_set.name, row.get("name"))
            check(obj, "status", PAUSED, row.get("status"))
            check(obj, "campaign_id", out.campaign_id, str(row.get("campaign_id") or ""))
            check(obj, "optimization_goal", OPTIMIZATION_GOAL, row.get("optimization_goal"))
            check(obj, "billing_event", BILLING_EVENT, row.get("billing_event"))
            check(obj, "destination_type", DESTINATION, row.get("destination_type"))
            po = row.get("promoted_object") or {}
            check(obj, "pixel", plan.pixel_id, str(po.get("pixel_id") or ""))
            check(obj, "conversion_event", CUSTOM_EVENT, po.get("custom_event_type"))
            check(obj, "schedule", "starts on activation, no end date",
                  "starts on activation, no end date" if not row.get("end_time") else
                  f"ends {row.get('end_time')}")
            t = row.get("targeting") or {}
            check(obj, "geo", targeting["geo_locations"]["countries"],
                  (t.get("geo_locations") or {}).get("countries"))
            check(obj, "age_min", targeting["age_min"], t.get("age_min"))
            check(obj, "age_max", targeting["age_max"], t.get("age_max"))
            check(obj, "languages", targeting.get("locales") or [], t.get("locales") or [])
            check(obj, "detailed_targeting", _flex_ids(targeting.get("flexible_spec")),
                  _flex_ids(t.get("flexible_spec")))
            check(obj, "os", targeting.get("user_os") or [], t.get("user_os") or [])
            check(obj, "placements", targeting.get("device_platforms") or "automatic",
                  t.get("device_platforms") or "automatic")

            ads = self.client.get_all(f"/{adset_id}/ads", {
                "fields": "id,name,status,adset_id,creative{id,object_story_spec}",
                "effective_status": LIVE_STATES, "limit": 100}, max_pages=2)
            by_name = {str(a.get("name") or "").strip(): a for a in ads}
            for ad in ad_set.ads:
                aobj = f"ad {ad.index} ({ad.name})"
                got = by_name.get(ad.name)
                if got is None:
                    check(aobj, "exists", True, False)
                    continue
                check(aobj, "status", PAUSED, got.get("status"))
                spec = ((got.get("creative") or {}).get("object_story_spec")) or {}
                check(aobj, "page", plan.page_id, str(spec.get("page_id") or ""))
                if ad.kind == "video":
                    vd = spec.get("video_data") or {}
                    check(aobj, "format", "video", "video" if vd else "image")
                    check(aobj, "link", plan.link, ((vd.get("call_to_action") or {}).get("value") or {}).get("link"))
                    check(aobj, "headline", ad.title, vd.get("title") or "")
                    check(aobj, "primary_text", ad.body, vd.get("message") or "")
                    check(aobj, "description", ad.description, vd.get("link_description") or "")
                else:
                    ld = spec.get("link_data") or {}
                    check(aobj, "format", "image", "image" if ld.get("image_hash") else "video")
                    check(aobj, "link", plan.link, ld.get("link"))
                    check(aobj, "headline", ad.title, ld.get("name") or "")
                    check(aobj, "primary_text", ad.body, ld.get("message") or "")
                    check(aobj, "description", ad.description, ld.get("description") or "")
        mismatches = [c for c in checks if not c["ok"]]
        return {"ok": not mismatches, "checked": len(checks), "checks": checks, "mismatches": mismatches}


def _flex_ids(spec: Any) -> list[list[str]]:
    out = []
    for group in spec or []:
        if not isinstance(group, dict):
            continue
        ids = sorted(str(i.get("id")) for bucket in group.values() if isinstance(bucket, list)
                     for i in bucket if isinstance(i, dict))
        out.append(ids)
    return sorted(out)


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
