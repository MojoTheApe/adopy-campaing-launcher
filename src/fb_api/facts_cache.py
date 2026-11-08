"""Долгий кэш разобранных терминов таргетинга (на машину, JSON), ACL-2267.

Хранятся только точные находки таргетинга: страна -> код, язык -> id, термин библиотеки -> picks.
Они не зависят от кабинета. Живут `ACL_FACTS_CACHE_DAYS` дней от записи (по умолчанию 60; 0 - кэш
выключен), повторная проверка срок не продлевает. id интересов из кэша каждый раз сверяются с Meta
тем же пакетом adinterestvalid (см. targeting_resolver); отвергнутый id удаляется.

Всё, что относится к кабинету, здесь НЕ хранится и читается каждый раз: доступ и `account_status`,
валюта и часовой пояс, пиксели, Страницы - бан, смена пикселя или потеря Страницы случаются в любую
минуту.

Секретов (токенов, cookies) в файле нет: только id и имена. Файл лежит рядом с прочим машинным
состоянием (`config.paths.get_local_config_dir()`), запись атомарная, доступ потокобезопасен в
процессе; любой сбой диска = «в кэше пусто», а не ошибка публикации.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

CACHE_FILE = "facts_cache.json"
DAYS_ENV = "ACL_FACTS_CACHE_DAYS"
DEFAULT_DAYS = 60.0
FUTURE_SKEW_S = 3600.0                  # допуск на рассинхрон часов; дальше «из будущего» = устарела
_VERSION = 1
_LOCK = threading.RLock()

#: Коды Graph (code, subcode), где отказ относится к таргетингу (термин убран из библиотеки / отвергнут).
#: None - любой код. Сверка только по кодам: текст ошибки сюда не годится (ACL-2267).
TARGETING_CODES = ((100, 1487694), (None, 2446394))
#: id интереса короче этого - не настоящий (у Meta 13+ цифр; «Swing trading»=61, ACL-2258): не пишем.
MIN_INTEREST_ID_DIGITS = 10


def ttl_days() -> float:
    try:
        value = float(str(os.environ.get(DAYS_ENV, "") or DEFAULT_DAYS).replace(",", "."))
    except ValueError:
        value = DEFAULT_DAYS
    return max(0.0, min(3650.0, value))


def plausible_pick(p: Any, buckets: tuple[str, ...]) -> bool:
    """pick годится для кэша: известный тип, числовой id; у интереса - не короче MIN_INTEREST_ID_DIGITS."""
    if not isinstance(p, dict) or p.get("type") not in buckets:
        return False
    pid = str(p.get("id") or "")
    if not re.fullmatch(r"\d{1,30}", pid):
        return False
    return p["type"] != "interests" or len(pid) >= MIN_INTEREST_ID_DIGITS


class FactsCache:
    """Файл фактов: `terms[вид|язык|термин] = {value, saved_at}`."""

    def __init__(self, path: str | os.PathLike | None = None, clock=None):
        self._path = Path(path) if path else None
        self._clock = clock or time.time

    @property
    def path(self) -> Path:
        if self._path is not None:
            return self._path
        from config import paths

        return Path(paths.get_local_config_dir()) / CACHE_FILE

    # -- файл
    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"version": _VERSION, "terms": {}}
        if not isinstance(data, dict) or data.get("version") != _VERSION:
            return {"version": _VERSION, "terms": {}}
        if not isinstance(data.get("terms"), dict):
            data["terms"] = {}
        return data

    def _write(self, data: dict) -> None:
        p = self.path
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".facts-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(data, fh, ensure_ascii=False, indent=1, sort_keys=True)
                os.replace(tmp, p)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
        except OSError:
            pass                      # кэш не обязателен: следующий прогон прочтёт факты заново

    def _fresh(self, row: Any) -> bool:
        days = ttl_days()
        if days <= 0 or not isinstance(row, dict):
            return False
        try:
            age = self._clock() - float(row.get("saved_at"))
            # запись «из будущего» (часы сдвинули назад) считается устаревшей; допуск на рассинхрон часов
            return -FUTURE_SKEW_S <= age <= days * 86400
        except (TypeError, ValueError):
            return False

    def _mutate(self, fn) -> None:
        # Удаление работает и при выключенном кэше: выключение не должно оставлять старые записи навсегда.
        with _LOCK:
            data = self._read()
            fn(data)
            # просроченные строки вычищаются при каждой записи: файл не растёт
            data["terms"] = {k: r for k, r in data["terms"].items() if self._fresh(r)}
            self._write(data)

    # -- термины таргетинга: не зависят от кабинета (kind: country|locale|term)
    @staticmethod
    def term_key(kind: str, norm_term: str, locale: str = "") -> str:
        return f"{kind}|{locale}|{norm_term}"

    def get_term(self, key: str) -> dict | None:
        with _LOCK:
            row = self._read()["terms"].get(key)
        return row.get("value") if self._fresh(row) and isinstance(row.get("value"), dict) else None

    def put_term(self, key: str, value: dict) -> None:
        if ttl_days() <= 0:
            return
        row = {"value": value, "saved_at": self._clock()}
        self._mutate(lambda d: d["terms"].__setitem__(key, row))

    def drop_terms(self, keys) -> None:
        keys = list(keys or [])
        if keys:
            self._mutate(lambda d: [d["terms"].pop(k, None) for k in keys])

    def drop_interest_ids(self, ids) -> list[str]:
        """Убрать все записи с интересом из `ids` (Meta его отвергла). Возвращает удалённые ключи."""
        bad = {str(i) for i in ids or []}
        gone: list[str] = []
        if not bad:
            return gone

        def fn(d: dict) -> None:
            for k, row in list(d["terms"].items()):
                value = row.get("value") if isinstance(row, dict) else None
                picks = value.get("picks") if isinstance(value, dict) else None
                if isinstance(picks, list) and any(
                        isinstance(p, dict) and p.get("type") == "interests" and str(p.get("id")) in bad
                        for p in picks):
                    d["terms"].pop(k, None)
                    gone.append(k)

        self._mutate(fn)
        return gone

    def clear(self) -> None:
        self._mutate(lambda d: d["terms"].clear())


_DEFAULT = FactsCache()


def default() -> FactsCache:
    """Кэш машины (путь берётся при каждом обращении: `ACL_CONFIG_DIR` может смениться)."""
    return _DEFAULT


def kinds_for_error(exc: BaseException) -> set[str]:
    """Какие кэшированные факты подозреваются по ошибке Graph: только `targeting` и только по коду."""
    from src.fb_api.graph import GraphError

    if not isinstance(exc, GraphError):
        return set()
    code, sub = getattr(exc, "code", None), getattr(exc, "subcode", None)
    if any((c is None or c == code) and s == sub for c, s in TARGETING_CODES):
        return {"targeting"}
    # Meta шлёт 2446394 и кодом (как каталог ошибок: (2446394, None))
    if code == 2446394:
        return {"targeting"}
    return set()
