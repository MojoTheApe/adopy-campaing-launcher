"""Вердикты по связкам креативов поверх архива статистики.

Связка = оффер × таргет × возраст × креатив × LP variant. Таргет и возраст живут
только внутри имени адсета, поэтому извлекаются парсингом (см. :func:`parse_adset`).

Четыре вещи, без которых цифры врут, и все четыре здесь сделаны:

1. **Разнос неприписанного спенда.** 31.5% расхода приходит без оффера, но несёт
   лишь 1% лидов — это сбой джойна с трекером, а не отдельный трафик. Если его не
   вернуть, ROI каждой связки завышен примерно в 1.4 раза. Возвращаем
   пропорционально по адсету; то, у чего нет приписанного близнеца (18.7%),
   остаётся сиротой и показывается как слепое пятно.
2. **Винзоризация выручки.** Одна строка даёт 25% всей выручки, три — 55%.
   Для вердиктов net режется потолком :data:`NET_CAP_PER_FTD` на депозит; сырой
   net сохраняется отдельно для P&L.
3. **Гейт зрелости.** Пока спенд < 2×CPA страны или с последней открутки прошло
   меньше :data:`MATURITY_DAYS` дней, связка получает статус ``insufficient`` и в
   бриф идёт как «в тесте», а не как факт.
4. **Метрика по уровню.** FTD есть лишь в 1.9% строк, поэтому связка судится по
   CPL; ROI и CPA поднимаются на роллапы и считаются только при
   :data:`MIN_FTD_FOR_ROI` депозитах, конверсия лид→FTD — при
   :data:`MIN_LEADS_FOR_CR` лидах.
"""
from __future__ import annotations

import datetime as dt
import re
import sqlite3
import statistics
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from .creative_archive import UNATTRIBUTED

# ── настраиваемые пороги ─────────────────────────────────────────────────────

#: Потолок выручки на один депозит при расчёте вердиктов (медиана строки — $100,
#: типичная выплата $100–200). Сырой net от этого не меняется.
NET_CAP_PER_FTD = 500.0

#: Дней с последней открутки, после которых связка считается дозревшей.
#: ВЗЯТО ОЦЕНОЧНО — пересчитать по таблице ``revision``, см. `measure_revenue_lag`.
MATURITY_DAYS = 14

#: Множитель к CPA страны: минимальный спенд, до которого вердикт не выдаётся.
SPEND_GATE_CPA_MULT = 2.0

MIN_FTD_FOR_ROI = 30
MIN_LEADS_FOR_CR = 100

#: Абсолютные границы ROI (действуют, пока в архиве < RELATIVE_AFTER_DAYS истории).
ROI_GOOD, ROI_FLOOR = 1.6, 1.0
#: Границы CPL связки — доля от базы страны (медиана CPL дозревших связок).
CPL_GOOD, CPL_BAD = 0.8, 1.2
#: Границы конверсии лид→FTD — доля от конверсии страны.
CR_GOOD, CR_BAD = 1.2, 0.8

#: С какого объёма истории по стране переходим на относительные границы.
RELATIVE_AFTER_DAYS = 90

GOOD, WATCH, BAD, INSUFFICIENT = "✅", "⚠️", "❌", "…"

_DEVICE_TOKENS = {"ios", "android", "all", "mobile", "desktop"}
_AGE_RE = re.compile(r"^(\d{2})\s*\+?$")
_AGE_GLUED_RE = re.compile(r"^(\d{2})\s*\+?\s*\d{1,2}[.,]\d{1,2}")


# ── парсинг имени адсета ─────────────────────────────────────────────────────


