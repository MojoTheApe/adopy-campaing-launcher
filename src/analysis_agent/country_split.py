# -*- coding: utf-8 -*-
"""Пострановая разбивка спенда, снятая с самого кабинета (acl-357).

Маршрут — Ads Reporting, решение владельца 2026-09-10 (issue #113):
сводная таблица с брейкдауном **Country** за период отвечает таблицей
«Страна | Amount spent» — точные пострановые числа без парсинга имён
кампаний и без брейкдауна внутри общей таблицы кампаний.

Как и всё чтение, это attach-only: цепляемся к уже открытому окну
профиля и никогда его не перезапускаем. Пустой отчёт — это «за период
ничего не крутилось», и об этом говорится вслух; ноль и «не читали» —
разные вещи.

Первая версия требует новую сводную при каждом чтении и сверяет итог
с полным числом отображённых строк. Неподтверждённая полнота — отказ. Сохранённый
view на аккаунте («один раз настроили — дальше открываем») — возможное
ускорение потом, когда живые прогоны покажут, что дороже: клики по
панели Customize или лишняя сущность на кабинете.

Селекторные шаги написаны по скриншотам владельца от 2026-09-10 и до
первого живого прогона остаются догадкой о вёрстке Meta — ровно как
список фраз в acl-124. После подключения отказ сопровождается попыткой
сохранить снимок экрана; до подключения доступен только текст ошибки.
"""
from __future__ import annotations

import json
import math
import re
import threading
from datetime import date, timedelta
from decimal import Decimal
from dataclasses import dataclass, field, asdict
from pathlib import Path

from config import paths
from src.fb_ads_rpa.operations import serialized_browser
from src.analysis_agent.account_spend import (
    _act_from_url,
    _profile_key,
    _attach_only,
    _entry_for,
    _period_matches,
    _read_status,
    _shoot,
    _sleep,
)

#: Экран отчётов Ads Manager. `?act=` обязателен — без него Reporting
#: откроет «какой-нибудь» кабинет, а чтение чужого кабинета — отказ.
REPORTING = "https://adsmanager.facebook.com/adsmanager/reporting"

REPORT_TIMEOUT_MS = 60_000

#: Валютные коды и знаки, которые Reporting пишет рядом с суммой.
#: Знак доллара нарочно НЕ превращается в "USD": $ пишут и за AUD, и за
#: MXN, и угаданная валюта хуже пустой — пустая честно говорит «смотри
#: на кабинет», угаданная выглядит как знание.
_CURRENCY_SIGNS = {"€": "EUR", "£": "GBP", "₪": "ILS"}


@dataclass
class CountrySplit:
    """Metadata follows SpendReading; per-country rows replace its single sum."""

    account: str
    date_from: str
    date_to: str
    source: str = "rpa-reporting"
    state: str = "failed"  # ok | empty | failed
    rows: list = field(default_factory=list)  # [{country, spend, currency}]
    status: str = ""
    status_text: str = ""
    how: str = ""
    error: str = ""
    shot: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# ── Хранилище: своя копилка, тот же принцип, что у показаний спенда ────
_LOCK = threading.RLock()


def _store_path() -> Path:
    return Path(paths.get_local_config_dir()) / "country_splits.json"


def _key(account: str, date_from: str, date_to: str) -> str:
    return f"{account}|{date_from}|{date_to}"


def save_split(split: "CountrySplit") -> None:
    with _LOCK:
        path = _store_path()
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if not isinstance(data, dict):
            raise ValueError("Country readings store is not an object; original file preserved")
        data[_key(split.account, split.date_from, split.date_to)] = split.to_dict()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1, allow_nan=False), encoding="utf-8")
        tmp.replace(path)


def get_split(account: str, date_from: str, date_to: str) -> dict | None:
    with _LOCK:
        path = _store_path()
        try:
            data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        value = data.get(_key(account, date_from, date_to or date_from))
        return value if isinstance(value, dict) else None


# ── Разбор ячеек: чистый Python, тестируется без браузера ──────────────

def money_and_currency(text: str) -> tuple[float | None, str]:
    """Parse a complete English-locale amount cell, never a matching substring.

    Keep signs and explicit currency codes; '$' alone stays unknown. Unsupported
    locale/precision is a refusal, not an invitation to guess the amount.
    """
    flat = " ".join((text or "").split()).replace("−", "-")
    number = r"(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]{1,3}(?: [0-9]{3})+|[0-9]+)(?:\.[0-9]{1,3})?"
    match = re.fullmatch(
        rf"(?P<sign1>[+-]?)\s*(?P<prefix>[A-Z]{{3}}|[$€£₪])?\s*"
        rf"(?P<sign2>[+-]?)\s*(?P<number>{number})\s*(?P<suffix>[A-Z]{{3}}|[$€£₪])?", flat
    )
    if not match or (match['sign1'] and match['sign2']):
        return None, ""
    tokens = [match['prefix'], match['suffix']]
    codes = {_CURRENCY_SIGNS.get(t, t) for t in tokens if t and t != "$"}
    if len(codes) > 1:
        return None, ""
    value = float(match['number'].replace(",", "").replace(" ", ""))
    if not math.isfinite(value):
        return None, ""
    if (match['sign1'] or match['sign2']) == "-":
        value = -value
    return value, next(iter(codes), "")


