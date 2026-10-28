"""Опись кампаний аккаунта для таблицы Home: Campaign > Ad set > Ad (ACL-1798).

Только чтение и только `ACL_FB_BACKEND=api`. Опись берётся тем же путём, что
«Read spend» (ACL-1756/1760): GET-запросы `fetch()` из вкладки профиля через
`account_spend_api._session` — та же очередь, тот же замок профиля, те же
потолок вызовов, охлаждение после лимита и правило «профиль, который не мы
подняли, не останавливаем» (`CLOSE_GRACE_S`). Второй сессии здесь нет.

Правила данных (решение владельца):

* **Только настоящая опись.** Дерево строится по id (`campaign_id`, `adset_id`),
  а не по именам. Нет id — нет строки. Выдуманных нулей и деревьев «по имени» нет.
* **Опись и Insights — отдельные чтения.** Объект без статистики остаётся
  в таблице; пустая ячейка — это «нет данных», а не 0.
* **Родитель не складывается из детей.** Reach и Frequency у каждого уровня
  свои, их читает сам Meta на этом уровне; CTR/CPM/CPC пересчитываются из
  сумм, а не усредняются.
* **BI цепляется только по точному совпадению** имени (с поправкой на
  пробелы, как у `build_tree_attribution`) внутри одного аккаунта; иначе —
  «evidence only».
* **Никаких записей в Meta.** Здесь нет ни одного POST.

Снимок хранится по аккаунту в `campaign_inventory.json` рядом с
`account_spend.json` (`paths.get_local_config_dir()`), вместе со временем
чтения. Таймеров и опроса Meta нет: чтение — только по нажатию.
"""
from __future__ import annotations

import json
import hashlib
import logging
import os
import tempfile
import threading
import time
from collections import Counter
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from config import paths
from src.analysis_agent import account_spend as _as
from src.fb_ads_rpa.operations import browser_operation

log = logging.getLogger(__name__)

NEEDS_API_MESSAGE = "Campaign inventory needs the API backend"
NEEDS_API_CODE = "needs_api_backend"

# ACL-1815: `adlabels{name}` (признак «создано ACL», метки `acl-*`) и `updated_time`
# («менялся после нашей записи») читаются теми же запросами, что и раньше:
# новых вызовов к Meta нет, потолок вызовов не затронут.
CAMPAIGN_FIELDS = ("id,name,status,effective_status,objective,daily_budget,lifetime_budget,bid_strategy,"
                   "adlabels{name},updated_time")
ADSET_FIELDS = ("id,name,status,effective_status,campaign_id,daily_budget,lifetime_budget,"
                "bid_strategy,bid_amount,adlabels{name},updated_time")
AD_FIELDS = "id,name,status,effective_status,campaign_id,adset_id,updated_time"
#: Префикс метки, которой ACL помечает созданное им (`creator.py`).
ACL_LABEL_PREFIX = "acl-"
#: Insights на каждом уровне — отдельным запросом: Reach и Frequency
#: родителя не выводятся из детей.
INSIGHT_ID = {"campaign": "campaign_id", "adset": "adset_id", "ad": "ad_id"}
INSIGHT_METRICS = "spend,reach,impressions,frequency,clicks"

#: Какие шаги чтения бывают (ключи `Snapshot.steps`).
STEPS = ("campaigns", "adsets", "ads", "insights_campaign", "insights_adset", "insights_ad")
#: ACL-2063: чтение по расписанию берёт только структуру (без Insights и без периода).
STRUCTURE_STEPS = ("campaigns", "adsets", "ads")

#: Бюджет в Graph приходит в минимальных единицах валюты. Делитель берём из
#: таблицы, а не «всегда 100»: у иены и воны копеек нет. Валюты вне таблицы
#: сумму не получают (единица не подтверждена) — лучше «—», чем неверный порядок.
_OFFSET_100 = {"USD", "EUR", "GBP", "ILS", "AUD", "CAD", "BRL", "MXN", "CHF", "SEK", "NOK", "DKK",
               "PLN", "CZK", "TRY", "ZAR", "SGD", "NZD", "AED"}
_OFFSET_1 = {"JPY", "KRW", "VND", "CLP"}

_BID_LABELS = {
    "LOWEST_COST_WITHOUT_CAP": "Lowest cost",
    "LOWEST_COST_WITH_BID_CAP": "Bid cap",
    "COST_CAP": "Cost cap",
    "LOWEST_COST_WITH_MIN_ROAS": "ROAS goal",
}

#: `effective_status` → (слово доставки, тон). Тон красит ячейку, а не строку.
_DELIVERY: dict[str, tuple[str, str]] = {
    "ACTIVE": ("Active", "ok"),
    "PAUSED": ("Off", "off"),
    "CAMPAIGN_PAUSED": ("Campaign off", "off"),
    "ADSET_PAUSED": ("Ad set off", "off"),
    "PENDING_REVIEW": ("In review", "warn"),
    "IN_PROCESS": ("In review", "warn"),
    "PREAPPROVED": ("In review", "warn"),
    "DISAPPROVED": ("Rejected", "bad"),
    "WITH_ISSUES": ("Active, with issues", "bad"),
    "PENDING_BILLING_INFO": ("Billing info needed", "bad"),
}


def norm_name(value: Any) -> str:
    """Имя для сопоставления: пробелы схлопнуты (как `_tree_name` отчёта)."""
    return " ".join(str(value or "").split())


# ───────────────────────── снимок ─────────────────────────