def parse_adset(name: str) -> tuple[str | None, str | None]:
    """Достать ``(таргет, возраст)`` из имени адсета.

    Формат плавает (``CO_BNC_all_FX_35_29.07`` против
    ``CO_SX_All_VID_GMBL|TradingYB_35_30.7``), поэтому читаем **справа**: дата,
    перед ней возраст, перед ним таргет. На реальных данных это извлекает 98.9%
    имён (по Колумбии 99.2%).

    Возраст нормализуется отбрасыванием ``+``: ``35`` и ``35+`` в этих названиях
    означают одно и то же, а разведение их пополам режет и без того скудную
    статистику.
    """
    if not name or name == "-":
        return None, None
    parts = [p for p in name.split("_") if p]
    age_i = None
    age_val = None
    for i in range(len(parts) - 1, -1, -1):
        seg = parts[i].strip()
        m = _AGE_RE.match(seg)
        if m:
            age_i, age_val = i, m.group(1)
            break
        m = _AGE_GLUED_RE.match(seg)  # «30+24.03» — возраст слипся с датой
        if m:
            age_i, age_val = i, m.group(1)
            break
    if age_i is None or age_i == 0:
        return None, None
    return _normalise_target(parts[age_i - 1]), age_val


def _normalise_target(raw: str) -> str | None:
    """Привести таргет к канону: нижний регистр, без device-хвоста."""
    s = (raw or "").strip()
    if not s:
        return None
    kept = [
        chunk
        for chunk in s.split("/")
        if chunk.strip() and chunk.strip().lower() not in _DEVICE_TOKENS
    ]
    s = "/".join(kept) if kept else s
    return s.strip().lower() or None


# ── модель ───────────────────────────────────────────────────────────────────


@dataclass
class Bundle:
    """Связка: оффер × таргет × возраст × креатив × LP variant."""

    offer: str
    target: str | None
    age: str | None
    creative: str
    variant: str

    leads: int = 0
    ftds: int = 0
    spend: float = 0.0          # уже с разнесённым неприписанным спендом
    spend_reported: float = 0.0  # как в экспорте, до разноса
    net_raw: float = 0.0
    net_capped: float = 0.0
    last_spend_date: str | None = None
    adsets: set[str] = field(default_factory=set)

    @property
    def cpl(self) -> float | None:
        return self.spend / self.leads if self.leads else None

    @property
    def cpa(self) -> float | None:
        return self.spend / self.ftds if self.ftds else None

    @property
    def roi(self) -> float | None:
        return self.net_capped / self.spend if self.spend else None

    @property
    def roi_raw(self) -> float | None:
        return self.net_raw / self.spend if self.spend else None

    @property
    def has_outlier(self) -> bool:
        """В связке есть кит: сырой net заметно выше урезанного."""
        return self.net_raw - self.net_capped > 1.0

    def key(self) -> tuple:
        return (self.offer, self.target, self.age, self.creative, self.variant)


@dataclass
class CountryStats:
    country: str
    spend: float = 0.0
    leads: int = 0
    ftds: int = 0
    net_raw: float = 0.0
    net_capped: float = 0.0
    orphan_spend: float = 0.0
    redistributed_spend: float = 0.0
    date_min: str | None = None
    date_max: str | None = None
    unparsed_adsets: int = 0
    total_adsets: int = 0
    coarse_dupes: int = 0
    coarse_dupe_spend: float = 0.0

    @property
    def cpa(self) -> float | None:
        return self.spend / self.ftds if self.ftds else None

    @property
    def cpl(self) -> float | None:
        return self.spend / self.leads if self.leads else None

    @property
    def roi(self) -> float | None:
        return self.net_capped / self.spend if self.spend else None

    @property
    def cr(self) -> float | None:
        return self.ftds / self.leads if self.leads else None

    @property
    def history_days(self) -> int:
        if not self.date_min or not self.date_max:
            return 0
        return (dt.date.fromisoformat(self.date_max) - dt.date.fromisoformat(self.date_min)).days


@dataclass
class Verdict:
    mark: str
    metric: str
    value: float | None
    basis: str
    rule: str


# ── сборка связок ────────────────────────────────────────────────────────────


def _cap_net(net: float, ftds: int) -> float:
    """Урезать выброс. Возвраты не трогаем — они реальны и завышать ROI нельзя."""
    if net <= 0:
        return net
    return min(net, NET_CAP_PER_FTD * max(ftds, 1))