_EMPTY_MARKERS = {"no data", "no results", "no data available", "no results found"}
_TOTAL_LABEL = re.compile(r"(?:total(?: results)?|all)(?:\s+([0-9]+)/([0-9]+) rows displayed)?", re.I)
_METRIC_COLUMNS = {"amount spent", "reach", "impressions", "cost per result", "results", "frequency"}


def _parse_cells(matrix: list[list[str]]) -> tuple[list[dict], dict | None, str]:
    """Reject incomplete rows instead of silently dropping countries."""
    headers = []
    for i, row in enumerate(matrix):
        names = [" ".join(c.lower().split()) for c in row]
        if "country" in names and "amount spent" in names:
            headers.append((i, names))
    if len(headers) != 1:
        return [], None, "refused: unique Country and Amount spent header not found"
    header_i, names = headers[0]
    if names.count("country") != 1 or names.count("amount spent") != 1:
        return [], None, "refused: ambiguous report columns"
    # An unrecognized dimension can still yield unique country names (e.g. only
    # one age bucket happened to deliver). Duplicate detection alone is unsafe.
    if any(n != "country" and n not in _METRIC_COLUMNS for n in names):
        return [], None, "refused: additional breakdown or unsupported report column"
    country_col, amount_col = names.index("country"), names.index("amount spent")
    rows, seen, total, empty = [], set(), None, False
    for cells in matrix[header_i + 1:]:
        cells = [" ".join(c.split()) for c in cells]
        if not any(cells):
            continue
        if " ".join(c for c in cells if c).lower() in _EMPTY_MARKERS:
            empty = True
            continue
        if len(cells) != len(names):
            return [], None, "refused: incomplete report row"
        country = cells[country_col]
        amount, currency = money_and_currency(cells[amount_col])
        if not country or amount is None:
            return [], None, "refused: unreadable country or amount"
        total_label = _TOTAL_LABEL.fullmatch(country)
        if total_label:
            if total is not None:
                return [], None, "refused: multiple totals"
            total = {"spend": amount, "currency": currency, "count": None}
            if total_label[1] is not None:
                displayed, count = int(total_label[1]), int(total_label[2])
                if displayed != count:
                    return [], None, "refused: only some report rows are displayed"
                total["count"] = count
            continue
        if country.lower() in seen:
            return [], None, "refused: duplicate country or unresolved breakdown"
        seen.add(country.lower())
        rows.append({"country": country, "spend": amount, "currency": currency})
    if empty:
        if rows or (total and (total["spend"] != 0 or total["count"] not in (None, 0))):
            return [], None, "refused: no-data marker conflicts with report rows"
        return [], total, "empty: explicit no-data marker"
    if not rows:
        return [], total, "refused: report has no readable country rows yet"
    return rows, total, f"pivot rows from cells, header row {header_i}, amount col {amount_col}"


def rows_from_cells(matrix: list[list[str]]) -> tuple[list[dict], str]:
    """Parse rows; this alone does not establish report completeness."""
    rows, _, how = _parse_cells(matrix)
    return rows, how


def _verified_rows(matrix: list[list[str]]) -> tuple[list[dict], str]:
    rows, total, how = _parse_cells(matrix)
    if how.startswith(("refused:", "empty:")):
        return rows, how
    # Reporting virtualizes rows. A matching total alone can hide omitted zero
    # rows or cancelling adjustments. Require the displayed/full row count too.
    if not total or total["count"] != len(rows):
        return [], "refused: complete country row count and total are not confirmed"
    if any(row["currency"] != total["currency"] for row in rows):
        return [], "refused: currencies differ between country rows and total"
    summed = sum((Decimal(str(row["spend"])) for row in rows), Decimal(0))
    if summed != Decimal(str(total["spend"])):
        return [], "refused: country rows do not reconcile with report total"
    return rows, how + "; complete row count and total reconciled"


# ── Браузерная часть ───────────────────────────────────────────────────

# Only one visible Country report may supply the matrix, ARIA or native HTML.
_VISIBLE_JS = """
const visible = el => {
  const style = getComputedStyle(el), box = el.getBoundingClientRect();
  return !!(box.width && box.height) && style.visibility === 'visible'
    && !el.closest('[hidden],[aria-hidden="true"]');
};
const norm = text => (text || '').trim().replace(/\\s+/g, ' ').toLowerCase();
"""

