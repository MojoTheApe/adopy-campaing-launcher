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
    return {
        "spend": _num(spend) or 0.0,
        "leads": _num(leads) or 0.0,
        "ftds": _num(ftds) or 0.0,
        "cpl": r["cpl"], "cpa": r["cpa"], "cr": r["cr"],
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
    """В строке есть что показать: хотя бы одно ненулевое число или has_data."""
    if not row:
        return False
    if "has_data" in row:
        return bool(row.get("has_data"))
    return any(_num(row.get(k)) for k in ("spend", "leads", "deposits"))


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
        self.empty = not any(
            _present(r) for rows in self._amap.values() for r in rows.values()
        ) and not any(self._book.values())

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


def state_for(cur: Optional[dict], prev: Optional[dict]) -> str:
    return row_state(_present(cur) if cur is not None else False, prev is not None)
