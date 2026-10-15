"""Python API для чтения Knowledge Base.

Читает данные из:
  - config/knowledge_base.json  (структурированный индекс)
  - knowledge_base/*.md         (текстовые описания)

Нет внешних зависимостей кроме стандартной библиотеки Python.

Поиск таргетов (get_target_context / resolve_target_code) работает в 3 этапа:
  1. Точное совпадение ключа
  2. Case-insensitive совпадение ключа
  3. Поиск по полю aliases[] (case-insensitive)
Если ничего не найдено — возвращает None; неизвестные коды можно логировать
через log_unknown_target().
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from src import acl_log
from typing import Any, Dict, List, Optional, Tuple

_PROJ_ROOT = Path(__file__).resolve().parents[2]
_KB_JSON = _PROJ_ROOT / "config" / "knowledge_base.json"
_KB_DIR = _PROJ_ROOT / "knowledge_base"
from config.paths import get_runtime_workspace
_UNKNOWN_LOG = get_runtime_workspace() / "output" / "unknown_targets.log"

_MARKDOWN_FILES = {
    "targets": "targets.md",
    "creatives": "creatives.md",
    "offers": "offers.md",
    "landing_pages": "landing_pages.md",
    "test_history": "test_history.md",
}

_cache: Optional[Dict[str, Any]] = None


# ── JSON loading ───────────────────────────────────────────────────────────────

def _load_json() -> Dict[str, Any]:
    global _cache
    if _cache is not None:
        return _cache
    if not _KB_JSON.is_file():
        _cache = {}
        return _cache
    try:
        with open(_KB_JSON, encoding="utf-8") as f:
            _cache = json.load(f)
    except (json.JSONDecodeError, OSError):
        _cache = {}
    return _cache


def reload_cache() -> None:
    """Сбросить кэш и перечитать knowledge_base.json."""
    global _cache, _terms_cache
    _cache = None
    _terms_cache = None
    _load_json()


# ── Internal lookup helpers ────────────────────────────────────────────────────

def _find_key_insensitive(d: Dict[str, Any], key: str) -> Optional[Any]:
    """Ищет ключ в словаре без учёта регистра."""
    if key in d:
        return d[key]
    key_lower = key.lower()
    for k, v in d.items():
        if k.lower() == key_lower:
            return v
    return None


def _find_canonical_by_aliases(targets: Dict[str, Any], code: str) -> Optional[Tuple[str, Dict[str, Any]]]:
    """Ищет таргет по его aliases[].

    Returns:
        (canonical_key, entry_dict) или None
    """
    code_lower = code.lower()
    for canonical_key, entry in targets.items():
        if not isinstance(entry, dict):
            continue
        aliases: List[str] = entry.get("aliases", [])
        for alias in aliases:
            if str(alias).lower() == code_lower:
                return canonical_key, entry
    return None


# ── Public API: Targets ────────────────────────────────────────────────────────

def get_target_context(code: str) -> Optional[Dict[str, Any]]:
    """Вернуть контекст таргета по коду (например 'EF', 'HV', 'Trading').

    Поиск: 1) точный ключ, 2) case-insensitive ключ, 3) aliases[].
    Возвращает dict или None если таргет не найден.
    """
    if not code or not str(code).strip():
        return None
    data = _load_json()
    targets = data.get("targets", {})
    if not isinstance(targets, dict):
        return None

    # Step 1 & 2: exact + case-insensitive key match
    result = _find_key_insensitive(targets, code)
    if result is not None:
        return result

    # Step 3: alias search
    alias_match = _find_canonical_by_aliases(targets, code)
    if alias_match is not None:
        return alias_match[1]

    return None


def resolve_target_code(code: str) -> Optional[str]:
    """Вернуть канонический код таргета по коду или алиасу.

    Например: resolve_target_code('highvalue') → 'HV'
               resolve_target_code('HV') → 'HV'
               resolve_target_code('unknown_xyz') → None

    Используется при парсинге кампейнов для нормализации названий.
    """
    if not code or not str(code).strip():
        return None
    data = _load_json()
    targets = data.get("targets", {})
    if not isinstance(targets, dict):
        return None

    # Step 1 & 2: exact + case-insensitive key match
    if code in targets:
        return code
    code_lower = code.lower()
    for k in targets:
        if k.lower() == code_lower:
            return k

    # Step 3: alias search
    alias_match = _find_canonical_by_aliases(targets, code)
    if alias_match is not None:
        return alias_match[0]

    return None


def logged_unknown_targets() -> set:
    """Коды, уже записанные в output/unknown_targets.log."""
    existing: set = set()
    if _UNKNOWN_LOG.is_file():
        try:
            with open(_UNKNOWN_LOG, encoding="utf-8") as f:
                for line in f:
                    parts = line.strip().split("|", 1)
                    if parts:
                        existing.add(parts[0].strip())
        except OSError:
            pass
    return existing


def log_unknown_target(code: str, source: str = "", *, known: Optional[set] = None) -> None:
    """Записать неизвестный код таргета в output/unknown_targets.log.

    Повторные записи одного и того же кода не дублируются.
    Args:
        code: код таргета, не найденный в KB
        source: контекст (например, имя адсета или Excel-строка)
        known: уже прочитанные коды журнала — чтобы сборка отчёта не читала
            файл заново на каждый из сотен кодов (ACL-1399)
    """
    _UNKNOWN_LOG.parent.mkdir(parents=True, exist_ok=True)

    existing = known if known is not None else logged_unknown_targets()
    if code in existing:
        return

    entry = f"{code}"
    if source:
        entry += f" | source: {source}"
    # Через `acl_log`: этот журнал пополняется из разбора драфтов, а их может
    # идти несколько сразу. Простое дописывание на Windows при этом теряет
    # записи бесследно (замер 15.09).
    try:
        acl_log.append_line(_UNKNOWN_LOG, entry)
        if known is not None:
            known.add(code)
    except acl_log.JournalWriteError as exc:
        logging.getLogger(__name__).error("Unknown-target journal write failed (%s)", type(exc).__name__)


def get_all_targets() -> Dict[str, Any]:
    """Вернуть все таргеты из KB."""
    data = _load_json()
    targets = data.get("targets", {})
    return targets if isinstance(targets, dict) else {}


# ── Fuzzy resolution (словарный fallback, задача Д4) ──────────────────────────
#
# Аудит output/unknown_targets.log (2026-07-30, 1097 уникальных нерезолвнутых
# значений): 78% содержат валидный KB-ключ ВНУТРИ строки, обвешанный мусором —
# префикс аккаунта/оффера ("AAY_Day"), возраст ("Trading_40-64"), девайс
# ("Investment/iOS"), номер варианта ("TradingYB_V1"), креатив ("Blue_HV").
# Позиционный parse_adset_name такое не вытаскивает. Fallback ниже сканирует
# текст по словарю всех ключей+алиасов и берёт самое длинное совпадение на
# границах слов. Точный резолв всегда пробуется первым — поведение для уже
# резолвящихся значений не меняется.

_terms_cache: Optional[List[Tuple[str, str]]] = None

# Части составного значения, которые являются чистым мусором парсинга
# (девайс/возраст/версия/дата, протёкшие в токен таргетинга), — их можно
# отбросить, не проваливая весь компаунд. Список сознательно узкий: часть
# отбрасывается ТОЛЬКО если сама не зарезолвилась ни ключом, ни сканом.
_NOISE_PART_RE = re.compile(
    r"""^(?:
        ios|android|all|desktop|mobile      # девайс/платформа
        | \d{1,3}\+?                        # возраст "35", "35+"
        | \d{2}[-+]\d{2}                    # возраст "40-64"
        | v?\d+                             # вариант "V1", "2"
        | \d{1,2}[.,]\d{1,2}(?:\.\d{2,4})?  # дата "27.2", "15.07"
    )$""",
    re.IGNORECASE | re.VERBOSE,
)

_COMPOUND_SPLIT_RE = re.compile(r"[|\\/]")


def _target_terms() -> List[Tuple[str, str]]:
    """Все ключи и алиасы KB как (term_lower, canonical), длинные первыми.

    Термины короче 3 символов и чисто числовые пропускаются: двухбуквенные
    ключи (ES, PO, HV, SM…) резолвятся только точным совпадением целой части
    (resolve_target_code), но не сканом внутри строки — иначе языковой маркер
    "ES" в мусоре вроде "ES_Lg_CL" ложно резолвился бы в Engaged Shoppers.
    """
    global _terms_cache
    if _terms_cache is not None:
        return _terms_cache
    terms: List[Tuple[str, str]] = []
    for canonical, entry in get_all_targets().items():
        names = [canonical]
        if isinstance(entry, dict):
            names.extend(str(a) for a in entry.get("aliases", []) or [])
        for name in names:
            t = name.strip().lower()
            if len(t) >= 3 and not t.isdigit():
                terms.append((t, canonical))
    # Длинные первыми: "Investment Plus" должен выиграть у "Investment",
    # "EF+" у "EF". При равной длине — детерминированный порядок по алфавиту.
    terms.sort(key=lambda x: (-len(x[0]), x[0]))
    _terms_cache = terms
    return terms


def find_target_in_text(text: str) -> Optional[str]:
    """Найти KB-таргет ВНУТРИ свободного текста (самое длинное совпадение).

    Совпадение засчитывается только на границах слов (сосед — не буква/цифра),
    чтобы "trade" не срабатывал внутри "TradeYB", а "1" внутри "134".

    Например: find_target_in_text('AAY_Day') → 'Day'
              find_target_in_text('dtml_HV|IPO_Lg' часть 'IPO_Lg') → 'IPO'
              find_target_in_text('TradingYB_V1') → None (граница не совпала)
              find_target_in_text('Blue_HV') → None (2-буквенные — только
              точным совпадением части, см. _target_terms)
    """
    if not text or not str(text).strip():
        return None
    hay = str(text).lower()
    for term, canonical in _target_terms():
        pos = hay.find(term)
        while pos >= 0:
            before_ok = pos == 0 or not hay[pos - 1].isalnum()
            end = pos + len(term)
            after_ok = end >= len(hay) or not hay[end].isalnum()
            if before_ok and after_ok:
                return canonical
            pos = hay.find(term, pos + 1)
    return None


def resolve_targeting_parts(text: str) -> Optional[List[str]]:
    """Разобрать значение таргетинга (в т.ч. составное `A|B` / `A/B` / `A\\B`)
    в список канонических KB-кодов.

    Каждая часть: точный резолв → скан по словарю → (если мимо) отброс, если
    часть — чистый мусор парсинга (девайс/возраст/версия/дата). Любая другая
    нерезолвнутая часть проваливает всё значение (строгая семантика — решение
    грила 2026-07-30: частичный резолв компаунда хуже честного None).

    Возвращает список канонических кодов (без дублей, порядок сохранён)
    или None.
    """
    raw = (str(text) if text is not None else "").strip()
    if not raw:
        return None
    parts = [p.strip() for p in _COMPOUND_SPLIT_RE.split(raw) if p.strip()]
    if not parts:
        return None
    resolved: List[str] = []
    for part in parts:
        canonical = resolve_target_code(part) or find_target_in_text(part)
        if canonical is None:
            if _NOISE_PART_RE.match(part):
                continue  # мусорная часть — отброс, не провал
            return None
        if canonical not in resolved:
            resolved.append(canonical)
    return resolved or None


def resolve_targeting_text(text: str) -> Optional[str]:
    """Как resolve_target_code, но компаунд-aware и со словарным fallback.

    Например: resolve_targeting_text('HV') → 'HV'
              resolve_targeting_text('AAY_Day') → 'Day'
              resolve_targeting_text('HV|Trading_40-64_Nokit') → 'HV|Trading'
              resolve_targeting_text('TRD|Investment') → None (обе части —
              реальные пробелы KB, ждут маппинга)
    """
    parts = resolve_targeting_parts(text)
    return "|".join(parts) if parts else None


# ── Public API: Offers ─────────────────────────────────────────────────────────

def get_offer_context(name: str) -> Optional[Dict[str, Any]]:
    """Вернуть контекст оффера по имени (например 'Amazon', 'Favorita').

    Поиск без учёта регистра.
    Возвращает dict или None если оффер не найден.
    """
    data = _load_json()
    offers = data.get("offers", {})
    if not isinstance(offers, dict):
        return None
    return _find_key_insensitive(offers, name)


def get_all_offers() -> Dict[str, Any]:
    """Вернуть все офферы из KB."""
    data = _load_json()
    offers = data.get("offers", {})
    return offers if isinstance(offers, dict) else {}


# ── Public API: Markdown sections ─────────────────────────────────────────────

def load_markdown_section(filename: str) -> str:
    """Загрузить содержимое .md файла из директории knowledge_base/.

    Args:
        filename: имя файла без пути, например 'targets.md'.
                  Можно также передать ключ из _MARKDOWN_FILES
                  ('targets', 'offers', 'creatives', 'landing_pages', 'test_history').

    Returns:
        Строка с содержимым файла, или пустая строка если файл не найден.
    """
    resolved = _MARKDOWN_FILES.get(filename, filename)
    path = _KB_DIR / resolved
    if not path.is_file():
        return ""
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


# ── Formatting helpers ─────────────────────────────────────────────────────────

_TARGET_AUDIENCE_EN: Dict[str, str] = {
    "EF": "financially literate users who understand trading and forex",
    "EF+": "broader EF audience with adjacent finance interests",
    "HV": "affluent users interested in investing and wealth management",
    "HNW": "ultra-high-net-worth clients with investment experience",
    "FX": "users interested in forex, including beginners",
    "Trading": "active traders interested in stocks and crypto",
    "SM": "users interested in the US stock market",
    "Day": "day traders looking for quick trading income",
    "IPO": "investors following stock listings and IPOs",
    "Wide": "all users within the selected demographic boundaries",
    "PF": "people interested in personal finance and saving money",
    "CCorg": "retargeting or lookalike audience from organic users",
    "dtml": "users who visited the site or interacted with ads",
    "Blue": "working-class users looking for additional income",
    "single": "single-interest audience without expansion",
    "Luxury": "luxury audience: travelers and premium-goods buyers, focused on Gulf markets",
    "Gambling AR com": "gambling audience configured for Argentina and .com accounts",
    "Sport AR com": "sports audience configured for Argentina and .com accounts",
    "GMBL": "gambling, sports betting, and online casino fans; applies across countries",
}


def _has_cyrillic(text: str) -> bool:
    return any("а" <= ch.lower() <= "я" or ch in "Ёё" for ch in text)


def format_target_context_line(interest: str, *, language: str = "source") -> Optional[str]:
    """Сформировать однострочный контекст таргета для вывода в отчёте.

    Использует resolve_target_code() для нормализации (поддерживает алиасы).

    Пример вывода:
        Target context: High Value — состоятельные пользователи, интересующиеся инвестициями

    Возвращает None если таргет не найден в KB.
    """
    if not interest or str(interest).strip().lower() in ("", "nan", "wide", "none"):
        return None
    canonical = resolve_target_code(interest.strip())
    ctx = get_target_context(canonical or interest.strip())
    if ctx is None:
        return None
    label = ctx.get("label", interest)
    audience = ctx.get("audience", "")
    if language == "en":
        audience = _TARGET_AUDIENCE_EN.get(canonical or "", audience)
        if _has_cyrillic(str(audience)):
            audience = ""
    if audience:
        return f"Target context: {label} — {audience}"
    return f"Target context: {label}"
