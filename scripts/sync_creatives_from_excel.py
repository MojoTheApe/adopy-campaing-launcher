"""Синхронизация креативов из analytics Excel → knowledge_base/creatives.md.

Читает analytics Excel (тот же FB_STATS_EXCEL), дедуплицирует колонку 'Ad Title',
группирует по префиксу (до первого _ или числа) и генерирует structured index.

Вывод:
  - Таблица известных креативов сгруппированных по префиксу
  - Секция "Unknown / To Review" для нераспознанных префиксов
  - Статистика: сколько депозитов у каждого Ad Title (из колонки # FTDs)

Использование:
    python scripts/sync_creatives_from_excel.py
    python scripts/sync_creatives_from_excel.py --excel path/to/file.xlsx
    python scripts/sync_creatives_from_excel.py --dry-run
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_PROJ_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJ_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJ_ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(_PROJ_ROOT / ".env")
except ImportError:
    pass

_KB_CREATIVES_MD = _PROJ_ROOT / "knowledge_base" / "creatives.md"

# ── Prefix definitions ─────────────────────────────────────────────────────────
# Known prefixes → human label + guessed type
_KNOWN_PREFIXES: Dict[str, Dict[str, str]] = {
    "vid":      {"label": "Video",              "type": "video"},
    "video":    {"label": "Video",              "type": "video"},
    "img":      {"label": "Image",              "type": "image"},
    "image":    {"label": "Image",              "type": "image"},
    "abml":     {"label": "ABML",               "type": "video/image"},
    "ab":       {"label": "AB Test",            "type": "video/image"},
    "fav":      {"label": "Favorita",           "type": "image"},
    "favcc":    {"label": "Favorita CC",        "type": "image"},
    "ccorg":    {"label": "CC Organic",         "type": "video/image"},
    "aidt":     {"label": "AI Day Trader",      "type": "video"},
    "dtml":     {"label": "DTML Remarketing",   "type": "image"},
    "dt":       {"label": "DT",                 "type": "video/image"},
    "ai":       {"label": "AI",                 "type": "video/image"},
    "amazon":   {"label": "Amazon",             "type": "video/image"},
    "amz":      {"label": "Amazon",             "type": "video/image"},
    "salik":    {"label": "Salik",              "type": "image"},
    "banco":    {"label": "Banco Industrial",   "type": "image"},
    "bi":       {"label": "Banco Industrial",   "type": "image"},
    "blue":     {"label": "Blue Collar",        "type": "video/image"},
    "mcdl":     {"label": "MCDL",               "type": "video/image"},
    "gen":      {"label": "Generic",            "type": "video/image"},
    "pro":      {"label": "Pro Concept",        "type": "video/image"},
    "reg":      {"label": "Reg Concept",        "type": "video/image"},
    "learn":    {"label": "Learn Concept",      "type": "video/image"},
}

# Language / region code tokens to strip when building prefix
_LANG_TOKENS = {"es", "en", "ar", "pt", "fr", "mx", "cl", "co", "gt", "ec", "ae", "kw", "bh"}


# ── Parsing helpers ────────────────────────────────────────────────────────────

def extract_prefix(ad_title: str) -> str:
    """Извлечь канонический префикс из названия Ad Title.

    Логика:
    1. Разбить на токены по разделителям: _ - . # пробел
    2. Пропустить pure-numeric токены
    3. Пропустить language/region коды (es, en, ar, mx, ...)
    4. Остановиться на первом pure-numeric токене или после первого смыслового токена
    5. Вернуть первый смысловой токен (только буквы, lower case)

    Примеры:
      'ABML1_Simple2'      → 'abml'
      'FAVCCblk_desc'      → 'favcc'
      'ccorg_ES Invest'    → 'ccorg'
      'Vid aidt - 5'       → 'vid'
      'dtml-ES Learn'      → 'dtml'
      'AB-ML-1-ES Like'    → 'ab'
      'DT - ES MCDL pro'   → 'dt'
      'blue_ES Ai Trader'  → 'blue'
      '134_Learn'          → ''  (starts with digit)
      '3.jpg-blue.mp4-gen' → ''  (starts with digit)
      'IMAGE#1319'         → 'image'
    """
    if not ad_title or not isinstance(ad_title, str):
        return ""
    title = ad_title.strip()

    # Tokenize on separators
    tokens = re.split(r"[_\-\.\s#]+", title)

    for token in tokens:
        if not token:
            continue
        # Skip pure-numeric tokens (e.g. "1", "134", "442")
        if token.isdigit():
            return ""  # starts with a number → no meaningful prefix
        # Extract only alpha part of this token, remove digits/special chars
        alpha_part = re.sub(r"[^a-zA-Z]", "", token).lower()
        if not alpha_part:
            return ""  # e.g. "3.jpg" → non-alpha first token
        # Skip pure language/region codes
        if alpha_part in _LANG_TOKENS:
            continue
        # This is our prefix
        return alpha_part

    return ""


def infer_type(ad_title: str, prefix: str) -> str:
    """Угадать тип (video/image) по названию и префиксу."""
    title_lower = ad_title.lower()
    if any(w in title_lower for w in ("vid", "video", "mp4")):
        return "video"
    if any(w in title_lower for w in ("img", "image", "photo", "pic", "banner")):
        return "image"
    known = _KNOWN_PREFIXES.get(prefix, {})
    return known.get("type", "?")


def prefix_label(prefix: str) -> str:
    return _KNOWN_PREFIXES.get(prefix, {}).get("label", prefix.upper() if prefix else "Other")


def is_known_prefix(prefix: str) -> bool:
    return prefix in _KNOWN_PREFIXES


# ── Excel reading ──────────────────────────────────────────────────────────────

def read_creatives_from_excel(path: str, sheet: Optional[str]) -> List[Dict[str, Any]]:
    """Вернуть список уникальных Ad Title с суммой FTDs."""
    try:
        import pandas as pd
    except ImportError:
        print("[ERROR] pandas не установлен: pip install pandas openpyxl", file=sys.stderr)
        sys.exit(1)

    read_kw: Dict[str, Any] = {"engine": "openpyxl"}
    if sheet:
        read_kw["sheet_name"] = sheet

    df = pd.read_excel(path, **read_kw)
    df.columns = [str(c).strip() for c in df.columns]

    if "Ad Title" not in df.columns:
        print(f"[ERROR] Колонка 'Ad Title' не найдена. Колонки в файле: {list(df.columns)}", file=sys.stderr)
        sys.exit(1)

    df["Ad Title"] = df["Ad Title"].fillna("").astype(str).str.strip()
    df = df[df["Ad Title"] != ""]

    # FTDs (deposits) per Ad Title
    ftd_col = None
    for candidate in ("# FTDs", "FTDs", "Deposits", "# Deposits"):
        if candidate in df.columns:
            ftd_col = candidate
            break

    if ftd_col:
        df[ftd_col] = pd.to_numeric(df[ftd_col], errors="coerce").fillna(0)
        grouped = (
            df.groupby("Ad Title", dropna=False)
            .agg(deposits=(ftd_col, "sum"), rows=("Ad Title", "count"))
            .reset_index()
        )
    else:
        grouped = (
            df.groupby("Ad Title", dropna=False)
            .agg(rows=("Ad Title", "count"))
            .reset_index()
        )
        grouped["deposits"] = 0

    # Ad Asset (offer) for each Ad Title
    offer_col = None
    for candidate in ("Ad Asset", "Offer", "Ad Asset Name"):
        if candidate in df.columns:
            offer_col = candidate
            break

    offer_map: Dict[str, str] = {}
    if offer_col:
        for title, grp in df.groupby("Ad Title"):
            offers_for_title = grp[offer_col].dropna().astype(str).str.strip().unique()
            offers_for_title = [o for o in offers_for_title if o and o.lower() != "nan"]
            offer_map[str(title)] = ", ".join(sorted(offers_for_title))

    result: List[Dict[str, Any]] = []
    for _, row in grouped.iterrows():
        title = str(row["Ad Title"])
        result.append({
            "ad_title": title,
            "deposits": int(row.get("deposits", 0)),
            "rows": int(row.get("rows", 0)),
            "offer": offer_map.get(title, ""),
        })

    return sorted(result, key=lambda x: (-x["deposits"], x["ad_title"]))


# ── Markdown generation ────────────────────────────────────────────────────────

def generate_creatives_md(rows: List[Dict[str, Any]]) -> str:
    """Генерировать creatives.md с группировкой по префиксу."""

    # Group by prefix
    by_prefix: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        pfx = extract_prefix(row["ad_title"])
        by_prefix[pfx].append(row)

    known_sections: List[Tuple[str, List[Dict[str, Any]]]] = []
    unknown_rows: List[Dict[str, Any]] = []

    for pfx, group in sorted(by_prefix.items()):
        if is_known_prefix(pfx):
            known_sections.append((pfx, group))
        else:
            unknown_rows.extend(group)

    lines: List[str] = [
        "# Creatives",
        "",
        "Авто-генерируется скриптом `scripts/sync_creatives_from_excel.py` из analytics Excel.",
        "_Тип (video/image) и описание угадываются по названию — проверь и уточни вручную._",
        "",
        f"**Всего уникальных Ad Title:** {len(rows)}",
        "",
        "---",
        "",
    ]

    # Known prefix sections
    for pfx, group in known_sections:
        label = prefix_label(pfx)
        lines.append(f"## {label} (`{pfx}`)")
        lines.append("")
        lines.append("| Ad Title | Type | Offer | Deposits |")
        lines.append("|----------|------|-------|----------|")
        for row in group:
            title = row["ad_title"].replace("|", "\\|")
            t = infer_type(row["ad_title"], pfx)
            offer = (row["offer"] or "—").replace("|", "\\|")[:40]
            deps = str(row["deposits"]) if row["deposits"] else "—"
            lines.append(f"| {title} | {t} | {offer} | {deps} |")
        lines.extend(["", "---", ""])

    # Unknown section
    if unknown_rows:
        lines.append("## Unknown / To Review")
        lines.append("")
        lines.append(
            "_Префикс этих Ad Title не распознан. "
            "Проверь и добавь в `_KNOWN_PREFIXES` в `scripts/sync_creatives_from_excel.py` "
            "или опиши вручную._"
        )
        lines.append("")
        lines.append("| Ad Title | Prefix | Offer | Deposits |")
        lines.append("|----------|--------|-------|----------|")
        for row in unknown_rows:
            title = row["ad_title"].replace("|", "\\|")
            pfx = extract_prefix(row["ad_title"]) or "—"
            offer = (row["offer"] or "—").replace("|", "\\|")[:40]
            deps = str(row["deposits"]) if row["deposits"] else "—"
            lines.append(f"| {title} | {pfx} | {offer} | {deps} |")
        lines.extend(["", "---", ""])

    return "\n".join(lines)


# ── Main ───────────────────────────────────────────────────────────────────────

def _get_excel_path() -> str:
    """Получить путь к analytics Excel из env."""
    from config.paths import get_excel_path
    return get_excel_path()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sync creatives from analytics Excel → knowledge_base/creatives.md"
    )
    parser.add_argument(
        "--excel", "-e",
        default=None,
        help="Path to analytics Excel (default: FB_STATS_EXCEL from .env or config/paths.py)",
    )
    parser.add_argument(
        "--sheet",
        default=None,
        help="Sheet tab name (default: first sheet)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print stats without writing creatives.md",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()

    excel_path = args.excel or _get_excel_path()
    if not Path(excel_path).is_file():
        print(f"[ERROR] Excel file not found: {excel_path}", file=sys.stderr)
        sys.exit(1)

    print(f"[sync_creatives] Reading: {excel_path}")
    rows = read_creatives_from_excel(excel_path, args.sheet)
    print(f"[sync_creatives] Found {len(rows)} unique Ad Titles")

    # Quick stats
    known_count = sum(1 for r in rows if is_known_prefix(extract_prefix(r["ad_title"])))
    unknown_count = len(rows) - known_count
    print(f"  Known prefix: {known_count}  |  Unknown / To Review: {unknown_count}")

    if args.dry_run:
        print("\n[dry-run] creatives.md not written.")
        print("\nTop 10 by deposits:")
        for r in rows[:10]:
            pfx = extract_prefix(r["ad_title"])
            print(f"  [{pfx or '?'}] {r['ad_title'][:60]}  Deposits: {r['deposits']}")
        return

    _KB_CREATIVES_MD.parent.mkdir(parents=True, exist_ok=True)
    md = generate_creatives_md(rows)
    with open(_KB_CREATIVES_MD, "w", encoding="utf-8") as f:
        f.write(md)
    print(f"[sync_creatives] Wrote knowledge_base/creatives.md")


if __name__ == "__main__":
    main()
