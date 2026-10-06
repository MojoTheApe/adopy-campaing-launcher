"""Явные правила равенства имён аккаунтов (config/account_name_rules.json).

Книга зовёт аккаунт `FacebookMKTOBFLB98`, Multilogin и дешборд — `KLD_BFL_B98`.
Это одно и то же имя под двумя префиксами. Совпадение по такому правилу — точное,
не догадка: оно побеждает угадывание по хвосту и нигде не помечается как догадка.
Новые правила добавляются в конфиг, код не меняется.
"""
from __future__ import annotations

import json
from pathlib import Path

_RULES_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "account_name_rules.json"
_CACHE: dict = {"mtime": None, "rules": []}


def _load_rules() -> list[tuple[str, list[str], str]]:
    """[(имя правила, префиксы в нижнем регистре, канонический префикс)] — перечитывается при смене файла."""
    try:
        mtime = _RULES_PATH.stat().st_mtime
    except OSError:
        return []
    if _CACHE["mtime"] == mtime:
        return _CACHE["rules"]
    rules: list[tuple[str, list[str], str]] = []
    try:
        data = json.loads(_RULES_PATH.read_text(encoding="utf-8"))
        for i, r in enumerate(data.get("rules", [])):
            raw = [str(p).strip() for p in r.get("prefixes", []) if str(p).strip()]
            prefixes = [p.casefold() for p in raw]
            if prefixes:
                canonical = raw[0]  # первый в списке — каноническая форма
                # самый длинный префикс первым
                prefixes.sort(key=len, reverse=True)
                rules.append((str(r.get("name") or f"rule{i}"), prefixes, canonical))
    except (OSError, ValueError, AttributeError):
        rules = []
    _CACHE["mtime"], _CACHE["rules"] = mtime, rules
    return rules


def _first_word(name) -> str:
    parts = ("" if name is None else str(name)).split()
    return parts[0] if parts else ""


def rule_split(name) -> tuple[int, str] | None:
    """(номер правила, id-суффикс в нижнем регистре), если имя подпадает под правило."""
    word = _first_word(name).casefold()
    for idx, (_n, prefixes, _c) in enumerate(_load_rules()):
        for p in prefixes:
            if word.startswith(p) and len(word) > len(p):
                return idx, word[len(p):]
    return None


def account_key(name) -> str:
    """Каноническое имя для сравнения: слово до пробела, нижний регистр.

    Под правилом оба префикса сводятся к одному, поэтому
    `FacebookMKTOBFLB98 SV Ait` и `KLD_BFL_B98` дают один ключ.
    """
    hit = rule_split(name)
    if hit:
        return f"rule{hit[0]}:{hit[1]}"
    return _first_word(name).casefold()


def exact_alias(label: str, known) -> str:
    """Имя из `known`, равное `label` по явному правилу, или "".

    Своё же имя не возвращается. Если под правило подпадают два разных имени
    (дубль в книге) — не выбираем наугад.
    """
    hit = rule_split(label)
    if not hit:
        return ""
    own = _first_word(label).casefold()
    seen: dict[str, str] = {}
    for a in known:
        w = _first_word(a).casefold()
        if w and w != own and rule_split(a) == hit:
            seen.setdefault(w, str(a))
    return next(iter(seen.values())) if len(seen) == 1 else ""


def id_suffix(name) -> str:
    """id аккаунта под правилом (в верхнем регистре) или ""."""
    hit = rule_split(name)
    return hit[1].upper() if hit else ""


def canonical_alias(name) -> str:
    """Слово до пробела; под правилом — с каноническим префиксом.

    Это ключ идентичности аккаунта: `FacebookMKTOBFLB98 SV Ait` и `KLD_BFL_B98`
    дают `KLD_BFL_B98`. Регистр суффикса сохраняется — как и раньше, сравнение
    ключей регистрозависимо там, где оно таким было.
    """
    word = _first_word(name)
    hit = rule_split(word)
    if not hit:
        return word
    canonical = _load_rules()[hit[0]][2]
    return canonical + word[len(word) - len(hit[1]):]


def rule_alternates(name) -> list[str]:
    """Остальные написания того же имени по правилу (без самого `name`)."""
    word = _first_word(name)
    hit = rule_split(word)
    if not hit:
        return []
    raw_suffix = word[len(word) - len(hit[1]):]
    prefixes = _RAW_PREFIXES()[hit[0]]
    return [p + raw_suffix for p in prefixes if (p + raw_suffix).casefold() != word.casefold()]


def _RAW_PREFIXES() -> list[list[str]]:
    """Префиксы в исходном регистре, в порядке конфига."""
    try:
        data = json.loads(_RULES_PATH.read_text(encoding="utf-8"))
        out = [[str(p).strip() for p in r.get("prefixes", []) if str(p).strip()] for r in data.get("rules", [])]
        return [x for x in out if x]  # тот же отбор, что и в _load_rules
    except (OSError, ValueError, AttributeError):
        return []
