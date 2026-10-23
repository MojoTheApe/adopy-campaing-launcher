"""Сравнение двух периодов на дешборде (ACL-110).

Страница по-прежнему отвечает за один выбранный период. Если включено
сравнение, рядом с каждым числом карты аккаунтов, обзора стран (CM Comparison)
и рекомендаций появляется значение «раньше» и изменение.

Решения владельца (2026-10-20):
  * режимов два: предыдущий период той же длины и свой диапазон;
  * изменение — процент относительно прошлого значения; если прошлое значение
    ноль или его нет — прочерк «—», а не процент;
  * строка, которая есть только в одном периоде, помечается «new» (есть сейчас,
    раньше не было) или «gone» (была раньше, сейчас нет).

Модуль чистый: ничего не знает про FastAPI, книгу и шаблоны. Отчёты за оба
периода собирает `app.py` тем же кодом, что и обычную страницу, — вторая
формула для того же числа рано или поздно разошлась бы с первой.
"""
from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Any, Iterable, Optional

from src.analysis_agent import metric_columns as mc

MODE_PREV = "prev"
MODE_CUSTOM = "custom"
DASH = mc.DASH

NEW = "new"
GONE = "gone"
BOTH = "both"

# Метрики, где меньше — лучше: рост CPL/CPA красится как ухудшение.
_LOWER_IS_BETTER = {"cpl", "cpa"}
_METRIC_KEYS = ("spend", "leads", "ftds", "cpl", "cpa", "cr")


def parse_day(text: Any) -> Optional[date]:
    try:
        return date.fromisoformat(str(text or "").strip())
    except ValueError:
        return None


def previous_period(date_from: str, date_to: str) -> Optional[tuple[str, str]]:
    """Предыдущий период той же длины, вплотную перед выбранным.

    7–13 октября (7 дней) → 30 сентября – 6 октября. Без обеих границ длины
    нет, и сравнивать не с чем.
    """
    start, end = parse_day(date_from), parse_day(date_to or date_from)
    if start is None or end is None or end < start:
        return None
    length = (end - start).days + 1
    prev_end = start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=length - 1)
    return prev_start.isoformat(), prev_end.isoformat()


def resolve_compare(
    mode: str, cmp_from: str, cmp_to: str, date_from: str, date_to: str, span: str = ""
) -> tuple[Optional[dict], str]:
    """(описание сравнения | None, пояснение для человека).

    Пояснение непустое, когда сравнение просили, а выдать его нельзя: страница
    должна сказать почему, а не молча показать обычные числа.
    """
    mode = (mode or "").strip().lower()
    if not mode:
        return None, ""
    if mode not in (MODE_PREV, MODE_CUSTOM):
        return None, f"Unknown comparison mode “{mode[:20]}”."
    if span == "all" or not (date_from or date_to):
        return None, "Pick a date range first: “All dates” has nothing to compare with."
    if mode == MODE_PREV:
        prev = previous_period(date_from, date_to)
        if prev is None:
            return None, "The selected dates are not a valid range, so there is no earlier period."
        return {"mode": MODE_PREV, "date_from": prev[0], "date_to": prev[1]}, ""
    start = parse_day(cmp_from)
    end = parse_day(cmp_to or cmp_from)
    if start is None or end is None:
        return None, "The comparison range needs a valid start date."
    if end < start:
        return None, "The comparison range ends before it starts."
    return {"mode": MODE_CUSTOM, "date_from": start.isoformat(), "date_to": end.isoformat()}, ""


def _num(value: Any) -> Optional[float]:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def delta_text(cur: Any, prev: Any) -> tuple[str, str]:
    """(текст, направление) изменения относительно прошлого значения.

    Прошлое значение ноль или его нет — «—»: процент от нуля не определён, и
    «+∞%» ничего не говорит. Направление: up / down / flat / "" (нет изменения
    для показа).
    """
    c, p = _num(cur), _num(prev)
    if c is None or p is None or p == 0:
        return DASH, ""
    pct = (c - p) / abs(p) * 100.0
    rounded = round(pct)
    if rounded == 0:
        return "0%", "flat"
    sign = "+" if rounded > 0 else "−"
    return f"{sign}{abs(rounded)}%", "up" if rounded > 0 else "down"


def _tone(key: str, direction: str) -> str:
    """good / bad / '' — для spend цвета нет: больше денег — не хорошо и не плохо."""
    if key == "spend" or direction in ("", "flat"):
        return ""
    better = (direction == "down") if key in _LOWER_IS_BETTER else (direction == "up")
    return "good" if better else "bad"