_GRAB_CELLS_JS = "() => {" + _VISIBLE_JS + """
  const rootSelector = 'table,[role="table"],[role="grid"],[role="treegrid"]';
  const candidates = [...document.querySelectorAll(rootSelector)].filter(visible).map(root => {
    const rows = [...root.querySelectorAll('tr,[role="row"]')]
      .filter(row => row.closest(rootSelector) === root && visible(row))
      .map(row => [...row.querySelectorAll('th,td,[role="cell"],[role="gridcell"],[role="columnheader"],[role="rowheader"]')]
        .filter(cell => cell.closest('tr,[role="row"]') === row && visible(cell))
        .map(cell => (cell.innerText || '').trim()));
    return {root, rows};
  }).filter(({rows}) => rows.some(row => row.map(norm).includes('country') && row.map(norm).includes('amount spent')));
  if (candidates.length !== 1) return {matrix: [], error: 'unique visible Country report not found'};
  const {root, rows} = candidates[0];
  const busy = root.closest('[aria-busy="true"]') ||
    [...root.querySelectorAll('[aria-busy="true"],[role="progressbar"]')].some(visible);
  return {matrix: rows, error: busy ? 'report is still loading' : ''};
}
"""

#: Клик по подписи: найти видимый элемент, чей текст по словам совпадает с
#: искомым, и кликнуть ближайший кликабельный. Подписи сравниваются по
#: словам — Meta переносит их как хочет.
_CLICK_LABEL_JS = "(wanted) => {" + _VISIBLE_JS + """
  const matches = [...document.querySelectorAll('a,button,[role="button"],[role="menuitem"]')]
    .filter(el => visible(el) && norm(el.innerText) === norm(wanted));
  if (matches.length !== 1 || matches[0].disabled || matches[0].getAttribute('aria-disabled') === 'true') return false;
  matches[0].click();
  return true;
}
"""

# Resolve and operate on the same unique, visible checkbox. A matching span
# elsewhere on the page must never toggle an unrelated control.
_CHECKBOX_JS = "(args) => {" + _VISIBLE_JS + """
  const wanted = typeof args === 'string' ? args : args.label;
  const name = el => {
    if (el.getAttribute('aria-label')) return norm(el.getAttribute('aria-label'));
    const ids = (el.getAttribute('aria-labelledby') || '').split(/\\s+/).filter(Boolean);
    if (ids.length) return norm(ids.map(id => document.getElementById(id)?.innerText || '').join(' '));
    if (el.labels?.length) return norm([...el.labels].map(label => label.innerText).join(' '));
    if (norm(el.innerText)) return norm(el.innerText);
    return norm((el.closest('label') || el.parentElement)?.innerText);
  };
  const matches = [...document.querySelectorAll('[role="checkbox"],input[type="checkbox"]')]
    .filter(el => visible(el) && name(el) === norm(wanted));
  if (matches.length !== 1) return null;
  const el = matches[0], aria = el.getAttribute('aria-checked');
  if (el.disabled || el.getAttribute('aria-disabled') === 'true' || el.indeterminate) return null;
  const state = aria !== null ? (aria === 'true' ? true : aria === 'false' ? false : null)
    : typeof el.checked === 'boolean' ? el.checked : null;
  if (state !== null && typeof args === 'object' && state !== args.wanted) el.click();
  return state;
}
"""
_CHECKED_JS = _CHECKBOX_JS


def _click_label(page, label: str, tries: int = 10) -> bool:
    for _ in range(tries):
        try:
            if page.evaluate(_CLICK_LABEL_JS, label):
                return True
        except Exception:
            pass
        _sleep(0.6)
    return False


def _set_checkbox(page, label: str, wanted: bool) -> bool:
    """Привести чекбокс к нужному состоянию. Неизвестное состояние после
    клика — отказ, а не надежда: галка, о которой мы гадаем, даст таблицу
    не с тем брейкдауном, и число будет выглядеть правильным."""
    for _ in range(6):
        try:
            state = page.evaluate(_CHECKED_JS, label)
        except Exception:
            state = None
        if state is wanted:
            return True
        if state is None:
            return False
        page.evaluate(_CHECKBOX_JS, {"label": label, "wanted": wanted})
        _sleep(0.8)
    return False


def _reporting_url(act: str, date_from: str, date_to: str) -> str:
    act_id = act.strip().removeprefix("act_")
    if not re.fullmatch(r"[0-9]+", act_id):
        raise ValueError("ad_account_id must contain digits, optionally prefixed with act_")
    start, end = date.fromisoformat(date_from), date.fromisoformat(date_to or date_from)
    if start.isoformat() != date_from or end.isoformat() != (date_to or date_from) or start > end:
        raise ValueError("Dates must be canonical YYYY-MM-DD in ascending order")
    return f"{REPORTING}?act={act_id}&date={start.isoformat()}_{(end + timedelta(days=1)).isoformat()}"