def _drop_coarse_duplicates(rows: Sequence) -> tuple[list, int, float]:
    """Убрать строки грубой детализации, у которых есть детальный близнец.

    Колонка ``Variant`` присутствует не во всех выгрузках. Одна и та же открутка
    приходит то с заполненным variant, то с пустым, получает два разных ключа
    ``fact`` и учитывается дважды — на июле 2026 это давало +18% к спенду
    (3 652 пары, суммы сторон совпадали с точностью 0.2%).

    Архив хранит обе записи намеренно: он — честный лог того, что пришло. Грань
    разрешается здесь. Строка с пустым variant отбрасывается, только если ровно
    та же (аккаунт, адсет, объявление, CM, оффер, дата) встречается с непустым
    variant; одиночный пустой variant — это законный сбой джойна трекера, его
    трогать нельзя.
    """
    def coarse_key(r) -> tuple:
        return (r["account"], r["cm"], r["adset"], r["ad"], r["asset"], r["date"])

    fine = {coarse_key(r) for r in rows if r["variant"] != UNATTRIBUTED}
    kept, dropped, dropped_spend = [], 0, 0.0
    for r in rows:
        if r["variant"] == UNATTRIBUTED and coarse_key(r) in fine:
            dropped += 1
            dropped_spend += r["spend"]
            continue
        kept.append(r)
    return kept, dropped, dropped_spend


def load_bundles(
    conn: sqlite3.Connection, country: str
) -> tuple[dict[tuple, Bundle], CountryStats]:
    """Собрать связки страны с разнесённым неприписанным спендом."""
    raw_rows = conn.execute(
        "SELECT account, cm, adset, ad, asset, variant, date, leads, ftds, spend, net"
        " FROM fact WHERE country = ?",
        (country,),
    ).fetchall()

    stats = CountryStats(country=country)
    if not raw_rows:
        return {}, stats

    rows, stats.coarse_dupes, stats.coarse_dupe_spend = _drop_coarse_duplicates(raw_rows)

    # 1. Неприписанный спенд и приписанный вес — по адсетам.
    unattributed: dict[str, float] = {}
    attributed_spend: dict[str, float] = {}
    attributed_leads: dict[str, int] = {}
    for r in rows:
        adset = r["adset"]
        if r["asset"] == UNATTRIBUTED:
            unattributed[adset] = unattributed.get(adset, 0.0) + r["spend"]
        else:
            attributed_spend[adset] = attributed_spend.get(adset, 0.0) + r["spend"]
            attributed_leads[adset] = attributed_leads.get(adset, 0) + r["leads"]

    for adset, extra in unattributed.items():
        if attributed_spend.get(adset, 0.0) > 0 or attributed_leads.get(adset, 0) > 0:
            stats.redistributed_spend += extra
        else:
            stats.orphan_spend += extra

    # 2. Связки.
    bundles: dict[tuple, Bundle] = {}
    adsets_seen: set[str] = set()
    adsets_unparsed: set[str] = set()
    for r in rows:
        stats.spend += r["spend"]
        stats.leads += r["leads"]
        stats.ftds += r["ftds"]
        stats.net_raw += r["net"]
        stats.net_capped += _cap_net(r["net"], r["ftds"])
        if stats.date_min is None or r["date"] < stats.date_min:
            stats.date_min = r["date"]
        if stats.date_max is None or r["date"] > stats.date_max:
            stats.date_max = r["date"]

        if r["asset"] == UNATTRIBUTED:
            continue  # спенд вернётся через разнос, метрик тут нет

        adset = r["adset"]
        adsets_seen.add(adset)
        target, age = parse_adset(adset)
        if target is None:
            adsets_unparsed.add(adset)

        # Доля адсетного неприписанного спенда, приходящаяся на эту строку.
        extra = 0.0
        pool = unattributed.get(adset, 0.0)
        if pool:
            base = attributed_spend.get(adset, 0.0)
            if base > 0:
                extra = pool * (r["spend"] / base)
            elif attributed_leads.get(adset, 0) > 0:
                extra = pool * (r["leads"] / attributed_leads[adset])

        key = (r["asset"], target, age, r["ad"], r["variant"])
        b = bundles.get(key)
        if b is None:
            b = Bundle(
                offer=r["asset"], target=target, age=age, creative=r["ad"],
                variant=r["variant"],
            )
            bundles[key] = b
        b.leads += r["leads"]
        b.ftds += r["ftds"]
        b.spend_reported += r["spend"]
        b.spend += r["spend"] + extra
        b.net_raw += r["net"]
        b.net_capped += _cap_net(r["net"], r["ftds"])
        b.adsets.add(adset)
        if r["spend"] > 0 and (b.last_spend_date is None or r["date"] > b.last_spend_date):
            b.last_spend_date = r["date"]

    stats.total_adsets = len(adsets_seen)
    stats.unparsed_adsets = len(adsets_unparsed)
    return bundles, stats


