"""Явные правила равенства имён аккаунтов (config/account_name_rules.json).

Книга зовёт аккаунт `FacebookMKTOBFLB98`, Multilogin и дешборд — `KLD_BFL_B98`.
Это одно и то же имя под двумя префиксами. Совпадение по такому правилу — точное,
не догадка: оно побеждает угадывание по хвосту. Правило применяется только там,
где имена СРАВНИВАЮТ; ключи словарей (`normalize_account_alias`) остаются прежними.
Новые правила добавляются в конфиг, код не меняется.

Битый файл правил — не причина ронять отчёт: любая ошибка формата означает
«правил нет» (одно предупреждение в лог, результат кэшируется).
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

_LOG = logging.getLogger(__name__)
_RULES_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "account_name_rules.json"
_CACHE: dict = {"stamp": None, "rules": [], "checked": 0.0}
#: ACL-1729: stat файла стоил ~0.6 мс на вызов, а вызовов были сотни тысяч за
#: одну отрисовку Home. Правка файла нужна в человеческом масштабе времени —
#: перепроверяем не чаще раза в это число секунд (горячая перезагрузка ACL-1653).
_RESTAT_EVERY_S = 1.0


def _parse(data) -> list[list[str]]:
    """Список правил (каждое — список префиксов) или ValueError, если формат неверный."""
    if not isinstance(data, dict):
        raise ValueError("top level must be an object")
    raw_rules = data.get("rules")
    if not isinstance(raw_rules, list):
        raise ValueError("'rules' must be a list")
    rules: list[list[str]] = []
    for r in raw_rules:
        if not isinstance(r, dict):
            raise ValueError("a rule must be an object")
        prefixes = r.get("prefixes")
        if not isinstance(prefixes, list) or len(prefixes) < 2:
            raise ValueError("'prefixes' must be a list of 2+ strings")
        if not all(isinstance(p, str) and p.strip() for p in prefixes):
            raise ValueError("every prefix must be a non-empty string")
        rules.append([p.strip() for p in prefixes])
    return rules


def _snapshot() -> list[list[str]]:
    """Единственный снимок правил: файл читается один раз на изменение."""
    now = time.monotonic()
    stamp0 = _CACHE["stamp"]
    if (stamp0 is not None and stamp0[0] == str(_RULES_PATH)
            and 0.0 <= now - _CACHE["checked"] < _RESTAT_EVERY_S):
        return _CACHE["rules"]
    _CACHE["checked"] = now
    try:
        st = _RULES_PATH.stat()
        stamp = (str(_RULES_PATH), st.st_mtime_ns, st.st_size)
    except OSError:
        stamp = (str(_RULES_PATH), None, None)
    if _CACHE["stamp"] == stamp:
        return _CACHE["rules"]
    rules: list[list[str]] = []
    if stamp[1] is not None:
        try:
            rules = _parse(json.loads(_RULES_PATH.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError) as exc:
            _LOG.warning("account_name_rules.json ignored: %s", exc)
            rules = []
    _CACHE["stamp"], _CACHE["rules"] = stamp, rules
    return rules


def _first_word(name) -> str:
    parts = ("" if name is None else str(name)).split()
    return parts[0] if parts else ""


def rule_split(name) -> tuple[int, str] | None:
    """(номер правила, id-суффикс в нижнем регистре), если имя подпадает под правило."""
    word = _first_word(name).casefold()
    for idx, prefixes in enumerate(_snapshot()):
        # самый длинный префикс первым
        for p in sorted((x.casefold() for x in prefixes), key=len, reverse=True):
            if word.startswith(p) and len(word) > len(p):
                return idx, word[len(p):]
    return None


def account_key(name) -> str:
    """Ключ СРАВНЕНИЯ: слово до пробела в нижнем регистре; под правилом оба префикса
    сводятся к одному. Не для ключей словарей и не для показа."""
    hit = rule_split(name)
    if hit:
        return f"rule{hit[0]}:{hit[1]}"
    return _first_word(name).casefold()


def has_own_name(label: str, known) -> bool:
    """Само имя аккаунта (без хвоста про страну/актив) есть среди `known`."""
    own = _first_word(label).casefold()
    return bool(own) and any(_first_word(a).casefold() == own for a in known)


def exact_alias(label: str, known) -> str:
    """Имя из `known`, равное `label` по явному правилу, или "".

    Если своё имя аккаунта в `known` есть — оно и есть ответ, альтернатива
    не нужна (""). Два разных имени под правилом (дубль в книге) — не выбираем.
    """
    hit = rule_split(label)
    if not hit or has_own_name(label, known):
        return ""
    seen: dict[str, str] = {}
    for a in known:
        w = _first_word(a).casefold()
        if w and rule_split(a) == hit:
            seen.setdefault(w, str(a))
    return next(iter(seen.values())) if len(seen) == 1 else ""


def id_suffix(name) -> str:
    """id аккаунта под правилом (в верхнем регистре) или ""."""
    hit = rule_split(name)
    return hit[1].upper() if hit else ""
