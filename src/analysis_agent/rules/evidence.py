"""Rules T2 (ACL-1815): доказательства для правил — точный джойн BI с вердиктами идентичности.

Дизайн: `C:\acl-notes\rules-design.md`, §3 (идентичность, зрелость, сверка), §4.2 (свежесть).
Модуль чистый: ничего не читает у Meta, не пишет, не открывает профили, не зовёт
таймеров. Вход — снимок описи (`campaign_inventory.Snapshot`) и таблица книги
статистики (pandas), выход — числа и ответ «можно ли этому верить».

Главные правила (решение владельца и дизайна):

* **Один джойн.** Таблица Home (`campaign_inventory.join_bi`) и правила считают
  через `exact_name_join`; ответ таблицы не менялся при выносе.
* **Вердикт на каждый объект.** `exact` и `exact_by_children` — единственные,
  что допускают Automatic; `copy_suffix` (имя рядом с `X - Copy`) — только Observe/Suggest. Остальные — только Observe/Suggest.
* **Нет строк — нет данных, а не ноль.** Объект не из книги никогда не получает
  `FTD = 0`: его числа — `None`, причина — `Missing mapping`.
* **Асимметричная зрелость.** Stop-loss: spend за `[start, through − D]`, FTD за
  `[start, through]`. Scale: оба за `[start, through − D]` плюс проверка «хвоста».
* **Свежесть блокирует Automatic**, а не Observe/Suggest.
"""
from __future__ import annotations

import math
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable

from src.analysis_agent.campaign_inventory import Snapshot, bi_metrics, build_tree, child_state, norm_name

# ───────────────────────── вердикты ─────────────────────────

EXACT = "exact"
EXACT_BY_CHILDREN = "exact_by_children"
COPY_SUFFIX = "copy_suffix"
ALIAS_HEURISTIC = "alias_heuristic"
PARTIAL = "partial"
DUPLICATE_NAME = "duplicate_name"
MULTI_COUNTRY = "multi_country"
RENAMED = "renamed"
NOT_IN_WORKBOOK = "not_in_workbook"

VERDICTS = (EXACT, EXACT_BY_CHILDREN, COPY_SUFFIX, ALIAS_HEURISTIC, PARTIAL, DUPLICATE_NAME,
            MULTI_COUNTRY, RENAMED, NOT_IN_WORKBOOK)

#: Только эти вердикты могут пропустить Automatic (§3.2). `copy_suffix` решением
#: координатора Automatic не пропускает: рядом живёт копия, и «то же ли это имя» решает человек.
AUTOMATIC_VERDICTS = frozenset({EXACT, EXACT_BY_CHILDREN})

#: Причина отказа в UI (§1.6): «нет строк» и «неоднозначно» — разные причины.
MISSING_MAPPING = "Missing mapping"
AMBIGUOUS_MAPPING = "Ambiguous mapping"
STALE_BI = "Stale BI"
STALE_SNAPSHOT = "Stale snapshot"
DATA_MISMATCH = "Data mismatch"
PENDING_OUTCOMES = "Pending outcomes"
INSUFFICIENT_EVIDENCE = "Insufficient evidence"

_REASON_OF = {
    NOT_IN_WORKBOOK: MISSING_MAPPING,
    ALIAS_HEURISTIC: AMBIGUOUS_MAPPING, PARTIAL: AMBIGUOUS_MAPPING, DUPLICATE_NAME: AMBIGUOUS_MAPPING,
    MULTI_COUNTRY: AMBIGUOUS_MAPPING, RENAMED: AMBIGUOUS_MAPPING, COPY_SUFFIX: AMBIGUOUS_MAPPING,
}

#: `X - Copy`, `X - Copy 2`, `X-copy` (Duplicate в Ads Manager и ручные копии).
_COPY_RE = re.compile(r"^(?P<base>.*?)\s*-\s*copy(?:\s*\d+)?$", re.IGNORECASE)


# ───────────────────────── общий джойн (бывший join_bi) ─────────────────────────

