"""Вкладка «ad texts» той же таблицы, что и таргеты.

Разметка (проверена вживую 2026-09-08, 327 строк):

    A: Ad Title     — как текст называют между собой («ES how much», «Stable halal»)
    B: Headline     — короткая строка-призыв, она же заголовок объявления
    C: Body         — тело; переносы строк значимы, ими разбиты абзацы
    D: Description  — у большинства строк это рейтинговая плашка «★★★★⯪4.6k»
    E: Asset        — заполнено у двух строк из 327
    F: Language     — AR / ES; у 113 строк пусто

Вкладка — **источник наполнения, а не рабочее место**: после переноса
библиотека живёт в реестре на машине, правки идут там. Отсюда только чтение;
писать обратно этот модуль не умеет намеренно.

Ключей к таблице два, и пробуются оба на самом запросе: сервисный аккаунт
получает от политики Adopy `403`, а клиент под него собирается без единой
жалобы — проверка «удалось ли создать клиент» здесь ничего не значит.

Переопределения окружением:
    GSHEET_TARGETING_SPREADSHEET_ID — id таблицы (тот же, что у таргетов)
    GSHEET_AD_TEXTS_TAB             — имя вкладки (по умолчанию «ad texts»)
"""
from __future__ import annotations

import os
import sys
import threading
import time
from dataclasses import dataclass, field

_DEFAULT_TAB = "ad texts"
_CACHE_TTL_S = 300.0

#: Почему последнее чтение не удалось — пусто, если удалось. Пустая вкладка и
#: отказ в доступе дают одинаковый пустой список, а действий требуют разных.
last_error: str = ""


@dataclass
class _Cache:
    loaded_at: float = 0.0
    rows: list = field(default_factory=list)


_cache = _Cache()
_lock = threading.RLock()


def _log(msg: str) -> None:
    print(f"[gsheet_ad_texts] {msg}", file=sys.stderr)


def _spreadsheet_id() -> str:
    from src.analysis_agent.gsheet_targeting import _DEFAULT_SPREADSHEET_ID

    return os.environ.get("GSHEET_TARGETING_SPREADSHEET_ID") or _DEFAULT_SPREADSHEET_ID


def load_ad_text_rows(*, force: bool = False) -> list:
    """Строки вкладки, без заголовка. Пустой список на любом отказе.

    Причина отказа — в `last_error`, и на экране она должна быть видна: иначе
    «библиотека пуста» и «таблица закрыта» выглядят одинаково.
    """
    global last_error
    with _lock:
        now = time.time()
        if not force and _cache.loaded_at and now - _cache.loaded_at < _CACHE_TTL_S:
            return _cache.rows

        from src.analysis_agent.gsheet_targeting import _sheets_services

        sheet_id = _spreadsheet_id()
        tab = os.environ.get("GSHEET_AD_TEXTS_TAB") or _DEFAULT_TAB
        troubles = []
        for label, svc in _sheets_services():
            try:
                meta = svc.spreadsheets().get(spreadsheetId=sheet_id).execute()
                titles = [s["properties"]["title"] for s in meta.get("sheets", [])]
                # Переименование вкладки на регистр или пробел не должно ломать чтение.
                actual = next((t for t in titles if t.strip().lower() == tab.strip().lower()), None)
                if actual is None:
                    last_error = f"вкладки «{tab}» нет в таблице (есть: {', '.join(titles)})"
                    _log(f"tab {tab!r} not found (have: {titles})")
                    return []
                quoted = actual.replace("'", "''")
                rows = (svc.spreadsheets().values()
                        .get(spreadsheetId=sheet_id, range=f"'{quoted}'!A2:F")
                        .execute().get("values", []))
                if not isinstance(rows, list) or any(not isinstance(r, list) for r in rows):
                    raise ValueError("некорректный ответ Google")
                last_error = ""
                _cache.rows, _cache.loaded_at = rows, now
                _log(f"loaded {len(rows)} ad-text rows from tab {actual!r} via {label}")
                return rows
            except Exception as exc:
                troubles.append(f"{label}: {exc}")
                _log(f"{label} refused ({exc!r})")

        last_error = "; ".join(troubles) or "нечем открыть таблицу: ни сервисного аккаунта, ни токена сотрудника"
        return []
