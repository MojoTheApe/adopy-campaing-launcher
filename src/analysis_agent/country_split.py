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

Первая версия собирает сводную с нуля при каждом чтении. Сохранённый
view на аккаунте («один раз настроили — дальше открываем») — возможное
ускорение потом, когда живые прогоны покажут, что дороже: клики по
панели Customize или лишняя сущность на кабинете.

Селекторные шаги написаны по скриншотам владельца от 2026-09-10 и до
первого живого прогона остаются догадкой о вёрстке Meta — ровно как
список фраз в acl-124. Каждый отказ оставляет снимок экрана.
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field, asdict
from pathlib import Path

from config import paths
from src.analysis_agent.account_spend import (
    ProfileNotAttachable,
    _act_from_url,
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
    """Одно чтение пострановой разбивки. Форма полей — как у SpendReading
    (acl-108b): кто зовёт, не должен уметь отличить маршрут по форме."""

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
        try:
            data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except Exception:
            data = {}
        data[_key(split.account, split.date_from, split.date_to)] = split.to_dict()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)


def get_split(account: str, date_from: str, date_to: str) -> dict | None:
    with _LOCK:
        path = _store_path()
        try:
            data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except Exception:
            return None
        return data.get(_key(account, date_from, date_to))


# ── Разбор ячеек: чистый Python, тестируется без браузера ──────────────

def money_and_currency(text: str) -> tuple[float | None, str]:
    """Сумма и валюта из текста ячейки.

    Правила те же, что у `_first_money` в account_spend: голое число без
    признаков суммы («4» из «4 campaigns») деньгами не считается. Валюта —
    только когда она написана: трёхбуквенный код (KWD 12.00) или
    однозначный знак; $ кодом не считается — долларов много разных.
    """
    import re

    flat = " ".join((text or "").split())
    number = r"(?:\d{1,3}(?:[, ]\d{3})+|\d+)(?:\.\d{1,2})?(?![\d.,])"
    code = re.search(rf"\b([A-Z]{{3}})\s*({number})", flat)
    if code:
        try:
            return float(code.group(2).replace(",", "").replace(" ", "")), code.group(1)
        except ValueError:
            pass
    signed = re.search(rf"([$€£₪])\s*({number})", flat)
    if signed:
        try:
            value = float(signed.group(2).replace(",", "").replace(" ", ""))
        except ValueError:
            return None, ""
        return value, _CURRENCY_SIGNS.get(signed.group(1), "")
    bare = re.search(r"(?<![\d.,])(\d{1,3}(?:[, ]\d{3})+(?:\.\d{1,2})?|\d+\.\d{1,2})(?![\d.,])", flat)
    if bare:
        try:
            return float(bare.group(1).replace(",", "").replace(" ", "")), ""
        except ValueError:
            pass
    return None, ""


#: Строки, которые в сводной не страны. Сравнение по нижнему регистру и
#: началу строки: «Total results 4/4 rows displayed» начинается с total.
_NOT_A_COUNTRY = ("total", "all", "results from", "rows displayed")


def rows_from_cells(matrix: list[list[str]]) -> tuple[list[dict], str]:
    """Пострановые строки из сырых ячеек таблицы.

    Вход — матрица текстов ячеек, как её вернул браузер (заголовок где-то
    среди строк, порядок колонок неизвестен). Выход — [{country, spend,
    currency}] и how-строка для телеметрии. Заголовок ищется по словам:
    Meta переносит подписи по-разному, точное совпадение строки — ловушка.
    """
    header_i = country_col = amount_col = -1
    for i, row in enumerate(matrix):
        lowered = [" ".join((c or "").lower().split()) for c in row]
        a_col = next((j for j, c in enumerate(lowered) if "amount spent" in c), -1)
        if a_col < 0:
            continue
        header_i, amount_col = i, a_col
        country_col = next((j for j, c in enumerate(lowered) if c == "country"), 0)
        break
    if header_i < 0:
        return [], "header 'Amount spent' not found"

    out: list[dict] = []
    for row in matrix[header_i + 1:]:
        if len(row) <= max(country_col, amount_col):
            continue
        name = " ".join((row[country_col] or "").split())
        if not name or any(name.lower().startswith(t) for t in _NOT_A_COUNTRY):
            continue
        spend, currency = money_and_currency(row[amount_col])
        if spend is None:
            # Страна без суммы — «—» у не откручивавшейся строки; нулей
            # Reporting не выдумывает, и мы не будем.
            continue
        out.append({"country": name, "spend": spend, "currency": currency})
    return out, f"pivot rows from cells, header row {header_i}, amount col {amount_col}"