def exact_name_join(snap: Snapshot, bi: dict | None) -> tuple[dict[str, dict], dict]:
    """Цепляет BI к объектам Meta (общий джойн таблицы Home и правил). Возвращает `(ключ строки → BI, сводка)`.

    Ключ совпадения — ИМЯ (точное, пробелы схлопнуты) внутри одного аккаунта и
    только если оно однозначно: два ad set с одним именем — нет совпадения,
    как и два объявления с одним заголовком в одном ad set. Книга хранит
    данные на уровне `Adset Name` / `Ad Title`:

    * ad set — точное имя среди ad set аккаунта;
    * кампания — точное имя (у наших кампаний имя ad set совпадает с именем
      кампании); либо, если имени нет, все её ad set совпали — тогда сумма
      по ним (отношения пересчитываются). Частичное покрытие не складываем;
    * объявление — заголовок под совпавшим ad set.

    Что не зацепилось ни за кого — `evidence_only` (только справка).
    """
    summary = {"matched": 0, "evidence_only": [], "available": bi is not None}
    if bi is None:
        return {}, summary
    book_c: dict[str, dict] = bi.get("campaigns") or {}
    book_a: dict[str, dict[str, dict]] = bi.get("ads") or {}
    tree = build_tree(snap)
    out: dict[str, dict] = {}
    used: set[str] = set()

    adset_names = Counter(norm_name(a["name"]) for a in snap.adsets)
    camp_names = Counter(norm_name(c["name"]) for c in snap.campaigns)

    def numbers(m: dict) -> dict:
        return bi_metrics(m.get("spend"), m.get("leads"), m.get("deposits"))

    # ad set
    for a in snap.adsets:
        n = norm_name(a["name"])
        if n and adset_names[n] == 1 and n in book_c:
            out[f"adset:{a['id']}"] = {"match": "name", **numbers(book_c[n])}
            used.add(n)
    # объявления под совпавшим ad set
    for ad in snap.ads:
        parent = tree["adsets"].get(ad.get("adset_id"))
        if parent is None or f"adset:{parent['id']}" not in out:
            continue
        pn = norm_name(parent["name"])
        titles = Counter(norm_name(x["name"]) for x in tree["ads_of"].get(parent["id"], []))
        t = norm_name(ad["name"])
        per = book_a.get(pn) or {}
        if t and titles[t] == 1 and t in per:
            out[f"ad:{ad['id']}"] = {"match": "name", **numbers(per[t])}
    # кампании
    for c in snap.campaigns:
        n = norm_name(c["name"])
        kids = tree["adsets_of"].get(c["id"], [])
        # Имя кампании = ключ книги (`Adset Name`) годится, только если строка книги
        # покрывает ВСЮ кампанию: ad set нет вовсе или все они носят это же имя.
        # Иначе (например, `X` и `X - Copy` после Duplicate) строка книги — лишь
        # часть кампании, и итог идёт путём по ad set: полное покрытие или partial.
        whole = all(norm_name(k["name"]) == n for k in kids)
        if n and camp_names[n] == 1 and n in book_c and whole:
            out[f"campaign:{c['id']}"] = {"match": "name", **numbers(book_c[n])}
            used.add(n)
            continue
        hit = [k for k in kids if f"adset:{k['id']}" in out]
        if kids and len(hit) == len(kids):
            spend = leads = ftds = None
            for k in hit:
                m = out[f"adset:{k['id']}"]
                spend = (spend or 0.0) + (m.get("spend") or 0.0)
                leads = (leads or 0) + (m.get("leads") or 0)
                ftds = (ftds or 0) + (m.get("ftds") or 0)
            out[f"campaign:{c['id']}"] = {"match": "ad_sets", "coverage": f"{len(hit)}/{len(kids)}",
                                          **bi_metrics(spend, leads, ftds)}
        elif hit:
            out[f"campaign:{c['id']}"] = {"match": "partial", "coverage": f"{len(hit)}/{len(kids)}"}
    summary["matched"] = len(used)
    summary["evidence_only"] = sorted(n for n in book_c if n not in used)
    return out, summary


# ───────────────────────── вердикты идентичности (§3.2) ─────────────────────────

def classify_alias(label: str, known: Iterable[str]) -> str:
    """Как имя аккаунта нашлось в книге: `exact` | `heuristic` | `none`.

    `exact` — явное правило имён (`account_names.exact_alias`) или своё имя
    аккаунта в книге. `heuristic` — книга знает аккаунт только по хвосту имени
    (`workbook_alias_for` → `_alias_by_tail`): такому совпадению Automatic не верит.
    `none` — книга не знает ни одного имени этой страны.
    """
    from src.analysis_agent.account_names import account_key, exact_alias, has_own_name

    names = [str(x) for x in known if str(x or "").strip()]
    if not names:
        return "none"
    if exact_alias(label, names) or has_own_name(label, names):
        return "exact"
    if account_key(label) in {account_key(n) for n in names}:
        return "exact"
    return "heuristic"