@dataclass
class Snapshot:
    """Что аккаунт сказал при последнем чтении описи."""

    account: str
    #: idle | ok | partial | empty | failed. «running» живёт только в памяти.
    state: str = "idle"
    source: str = "api"
    started_at: str = ""
    finished_at: str = ""
    #: ISO UTC (`2026-10-12T07:31:45+00:00`) рядом с локальным `finished_at` без зоны:
    #: правилам нужно сравнение возраста, не зависящее от часового пояса машины.
    finished_at_utc: str = ""
    took_s: float = 0.0
    date_from: str = ""
    date_to: str = ""
    currency: str = ""
    error: str = ""
    error_code: str = ""
    #: Вид остановки Meta/клиента (`checkpoint`, `rate_limited`, `call_budget`…), если чтение
    #: оборвалось на ней — даже когда снимок «partial» и `error` пуст (ACL-2063).
    stop: str = ""
    #: Человеческие причины «неполно»: страницы упёрлись в лимит, шаг не прочитан…
    problems: list[str] = field(default_factory=list)
    #: шаг → ok | truncated | failed | skipped
    steps: dict[str, str] = field(default_factory=dict)
    calls: int = 0
    campaigns: list[dict] = field(default_factory=list)
    adsets: list[dict] = field(default_factory=list)
    ads: list[dict] = field(default_factory=list)
    #: уровень → {id: {spend, reach, impressions, frequency, clicks}}
    insights: dict[str, dict[str, dict]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _snapshot_from(raw: dict) -> Snapshot:
    known = set(Snapshot.__dataclass_fields__)  # noqa: SLF001 — поля датакласса
    return Snapshot(**{k: v for k, v in (raw or {}).items() if k in known})


def read_succeeded(snap: Snapshot) -> bool:
    """Чтение удалось: обрезка по лимиту допустима, ошибка/остановка — нет."""
    return (snap.state in {"ok", "partial", "empty"} and not snap.stop and not snap.error
            and not snap.error_code and "failed" not in snap.steps.values())


# ───────────────────────── разбор ответов Graph ─────────────────────────

def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> int | None:
    number = _to_float(value)
    return None if number is None else int(number)


def _iso_utc(value: Any) -> str:
    """Время Graph (`2026-10-12T10:11:12+0000`) → ISO UTC; нечитаемое → пустая строка."""
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            moment = datetime.strptime(text, "%Y-%m-%dT%H:%M:%S%z")
        except ValueError:
            return ""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def _label_names(raw: Any) -> list[str]:
    """Имена меток: Graph отдаёт список или `{"data": [...]}`; неразборчивое пропускаем."""
    rows = raw.get("data") if isinstance(raw, dict) else raw
    names = []
    for item in rows if isinstance(rows, list) else []:
        name = str(item.get("name") if isinstance(item, dict) else item or "").strip()
        if name:
            names.append(name)
    return names


def parse_inventory(rows: list[dict], level: str) -> tuple[list[dict], int]:
    """Строки Graph → объекты описи. Возвращает `(объекты, сколько отброшено)`.

    Строка без `id` отбрасывается и считается: без id её не к чему прицепить,
    а подставлять имя вместо id нельзя. Повторный id — тоже отброс (первый
    остаётся), иначе дерево получило бы двойника.
    """
    out: list[dict] = []
    seen: set[str] = set()
    dropped = 0
    for row in rows or []:
        if not isinstance(row, dict):
            dropped += 1
            continue
        oid = str(row.get("id") or "").strip()
        if not oid or oid in seen:
            dropped += 1
            continue
        seen.add(oid)
        obj = {
            "id": oid,
            "name": _as.clean_object_name(row.get("name")),
            "status": str(row.get("status") or "").upper(),
            "effective_status": str(row.get("effective_status") or "").upper(),
            "daily_budget": _to_int(row.get("daily_budget")),
            "lifetime_budget": _to_int(row.get("lifetime_budget")),
            "bid_strategy": str(row.get("bid_strategy") or "").upper(),
            "bid_amount": _to_int(row.get("bid_amount")),
            "objective": str(row.get("objective") or "").upper(),
            "campaign_id": str(row.get("campaign_id") or "").strip(),
            "adset_id": str(row.get("adset_id") or "").strip(),
        }
        # ACL-1815: метки и время правки. Нет поля в ответе — пусто, а не «не ACL»:
        # `acl_created` честно `None`, когда метки не читались.
        if "adlabels" in row:
            labels = _label_names(row.get("adlabels"))
            obj["adlabels"] = labels
            obj["acl_created"] = any(x.casefold().startswith(ACL_LABEL_PREFIX) for x in labels)
        updated = _iso_utc(row.get("updated_time"))
        if updated:
            obj["updated_time"] = updated
        if level == "campaign":
            obj["campaign_id"] = obj["adset_id"] = ""
        elif level == "adset":
            obj["adset_id"] = ""
        out.append(obj)
    return out, dropped


def parse_insights(rows: list[dict], level: str) -> tuple[dict[str, dict], int]:
    """Строки Insights → `{id: метрики}`. Отсутствующее поле — `None`, не 0.

    Id объекта с двумя строками не однозначен (значит, был разрез, которого мы
    не просили): такой id выпадает целиком и считается в `ambiguous`, а не
    складывается молча.
    """
    key = INSIGHT_ID[level]
    found: dict[str, dict] = {}
    bad: set[str] = set()
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        oid = str(row.get(key) or "").strip()
        if not oid:
            continue
        if oid in found or oid in bad:
            bad.add(oid)
            found.pop(oid, None)
            continue
        found[oid] = {
            "spend": _to_float(row.get("spend")),
            "reach": _to_int(row.get("reach")),
            "impressions": _to_int(row.get("impressions")),
            "frequency": _to_float(row.get("frequency")),
            "clicks": _to_int(row.get("clicks")),
            "currency": str(row.get("account_currency") or ""),
        }
    return found, len(bad)


def derive_ratios(spend: float | None, impressions: int | None, clicks: int | None) -> dict[str, float | None]:
    """CTR/CPM/CPC из сумм. Нулевой знаменатель — `None`, не 0.

    Среднее строковых CTR/CPM/CPC считать нельзя; пересчёт из итогов — единственный
    честный путь, и он же нужен при любой агрегации.
    """
    ctr = cpm = cpc = None
    if impressions and impressions > 0:
        if clicks is not None:
            ctr = 100.0 * clicks / impressions
        if spend is not None:
            cpm = 1000.0 * spend / impressions
    if clicks and clicks > 0 and spend is not None:
        cpc = spend / clicks
    return {"ctr": ctr, "cpm": cpm, "cpc": cpc}


# ───────────────────────── дерево по id ─────────────────────────

def build_tree(snap: Snapshot) -> dict[str, Any]:
    """Дерево кампания → ad set → объявление строго по id родителей.

    Объект, чей родитель не вернулся (список неполон или id пуст), в дерево не
    вешается: он считается в `orphans`, а не прикрепляется «по имени».
    """
    campaigns = {c["id"]: c for c in snap.campaigns}
    adsets = {a["id"]: a for a in snap.adsets}
    adsets_of: dict[str, list[dict]] = {cid: [] for cid in campaigns}
    ads_of: dict[str, list[dict]] = {aid: [] for aid in adsets}
    orphan_adsets: list[dict] = []
    orphan_ads: list[dict] = []
    for a in snap.adsets:
        if a.get("campaign_id") in campaigns:
            adsets_of[a["campaign_id"]].append(a)
        else:
            orphan_adsets.append(a)
    for ad in snap.ads:
        if ad.get("adset_id") in adsets:
            ads_of[ad["adset_id"]].append(ad)
        else:
            orphan_ads.append(ad)
    return {"campaigns": campaigns, "adsets": adsets, "adsets_of": adsets_of, "ads_of": ads_of,
            "orphan_adsets": orphan_adsets, "orphan_ads": orphan_ads}


def child_state(snap: Snapshot, level: str) -> str:
    """Состояние чтения детей уровня `adset` / `ad`: ok | partial | not_read."""
    step = snap.steps.get("adsets" if level == "adset" else "ads", "skipped")
    if step == "ok":
        return "ok"
    if step == "truncated":
        return "partial"
    return "not_read"


# ───────────────────────── строки для экрана ─────────────────────────

def _budget_amount(minor: int | None, currency: str) -> float | None:
    code = (currency or "").upper()
    if minor is None:
        return None
    if code in _OFFSET_100:
        return minor / 100.0
    if code in _OFFSET_1:
        return float(minor)
    return None


def _budget(obj: dict, level: str, currency: str, parent: dict | None) -> dict:
    """Что показать в ячейке бюджета. Бюджет есть у кампании или у ad set, у объявления — никогда."""
    if level == "ad":
        return {"kind": "none", "amount": None}
    own = [("daily", obj.get("daily_budget")), ("lifetime", obj.get("lifetime_budget"))]
    for kind, minor in own:
        if minor:
            amount = _budget_amount(minor, currency)
            return {"kind": kind if amount is not None else "unit_unknown", "amount": amount}
    if level == "campaign":
        return {"kind": "ad_sets", "amount": None}
    if parent and (parent.get("daily_budget") or parent.get("lifetime_budget")):
        return {"kind": "inherited", "amount": None}
    return {"kind": "none", "amount": None}


def _constraint(level: str, obj: dict, tree: dict) -> str:
    """Включён сам, но выключен родитель: «On · Campaign paused» (не гасим переключатель)."""
    if obj.get("status") != "ACTIVE":
        return ""
    if level in ("adset", "ad"):
        if level == "ad":
            adset = tree["adsets"].get(obj.get("adset_id"))
            if adset is not None and adset.get("status") == "PAUSED":
                return "Ad set paused"
        campaign = tree["campaigns"].get(obj.get("campaign_id"))
        if campaign is not None and campaign.get("status") == "PAUSED":
            return "Campaign paused"
    return ""


def _meta_cells(snap: Snapshot, level: str, oid: str) -> dict:
    step = snap.steps.get({"campaign": "insights_campaign", "adset": "insights_adset", "ad": "insights_ad"}[level],
                          "skipped")
    if step not in ("ok", "truncated"):
        return {"state": "not_read"}
    row = (snap.insights.get(level) or {}).get(oid)
    if row is None:
        # Встретится и при «нет показов», и при «страница обрезана» — не ноль и не вывод.
        return {"state": "partial_unknown" if step == "truncated" else "no_row"}
    out = {"state": "ok", "spend": row.get("spend"), "reach": row.get("reach"),
           "impressions": row.get("impressions"), "frequency": row.get("frequency"),
           "clicks": row.get("clicks")}
    out.update(derive_ratios(row.get("spend"), row.get("impressions"), row.get("clicks")))
    return out


def row_payload(snap: Snapshot, tree: dict, level: str, obj: dict, bi: dict | None) -> dict:
    status = obj.get("status") or ""
    effective = obj.get("effective_status") or ""
    label, tone = _DELIVERY.get(effective, (effective.replace("_", " ").capitalize() or "Unknown",
                                            "off" if not effective else "warn"))
    parent = None
    if level == "adset":
        parent = tree["campaigns"].get(obj.get("campaign_id"))
    elif level == "ad":
        parent = tree["adsets"].get(obj.get("adset_id"))
    if level == "campaign":
        count = len(tree["adsets_of"].get(obj["id"], []))
        kids, kids_level = count, "adset"
    elif level == "adset":
        count = len(tree["ads_of"].get(obj["id"], []))
        kids, kids_level = count, "ad"
    else:
        kids, kids_level = None, ""
    return {
        "key": f"{level}:{obj['id']}",
        "level": level,
        "id": obj["id"],
        "name": obj.get("name") or "",
        "account": snap.account,
        "parent_id": (obj.get("campaign_id") if level == "adset" else obj.get("adset_id") if level == "ad" else ""),
        "status": "on" if status == "ACTIVE" else "off" if status == "PAUSED" else "unknown",
        "status_raw": status,
        "delivery": {"label": label, "tone": tone, "effective": effective,
                     "constraint": _constraint(level, obj, tree)},
        "child_count": kids,
        "child_state": child_state(snap, kids_level) if kids_level else "",
        "budget": _budget(obj, level, snap.currency, parent),
        "bid_strategy_raw": obj.get("bid_strategy") or "",
        "bid_amount": _budget_amount(obj.get("bid_amount"), snap.currency) if level == "adset" else None,
        "bid_strategy": _BID_LABELS.get(obj.get("bid_strategy") or "",
                                        (obj.get("bid_strategy") or "").replace("_", " ").capitalize()) or None,
        "objective": (obj.get("objective") or "").replace("_", " ").capitalize() or None if level == "campaign" else None,
        "meta": _meta_cells(snap, level, obj["id"]),
        "bi": (bi or {}).get(f"{level}:{obj['id']}") or {"match": "none"},
        "synced_at": snap.finished_at,
        "currency": snap.currency,
    }


def campaign_rows(snap: Snapshot, bi: dict | None = None) -> list[dict]:
    tree = build_tree(snap)
    return [row_payload(snap, tree, "campaign", c, bi) for c in snap.campaigns]


def child_rows(snap: Snapshot, level: str, parent_id: str, bi: dict | None = None) -> dict:
    """Дети одного родителя из снимка: `{state, rows, note}`.

    `state`: `ok` | `empty` | `partial` | `not_read` — четыре разных ответа.
    Пустой список при прочитанном шаге — `empty`; шаг не читался или упал —
    `not_read` (это НЕ «детей нет»); страницы обрезаны — `partial` (дети есть,
    но не все).
    """
    if level not in ("adset", "ad"):
        raise ValueError("level must be adset or ad")
    tree = build_tree(snap)
    if level == "adset":
        parent_exists = parent_id in tree["campaigns"]
        kids = tree["adsets_of"].get(parent_id, [])
    else:
        parent_exists = parent_id in tree["adsets"]
        kids = tree["ads_of"].get(parent_id, [])
    if not parent_exists:
        return {"state": "not_read", "rows": [], "note": "This object is not in the saved inventory"}
    state = child_state(snap, level)
    rows = [row_payload(snap, tree, level, k, bi) for k in kids]
    if state == "ok":
        return {"state": "ok" if rows else "empty", "rows": rows, "note": ""}
    if state == "partial":
        return {"state": "partial", "rows": rows,
                "note": "The list was cut at the page limit; some children may be missing"}
    why = snap.problems[0] if snap.problems else "this level was not read"
    return {"state": "not_read", "rows": [], "note": why}


# ───────────────────────── BI из книги статистики ─────────────────────────

def merge_attribution(parts: list[dict]) -> dict:
    """Склеить `build_tree_attribution` по странам одного аккаунта.

    Срезы стран не пересекаются (`Country Final`), поэтому Spend/Leads/FTDs
    одного имени складываются; отношения здесь не хранятся, их пересчитывает
    `_bi_metrics` из сумм.
    """
    campaigns: dict[str, dict] = {}
    ads: dict[str, dict[str, dict]] = {}

    def add(dst: dict, src: dict) -> None:
        dst["spend"] = (dst.get("spend") or 0.0) + (src.get("spend") or 0.0)
        dst["leads"] = (dst.get("leads") or 0) + (src.get("leads") or 0)
        dst["deposits"] = (dst.get("deposits") or 0) + (src.get("deposits") or 0)

    for part in parts:
        for name, m in (part.get("campaigns") or {}).items():
            add(campaigns.setdefault(name, {}), m)
        for name, per in (part.get("ads") or {}).items():
            target = ads.setdefault(name, {})
            for title, m in per.items():
                add(target.setdefault(title, {}), m)
    return {"campaigns": campaigns, "ads": ads}


def bi_metrics(spend: float | None, leads: int | None, ftds: int | None) -> dict:
    """Шесть метрик BI из сумм. Нулевой знаменатель — `None`; CPA при нуле FTD помечен."""
    cpl = spend / leads if (spend is not None and leads) else None
    cpa = spend / ftds if (spend is not None and ftds) else None
    cr = 100.0 * ftds / leads if (ftds is not None and leads) else None
    return {"spend": spend, "leads": leads, "ftds": ftds, "cpl": cpl, "cpa": cpa, "cr": cr,
            "zero_ftd": bool(spend and spend > 0 and ftds == 0)}


def join_bi(snap: Snapshot, bi: dict | None) -> tuple[dict[str, dict], dict]:
    """Цепляет BI к объектам Meta. Возвращает `(ключ строки → BI, сводка)`.

    ACL-1815: сам джойн вынесен в `rules.evidence.exact_name_join` — один на
    таблицу Home и на правила; здесь остался тонкий вход с прежним ответом.
    Правила и вердикты идентичности — в докстринге того модуля.
    """
    from src.analysis_agent.rules.evidence import exact_name_join

    return exact_name_join(snap, bi)


# ───────────────────────── хранилище ─────────────────────────

_LOCK = threading.RLock()
_RUNNING: set[str] = set()


def _store_path() -> Path:
    return Path(paths.get_local_config_dir()) / "campaign_inventory.json"


def _load_all() -> dict[str, dict]:
    path = _store_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — испорченный файл = «ещё не читали», не падение страницы
        return {}
    return data if isinstance(data, dict) else {}


def _save_all(data: dict[str, dict]) -> None:
    path = _store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=path.name + ".",
                                         suffix=".tmp", delete=False) as handle:
            tmp = Path(handle.name)
            json.dump(data, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        paths._replace_when_free(tmp, path)  # noqa: SLF001 — как у account_spend
    finally:
        if tmp is not None:
            with suppress(OSError):
                tmp.unlink(missing_ok=True)


def get_snapshot(account: str) -> Snapshot | None:
    """Последний принятый снимок аккаунта (или `None`, если ни разу не читали)."""
    with _LOCK:
        rec = _load_all().get(account)
    snap = (rec or {}).get("snapshot") if isinstance(rec, dict) and _record_matches(rec, account) else None
    return _snapshot_from(snap) if isinstance(snap, dict) else None


def get_attempt(account: str) -> dict | None:
    """Последняя неудачная попытка, если она новее снимка: `{state, at, error, code}`."""
    with _LOCK:
        rec = _load_all().get(account)
    attempt = (rec or {}).get("attempt") if isinstance(rec, dict) and _record_matches(rec, account) else None
    return attempt if isinstance(attempt, dict) else None


def _record_matches(rec: dict, account: str) -> bool:
    expected = rec.get("machine_binding")
    if expected is None:
        return True  # preserved legacy inventory is not invented binding proof
    from src.analysis_agent.account_refresh_machine import binding
    try:
        return expected == binding(account)
    except (RuntimeError, ValueError):
        return False


def _saved_stop(rec: dict) -> tuple[str, str]:
    active = rec.get("active_stop")
    if isinstance(active, dict) and active.get("kind"):
        return str(active["kind"]), str(active.get("at_utc") or "")
    attempt = rec.get("attempt")
    source = attempt if isinstance(attempt, dict) else rec.get("snapshot")
    source = source if isinstance(source, dict) else {}
    return str(source.get("stop") or ""), str(source.get("at_utc") or source.get("finished_at_utc") or "")


def latest_stops() -> dict[str, tuple[str, str]]:
    """Аккаунт → (вид остановки Meta, ISO UTC времени) по последней записи: попытке или снимку.

    Только непустые остановки. Новая удача перезаписывает запись и снимает остановку.
    """
    with _LOCK:
        data = _load_all()
    out: dict[str, tuple[str, str]] = {}
    for account, rec in data.items():
        if not isinstance(rec, dict):
            continue
        kind, at = _saved_stop(rec)
        if kind:
            out[account] = (kind, at)
    return out


def _put_result(done: Snapshot) -> None:
    """Принять результат чтения.

    Удача (`ok`/`partial`/`empty`) заменяет снимок. Неудача его НЕ стирает: прежняя
    опись остаётся на экране с пометкой, что последнее чтение не удалось.
    """
    with _LOCK:
        data = _load_all()
        rec = data.get(done.account) if isinstance(data.get(done.account), dict) else {}
        old_stop, old_at = _saved_stop(rec)
        clean_success = read_succeeded(done)
        if clean_success:
            active_stop = None
        elif done.stop and not (old_stop == "checkpoint" and done.stop != "checkpoint"):
            active_stop = {"kind": done.stop, "at_utc": done.finished_at_utc}
        else:
            active_stop = {"kind": old_stop, "at_utc": old_at} if old_stop else None
        if active_stop is not None or "active_stop" in rec:
            # Локальная ошибка/неполная попытка не доказывает снятый checkpoint
            # и не начинает заново старое остывание. Новая удача снимает запись.
            rec["active_stop"] = active_stop
        if done.state == "failed":
            # `stop` и `at_utc` нужны очереди обновления: пауза по чекпоинту переживает рестарт
            # и видна второму процессу дешборда (ACL-2063).
            rec["attempt"] = {"state": "failed", "at": done.finished_at, "at_utc": done.finished_at_utc,
                              "error": done.error, "code": done.error_code, "stop": done.stop,
                              "date_from": done.date_from, "date_to": done.date_to}
        else:
            rec["snapshot"] = done.to_dict()
            rec["attempt"] = None
        rec["machine_revision"] = getattr(done, "_machine_revision", None)
        rec["machine_binding"] = getattr(done, "_machine_binding", None)
        data[done.account] = rec
        _save_all(data)
    if done.state != "failed":
        _notify_result(done)


# ACL-1816: слушатели «человек прочитал опись аккаунта» (удачное чтение). Rules ставит
# здесь только задание в очередь: оценка ничего не читает у Meta.
_RESULT_LISTENERS: list[Callable[[Snapshot], None]] = []


def add_result_listener(fn: Callable[[Snapshot], None]) -> None:
    if fn not in _RESULT_LISTENERS:
        _RESULT_LISTENERS.append(fn)


def _notify_result(done: Snapshot) -> None:
    for fn in list(_RESULT_LISTENERS):
        try:
            fn(done)
        except Exception:  # noqa: BLE001 - слушатель не должен ломать сохранение снимка
            log.exception("Inventory listener failed")


def put_result(done: Snapshot) -> None:
    # The short local projection lock is distinct from the machine read lease.
    # It prevents another dashboard process losing a different account's result.
    with _LOCK, paths.machine_file_lock(_store_path().with_suffix(".lock"), timeout=10):
        _put_result(done)


def adopt_machine_result(done: Snapshot, revision: int) -> bool:
    """Idempotent local projection; never hide a newer/unknown local attempt."""
    with _LOCK, paths.machine_file_lock(_store_path().with_suffix(".lock"), timeout=10):
        from src.analysis_agent.account_refresh_machine import binding
        try:
            if binding(done.account) != getattr(done, "_machine_binding", None):
                return False
        except (ValueError, RuntimeError):
            return False
        rec = _load_all().get(done.account) or {}
        if rec.get("machine_binding") not in (None, getattr(done, "_machine_binding", None)):
            rec = {}  # the existing file is preserved; the old target is not freshness for the new target
        previous = rec.get("machine_revision")
        if isinstance(previous, int):
            if previous >= revision:
                return False
        else:
            latest = rec.get("attempt") or rec.get("snapshot")
            if isinstance(latest, dict):
                stamp = latest.get("at_utc") or latest.get("finished_at_utc") or ""
                if not stamp or stamp >= done.finished_at_utc:
                    return False
        done._machine_revision = revision
        _put_result(done)
        return True


def summary(snap: Snapshot | None, attempt: dict | None = None, *, now: float | None = None) -> dict:
    """Лёгкая сводка для списка аккаунтов: без объектов."""
    out: dict[str, Any] = {"state": "idle", "attempt": attempt}
    if snap is None:
        return out
    out.update({
        "state": snap.state, "finished_at": snap.finished_at, "age_s": _age_s(snap.finished_at, now),
        "revision": hashlib.sha256(json.dumps(snap.to_dict(), sort_keys=True).encode()).hexdigest(),
        "date_from": snap.date_from, "date_to": snap.date_to, "currency": snap.currency,
        "problems": list(snap.problems), "steps": dict(snap.steps), "error": snap.error,
        "counts": {"campaigns": len(snap.campaigns), "adsets": len(snap.adsets), "ads": len(snap.ads)},
        "calls": snap.calls,
    })
    return out


def summaries(accounts: list[str]) -> dict[str, dict]:
    """Сводки нескольких аккаунтов за ОДИН разбор файла (а не по два на аккаунт)."""
    with _LOCK:
        data = _load_all()
    out = {}
    for account in accounts:
        rec = data.get(account) if isinstance(data.get(account), dict) else {}
        if not _record_matches(rec, account):
            rec = {}
        raw = rec.get("snapshot")
        attempt = rec.get("attempt") if isinstance(rec.get("attempt"), dict) else None
        out[account] = summary(_snapshot_from(raw) if isinstance(raw, dict) else None, attempt)
    return out


def _age_s(stamp: str, now: float | None = None) -> int | None:
    """Сколько секунд назад было чтение; `None`, если время нечитаемо."""
    try:
        read = time.mktime(time.strptime(stamp, "%Y-%m-%d %H:%M:%S"))
    except (TypeError, ValueError, OverflowError):
        return None
    return max(0, int((time.time() if now is None else now) - read))


# ───────────────────────── чтение ─────────────────────────

def _truncated_since(client: Any, before: int) -> bool:
    return len(getattr(client, "truncated", []) or []) > before


def read_into(snap: Snapshot, client: Any, act: str, *, structure_only: bool = False) -> None:
    """Чтение описи и Insights в `snap` через уже открытого клиента (только GET).

    `structure_only` (ACL-2063): только кампании, ad set и объявления — ни одного
    запроса Insights, в `snap.steps` лишь три шага.

    Исключения `GraphStop` (лимит, checkpoint, потолок вызовов…) шаг помечают
    `failed`, остальные шаги — `skipped`, и дальше клиент не зовётся: решение за
    человеком. Исключение уровня кампаний наружу не выходит, если хоть что-то
    уже прочитано; до списка кампаний не дошли — бросается наверх.
    """
    from src.analysis_agent import account_spend_api as _api
    from src.fb_api.graph import SHOWN_STATES, GraphError, GraphStop

    acc = client.account(act)
    _api._check_account(acc, act)  # noqa: SLF001
    snap.currency = str(acc.get("currency") or "")
    path = f"/{act}"

    def fetch(step: str, sub: str, fields: str, level: str):
        """Один список. Возвращает объекты или `None` при отказе (причина — в `snap`)."""
        before = len(getattr(client, "truncated", []) or [])
        try:
            rows = client.get_all(f"{path}/{sub}", {"fields": fields, "effective_status": SHOWN_STATES,
                                                    "limit": 200}, max_pages=5)
        except GraphStop as exc:
            snap.steps[step] = "failed"
            snap.stop = snap.stop or exc.kind
            snap.problems.append(f"{step} were not read: {_api._failure(snap.account, exc)[0]}")  # noqa: SLF001
            raise
        except GraphError as exc:
            snap.steps[step] = "failed"
            snap.problems.append(f"{step} were not read: {exc}")
            return None
        objs, dropped = parse_inventory(rows, level)
        snap.steps[step] = "truncated" if _truncated_since(client, before) else "ok"
        if snap.steps[step] == "truncated":
            snap.problems.append(f"the {step} list is partial: the page limit was reached")
        if dropped:
            snap.problems.append(f"{dropped} {step} rows without a usable id were ignored")
        return objs

    def insights(level: str, objs: list[dict]) -> None:
        step = f"insights_{level}"
        if not objs:
            snap.steps[step] = "skipped"
            return
        before = len(getattr(client, "truncated", []) or [])
        try:
            rows = client.insights_range(act, snap.date_from, snap.date_to, level=level,
                                         fields=f"{INSIGHT_ID[level]},{INSIGHT_METRICS},account_currency",
                                         max_pages=3)
        except GraphStop as exc:
            snap.steps[step] = "failed"
            snap.stop = snap.stop or exc.kind
            snap.problems.append(f"{level} insights were not read: {_api._failure(snap.account, exc)[0]}")  # noqa: SLF001
            raise
        except GraphError as exc:
            snap.steps[step] = "failed"
            snap.problems.append(f"{level} insights were not read: {exc}")
            return
        got, ambiguous = parse_insights(rows, level)
        snap.insights[level] = got
        snap.steps[step] = "truncated" if _truncated_since(client, before) else "ok"
        if snap.steps[step] == "truncated":
            snap.problems.append(f"the {level} insights are partial: the page limit was reached")
        if ambiguous:
            snap.problems.append(f"{ambiguous} {level} insights rows were ambiguous and ignored")
        if not snap.currency:
            snap.currency = next((m["currency"] for m in got.values() if m.get("currency")), "")

    for step in (STRUCTURE_STEPS if structure_only else STEPS):
        snap.steps[step] = "skipped"

    campaigns = fetch("campaigns", "campaigns", CAMPAIGN_FIELDS, "campaign")
    if campaigns is None:
        raise _api.ApiReadError(snap.problems[-1] if snap.problems else "Campaigns were not read")
    snap.campaigns = campaigns
    if not campaigns:
        return

    try:
        adsets = fetch("adsets", "adsets", ADSET_FIELDS, "adset")
        if adsets is not None:
            snap.adsets = adsets
        ads = fetch("ads", "ads", AD_FIELDS, "ad")
        if ads is not None:
            snap.ads = ads
        if not structure_only:
            insights("campaign", snap.campaigns)
            insights("adset", snap.adsets)
            insights("ad", snap.ads)
    except GraphStop:
        # Стоп Meta (лимит, checkpoint…) не бросаем: прочитанное остаётся, снимок
        # «неполный»; причина уже в `snap.problems`, клиент больше не зовётся.
        pass
    snap.calls = int(getattr(client, "calls", 0) or 0)


def finalize(snap: Snapshot) -> None:
    """Итоговое состояние по шагам: ok | partial | empty | failed."""
    if snap.steps.get("campaigns") not in ("ok", "truncated"):
        snap.state = "failed"
        return
    if not snap.campaigns:
        snap.state = "empty"
        return
    tree = build_tree(snap)
    if tree["orphan_adsets"]:
        snap.problems.append(f"{len(tree['orphan_adsets'])} ad sets belong to campaigns that were not returned")
    if tree["orphan_ads"]:
        snap.problems.append(f"{len(tree['orphan_ads'])} ads belong to ad sets that were not returned")
    # Структурный снимок (ACL-2063) не знает шагов Insights: «ok» — когда ok три шага.
    needed = STEPS if "insights_campaign" in snap.steps else STRUCTURE_STEPS
    clean = all(snap.steps.get(s) == "ok" for s in needed) and not snap.problems
    snap.state = "ok" if clean else "partial"


def read_via_api(account: str, date_from: str = "", date_to: str = "", *,
                 structure_only: bool = False, trigger: str = "") -> Snapshot:
    """Опись аккаунта через Graph API в профиле. Тот же `_session`, что у Read spend."""
    from src.analysis_agent import account_spend_api as _api
    from src.fb_api.graph import GraphStop

    if structure_only:
        date_from = date_to = ""
    else:
        date_to = date_to or date_from
    snap = Snapshot(account=account, state="failed", date_from=date_from, date_to=date_to)
    started = time.monotonic()
    notes: dict = {}
    stop = ""
    try:
        with _api._session(account) as sess:  # noqa: SLF001
            from src.analysis_agent.account_refresh_machine import verify_session
            verify_session(sess)
            try:
                try:
                    read_into(snap, sess.client, sess.act, structure_only=structure_only)
                finally:
                    snap.calls = int(getattr(sess.client, "calls", 0) or 0)
                if sess.client.stopped is not None:
                    stop = sess.client.stopped.kind
                    _api._report_stop(sess.client.stopped, account)  # noqa: SLF001
            finally:
                notes = sess.notes
        finalize(snap)
    except Exception as exc:  # noqa: BLE001 — чтение не должно ронять сервер
        snap.error, snap.error_code = _api._failure(account, exc)  # noqa: SLF001
        if snap.steps.get("campaigns") in ("ok", "truncated"):
            # Список кампаний уже прочитан: остаток — «неполно», а не «ничего».
            snap.problems.append(snap.error)
            finalize(snap)
        else:
            snap.state = "failed"
        if isinstance(exc, GraphStop):
            stop = exc.kind
            _api._report_stop(exc, account)  # noqa: SLF001
    if stop:
        snap.stop = snap.stop or stop
    if trigger:
        notes = {**notes, "trigger": trigger}
    _api._telemetry("campaigns", account, read_succeeded(snap), time.monotonic() - started, notes,  # noqa: SLF001
                    error=snap.error, stop=stop)
    return snap


def _read_in_session(account: str, sess: Any, *, trigger: str) -> Snapshot:
    """Обновить структуру уже открытой сессией после команды/публикации (ACL-2063).

    Ни открытия профиля, ни нового клиента, ни сброса бюджета. Вызывающий
    сохраняет физическую аренду профиля на весь вызов.
    """
    from src.analysis_agent import account_spend_api as _api
    from src.analysis_agent.account_refresh_state import read_session
    from src.fb_ads_rpa.profile_lease import require_profile_lease
    from src.fb_api.graph import GraphStop

    snap = Snapshot(account=account, state="failed", started_at=_as._now())  # noqa: SLF001
    entry = _as._entry_for(account)  # noqa: SLF001
    if entry is None:
        # Удаление привязки не должно ни читать старый профиль, ни стирать его
        # прежнюю остановку/историю новой фиктивной попыткой.
        snap.error, snap.error_code = "This account was removed from the dashboard.", "account_removed"
        return snap
    started = time.monotonic()
    before = int(getattr(sess.client, "calls", 0) or 0)
    stop = ""
    try:
        if not backend_ready():
            raise NeedsApiBackend(NEEDS_API_MESSAGE)
        if _api.normalize_act(str(getattr(entry, "ad_account_id", "") or "")) != sess.act:
            raise ValueError("The account map changed. Its list was not read with the previous session.")
        require_profile_lease(_as._profile_key(account))  # noqa: SLF001
        with read_session(priority=trigger in {"action", "retry"}):
            from src.analysis_agent.account_refresh_machine import verify_session
            verify_session(sess)
            read_into(snap, sess.client, sess.act, structure_only=True)
            if getattr(sess.client, "stopped", None) is not None:
                stop = sess.client.stopped.kind
                _api._report_stop(sess.client.stopped, account)  # noqa: SLF001
            finalize(snap)
    except Exception as exc:  # noqa: BLE001 — исход команды не меняется
        snap.error, snap.error_code = _api._failure(account, exc)  # noqa: SLF001
        if snap.steps.get("campaigns") in ("ok", "truncated"):
            snap.problems.append(snap.error)
            finalize(snap)
        if isinstance(exc, GraphStop):
            stop = exc.kind
            _api._report_stop(exc, account)  # noqa: SLF001
    snap.stop = snap.stop or stop
    snap.calls = max(0, int(getattr(sess.client, "calls", 0) or 0) - before)
    snap.finished_at = _as._now()  # noqa: SLF001
    snap.finished_at_utc = datetime.now(timezone.utc).isoformat(timespec="seconds")
    snap.took_s = round(time.monotonic() - started, 1)
    notes = {**getattr(sess, "notes", {}), "trigger": trigger, "calls": snap.calls, "same_session": True}
    _api._telemetry("campaigns", account, read_succeeded(snap), snap.took_s, notes,  # noqa: SLF001
                    error=snap.error, stop=snap.stop)
    return snap


def _run_read(account: str, *, trigger: str, baseline=None, persist: bool = True) -> Snapshot:
    """Синхронное чтение только структуры (ACL-2063): без Insights, без периода.

    Очередь та же, что у `start_read`: `browser_operation.reading()` и замок
    профиля аккаунта. Результат кладётся в хранилище через `put_result`.
    """
    began = time.time()
    started_at = _as._now()  # noqa: SLF001
    try:
        if not backend_ready():
            # Как `start_read`: без бэкенда API профиль не открываем вовсе.
            done = Snapshot(account=account, state="failed", error=NEEDS_API_MESSAGE, error_code=NEEDS_API_CODE)
        else:
            # The caller now holds the physical target and read fence through
            # this result's durable settlement, not only through _session.
            from src.analysis_agent import account_refresh, account_spend_api
            cached = get_snapshot(account)
            if (account_refresh.is_fresh(account)
                    and (trigger.split(":")[0] in {"schedule", "attention"}
                         or summary(cached).get("revision") != baseline)):
                account_spend_api._telemetry_skip(account, trigger)  # noqa: SLF001
                return cached
            done = read_via_api(account, structure_only=True, trigger=trigger)
    except Exception as exc:  # noqa: BLE001
        done = Snapshot(account=account, state="failed", error=f"{type(exc).__name__}: {exc}")
    done.started_at = started_at
    done.finished_at = _as._now()  # noqa: SLF001
    done.finished_at_utc = datetime.now(timezone.utc).isoformat(timespec="seconds")
    done.took_s = round(time.time() - began, 1)
    if persist:
        try:
            put_result(done)
        except Exception:  # noqa: BLE001
            log.exception("Could not persist the campaign inventory of %s", account)
    return done


def _machine_read(account: str, trigger: str, operation, *, machine=None, now=None) -> Snapshot:
    """Fence actual open/read, outcome and durable halt before the next reader.

    It deliberately does not require the queued dispatcher. A publisher already
    owns this physical target and can reenter this same-thread read fence.
    """
    from src.analysis_agent.account_refresh_machine import MachineStore, binding
    from src.analysis_agent.account_refresh_state import read_session, target_activity
    coordinator = machine if machine is not None else MachineStore()
    clock = now if now is not None else time.time
    force = trigger.split(":")[0] in {"action", "retry"}
    try:
        profile = _as._profile_key(account)  # noqa: SLF001
        with browser_operation.reading(), _as._lock_for(account), target_activity(profile), read_session(priority=force):
            key = binding(account)
            coordinator.recover_running(now=clock())
            halt = coordinator.halt(now=clock())
            if halt and (halt["until"] is not None or not force):
                paused = Snapshot(account=account, state="failed", error=halt["reason"])
                paused._machine_paused = halt
                paused._machine_committed = True
                return paused
            token = coordinator.begin(key, "inline", trigger)
            with coordinator.executing(token, key):
                own_recovery = coordinator.stop_generation(key)
                done = operation()
                revision = coordinator.publish(key, done, now=clock(), force=force,
                                               recovery=halt["generation"] if halt else None,
                                               own_recovery=own_recovery, token=token)
                try:
                    still_bound = binding(account) == key
                except (ValueError, RuntimeError):
                    still_bound = False
                if not still_bound:
                    # Keep the old physical target's durable outcome/stop, but
                    # never project it as the new/removed map row's own list.
                    changed = Snapshot(account=account, state="failed", error="The account map changed while its list was read.",
                                       error_code="account_map_changed")
                    changed._machine_committed = True
                    return changed
                done._machine_revision = revision
                done._machine_binding = key
                done._machine_committed = True
                try:
                    put_result(done)
                except Exception:
                    # The durable shared result remains authoritative and can
                    # be projected on the next status request without rereading.
                    log.exception("The committed account result awaits its local cache projection")
                return done
    except Exception as exc:  # noqa: BLE001 — no private fallback and no second external attempt
        failed = Snapshot(account=account, state="failed", error=f"{type(exc).__name__}: {exc}",
                          error_code="coordination_unavailable", finished_at=_as._now(),
                          finished_at_utc=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        failed._machine_committed = True
        try:
            put_result(failed)
        except Exception:
            log.exception("Could not save the account-refresh refusal")
        return failed


def run_read(account: str, *, trigger: str, machine=None, now=None) -> Snapshot:
    if not backend_ready():
        # A config-local disabled backend is not an external stop on another
        # dashboard's valid API session. Preserve the original no-open refusal.
        done = Snapshot(account=account, state="failed", error=NEEDS_API_MESSAGE, error_code=NEEDS_API_CODE,
                        finished_at=_as._now(), finished_at_utc=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        done._machine_committed = True
        put_result(done)
        return done
    baseline = summary(get_snapshot(account)).get("revision")
    return _machine_read(account, trigger, lambda: _run_read(account, trigger=trigger, baseline=baseline, persist=False),
                         machine=machine, now=now)


def read_in_session(account: str, sess: Any, *, trigger: str) -> Snapshot:
    from src.fb_ads_rpa.profile_lease import require_profile_lease
    if _as._entry_for(account) is None:
        failed = Snapshot(account=account, state="failed", error="This account was removed from the dashboard.",
                          error_code="account_removed")
        failed._machine_committed = True
        return failed
    require_profile_lease(_as._profile_key(account))  # noqa: SLF001
    return _machine_read(account, trigger, lambda: _read_in_session(account, sess, trigger=trigger))


def default_reader() -> Callable[[str, str, str], Snapshot]:
    return read_via_api


# ───────────────────────── задание ─────────────────────────

def backend_ready() -> bool:
    from src.analysis_agent import account_spend_api

    return account_spend_api.api_enabled()


def is_running(account: str) -> bool:
    with _LOCK:
        return account in _RUNNING


def running_now() -> list[str]:
    with _LOCK:
        return sorted(_RUNNING)


class NeedsApiBackend(RuntimeError):
    """`ACL_FB_BACKEND` не `api`: опись не читается, ничего не запущено."""

    code = NEEDS_API_CODE


def start_read(account: str, date_from: str, date_to: str = "", *,
               reader: Callable[[str, str, str], Snapshot] | None = None) -> dict:
    """Поставить чтение описи в работу и сразу ответить. Как `account_spend.start_read`.

    Очередь та же: `browser_operation.reading()` и замок профиля аккаунта. Без
    `ACL_FB_BACKEND=api` бросает `NeedsApiBackend` и не запускает ничего. Чтение
    идёт в отдельном потоке; повторное нажатие на тот же аккаунт присоединяется.
    """
    if not backend_ready():
        raise NeedsApiBackend(NEEDS_API_MESSAGE)
    date_to = date_to or date_from
    with _LOCK:
        if account in _RUNNING:
            return {"started": False, "running": True}
        _RUNNING.add(account)
    fn = reader or default_reader()
    started_at = _as._now()  # noqa: SLF001

    def run() -> None:
        began = time.time()
        try:
            with browser_operation.reading(), _as._lock_for(account):  # noqa: SLF001
                done = fn(account, date_from, date_to)
        except Exception as exc:  # noqa: BLE001
            done = Snapshot(account=account, state="failed", date_from=date_from, date_to=date_to,
                            error=_as._failure_text(exc))  # noqa: SLF001
        done.started_at = started_at
        done.finished_at = _as._now()  # noqa: SLF001
        done.finished_at_utc = datetime.now(timezone.utc).isoformat(timespec="seconds")
        done.took_s = round(time.time() - began, 1)
        try:
            put_result(done)
        except Exception:  # noqa: BLE001
            log.exception("Could not persist the campaign inventory of %s", account)
        finally:
            with _LOCK:
                _RUNNING.discard(account)

    try:
        threading.Thread(target=run, name=f"inventory:{account}", daemon=True).start()
    except Exception:
        with _LOCK:
            _RUNNING.discard(account)
        raise
    return {"started": True, "running": True}