# ── зрелость и вердикты ──────────────────────────────────────────────────────


def spend_gate(stats: CountryStats) -> float:
    cpa = stats.cpa
    return SPEND_GATE_CPA_MULT * cpa if cpa else 0.0


def is_mature(b: Bundle, stats: CountryStats, as_of: str) -> bool:
    """Дозрела ли связка: хватает спенда И прошло достаточно дней."""
    if b.spend < spend_gate(stats):
        return False
    if not b.last_spend_date:
        return False
    days = (dt.date.fromisoformat(as_of) - dt.date.fromisoformat(b.last_spend_date)).days
    return days >= MATURITY_DAYS


def cpl_baseline(bundles: Iterable[Bundle], stats: CountryStats, as_of: str) -> float | None:
    """База CPL страны — медиана по дозревшим связкам с лидами."""
    vals = [
        b.cpl
        for b in bundles
        if b.leads > 0 and b.cpl is not None and is_mature(b, stats, as_of)
    ]
    return statistics.median(vals) if vals else stats.cpl


def judge_bundle(
    b: Bundle, stats: CountryStats, baseline: float | None, as_of: str
) -> Verdict:
    """Вердикт связке — по CPL, единственной плотной метрике на этом уровне."""
    if not is_mature(b, stats, as_of):
        gate = spend_gate(stats)
        if b.spend < gate:
            why = f"спенд ${b.spend:,.0f} < ${gate:,.0f}"
        else:
            why = f"с последней открутки < {MATURITY_DAYS} дн."
        return Verdict(INSUFFICIENT, "CPL", b.cpl, why, "гейт зрелости")
    if not b.leads or b.cpl is None or not baseline:
        return Verdict(INSUFFICIENT, "CPL", None, "нет лидов", "гейт зрелости")

    ratio = b.cpl / baseline
    if ratio <= CPL_GOOD:
        mark = GOOD
    elif ratio <= CPL_BAD:
        mark = WATCH
    else:
        mark = BAD
    return Verdict(
        mark, "CPL", b.cpl,
        f"{ratio:.2f}× базы (${baseline:,.2f}), лидов {b.leads}",
        f"CPL vs база страны ({CPL_GOOD}/{CPL_BAD})",
    )


def judge_rollup(
    label: str, group: Sequence[Bundle], stats: CountryStats
) -> tuple[str, Verdict | None, Verdict | None]:
    """Вердикт срезу: ROI при ≥30 FTD, конверсия лид→FTD при ≥100 лидов."""
    spend = sum(b.spend for b in group)
    ftds = sum(b.ftds for b in group)
    leads = sum(b.leads for b in group)
    net = sum(b.net_capped for b in group)

    roi_v = None
    if ftds >= MIN_FTD_FOR_ROI and spend > 0:
        roi = net / spend
        mark = GOOD if roi >= ROI_GOOD else (WATCH if roi >= ROI_FLOOR else BAD)
        roi_v = Verdict(
            mark, "ROI", roi, f"спенд ${spend:,.0f}, депозитов {ftds}",
            f"ROI абс. ({ROI_GOOD}/{ROI_FLOOR})",
        )

    cr_v = None
    country_cr = stats.cr
    if leads >= MIN_LEADS_FOR_CR and country_cr:
        cr = ftds / leads
        ratio = cr / country_cr
        mark = GOOD if ratio >= CR_GOOD else (WATCH if ratio >= CR_BAD else BAD)
        cr_v = Verdict(
            mark, "лид→FTD", cr,
            f"{ratio:.2f}× страны ({country_cr * 100:.2f}%), лидов {leads}",
            f"CR vs страна ({CR_GOOD}/{CR_BAD})",
        )
    return label, roi_v, cr_v