def find_multi_country(df: Any, aliases: dict[str, str]) -> set[str]:
    """Имена `Adset Name`, которые у аккаунта встречаются в книге в ≥ 2 странах.

    `merge_attribution` складывает такие имена молча; правила должны знать, что
    сумма — смесь стран. `aliases` — `{страна: имя аккаунта в книге}` по ВСЕМ
    странам, где аккаунт мог светиться.
    """
    from src.analysis_agent.analyze_fb_stats import alias_matches
    from src.analysis_agent.morning_report import _tree_name

    seen: dict[str, set[str]] = defaultdict(set)
    for country, alias in (aliases or {}).items():
        part = df[(df["Country Final"] == country) & alias_matches(df["Account Alias"], alias)]
        for name in {_tree_name(v) for v in part["Adset Name"].tolist()}:
            if name:
                seen[name].add(country)
    return {n for n, cs in seen.items() if len(cs) > 1}


def _copy_related(names: Iterable[str]) -> set[str]:
    """Имена, у которых в том же наборе есть родня: `X` рядом с `X - Copy` (и наоборот)."""
    groups: dict[str, set[str]] = defaultdict(set)
    for name in names:
        if not name:
            continue
        hit = _COPY_RE.match(name)
        base = norm_name(hit.group("base")) if hit and hit.group("base").strip() else name
        groups[base.casefold()].add(name)
    return {n for g in groups.values() if len(g) > 1 for n in g}


def _verdict(kind: str, reason: str = "") -> dict:
    return {"verdict": kind, "automatic_ok": kind in AUTOMATIC_VERDICTS,
            "blocker": _REASON_OF.get(kind, ""), "reason": reason}


def identity_verdicts(snap: Snapshot, joined: dict[str, dict], *, bi_available: bool = True,
                      alias_quality: str = "exact", multi_country: Iterable[str] = (),
                      assigned_names: dict[str, str] | None = None,
                      current_names: dict[str, str] | None = None) -> dict[str, dict]:
    """Вердикт идентичности на каждый объект снимка: `{"campaign:1": {"verdict": …}, …}`.

    `joined` — ответ `exact_name_join`. `assigned_names` / `current_names` —
    `{ключ: имя}` из назначения правила и из повторного чтения: расхождение с
    именем снимка — `renamed`, история под старым именем не подтянута.
    ВНИМАНИЕ: вердикт `renamed` возникает только если вызывающий передал эти два
    словаря (имя при назначении правила и имя из re-read); без них он не выдаётся.
    """
    tree = build_tree(snap)
    multi = {norm_name(x) for x in multi_country}
    assigned, current = assigned_names or {}, current_names or {}
    camp_count = Counter(norm_name(c["name"]) for c in snap.campaigns)
    adset_count = Counter(norm_name(a["name"]) for a in snap.adsets)
    copy_camps, copy_adsets = _copy_related(camp_count), _copy_related(adset_count)
    heuristic = alias_quality != "exact"
    out: dict[str, dict] = {}

    def renamed(key: str, name: str) -> bool:
        return any(norm_name(m.get(key)) not in ("", name) for m in (assigned, current))

    def common(key: str, name: str, count: int) -> dict | None:
        """Проверки, общие для всех уровней, в порядке строгости."""
        if renamed(key, name):
            return _verdict(RENAMED, "the name differs from the one saved or assigned")
        if not bi_available:
            return _verdict(NOT_IN_WORKBOOK, "the stats workbook is not available")
        if not name or count > 1:
            return _verdict(DUPLICATE_NAME if name else NOT_IN_WORKBOOK,
                            "several objects of this account share the name" if name else "the object has no name")
        if name in multi:
            return _verdict(MULTI_COUNTRY, "the workbook has this name under more than one country")
        return None

    for a in snap.adsets:
        key, name = f"adset:{a['id']}", norm_name(a["name"])
        hit = common(key, name, adset_count[name])
        if hit is None:
            entry = joined.get(key)
            if entry is None:
                hit = _verdict(NOT_IN_WORKBOOK, "no workbook rows under this exact name")
            elif heuristic:
                hit = _verdict(ALIAS_HEURISTIC, "the account was matched to the workbook by a name tail only")
            elif name in copy_adsets:
                hit = _verdict(COPY_SUFFIX, "unique exact name next to a copy")
            else:
                hit = _verdict(EXACT)
        out[key] = hit

    for ad in snap.ads:
        key, title = f"ad:{ad['id']}", norm_name(ad["name"])
        parent = tree["adsets"].get(ad.get("adset_id"))
        siblings = Counter(norm_name(x["name"]) for x in tree["ads_of"].get(parent["id"], [])) if parent else Counter()
        hit = common(key, title, siblings[title])
        if hit is None:
            entry = joined.get(key)
            up = out.get(f"adset:{parent['id']}") if parent else None
            if entry is None:
                hit = _verdict(NOT_IN_WORKBOOK, "no workbook rows under this ad title")
            elif up and up["verdict"] in (ALIAS_HEURISTIC, MULTI_COUNTRY, RENAMED, COPY_SUFFIX):
                hit = _verdict(up["verdict"], "inherited from the ad set")
            elif title in _copy_related(siblings):
                hit = _verdict(COPY_SUFFIX, "unique exact title next to a copy")
            else:
                hit = _verdict(EXACT)
        out[key] = hit

    adsets_ok = child_state(snap, "adset") == "ok"
    for c in snap.campaigns:
        key, name = f"campaign:{c['id']}", norm_name(c["name"])
        hit = common(key, name, camp_count[name])
        if hit is None:
            entry = joined.get(key)
            kids = tree["adsets_of"].get(c["id"], [])
            if entry is None:
                hit = _verdict(NOT_IN_WORKBOOK, "no workbook rows under this name or under its ad sets")
            elif entry["match"] == "partial":
                hit = _verdict(PARTIAL, f"only {entry.get('coverage', '?')} of its ad sets were matched")
            elif not adsets_ok:
                # Список ad set обрезан или не читался: «вся кампания покрыта» утверждать нельзя.
                hit = _verdict(PARTIAL, "the ad set list is incomplete, so full coverage cannot be shown")
            elif entry["match"] == "ad_sets":
                kinds = {out[f"adset:{k['id']}"]["verdict"] for k in kids}
                worst = next((v for v in (RENAMED, MULTI_COUNTRY, ALIAS_HEURISTIC, COPY_SUFFIX) if v in kinds), "")
                hit = (_verdict(worst, "inherited from its ad sets") if worst
                       else _verdict(EXACT_BY_CHILDREN, f"all {len(kids)} ad sets matched exactly"))
            elif heuristic:
                hit = _verdict(ALIAS_HEURISTIC, "the account was matched to the workbook by a name tail only")
            elif name in copy_camps:
                hit = _verdict(COPY_SUFFIX, "unique exact name next to a copy")
            else:
                hit = _verdict(EXACT)
        out[key] = hit
    return out