_PERIOD_JS = "() => {" + _VISIBLE_JS + """
  const labels = [...document.querySelectorAll('button,[role="button"]')].filter(visible)
    .map(el => (el.innerText || '').trim())
    .filter(text => /\\b\\w{3} \\d{1,2}, \\d{4}|\\b\\d{4}-\\d{2}-\\d{2}\\b/.test(text));
  const unique = [...new Set(labels)];
  return unique.length === 1 ? unique[0] : '';
}
"""


def _shown_period_text(page) -> str:
    return " ".join((page.evaluate(_PERIOD_JS) or "").split())


@serialized_browser(key=_profile_key, reading=True)
def read_country_split(account: str, date_from: str, date_to: str = "") -> CountrySplit:
    """Read one fresh report, refusing unverified or partial results.

    Uses ACL's process-wide browser lock. As with the ordinary spend reader,
    one live profile must not be shared by several ACL processes.
    """
    date_to = date_to or date_from
    out = CountrySplit(account=account, date_from=date_from, date_to=date_to)
    conn = None
    try:
        entry = _entry_for(account)
        if entry is None:
            raise ValueError(f"{account} нет в карте аккаунтов")
        act = (entry.ad_account_id or "").strip()
        url = _reporting_url(act, date_from, date_to)  # validate before attach
        expected_act = "act_" + act.removeprefix("act_")
        conn = _attach_only(entry, account)
        page = conn.page
        page.goto(url, timeout=REPORT_TIMEOUT_MS, wait_until="domcontentloaded")
        _sleep(4)
        if _act_from_url(page, tries=6) != expected_act:
            raise ValueError("Ads Reporting opened a different account; reading refused")
        out.status, out.status_text = _read_status(page)
        # Reusing a saved view could retain filters and quietly undercount spend.
        # A new report is mandatory until the live pilot proves filter handling.
        for label in ("Create report", "Pivot table", "Customize"):
            if not _click_label(page, label, tries=4):
                raise ValueError(f"could not open fresh report: {label}")
            _sleep(2)
        for label, wanted in (("Campaign name", False), ("Ad set name", False), ("Country", True)):
            if not _set_checkbox(page, label, wanted):
                raise ValueError(f"could not verify breakdown '{label}'")
        _sleep(3)
        if not _period_matches(_shown_period_text(page), date_from, date_to):
            raise ValueError("Displayed report period does not match the requested dates")

        rows, how = [], "refused: report not loaded"
        for attempt in range(10):
            snapshot = page.evaluate(_GRAB_CELLS_JS)
            if not isinstance(snapshot, dict) or snapshot.get("error"):
                how = "refused: " + (snapshot.get("error", "unreadable report") if isinstance(snapshot, dict) else "unreadable report")
            else:
                rows, how = _verified_rows(snapshot.get("matrix") or [])
                if not how.startswith("refused:"):
                    break
            if attempt < 9:
                _sleep(2)
        out.how = how
        if how.startswith("refused:"):
            raise ValueError(how)
        if _act_from_url(page, tries=1) != expected_act:
            raise ValueError("Account changed during reading; result refused")
        if not _period_matches(_shown_period_text(page), date_from, date_to):
            raise ValueError("Displayed period changed during reading; result refused")
        out.rows = rows
        out.state = "ok" if rows else "empty"
        if not rows:
            out.how = "Report explicitly shows no data for the requested period"
    except Exception as exc:
        out.state, out.rows = "failed", []
        out.error = str(exc) or type(exc).__name__
    finally:
        # Persist failures too: a failed retry must not expose an old success.
        try:
            save_split(out)
        except Exception as exc:
            out.state, out.rows = "failed", []
            out.error = (out.error + "; " if out.error else "") + f"Could not save reading ({type(exc).__name__}); stored data was not replaced"
        if conn is not None:
            if out.state == "failed":
                try:
                    out.shot = _shoot(conn.page, account, date_from)
                    # Update the persisted failure with its diagnostic path.
                    save_split(out)
                except Exception:
                    pass
            try:
                conn.page.close()  # _attach_only creates a separate owned tab
            except Exception:
                pass
            try:
                conn.close(keep_browser_open=True)
            except Exception:
                pass
    return out


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 3:
        raise SystemExit("usage: python -m src.analysis_agent.country_split ACCOUNT DATE_FROM [DATE_TO]")
    result = read_country_split(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "")
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=1))
    raise SystemExit(1 if result.state == "failed" else 0)
