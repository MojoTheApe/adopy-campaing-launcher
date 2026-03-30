"""Синхронизация офферов из вкладки «LP's» Excel → knowledge_base/offers.md + config/knowledge_base.json.

Вкладка LP's содержит колонки:
  - Stock      → название оффера (канонический ключ после нормализации)
  - Lp's       → один или несколько LP URL, разделённых переносом строки
  - Brand offer → описание кампании (сохраняется как примечание, не используется как ключ)
  - Country 1  → код страны (игнорируется при группировке)

Скрипт:
  1. Читает вкладку «LP's» из Excel (путь через --excel или OFFERS_EXCEL в .env).
  2. Группирует строки по нормализованному названию оффера (Stock, case-insensitive).
     Дубликаты с разным регистром объединяются в один оффер (берётся первый вариант написания).
  3. Собирает список уникальных URL (lp_urls) для каждого оффера.
  4. Мёрджит с текущим knowledge_base.json.
  5. Перезаписывает knowledge_base/offers.md и config/knowledge_base.json.

Использование:
    python scripts/sync_offers_from_excel.py
    python scripts/sync_offers_from_excel.py --excel path/to/file.xlsx
    python scripts/sync_offers_from_excel.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import sys
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

_KB_JSON = _PROJ_ROOT / "config" / "knowledge_base.json"
_KB_OFFERS_MD = _PROJ_ROOT / "knowledge_base" / "offers.md"

_LP_SHEET = "LP's"
_COL_STOCK = "Stock"
_COL_LPS = "Lp's"
_COL_COUNTRY = "Country 1"


# ── KB JSON helpers ────────────────────────────────────────────────────────────

def load_kb_json() -> Dict[str, Any]:
    if not _KB_JSON.is_file():
        return {"targets": {}, "offers": {}}
    try:
        with open(_KB_JSON, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"targets": {}, "offers": {}}


def save_kb_json(data: Dict[str, Any]) -> None:
    _KB_JSON.parent.mkdir(parents=True, exist_ok=True)
    with open(_KB_JSON, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ── Excel reading ──────────────────────────────────────────────────────────────

def _parse_urls(raw: str) -> List[str]:
    """Split a multi-line URL cell into individual URLs."""
    urls = []
    for line in raw.replace("\r", "\n").split("\n"):
        u = line.strip()
        if u and u.lower().startswith("http"):
            urls.append(u)
    return urls


def read_lps_sheet(path: str) -> Dict[str, List[str]]:
    """Read LP's sheet and return {canonical_offer_name: [url, ...]} mapping.

    Case-insensitive grouping: 'AI Trader' and 'AI trader' → same key.
    Canonical name = first encountered capitalisation.
    """
    try:
        import pandas as pd
    except ImportError:
        print("[ERROR] pandas не установлен: pip install pandas openpyxl", file=sys.stderr)
        sys.exit(1)

    df = pd.read_excel(path, sheet_name=_LP_SHEET, engine="openpyxl")
    df.columns = [str(c).strip() for c in df.columns]

    # Validate required columns
    missing = [c for c in (_COL_STOCK, _COL_LPS) if c not in df.columns]
    if missing:
        print(f"[ERROR] Колонки не найдены в вкладке {_LP_SHEET!r}: {missing}", file=sys.stderr)
        print(f"  Доступные колонки: {list(df.columns)}", file=sys.stderr)
        sys.exit(1)

    # Group: lower(name) → (canonical_name, [urls])
    grouped: Dict[str, Tuple[str, List[str]]] = {}
    seen_urls: Dict[str, set] = {}  # key → set of urls for dedup

    for _, row in df.iterrows():
        stock_raw = str(row.get(_COL_STOCK, "") or "").strip()
        lp_raw = str(row.get(_COL_LPS, "") or "").strip()

        if not stock_raw or stock_raw.lower() == "nan":
            continue

        key = stock_raw.lower()
        urls = _parse_urls(lp_raw)

        if key not in grouped:
            grouped[key] = (stock_raw, [])
            seen_urls[key] = set()

        for u in urls:
            if u not in seen_urls[key]:
                grouped[key][1].append(u)
                seen_urls[key].add(u)

    return {canonical: urls for canonical, urls in grouped.values()}


# ── Merge logic ────────────────────────────────────────────────────────────────

def merge_offers(
    existing: Dict[str, Any],
    excel_offers: Dict[str, List[str]],
) -> Tuple[Dict[str, Any], List[str], List[str]]:
    """Merge offers from Excel into existing KB offers dict.

    Returns: (merged, added_names, changed_names)
    """
    # Build lowercase → canonical key map for existing
    existing_lower: Dict[str, str] = {k.lower(): k for k in existing}

    merged = dict(existing)
    added: List[str] = []
    changed: List[str] = []

    for name, urls in excel_offers.items():
        key = name.lower()
        if key in existing_lower:
            canon = existing_lower[key]
            old_urls = merged[canon].get("lp_urls") or (
                [merged[canon]["lp_url"]] if merged[canon].get("lp_url") else []
            )
            # Migrate single lp_url → lp_urls if needed
            if "lp_url" in merged[canon]:
                del merged[canon]["lp_url"]
            if set(old_urls) != set(urls):
                changed.append(canon)
            merged[canon]["lp_urls"] = urls
        else:
            merged[name] = {
                "label": name,
                "lp_urls": urls,
                "language": [],
                "top_countries": [],
                "audience": "",
                "usp": "",
                "notes": "",
            }
            added.append(name)

    # Also migrate any remaining single lp_url entries to lp_urls
    for k, entry in merged.items():
        if isinstance(entry, dict) and "lp_url" in entry and "lp_urls" not in entry:
            old = entry.pop("lp_url")
            entry["lp_urls"] = [old] if old else []

    return merged, added, changed


# ── Markdown generation ────────────────────────────────────────────────────────

def _md_offer_section(name: str, entry: Dict[str, Any]) -> str:
    label = entry.get("label") or name
    lp_urls: List[str] = entry.get("lp_urls") or []
    language = entry.get("language") or []
    top_countries = entry.get("top_countries") or []
    audience = entry.get("audience") or "—"
    usp = entry.get("usp") or "—"
    notes = entry.get("notes") or ""

    lines = [f"## {name}", ""]
    if lp_urls:
        if len(lp_urls) == 1:
            lines.append(f"- **LP URL:** {lp_urls[0]}")
        else:
            lines.append(f"- **LP URLs ({len(lp_urls)}):**")
            for u in lp_urls:
                lines.append(f"  - {u}")
    else:
        lines.append("- **LP URLs:** _не настроены_")

    if language:
        lang_val = ", ".join(language) if isinstance(language, list) else str(language)
        lines.append(f"- **Язык:** {lang_val}")
    lines.append(f"- **Аудитория:** {audience}")
    lines.append(f"- **УТП:** {usp}")
    if top_countries:
        lines.append(f"- **Топ-страны:** {', '.join(top_countries)}")
    if notes:
        lines.append(f"- **Примечание:** {notes}")
    lines.extend(["", "---", ""])
    return "\n".join(lines)


def generate_offers_md(offers: Dict[str, Any]) -> str:
    from datetime import datetime
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    header = (
        "# Offers\n\n"
        "Справочник офферов из вкладки **LP's** файла Upload FB.xlsx.\n"
        f"_Последнее обновление: {ts}_\n\n---\n\n"
    )
    body = "".join(_md_offer_section(name, entry) for name, entry in sorted(offers.items()))
    return header + body


# ── Main ───────────────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sync offers from LP's sheet → knowledge_base/offers.md + config/knowledge_base.json"
    )
    parser.add_argument(
        "--excel", "-e",
        default=os.environ.get("OFFERS_EXCEL", ""),
        help="Path to Excel file (or set OFFERS_EXCEL in .env)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print diff without writing files",
    )
    return parser.parse_args()


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    args = _parse_args()

    if not args.excel:
        print(
            "[ERROR] Путь к Excel не задан.\n"
            "Укажи через --excel или добавь в .env: OFFERS_EXCEL=path/to/file.xlsx",
            file=sys.stderr,
        )
        sys.exit(1)

    excel_path = Path(args.excel)
    if not excel_path.is_absolute():
        excel_path = _PROJ_ROOT / excel_path

    if not excel_path.is_file():
        print(f"[ERROR] Файл не найден: {excel_path}", file=sys.stderr)
        sys.exit(1)

    print(f"[sync_offers] Reading sheet '{_LP_SHEET}' from: {excel_path}")
    excel_offers = read_lps_sheet(str(excel_path))
    total_urls = sum(len(v) for v in excel_offers.values())
    print(f"[sync_offers] Found {len(excel_offers)} offers, {total_urls} total LP URLs")

    kb = load_kb_json()
    existing_offers: Dict[str, Any] = kb.get("offers", {})

    merged, added, changed = merge_offers(existing_offers, excel_offers)

    print(f"\n--- Diff ---")
    if added:
        print(f"  Added   ({len(added)}): {', '.join(added)}")
    if changed:
        print(f"  Changed ({len(changed)}): {', '.join(changed)}")
    if not added and not changed:
        print("  No changes.")
    print()

    if args.dry_run:
        print("[dry-run] No files written.")
        return

    kb["offers"] = merged
    save_kb_json(kb)
    print(f"[sync_offers] Wrote config/knowledge_base.json ({len(merged)} offers)")

    _KB_OFFERS_MD.parent.mkdir(parents=True, exist_ok=True)
    with open(_KB_OFFERS_MD, "w", encoding="utf-8") as f:
        f.write(generate_offers_md(merged))
    print(f"[sync_offers] Wrote knowledge_base/offers.md")


if __name__ == "__main__":
    main()