def coverage(identities: dict[str, dict], keys: Iterable[str] | None = None, *,
             evidence_only: Iterable[str] = ()) -> dict:
    """Покрытие правила (§3.3): `{assigned, exact, heuristic, partial, missing, ambiguous, evidence_only}`."""
    chosen = list(identities) if keys is None else [k for k in keys]
    kinds = Counter(identities.get(k, _verdict(NOT_IN_WORKBOOK))["verdict"] for k in chosen)
    return {
        "assigned": len(chosen),
        "exact": kinds[EXACT] + kinds[EXACT_BY_CHILDREN],
        "heuristic": kinds[ALIAS_HEURISTIC],
        "partial": kinds[PARTIAL],
        "missing": kinds[NOT_IN_WORKBOOK],
        "ambiguous": kinds[DUPLICATE_NAME] + kinds[MULTI_COUNTRY] + kinds[RENAMED] + kinds[COPY_SUFFIX],
        "evidence_only": sorted(evidence_only),
    }


# ───────────────────────── окна дат и зрелость (§3.4) ─────────────────────────

#: Сколько последних дней книги ещё «не дозрели» (поздние FTD), по регионам.
#: Значения по умолчанию — из дизайна; владелец настраивает в правиле (`days`).
DEFAULT_MATURITY_DAYS = 2
MATURITY_DAYS_BY_REGION: dict[str, int] = {"LATAM": 2, "GCC": 2}
MAX_MATURITY_DAYS = 14

STOP_LOSS = "stop_loss"
SCALE = "scale"


def maturity_days(region: str | None = None, override: int | None = None) -> int:
    """`D` для региона: явное значение правила → таблица региона → умолчание. Диапазон 0..14."""
    if override is not None:
        value = override
    else:
        value = MATURITY_DAYS_BY_REGION.get(str(region or "").strip().upper(), DEFAULT_MATURITY_DAYS)
    return max(0, min(MAX_MATURITY_DAYS, int(value)))