# ── Браузерная часть ───────────────────────────────────────────────────

#: Снять таблицу как матрицу текстов. Сначала ARIA-грид (Reporting — это
#: div-таблица с ролями), затем обычный <table> на случай смены вёрстки.
_GRAB_CELLS_JS = """
() => {
  const текст = el => (el.innerText || '').trim();
  const ролевые = [...document.querySelectorAll('[role="row"]')]
    .map(r => [...r.querySelectorAll('[role="gridcell"],[role="columnheader"],[role="rowheader"]')].map(текст))
    .filter(r => r.length > 1);
  if (ролевые.length > 1) return ролевые;
  const таблица = [...document.querySelectorAll('table')]
    .map(t => [...t.rows].map(row => [...row.cells].map(текст)))
    .find(rows => rows.some(r => r.some(c => c.toLowerCase().includes('amount spent'))));
  return таблица || [];
}
"""

#: Клик по подписи: найти видимый элемент, чей текст по словам совпадает с
#: искомым, и кликнуть ближайший кликабельный. Подписи сравниваются по
#: словам — Meta переносит их как хочет.
_CLICK_LABEL_JS = """
(wanted) => {
  const norm = s => (s || '').toLowerCase().split(/\\s+/).filter(Boolean).join(' ');
  const target = norm(wanted);
  const all = [...document.querySelectorAll('a,button,[role="button"],[role="menuitem"],[role="checkbox"],label,span,div')];
  for (const el of all) {
    if (norm(el.innerText) !== target) continue;
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height) continue;
    const clickable = el.closest('a,button,[role="button"],[role="menuitem"],[role="checkbox"],label') || el;
    clickable.click();
    return true;
  }
  return false;
}
"""

#: Стоит ли галка у подписи (чекбоксы панели Customize).
_CHECKED_JS = """
(wanted) => {
  const norm = s => (s || '').toLowerCase().split(/\\s+/).filter(Boolean).join(' ');
  const target = norm(wanted);
  for (const el of document.querySelectorAll('[role="checkbox"],input[type="checkbox"]')) {
    const box = el.closest('label') || el.parentElement || el;
    if (!norm(box.innerText).includes(target)) continue;
    if (el.getAttribute('aria-checked') !== null) return el.getAttribute('aria-checked') === 'true';
    if ('checked' in el) return !!el.checked;
  }
  return null;
}
"""


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
        page.evaluate(_CLICK_LABEL_JS, label)
        _sleep(0.8)
    return False


def _reporting_url(act: str, date_from: str, date_to: str) -> str:
    """Адрес Reporting. `date=` кладём как в общей таблице (конец
    исключающий) — если экран его не понимает, это поймает сверка периода,
    а не молчаливо другой отрезок."""
    from datetime import date as _date, timedelta as _td

    act_id = act if act.startswith("act_") else f"act_{act}"
    end = date_to or date_from
    try:
        y, m, d = (int(x) for x in end.split("-"))
        end = (_date(y, m, d) + _td(days=1)).isoformat()
    except Exception:
        pass
    return f"{REPORTING}?act={act_id}&date={date_from}_{end}"