def rollup(bundles: Iterable[Bundle], dimension: str) -> dict[str, list[Bundle]]:
    """Сгруппировать связки по оси: ``offer`` / ``target`` / ``age`` /
    ``creative`` / ``variant``."""
    out: dict[str, list[Bundle]] = {}
    for b in bundles:
        val = getattr(b, dimension)
        if val in (None, "", "-"):
            continue
        out.setdefault(str(val), []).append(b)
    return out


# ── калибровка лага выручки ──────────────────────────────────────────────────


#: Насколько свежей должна быть строка при первом наблюдении, чтобы её лаг
#: вообще что-то значил. Строку, впервые увиденную через полгода после открутки
#: (так выглядит бэкфилл старых выгрузок), измерять бессмысленно: её «лаг» —
#: это возраст выгрузки, а не скорость прихода денег.
LAG_FRESH_WITHIN_DAYS = 3


def measure_revenue_lag(
    conn: sqlite3.Connection,
    country: str | None = None,
    *,
    fresh_within: int = LAG_FRESH_WITHIN_DAYS,
) -> dict:
    """Померить, через сколько дней после открутки выручка перестаёт меняться.

    Этим заменяется взятое оценочно :data:`MATURITY_DAYS`. Для каждого ключа
    ``fact`` смотрим всю историю его наблюдений в ``revision`` и берём последнее,
    в котором ``net`` ещё менялся.

    Учитываются только строки, впервые увиденные не позже чем через
    ``fresh_within`` дней после своей даты — иначе бэкфилл, где старая строка
    впервые попадает в архив спустя месяцы, даёт лаг в сотни дней и метрика
    превращается в мусор.
    """
    where = "WHERE country = ?" if country else ""
    args = (country,) if country else ()
    rows = conn.execute(
        f"SELECT country, account, adset, ad, cm, asset, variant, date,"
        f" observed_at, net FROM revision {where}"
        " ORDER BY country, account, adset, ad, cm, asset, variant, date, observed_at",
        args,
    ).fetchall()

    lags: list[int] = []
    skipped_stale = 0
    eligible = 0
    cur_key: tuple | None = None
    obs: list[tuple[str, float]] = []

    def flush(key: tuple | None, series: list[tuple[str, float]]) -> None:
        nonlocal skipped_stale, eligible
        if not key or not series:
            return
        row_date = key[-1]
        try:
            d0 = dt.date.fromisoformat(row_date)
            first_seen = dt.date.fromisoformat(series[0][0])
        except ValueError:
            return
        if (first_seen - d0).days > fresh_within:
            skipped_stale += 1
            return
        eligible += 1

        # Считаем лаг только по строкам, где выручка реально двигалась после
        # первого наблюдения. Строка, записанная один раз и больше не менявшаяся,
        # ничего не говорит о скорости прихода денег — её «лаг» это просто
        # задержка выгрузки, и включать её значит занижать медиану.
        last_change_at = None
        prev = series[0][1]
        for observed_at, net in series[1:]:
            if abs(net - prev) >= 0.005:
                last_change_at = observed_at
            prev = net
        if last_change_at is None:
            return
        try:
            lag = (dt.date.fromisoformat(last_change_at) - d0).days
        except ValueError:
            return
        if lag >= 0:
            lags.append(lag)

    for r in rows:
        key = (
            r["country"], r["account"], r["adset"], r["ad"], r["cm"],
            r["asset"], r["variant"], r["date"],
        )
        if key != cur_key:
            flush(cur_key, obs)
            cur_key, obs = key, []
        obs.append((r["observed_at"], r["net"]))
    flush(cur_key, obs)

    base = {
        "eligible": eligible,
        "skipped_stale": skipped_stale,
        "revised": len(lags),
        "revised_share": (len(lags) / eligible) if eligible else 0.0,
    }
    if not lags:
        return {**base, "samples": 0}
    lags.sort()

    def pct(p: float) -> int:
        return lags[min(len(lags) - 1, int(len(lags) * p))]

    return {
        **base,
        "samples": len(lags),
        "median": statistics.median(lags),
        "p75": pct(0.75),
        "p90": pct(0.90),
        "p95": pct(0.95),
        "p99": pct(0.99),
        "max": lags[-1],
    }