def _as_date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


@dataclass(frozen=True)
class Windows:
    """Окна дат одной оценки. `None` на конце — окно пустое (зрелых дней ещё нет)."""

    strategy: str
    start: date
    through: date
    days: int
    spend: tuple[date, date] | None
    ftd: tuple[date, date] | None
    tail: tuple[date, date] | None

    def to_dict(self) -> dict:
        def span(w):
            return None if w is None else [w[0].isoformat(), w[1].isoformat()]
        return {"strategy": self.strategy, "start": self.start.isoformat(), "through": self.through.isoformat(),
                "maturity_days": self.days, "spend": span(self.spend), "ftd": span(self.ftd), "tail": span(self.tail)}


def maturity_windows(strategy: str, start: Any, through: Any, days: int) -> Windows:
    """Асимметричные окна (§3.4).

    Stop-loss: spend за `[start, through − D]`, FTD за `[start, through]` — поздние
    FTD засчитываются, ложных пауз меньше. Scale: spend и FTD за `[start, through − D]`,
    плюс «хвост» `[through − D + 1, through]`, который должен тоже держаться.
    """
    if strategy not in (STOP_LOSS, SCALE):
        raise ValueError(f"unknown strategy: {strategy}")
    first, last = _as_date(start), _as_date(through)
    if first is None or last is None:
        raise ValueError("start and through must be dates")
    days = max(0, int(days))
    mature_end = last - timedelta(days=days)
    mature = (first, mature_end) if mature_end >= first else None
    tail = (max(first, mature_end + timedelta(days=1)), last) if days > 0 and last >= first else None
    if strategy == STOP_LOSS:
        return Windows(strategy, first, last, days, mature, (first, last) if last >= first else None, None)
    return Windows(strategy, first, last, days, mature, mature, tail)


def workbook_through(df: Any) -> date | None:
    """Последний день книги (`max(Date)`) — граница «through»."""
    import pandas as pd

    if df is None or "Date" not in getattr(df, "columns", []):
        return None
    days = pd.to_datetime(df["Date"], errors="coerce").dt.date.dropna()
    return max(days) if len(days) else None


def filter_window(df: Any, window: tuple[date, date] | None) -> Any:
    """Строки книги в `[from, to]`. Пустое окно — пустой кадр, а не вся книга."""
    import pandas as pd

    days = pd.to_datetime(df["Date"], errors="coerce").dt.date
    if window is None:
        return df.iloc[0:0]
    return df[days.notna() & (days >= window[0]) & (days <= window[1])]


def window_attribution(df: Any, aliases: dict[str, str], window: tuple[date, date] | None) -> dict:
    """Числа книги по имени за окно: то же, что `_inventory_bi`, но на кадре окна."""
    from src.analysis_agent.campaign_inventory import merge_attribution
    from src.analysis_agent.morning_report import build_tree_attribution

    frame = filter_window(df, window)
    parts = []
    for country, alias in (aliases or {}).items():
        got = build_tree_attribution(frame, country, alias)
        if got.get("has_data"):
            parts.append(got)
    return merge_attribution(parts)


def rebase_bi(full: dict, part: dict) -> dict:
    """Книга с составом имён `full`, но с числами окна `part`.

    Имя, которое есть в книге вообще, но не имеет строк в подокне, получает
    настоящие нули ЭТОГО окна (строки в книге у объекта есть — в эти дни он не
    тратил). Идентичность определяется по всей книге, а не по подокну: иначе
    кампания «теряла бы» ad set и превращалась в `partial` лишь из-за хвоста.
    """
    zero = {"spend": 0.0, "leads": 0, "deposits": 0}
    camps = {n: dict((part.get("campaigns") or {}).get(n) or zero) for n in (full.get("campaigns") or {})}
    ads: dict[str, dict] = {}
    for n, per in (full.get("ads") or {}).items():
        got = (part.get("ads") or {}).get(n) or {}
        ads[n] = {t: dict(got.get(t) or zero) for t in per}
    return {"campaigns": camps, "ads": ads}


# ───────────────────────── свежесть (§4.2) ─────────────────────────

BI_MAX_BUILD_AGE_H = 30.0
SNAPSHOT_MAX_AGE_H_DEFAULT = 24.0