def _values(spend: Any, leads: Any, ftds: Any) -> dict[str, Optional[float]]:
    r = mc.ratios(spend, leads, ftds)
    s, l, f = _num(spend), _num(leads), _num(ftds)
    return {
        "spend": s, "leads": l, "ftds": f,
        "cpl": r["cpl"] if s is not None and l is not None else None,
        "cpa": r["cpa"] if s is not None and f is not None else None,
        "cr": r["cr"] if l is not None and f is not None else None,
    }


def metric_compare(cur: Optional[dict], prev: Optional[dict]) -> list[dict]:
    """Шесть ячеек «раньше + изменение» в порядке METRIC_HEADERS.

    `cur` / `prev` — словари со spend, leads, deposits либо None, если строки в
    этом периоде нет. Нет строки — прошлое значение «—» и изменение «—».
    """
    formats = (mc.fmt_money, mc.fmt_count, mc.fmt_count, mc.fmt_money, mc.fmt_money, mc.fmt_cr)
    if prev is None:
        return [{"prev": DASH, "delta": DASH, "dir": "", "tone": ""} for _ in _METRIC_KEYS]
    pv = _values(prev.get("spend"), prev.get("leads"), prev.get("deposits"))
    cv = _values(cur.get("spend"), cur.get("leads"), cur.get("deposits")) if cur is not None else None
    out = []
    for key, fmt in zip(_METRIC_KEYS, formats):
        text, direction = delta_text(cv[key] if cv else None, pv[key])
        out.append({
            "prev": fmt(pv[key]), "delta": text, "dir": direction, "tone": _tone(key, direction),
        })
    return out


def value_compare(cur: Any, prev: Any, kind: str = "money") -> dict:
    """A supplementary report number uses the same zero/missing delta rule."""
    p = _num(prev)
    text, direction = delta_text(cur, p)
    formatted = (mc.fmt_money(p) if kind == "money" else mc.fmt_count(p) if kind == "count" else
                 (f"{p:.2f}" if p is not None else DASH))
    return {"prev": formatted, "delta": text, "dir": direction, "tone": ""}


def row_state(cur_present: bool, prev_present: bool) -> str:
    """new — есть сейчас, раньше не было; gone — было, сейчас нет; иначе both/''."""
    if cur_present and prev_present:
        return BOTH
    if cur_present:
        return NEW
    if prev_present:
        return GONE
    return ""


def _present(row: Optional[dict]) -> bool:
    """A recorded zero is data; an explicit no-data account is not."""
    if not row:
        return False
    if "has_data" in row:
        return bool(row.get("has_data"))
    return any(_num(row.get(k)) is not None for k in ("spend", "leads", "deposits"))


def _norm(name: Any) -> str:
    return " ".join(str(name or "").split()).casefold()