def _shown_period_text(page) -> str:
    """Строка периода на экране Reporting («This month: Sep 1, 2026 – Sep
    10, 2026»). Ищем в шапке отчёта; пустая строка — «не нашли»."""
    try:
        raw = page.evaluate(
            "() => { const el = [...document.querySelectorAll('button,[role=\"button\"],span')]"
            ".find(e => /\\b\\w{3} \\d{1,2}, \\d{4}/.test(e.innerText || '')); return el ? el.innerText : ''; }"
        )
        return " ".join((raw or "").split())
    except Exception:
        return ""


def read_country_split(account: str, date_from: str, date_to: str = "") -> CountrySplit:
    """Прочитать пострановую разбивку спенда за период с самого кабинета."""
    date_to = date_to or date_from
    out = CountrySplit(account=account, date_from=date_from, date_to=date_to)

    entry = _entry_for(account)
    if entry is None:
        out.error = f"{account} нет в карте аккаунтов"
        return out
    act = (entry.ad_account_id or "").strip()
    if not act:
        out.error = f"у {account} не заполнен ad_account_id"
        return out
    expected_act = act if act.startswith("act_") else "act_" + act

    conn = None
    try:
        try:
            conn = _attach_only(entry, account)
        except ProfileNotAttachable as exc:
            out.error = str(exc)
            return out
        page = conn.page
        page.goto(_reporting_url(act, date_from, date_to), timeout=REPORT_TIMEOUT_MS, wait_until="domcontentloaded")
        _sleep(4)

        if _act_from_url(page, tries=6) != expected_act:
            out.error = "Ads Reporting opened a different account; reading refused."
            return out
        out.status, out.status_text = _read_status(page)

        # Свежий отчёт: Create report → сводная. Если экран открылся сразу
        # в отчёте (сохранённый view), кнопки не будет — это не ошибка.
        if _click_label(page, "Create report", tries=4):
            _sleep(2)
            _click_label(page, "Pivot table", tries=2)
            _sleep(2)

        # Панель Customize: уровень «по кампаниям» долой, страну — да.
        _click_label(page, "Customize", tries=4)
        _sleep(1)
        for label, wanted in (("Campaign name", False), ("Ad set name", False), ("Country", True)):
            if not _set_checkbox(page, label, wanted):
                out.error = f"could not set breakdown '{label}' — Reporting markup changed?"
                out.shot = _shoot(page, account, date_from)
                return out
        _sleep(3)

        shown = _shown_period_text(page)
        if not _period_matches(shown, date_from, date_to):
            asked = date_from if date_from == date_to else f"{date_from}…{date_to}"
            out.error = (
                f"на экране период «{shown or 'не найден'}», а спрашивали {asked} — числа не берём"
            )
            out.shot = _shoot(page, account, date_from)
            return out

        matrix = []
        for _ in range(10):
            try:
                matrix = page.evaluate(_GRAB_CELLS_JS) or []
            except Exception:
                matrix = []
            if matrix:
                rows, how = rows_from_cells(matrix)
                if rows or "not found" not in how:
                    break
            _sleep(2)
        rows, how = rows_from_cells(matrix)
        out.how = how
        if not matrix or "not found" in how:
            out.error = "не нашёл сводную таблицу с колонкой Amount spent"
            out.shot = _shoot(page, account, date_from)
            return out

        if _act_from_url(page, tries=1) != expected_act:
            out.error = "Account changed during reading; result refused."
            return out

        out.rows = rows
        out.state = "ok" if rows else "empty"
        if not rows:
            out.error = "отчёт пуст — за этот период ничего не крутилось"
        save_split(out)
        return out
    finally:
        if conn is not None:
            try:
                conn.page.close()
            except Exception:
                pass
            try:
                conn.close(keep_browser_open=True)
            except Exception:
                pass


if __name__ == "__main__":  # прогон руками: аккаунт, от, до
    import sys

    if len(sys.argv) < 3:
        raise SystemExit("usage: python -m src.analysis_agent.country_split ACCOUNT DATE_FROM [DATE_TO]")
    result = read_country_split(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "")
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=1))