def max_snapshot_age_h() -> float:
    """`ACL_RULES_MAX_SNAPSHOT_AGE_H` в пределах 1–72, иначе 24."""
    try:
        value = float(os.environ.get("ACL_RULES_MAX_SNAPSHOT_AGE_H", ""))
    except ValueError:
        return SNAPSHOT_MAX_AGE_H_DEFAULT
    if not math.isfinite(value):  # nan / inf — мусор, а не «максимум диапазона»
        return SNAPSHOT_MAX_AGE_H_DEFAULT
    return max(1.0, min(72.0, value))


def _utc(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), timezone.utc)
    else:
        try:
            moment = datetime.fromisoformat(str(value))
        except ValueError:
            return None
    return moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment.astimezone(timezone.utc)


def snapshot_age_s(snap: Snapshot, now: Any = None) -> int | None:
    """Возраст снимка в секундах: по ISO UTC, а у старых снимков — по локальному `finished_at`."""
    import time

    current = _utc(now) or datetime.now(timezone.utc)
    read = _utc(getattr(snap, "finished_at_utc", ""))
    if read is None:
        try:
            read = datetime.fromtimestamp(time.mktime(time.strptime(snap.finished_at, "%Y-%m-%d %H:%M:%S")),
                                          timezone.utc)
        except (TypeError, ValueError, OverflowError):
            return None
    return max(0, int((current - read).total_seconds()))


def bi_freshness(bi_through: Any, built_at: Any, *, now: Any = None, today: date | None = None,
                 max_build_age_h: float = BI_MAX_BUILD_AGE_H) -> dict:
    """Книга свежа, если `through ≥ вчера` (дата машины) и сборка не старше 30 ч."""
    current = _utc(now) or datetime.now(timezone.utc)
    day = today or current.astimezone().date()
    through, built = _as_date(bi_through), _utc(built_at)
    reasons = []
    if through is None:
        reasons.append("the workbook has no dates")
    elif through < day - timedelta(days=1):
        reasons.append(f"the workbook ends on {through.isoformat()}, older than yesterday")
    if built is None:
        reasons.append("the build time of the workbook is unknown")
    elif (current - built).total_seconds() > max_build_age_h * 3600:
        reasons.append(f"the workbook was built more than {max_build_age_h:g} h ago")
    return {"fresh": not reasons, "blocker": "" if not reasons else STALE_BI, "reasons": reasons,
            "bi_through": through.isoformat() if through else "",
            "built_at": built.isoformat(timespec="seconds") if built else ""}


#: Какие шаги описи нужны для идентичности объектов уровня (§4.2).
_NEEDED_STEPS = {"campaign": ("campaigns", "adsets"), "adset": ("campaigns", "adsets"),
                 "ad": ("campaigns", "adsets", "ads")}


def snapshot_freshness(snap: Snapshot | None, *, level: str = "adset", now: Any = None,
                       max_age_h: float | None = None) -> dict:
    """Снимок описи годится для Automatic: нужные шаги `ok` и возраст в пределах порога.

    Общий `state` должен быть `ok`; `partial` допустим только если нужные
    уровню шаги прочитаны полностью (недочитанные insights идентичность не рушат).
    """
    limit = max_snapshot_age_h() if max_age_h is None else max_age_h
    if snap is None:
        return {"fresh": False, "blocker": STALE_SNAPSHOT, "reasons": ["the account inventory was never read"],
                "age_s": None, "finished_at_utc": ""}
    reasons = []
    if snap.state not in ("ok", "partial"):
        reasons.append(f"the last inventory read ended as {snap.state}")
    for step in _NEEDED_STEPS.get(level, ()):
        if snap.steps.get(step) != "ok":
            reasons.append(f"the {step} step is {snap.steps.get(step, 'not read')}")
    age = snapshot_age_s(snap, now)
    if age is None:
        reasons.append("the time of the inventory read is unknown")
    elif age > limit * 3600:
        reasons.append(f"the inventory is older than {limit:g} h")
    return {"fresh": not reasons, "blocker": "" if not reasons else STALE_SNAPSHOT, "reasons": reasons,
            "age_s": age, "finished_at_utc": snap.finished_at_utc}


# ───────────────────────── сверка с Meta (§3.5) ─────────────────────────

RECONCILE_TOLERANCE = 0.20


def meta_spend_total(snap: Snapshot) -> float | None:
    """Spend аккаунта по Meta из insights кампаний снимка; обрезанный или непрочитанный шаг — `None`."""
    if snap.steps.get("insights_campaign") != "ok":
        return None
    rows = (snap.insights.get("campaign") or {}).values()
    values = [r.get("spend") for r in rows if r.get("spend") is not None]
    return float(sum(values)) if values else None