class PeriodCompare:
    """Всё, что шаблонам нужно знать о прошлом периоде.

    Строится один раз на запрос из отчёта и карты аккаунтов за сравниваемый
    период. Методы отвечают на вопрос «что было с этой строкой раньше»: словарь
    с метриками либо None, если строки тогда не было.
    """

    def __init__(self, info: dict, prev_report: dict, prev_amap: Optional[dict] = None):
        self.mode = info["mode"]
        self.date_from = info["date_from"]
        self.date_to = info["date_to"]
        self._report = prev_report or {}
        self._countries = {
            _norm(c.get("country")): c for c in (prev_report or {}).get("countries", [])
        }
        self._amap = {}
        self._book = {}
        self._cm_groups = {}
        for mcr in (prev_amap or {}).get("countries", []):
            key = _norm(mcr.get("country"))
            self._book[key] = mcr.get("book") or {}
            self._amap[key] = {_norm(r.get("account")): r for r in mcr.get("rows") or []}
        self._prev_totals = (prev_amap or {}).get("totals")
        for g in (prev_amap or {}).get("cm_section", []):
            self._cm_groups[_norm(g.get("country"))] = g
        # A retired map assignment is not restored. Keep workbook evidence only,
        # so a missing account can be displayed separately without any controls.
        for key, country in self._countries.items():
            self._book.setdefault(key, country.get("my_totals") or {})
            rows = self._amap.setdefault(key, {})
            for account in country.get("account_rows") or []:
                label = account.get("account_label") or account.get("alias")
                excel = account.get("account_excel") or {}
                if label and _present(excel):
                    rows.setdefault(_norm(label), {**excel, "account": label})
        self.empty = not any(
            _present(r) for rows in self._amap.values() for r in rows.values()
        ) and not any(self._book.values()) and not any(
            _present(r) for c in self._countries.values() for r in c.get("cm_comparison") or [])

    @property
    def label(self) -> str:
        if self.date_from == self.date_to:
            return self.date_from
        return f"{self.date_from} → {self.date_to}"

    # --- простые поисковики -------------------------------------------------
    def country_book(self, country: str) -> Optional[dict]:
        book = self._book.get(_norm(country))
        return book if _present(book) else None

    def totals(self) -> Optional[dict]:
        return self._prev_totals if _present(self._prev_totals) else None

    def account(self, country: str, account: str) -> Optional[dict]:
        row = self._amap.get(_norm(country), {}).get(_norm(account))
        return row if _present(row) else None

    def cm_group(self, country: str) -> Optional[dict]:
        g = self._cm_groups.get(_norm(country))
        return g if _present(g) else None

    def cm(self, country: str, cm: str) -> Optional[dict]:
        c = self._countries.get(_norm(country)) or {}
        for r in c.get("cm_comparison") or []:
            if _norm(r.get("cm") or "(unknown)") == _norm(cm or "(unknown)"):
                return r if _present(r) else None
        return None

    def cm_gone(self, country: str, current: Iterable[str]) -> list[dict]:
        have = {_norm(x or "(unknown)") for x in current}
        c = self._countries.get(_norm(country)) or {}
        return [
            r for r in c.get("cm_comparison") or []
            if _norm(r.get("cm") or "(unknown)") not in have and _present(r)
        ]

    def adset(self, country: str, account_label: str, adset: str) -> Optional[dict]:
        for r in self._close(country, account_label):
            if _norm(r.get("adset_name")) == _norm(adset):
                return r if _present(r) else None
        return None

    def adset_gone(self, country: str, account_label: str, current: Iterable[str]) -> list[dict]:
        have = {_norm(x) for x in current}
        return [
            r for r in self._close(country, account_label)
            if _norm(r.get("adset_name")) not in have and _present(r)
        ]

    def _close(self, country: str, account_label: str) -> list[dict]:
        c = self._countries.get(_norm(country)) or {}
        return list((c.get("close_recs") or {}).get(account_label) or [])

    # --- рекомендации на открытие ---------------------------------------
    @staticmethod
    def _rec_key(rec: dict) -> tuple:
        return (rec.get("strategy_type"), _norm(rec.get("targeting")))

    def open_recs_diff(self, country: str, account_label: str, current: list[dict]) -> dict:
        """Какие рекомендации на открытие появились и исчезли за период.

        Рекомендация — это пара «стратегия + таргетинг». Названия кампаний в
        них зависят от даты и сравнивать по ним нельзя.
        """
        c = self._countries.get(_norm(country)) or {}
        before = list((c.get("open_recs") or {}).get(account_label) or [])
        now_keys = {self._rec_key(r) for r in current or []}
        before_keys = {self._rec_key(r) for r in before}
        return {
            "now": len(current or []),
            "before": len(before),
            "new": sorted(
                {str(r.get("targeting") or "?") for r in current or []
                 if self._rec_key(r) not in before_keys}),
            "gone": sorted(
                {str(r.get("targeting") or "?") for r in before
                 if self._rec_key(r) not in now_keys}),
        }

    def countries_gone(self, current: Iterable[dict]) -> list[dict]:
        have = {_norm(c.get("country")) for c in current}
        return [dict(c, book=self.country_book(c.get("country")))
                for key, c in self._countries.items() if key not in have]

    def accounts_gone(self, country: str, current: Iterable[dict]) -> list[dict]:
        current = list(current)
        have = {_norm(r.get("account") or r.get("account_label")) for r in current}
        have.update(_norm(r.get("workbook_alias")) for r in current if r.get("workbook_alias"))
        return [r for key, r in self._amap.get(_norm(country), {}).items()
                if key not in have and _present(r)]

    def home_cm_groups(self, current: Iterable[dict]) -> list[dict]:
        groups = []
        have = set()
        for group in current:
            country = group.get("country", "")
            have.add(_norm(country))
            rows = list(group.get("rows") or [])
            gone = self.cm_gone(country, [r.get("cm") for r in rows])
            groups.append({**group, "rows": rows + [dict(r, comparison_only=True) for r in gone]})
        for key, group in self._cm_groups.items():
            if key not in have:
                groups.append({**group, "comparison_only": True,
                               "rows": [dict(r, comparison_only=True) for r in group.get("rows") or []]})
        return groups

    def close_accounts(self, country: str, current: dict) -> list[str]:
        before = (self._countries.get(_norm(country)) or {}).get("close_recs") or {}
        return sorted(label for label in set(current) | set(before)
                      if current.get(label) or any(_present(r) for r in before.get(label) or []))

    def open_accounts_gone(self, country: str, current: dict) -> list[dict]:
        before = (self._countries.get(_norm(country)) or {}).get("open_recs") or {}
        return [{"account": label, "diff": self.open_recs_diff(country, label, [])}
                for label in sorted(before) if label not in current and before[label]]

    @staticmethod
    def _section_rows(country: dict, section: str) -> list[dict]:
        if section == "trend":
            return [r for key in ("trending_up", "stable", "trending_down")
                    for r in (country.get("trend") or {}).get(key) or []]
        return list(country.get(section) or [])

    @staticmethod
    def _row_key(section: str, row: dict) -> tuple:
        keys = {"asset_summary": ("offer",), "variants": ("variant",),
                "asset_variants": ("offer", "variant"),
                "top_combos": ("offer", "device", "interest", "age", "creative"),
                "targeting": ("interest",), "creatives": ("creative_id", "text_name"),
                "trend": ("offer", "device", "interest", "creative")}[section]
        # Preserve normalized aggregate identity (including text segment) where
        # the report supplies it; category/rank/date are deliberately not keys.
        aggregate = row.get("aggregate_key") or {}
        if section in ("top_combos", "targeting") and aggregate:
            return tuple((k, _norm(v)) for k, v in sorted(aggregate.items())
                         if k not in ("section", "country"))
        return tuple(_norm(row.get(k)) for k in keys)

    def breakdown(self, country: str, section: str, row: dict) -> Optional[dict]:
        before = self._section_rows(self._countries.get(_norm(country)) or {}, section)
        key = self._row_key(section, row)
        return next((r for r in before if self._row_key(section, r) == key), None)

    def breakdown_gone(self, country: str, section: str, current: Iterable[dict]) -> list[dict]:
        have = {self._row_key(section, r) for r in current}
        before = self._section_rows(self._countries.get(_norm(country)) or {}, section)
        return [r for r in before if self._row_key(section, r) not in have]

    @staticmethod
    def row_label(section: str, row: dict) -> str:
        keys = {"asset_summary": ("offer",), "variants": ("variant",),
                "asset_variants": ("offer", "variant"),
                "top_combos": ("offer", "device", "interest", "age", "creative"),
                "targeting": ("interest",), "creatives": ("creative_id", "text_name"),
                "trend": ("offer", "device", "interest", "creative")}[section]
        return " · ".join(str(row.get(k) or "—") for k in keys)

    def earlier_only_tables(self, country: dict, *, all_rows: bool = False) -> list[dict]:
        out = []
        for section, title in (("asset_summary", "Assets"), ("variants", "Landings"),
                               ("asset_variants", "Asset landing breakdown"),
                               ("top_combos", "Top combos"), ("targeting", "Targeting"),
                               ("creatives", "Creative breakdown"), ("trend", "Recent trend")):
            current = [] if all_rows else self._section_rows(country, section)
            rows = self.breakdown_gone(country["country"], section, current)
            if rows:
                out.append({"title": title, "section": section,
                            "rows": [{**r, "label": self.row_label(section, r)} for r in rows]})
        current_creatives = {self._row_key("creatives", r): r for r in country.get("creatives") or []}
        before = self._countries.get(_norm(country["country"])) or {}
        for prior in before.get("creatives") or []:
            row = {} if all_rows else current_creatives.get(self._row_key("creatives", prior)) or {}
            have = {_norm(v.get("variant")) for v in row.get("variants") or []}
            gone = [v for v in prior.get("variants") or [] if _norm(v.get("variant")) not in have]
            if gone:
                out.append({"title": self.row_label("creatives", prior) + " variants", "section": "creative_variants",
                            "rows": [{**v, "label": v.get("label") or v.get("variant") or "No variant"} for v in gone]})
        out.extend(self.earlier_workbench_tables(country, all_rows=all_rows))
        return out

    def earlier_workbench_tables(self, country: dict, *, all_rows: bool = False) -> list[dict]:
        if country.get("workbench") and not all_rows:
            return []
        before = (self._countries.get(_norm(country["country"])) or {}).get("workbench") or {}
        tables = []
        for asset, sl in (before.get("slices") or {}).items():
            for tab in ("campaigns", "setups", "landings", "targets", "creatives", "buyers"):
                rows = self._workbench_gone_rows(sl.get(tab) or [], tab)
                if rows:
                    tables.append({"title": (asset or "All assets") + " · " + tab,
                                   "section": "workbench", "rows": [dict(r, deposits=r.get("ftds"),
                                    label=(r.get("raw") if tab == "creatives" else None)
                                    or " · ".join(str(x) for x in self._workbench_key(tab, r)) or "—") for r in rows]})
        return tables

    def creative_variant(self, country: str, creative: dict, variant: dict) -> Optional[dict]:
        before = self.breakdown(country, "creatives", creative) or {}
        return next((r for r in before.get("variants") or []
                     if _norm(r.get("variant")) == _norm(variant.get("variant"))), None)

    def asset_variants(self, country: str, current: list[dict]) -> list[dict]:
        before = (self._countries.get(_norm(country)) or {}).get("asset_variants") or []
        key = lambda r: (_norm(r.get("offer")), _norm(r.get("variant")))
        old = {key(r): r for r in before}
        out = []
        for row in current:
            prior = old.pop(key(row), None)
            out.append({**row, "_period": metric_compare(row, prior),
                        "_period_state": BOTH if prior is not None else NEW})
        out.extend({**r, "_period": metric_compare(None, r), "_period_state": GONE} for r in old.values())
        for row in out:
            def total(rows):
                matching = [r for r in rows if _norm(r.get("offer")) == _norm(row.get("offer"))]
                return {k: sum(_num(r.get(k)) or 0 for r in matching) for k in ("spend", "leads", "deposits")} if matching else None
            row["_period_total"] = metric_compare(total(current), total(before))
        return out

    def trend_metrics(self, country: str, row: dict, suffix: str) -> Optional[dict]:
        before = self.breakdown(country, "trend", row)
        return ({k: before.get(k + suffix) for k in ("spend", "leads", "deposits")}
                if before is not None else None)

    def home_tiles(self, current: dict) -> dict:
        from src.analysis_agent.country_workbench import home_you_vs_others, home_kpi_tiles

        now = home_you_vs_others(current.get("countries", []), current.get("my_cm", ""))
        before = home_you_vs_others(self._report.get("countries", []), current.get("my_cm", ""))
        before_tiles = {t["key"]: t for t in home_kpi_tiles(before)}
        out = {}
        for key in ("ftds", "leads", "spend", "cpl", "cpa", "cr"):
            out[key] = {}
            for side in ("you", "others"):
                n = now[side].get(key) if now["countries_used"] else None
                p = before[side].get(key) if before["countries_used"] else None
                text, direction = delta_text(n, p)
                out[key][side] = {"prev": before_tiles[key][side] if p is not None else DASH,
                                  "delta": text, "dir": direction, "tone": _tone(key, direction)}
        return out

    @staticmethod
    def _workbench_key(tab: str, row: dict) -> tuple:
        keys = {"campaigns": ("account", "name"), "setups": ("device", "age", "target", "pack", "landing"),
                "landings": ("landing",), "targets": ("target",), "creatives": ("pack",),
                "buyers": ("cm",), "versions": ("key",)}[tab]
        return tuple(_norm(row.get(k)) for k in keys)

    @staticmethod
    def _workbench_metrics(row: Optional[dict]) -> Optional[dict]:
        return ({"spend": row.get("spend"), "leads": row.get("leads"), "deposits": row.get("ftds")}
                if row is not None else None)

    @staticmethod
    def _had_you(row: Optional[dict]) -> bool:
        return (_num(((row or {}).get("you") or {}).get("spend")) or 0) > 0

    @classmethod
    def _workbench_gone_rows(cls, rows: Iterable[dict], tab: str) -> list[dict]:
        out = []
        for row in rows:
            out.append(dict(row, _period=metric_compare(None, cls._workbench_metrics(row)),
                            _period_you=metric_compare(None, cls._workbench_metrics(row.get("you"))),
                            _period_you_visible=cls._had_you(row)))
            if tab == "creatives":
                for v in row.get("versions") or []:
                    out.append(dict(v, name=v.get("raw") or v.get("version") or "No version in title",
                                    _period=metric_compare(None, cls._workbench_metrics(v)),
                                    _period_you=metric_compare(None, cls._workbench_metrics(v.get("you"))),
                                    _period_you_visible=cls._had_you(v)))
        return out

    def asset_counts(self, country: str, current: dict) -> dict:
        before = (self._countries.get(_norm(country)) or {}).get("workbench") or {}
        prior = next((a for a in before.get("assets") or []
                      if _norm(a.get("name")) == _norm(current.get("name"))), None)
        return {"you": value_compare(current.get("you_ftds"), (prior or {}).get("you_ftds"), "count"),
                "all": value_compare(current.get("ftds"), (prior or {}).get("ftds"), "count"),
                "state": NEW if prior is None else BOTH}

    def workbench(self, country: str, current: dict) -> dict:
        """Annotate a render-only copy; never insert historical rows into Use indices."""
        from copy import deepcopy

        out = deepcopy(current)
        before = (self._countries.get(_norm(country)) or {}).get("workbench") or {}
        before_slices = {_norm(k): v for k, v in (before.get("slices") or {}).items()}
        for asset, sl in (out.get("slices") or {}).items():
            prev = before_slices.get(_norm(asset)) or {}
            sl["_period_gone"] = {}
            for tab in ("campaigns", "setups", "landings", "targets", "creatives", "buyers"):
                rows = sl.get(tab) or []
                old = {self._workbench_key(tab, r): r for r in prev.get(tab) or []}
                for row in rows:
                    prior = old.pop(self._workbench_key(tab, row), None)
                    row["_period"] = metric_compare(self._workbench_metrics(row), self._workbench_metrics(prior))
                    row["_period_you"] = metric_compare(self._workbench_metrics(row.get("you")), self._workbench_metrics((prior or {}).get("you")))
                    row["_period_you_visible"] = self._had_you(prior)
                    row["_period_state"] = NEW if prior is None else BOTH
                    if tab == "creatives":
                        versions = {self._workbench_key("versions", v): v for v in (prior or {}).get("versions") or []}
                        for version in row.get("versions") or []:
                            pv = versions.pop(self._workbench_key("versions", version), None)
                            version["_period"] = metric_compare(self._workbench_metrics(version), self._workbench_metrics(pv))
                            version["_period_state"] = NEW if pv is None else BOTH
                            version["_period_you"] = metric_compare(self._workbench_metrics(version.get("you")), self._workbench_metrics((pv or {}).get("you")))
                            version["_period_you_visible"] = self._had_you(pv)
                        row["_period_gone_versions"] = [dict(v, _period=metric_compare(None, self._workbench_metrics(v)),
                                                            _period_you=metric_compare(None, self._workbench_metrics(v.get("you"))),
                                                            _period_you_visible=self._had_you(v))
                                                        for v in versions.values()]
                sl["_period_gone"][tab] = self._workbench_gone_rows(old.values(), tab)
            sl["_period_compare"] = {}
            for side in ("you", "others", "unattributed"):
                sl["_period_compare"][side] = metric_compare(
                    self._workbench_metrics((sl.get("compare") or {}).get(side)),
                    self._workbench_metrics((prev.get("compare") or {}).get(side)))
            def attributed(data):
                values = [_num(((data or {}).get(side) or {}).get("ftds")) for side in ("you", "others")]
                return sum(values) if all(v is not None for v in values) else None
            sl["_period_compare"]["attributed"] = value_compare(
                attributed(sl.get("compare")), attributed(prev.get("compare")), "count")
        for asset in out.get("assets") or []:
            asset["_period_ftds"] = self.asset_counts(country, asset)
        out["_period_assets_gone"] = [a for a in before.get("assets") or []
                                       if _norm(a.get("name")) not in {_norm(k) for k in (out.get("slices") or {})}]
        out["_period_earlier_slices"] = {
            k: {tab: self._workbench_gone_rows(v.get(tab) or [], tab)
                for tab in ("campaigns", "setups", "landings", "targets", "creatives", "buyers")}
            for k, v in (before.get("slices") or {}).items()
            if _norm(k) not in {_norm(a) for a in (out.get("slices") or {})}}
        return out


def state_for(cur: Optional[dict], prev: Optional[dict]) -> str:
    return row_state(_present(cur) if cur is not None else False, prev is not None)