def reconcile_spend(snap: Snapshot, window: tuple[date, date] | None, bi_spend: float | None) -> dict:
    """BI `$ Spend` против Meta `spend` снимка за ТОТ ЖЕ период (только USD).

    Расхождение больше 20% от большего — `Data mismatch`. Если сверку сделать
    нельзя (не-USD, период снимка не равен окну, нет числа с одной из сторон),
    статус `not_reconciled` и у него ТОЖЕ есть blocker `Not reconciled: …`:
    Automatic без сверки не идёт (решение координатора), Observe/Suggest — идут.
    Курс не придумываем.
    """
    def skipped(why: str) -> dict:
        return {"status": "not_reconciled", "blocker": f"Not reconciled: {why}", "note": f"Not reconciled: {why}",
                "diff": None}

    currency = (snap.currency or "").upper()
    if currency != "USD":
        return skipped(f"currency {currency or 'unknown'}")
    if window is None or (snap.date_from, snap.date_to) != (window[0].isoformat(), window[1].isoformat()):
        return skipped("snapshot period differs")
    meta = meta_spend_total(snap)
    if meta is None or bi_spend is None:
        return skipped("spend missing")
    top = max(abs(meta), abs(bi_spend))
    diff = 0.0 if top == 0 else abs(meta - bi_spend) / top
    bad = diff > RECONCILE_TOLERANCE
    return {"status": "mismatch" if bad else "ok", "blocker": DATA_MISMATCH if bad else "",
            "note": f"BI {bi_spend:.2f} vs Meta {meta:.2f}", "diff": round(diff, 4)}


# ───────────────────────── числа объекта за окна ─────────────────────────

def _nums(entry: dict | None) -> dict | None:
    """Spend/Leads/FTD записи джойна; у `partial` и отсутствующей записи чисел нет — `None`."""
    if not entry or entry.get("match") == "partial" or entry.get("spend") is None:
        return None
    return {"spend": entry.get("spend"), "leads": entry.get("leads"), "ftds": entry.get("ftds")}


def object_numbers(strategy: str, key: str, ident: dict, spend_w: dict | None, ftd_w: dict | None,
                   tail_w: dict | None, *, target_cpa: float | None = None) -> dict:
    """Числа объекта и состояние доказательства. «Нет строк» никогда не превращается в 0.

    `state`: `no_data` (объект не из книги / неоднозначен — чисел нет вообще),
    `no_mature_spend` (зрелого spend нет), `zero_ftd` (строки есть, spend есть, FTD
    настоящий ноль), `ok`; у scale ещё `tail_ok` / `tail_pending`.
    """
    spend, ftd = _nums(spend_w), _nums(ftd_w)
    base = {"key": key, "verdict": ident["verdict"]}
    if ident["verdict"] in (NOT_IN_WORKBOOK, DUPLICATE_NAME, PARTIAL) or spend is None or ftd is None:
        return {**base, "state": "no_data", "spend": None, "leads": None, "ftds": None, "cpa": None,
                "zero_ftd": False, "reason": INSUFFICIENT_EVIDENCE}
    sp, fd = spend["spend"], ftd["ftds"]
    out = {**base, "spend": sp, "leads": ftd["leads"], "ftds": fd, "cpa": (sp / fd) if (sp and fd) else None,
           "zero_ftd": bool(sp and sp > 0 and fd == 0), "reason": ""}
    if not sp:
        out.update(state="no_mature_spend", reason=INSUFFICIENT_EVIDENCE)
    else:
        out["state"] = "zero_ftd" if fd == 0 else "ok"
    if strategy == SCALE:
        t = _nums(tail_w)
        tail_spend = (t or {}).get("spend") or 0.0
        tail_ftds = (t or {}).get("ftds") or 0
        holds = (target_cpa is not None and tail_spend > 0 and tail_ftds > 0
                 and tail_spend / tail_ftds <= target_cpa)
        out["tail"] = {"spend": tail_spend, "ftds": tail_ftds,
                       "cpa": (tail_spend / tail_ftds) if tail_ftds else None,
                       "state": "tail_ok" if holds else "tail_pending"}
        if not holds and out["state"] == "ok":
            out["reason"] = PENDING_OUTCOMES
    return out


# ───────────────────────── сборка доказательств ─────────────────────────

def build_evidence(snap: Snapshot | None, df: Any, aliases: dict[str, str], *, strategy: str, start: Any,
                   bi_built_at: Any, account_label: str = "", through: Any = None, region: str | None = None,
                   days: int | None = None, target_cpa: float | None = None, now: Any = None,
                   today: date | None = None, alias_quality: str | None = None,
                   multi_country: Iterable[str] | None = None, assigned_names: dict[str, str] | None = None,
                   current_names: dict[str, str] | None = None, scope_keys: Iterable[str] | None = None) -> dict:
    """Доказательства аккаунта для оценки правила. Только чтение книги и описи.

    `bi_built_at` обязателен: время сборки книги (`None` = неизвестно = `Stale BI`).
    `assigned_names` / `current_names` нужны для вердикта `renamed` — их должен
    передать вызывающий (имя при назначении правила и имя из re-read).

    Возвращает словарь (годится для `rule_evaluations.jsonl`): окна, вердикты,
    числа, покрытие, свежесть, сверку и итог по Automatic на каждый объект.
    """
    if snap is None:
        raise ValueError("the inventory snapshot is required: objects come from it")
    last = _as_date(through) or workbook_through(df)
    if last is None:
        raise ValueError("the workbook has no dates")
    windows = maturity_windows(strategy, start, last, maturity_days(region, days))

    full_window = (windows.start, windows.through)
    full = window_attribution(df, aliases, full_window)
    bi_available = bool(full.get("campaigns"))
    joined, summ = exact_name_join(snap, full if bi_available else None)

    if alias_quality is None:
        quality = "exact"
        for country, alias in (aliases or {}).items():
            sub = df[df["Country Final"] == country]["Account Alias"]
            if classify_alias(account_label or alias, sub.dropna().astype(str).unique()) == "heuristic":
                quality = "heuristic"
    else:
        quality = alias_quality
    if multi_country is None:
        multi_country = find_multi_country(df, aliases) if aliases else set()
    ids = identity_verdicts(snap, joined, bi_available=bi_available, alias_quality=quality,
                            multi_country=multi_country, assigned_names=assigned_names,
                            current_names=current_names)

    def joined_for(window: tuple[date, date] | None) -> dict:
        if not bi_available:
            return {}
        # Пустое окно (зрелых дней ещё нет): имена те же, числа — настоящие нули окна,
        # и состояние будет `no_mature_spend`, а не «нет данных».
        part = {} if window is None else window_attribution(df, aliases, window)
        return exact_name_join(snap, rebase_bi(full, part))[0]

    spend_j, ftd_j, tail_j = joined_for(windows.spend), joined_for(windows.ftd), joined_for(windows.tail)
    numbers = {k: object_numbers(strategy, k, ids[k], spend_j.get(k), ftd_j.get(k), tail_j.get(k),
                                 target_cpa=target_cpa) for k in ids}

    fresh_bi = bi_freshness(last, bi_built_at, now=now, today=today)
    fresh_snap = snapshot_freshness(snap, level="adset", now=now)
    total_bi_spend = None
    if bi_available and windows.spend is not None:
        total_bi_spend = float(sum((m.get("spend") or 0.0)
                                   for m in window_attribution(df, aliases, windows.spend)["campaigns"].values()))
    recon = reconcile_spend(snap, windows.spend, total_bi_spend)

    global_blockers = [b for b in (fresh_bi["blocker"], fresh_snap["blocker"], recon["blocker"]) if b]
    automatic: dict[str, dict] = {}
    for key, ident in ids.items():
        level = key.split(":", 1)[0]
        reasons = list(global_blockers)
        if level == "ad" and not snapshot_freshness(snap, level="ad", now=now)["fresh"] \
                and STALE_SNAPSHOT not in reasons:
            reasons.append(STALE_SNAPSHOT)
        if not ident["automatic_ok"]:
            reasons.append(ident["blocker"] or AMBIGUOUS_MAPPING)
        if numbers[key].get("reason"):
            reasons.append(numbers[key]["reason"])
        automatic[key] = {"allowed": not reasons, "reasons": list(dict.fromkeys(reasons))}

    return {
        "strategy": strategy,
        "windows": windows.to_dict(),
        "bi_through": last.isoformat(),
        "alias_quality": quality,
        "identity": ids,
        "numbers": numbers,
        "coverage": coverage(ids, scope_keys, evidence_only=summ.get("evidence_only") or []),
        "freshness": {"bi": fresh_bi, "snapshot": fresh_snap},
        "reconciliation": recon,
        "global_blockers": global_blockers,
        "automatic": automatic,
    }
