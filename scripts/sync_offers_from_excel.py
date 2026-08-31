"""Синхронизация офферов из вкладки «LP's» Excel → knowledge_base/offers.md + config/knowledge_base.json.

Вкладка LP's содержит колонки:
  - Brand offer → "Algobi - AI Trader AR LPs for - UAE - Media" (источник канонических имён)
  - Lp's        → один или несколько LP URL, разделённых переносом строки
  - Stock       → короткое название (используется только как резервный источник если Brand offer не парсится)
  - Country 1   → код страны (игнорируется при группировке)

Алгоритм именования:
  1. Из каждой строки Brand offer вырезается имя оффера (между "Algobi -" и "LPs for").
  2. Все извлечённые имена группируются по кратчайшему prefix-совпадению:
     "AI Trader AR", "AI Trader Dec 25" → канонический "AI Trader".
  3. Явные слияния через MERGE_RULES: TradeGPT→Trade GPT, Amzon→Amazon и т.д.
  4. Более длинные варианты сохраняются как aliases.

Использование:
    python scripts/sync_offers_from_excel.py
    python scripts/sync_offers_from_excel.py --excel path/to/file.xlsx
    python scripts/sync_offers_from_excel.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

_PROJ_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJ_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJ_ROOT))

try:
    from config.paths import load_env

    load_env()
except Exception:
    pass

_KB_JSON = _PROJ_ROOT / "config" / "knowledge_base.json"
_KB_OFFERS_MD = _PROJ_ROOT / "knowledge_base" / "offers.md"

_LP_SHEET = "LP's"
_COL_BRAND = "Brand offer"
_COL_LPS = "Lp's"
_COL_STOCK = "Stock"

# ── Explicit merge rules: raw_name_lower → canonical_name ─────────────────────
# Keys are lowercase; values are the desired canonical form.
MERGE_RULES: Dict[str, str] = {
    # Spelling / case fixes
    "tradegpt":                   "Trade GPT",
    "amzon":                      "Amazon",
    "fab":                        "FAB",
    "ai trader":                  "AI Trader",    # case
    "ai trading":                 "AI Trader",    # variant
    "nvidia":                     "Nvidia",
    "qatar energy":               "Qatar Energy",
    "sqm":                        "SQM",

    # "AR - X" prefix  → base offer (AR = Arabic/UAE region prefix)
    "ait ar":                     "AI Trader",
    "ar ai trader text":          "AI Trader",
    "ar - $ trade gpt doctor":    "Trade GPT",
    "ar -trade gpt - $":          "Trade GPT",
    "ar adnoc news":              "Adnoc",
    "ar - du 1":                  "DU",
    "ar - du 2":                  "DU",
    "ar -hci holding":            "HCI",
    "al ansari ar":               "Al Ansari",

    # Region prefix  → base offer
    "kw - trade gpt":             "Trade GPT",
    "uae - trade gpt c1":         "Trade GPT",
    "uae - trade gpt c2":         "Trade GPT",
    "uae - trade gpt c3":         "Trade GPT",
    "uae - trade gpt c4":         "Trade GPT",
    "uae -trade gpt - aed":       "Trade GPT",
    "uae - open ai chip":         "Open AI",
    "uae -open ai black - aed":   "Open AI",
    "uae -open ai person - aed":  "Open AI",
    "uae -open ai white - aed":   "Open AI",
    "ec ai trader robot":         "AI Trader",
    "gt ai trader robot":         "AI Trader",
    "mx ai trader robot":         "AI Trader",
    "es ai trader 09.02 test":    "AI Trader",
    "es ait 09.02 test":          "AI Trader",
    "es arg ypf":                 "YPF",

    # Broker prefix (SuccessFX / Fxroad) → base offer
    "successfx - amazon":         "Amazon",
    "successfx - avianca":        "Avianca",
    "successfx - bancolombia":    "Bancolombia",
    "successfx - bancolombia new":"Bancolombia",
    "success fx - bancolombia flag": "Bancolombia",
    "successfx - ecopetrol":      "Ecopetrol",
    "successfx - enel":           "Enel",
    "successfx - geb":            "GEB",
    "successfx - gold":           "Gold",
    "successfx - grupo aval":     "Grupo Aval",
    "successfx - rappi":          "Rappi",
    "successfx - trade gpt 4":    "Trade GPT",
    "successfx - ai trader es long": "AI Trader",
    "fxroad - tradegpt":          "Trade GPT",
    "latam - tradegpt ophelia":   "Trade GPT",

    # Equity prefix → base offer
    "equity - epam":              "EPAM",
    "equity - grupo nutresa":     "Grupo Nutresa",

    # Other variants
    "ait 29.01":                  "AI Trader",
    "ait tva lp's":               "Trade Vector AI",
    "amz test 17.02":             "Amazon",
    "tradegpt doctor":            "Trade GPT",
    "cervesaria nacional test":   "Cervesaria Nacional",
    "boubyan bank kw":            "Boubyan Bank",
    "aluar arg":                  "Aluar",
    "alec":                       "ALEC",
    "sql":                        "SQM",
}


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


# ── Step 1: Extract offer name from Brand offer cell ──────────────────────────

def _extract_offer_name(brand_offer: str) -> Optional[str]:
    """Extract the offer name from a Brand offer cell.

    Input:  "Algobi -  AI Trader AR LPs for    - UAE - Media"
    Output: "AI Trader AR"

    Returns None if the cell doesn't match the expected pattern.
    """
    raw = str(brand_offer).strip()
    # Remove leading "Algobi" and surrounding dashes/spaces
    raw = re.sub(r"^Algobi\s*-\s*", "", raw, flags=re.IGNORECASE).strip()
    # Remove "LPs for ..." / "LP's for ..." and everything after
    raw = re.sub(r"LP'?s?\s+for.*$", "", raw, flags=re.IGNORECASE).strip()
    # Strip trailing dashes/spaces left over
    raw = raw.strip(" -").strip()
    return raw if raw and raw.lower() != "nan" else None


# ── Step 2: Group names by shortest word-boundary prefix ─────────────────────

def _group_by_shortest_prefix(names: List[str]) -> Dict[str, List[str]]:
    """Group offer name variants by their shortest canonical prefix.

    "AI Trader", "AI Trader AR", "AI Trader Dec 25" → {"AI Trader": ["AI Trader AR", "AI Trader Dec 25"]}
    Names that are not prefixes of anything stay as singletons.

    Returns {canonical: [alias1, alias2, ...]}  (canonical not included in alias list)
    """
    unique = list(dict.fromkeys(names))  # deduplicate, preserve order
    # Sort shortest first so we find the canonical quickly
    by_len = sorted(unique, key=lambda n: (len(n), n.lower()))

    assigned: Set[str] = set()
    groups: Dict[str, List[str]] = {}  # canonical → [aliases]

    for candidate in by_len:
        if candidate in assigned:
            continue
        # Collect all names that start with candidate (word boundary)
        prefix_lower = candidate.lower()
        members = [
            n for n in by_len
            if n not in assigned and (
                n.lower() == prefix_lower
                or n.lower().startswith(prefix_lower + " ")
            )
        ]
        for m in members:
            assigned.add(m)
        aliases = [m for m in members if m != candidate]
        groups[candidate] = aliases

    return groups


# ── Step 3: Apply MERGE_RULES ─────────────────────────────────────────────────

def _apply_merge_rules(
    groups: Dict[str, List[str]],
) -> Dict[str, List[str]]:
    """Merge groups according to MERGE_RULES, returning updated groups dict."""
    result: Dict[str, List[str]] = {}

    for canon, aliases in groups.items():
        target = MERGE_RULES.get(canon.lower(), canon)
        # Normalise target capitalisation: if target already exists, keep it
        target_key = next(
            (k for k in result if k.lower() == target.lower()), target
        )
        if target_key not in result:
            result[target_key] = []
        # Add canon as alias if it was remapped
        if canon != target_key and canon not in result[target_key]:
            result[target_key].append(canon)
        # Add all existing aliases
        for a in aliases:
            if a not in result[target_key] and a != target_key:
                result[target_key].append(a)

    return result


# ── Excel reading ──────────────────────────────────────────────────────────────

def _parse_urls(raw: str) -> List[str]:
    urls = []
    for line in raw.replace("\r", "\n").split("\n"):
        u = line.strip()
        if u and u.lower().startswith("http"):
            urls.append(u)
    return urls


def read_lps_sheet(path: str) -> Tuple[Dict[str, List[str]], Dict[str, List[str]]]:
    """Read LP's sheet.

    Returns:
        offers_urls: {canonical_name: [url, ...]}
        offers_aliases: {canonical_name: [alias, ...]}
    """
    try:
        import pandas as pd
    except ImportError:
        print("[ERROR] pandas не установлен: pip install pandas openpyxl", file=sys.stderr)
        sys.exit(1)

    df = pd.read_excel(path, sheet_name=_LP_SHEET, engine="openpyxl")
    df.columns = [str(c).strip() for c in df.columns]

    # Require at minimum Lp's column; Brand offer preferred, Stock as fallback
    if _COL_LPS not in df.columns:
        print(f"[ERROR] Колонка '{_COL_LPS}' не найдена.", file=sys.stderr)
        sys.exit(1)

    use_brand = _COL_BRAND in df.columns
    if not use_brand:
        print(f"[WARN] Колонка '{_COL_BRAND}' не найдена — используем '{_COL_STOCK}'")

    # ── Pass 1: collect (raw_name, [urls]) per row ────────────────────────────
    raw_entries: List[Tuple[str, List[str]]] = []
    for _, row in df.iterrows():
        if use_brand:
            name = _extract_offer_name(str(row.get(_COL_BRAND, "") or ""))
        else:
            name = str(row.get(_COL_STOCK, "") or "").strip()
            name = name if name and name.lower() != "nan" else None

        if not name:
            # Fallback to Stock
            name = str(row.get(_COL_STOCK, "") or "").strip()
            name = name if name and name.lower() != "nan" else None
        if not name:
            continue

        lp_raw = str(row.get(_COL_LPS, "") or "").strip()
        raw_entries.append((name, _parse_urls(lp_raw)))

    # ── Pass 2: group names by shortest prefix ────────────────────────────────
    all_names = [n for n, _ in raw_entries]
    prefix_groups = _group_by_shortest_prefix(all_names)
    # Apply explicit merge rules
    prefix_groups = _apply_merge_rules(prefix_groups)

    # Build reverse map: any_variant_lower → canonical
    variant_to_canon: Dict[str, str] = {}
    for canon, aliases in prefix_groups.items():
        variant_to_canon[canon.lower()] = canon
        for a in aliases:
            variant_to_canon[a.lower()] = canon

    # ── Pass 3: aggregate URLs per canonical name ─────────────────────────────
    offers_urls: Dict[str, List[str]] = {}
    seen_urls: Dict[str, Set[str]] = {}

    for name, urls in raw_entries:
        # Resolve canonical
        canon = variant_to_canon.get(name.lower(), name)
        if canon not in offers_urls:
            offers_urls[canon] = []
            seen_urls[canon] = set()
        for u in urls:
            if u not in seen_urls[canon]:
                offers_urls[canon].append(u)
                seen_urls[canon].add(u)

    return offers_urls, prefix_groups


# ── Merge with existing KB ─────────────────────────────────────────────────────

def merge_offers(
    existing: Dict[str, Any],
    excel_urls: Dict[str, List[str]],
    excel_aliases: Dict[str, List[str]],
) -> Tuple[Dict[str, Any], List[str], List[str]]:
    """Merge offers from Excel into existing KB offers dict.

    Returns: (merged, added_names, changed_names)
    """
    existing_lower: Dict[str, str] = {k.lower(): k for k in existing}

    # Remove old KB entries that are now merged into a different canonical
    # (e.g. "AI trader" merged into "AI Trader" — remove "AI trader" entry)
    all_new_canonicals_lower = {k.lower() for k in excel_urls}
    to_remove = []
    for k in list(existing.keys()):
        if k.lower() not in all_new_canonicals_lower:
            # Check if this key is now an alias of some new canonical
            for canon_lower, canon in {v.lower(): v for v in excel_urls}.items():
                aliases_lower = [a.lower() for a in excel_aliases.get(canon, [])]
                if k.lower() in aliases_lower:
                    to_remove.append((k, canon))
                    break

    merged = dict(existing)
    consolidated: List[str] = []
    for old_key, new_canon in to_remove:
        old_entry = merged.pop(old_key)
        # Migrate old lp_urls into the new canonical
        old_urls = old_entry.get("lp_urls") or ([old_entry["lp_url"]] if old_entry.get("lp_url") else [])
        if new_canon in merged:
            existing_urls = set(merged[new_canon].get("lp_urls") or [])
            merged[new_canon]["lp_urls"] = list(existing_urls | set(old_urls))
        consolidated.append(f"{old_key} → {new_canon}")

    if consolidated:
        print(f"  [CONSOLIDATE] {'; '.join(consolidated)}")

    added: List[str] = []
    changed: List[str] = []
    existing_lower = {k.lower(): k for k in merged}

    for name, urls in excel_urls.items():
        aliases = excel_aliases.get(name, [])
        key = name.lower()

        if key in existing_lower:
            canon = existing_lower[key]
            old_urls = merged[canon].get("lp_urls") or (
                [merged[canon]["lp_url"]] if merged[canon].get("lp_url") else []
            )
            if "lp_url" in merged[canon]:
                del merged[canon]["lp_url"]
            if set(old_urls) != set(urls):
                changed.append(canon)
            merged[canon]["lp_urls"] = urls
            # Merge aliases (preserve existing + add new)
            existing_aliases: List[str] = merged[canon].get("aliases") or []
            new_aliases = list(dict.fromkeys(existing_aliases + aliases))
            merged[canon]["aliases"] = new_aliases
        else:
            merged[name] = {
                "label": name,
                "aliases": aliases,
                "lp_urls": urls,
                "language": [],
                "top_countries": [],
                "audience": "",
                "usp": "",
                "notes": "",
            }
            added.append(name)

    # Migrate any remaining single lp_url → lp_urls
    for k, entry in merged.items():
        if isinstance(entry, dict) and "lp_url" in entry and "lp_urls" not in entry:
            old = entry.pop("lp_url")
            entry["lp_urls"] = [old] if old else []

    return merged, added, changed


# ── Markdown generation ────────────────────────────────────────────────────────

def _md_offer_section(name: str, entry: Dict[str, Any]) -> str:
    lp_urls: List[str] = entry.get("lp_urls") or []
    aliases: List[str] = entry.get("aliases") or []
    language = entry.get("language") or []
    top_countries = entry.get("top_countries") or []
    audience = entry.get("audience") or "—"
    usp = entry.get("usp") or "—"
    notes = entry.get("notes") or ""

    lines = [f"## {name}", ""]
    if aliases:
        lines.append(f"- **Алиасы:** {', '.join(aliases)}")
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
    excel_urls, excel_aliases = read_lps_sheet(str(excel_path))

    total_urls = sum(len(v) for v in excel_urls.values())
    print(f"[sync_offers] Parsed {len(excel_urls)} canonical offers, {total_urls} total LP URLs")
    print(f"\n--- Offer groups ---")
    for canon in sorted(excel_urls):
        aliases = excel_aliases.get(canon, [])
        url_count = len(excel_urls[canon])
        alias_str = f"  aliases: {aliases}" if aliases else ""
        print(f"  {canon!r:30s}  {url_count} URLs{alias_str}")

    kb = load_kb_json()
    existing_offers: Dict[str, Any] = kb.get("offers", {})

    print(f"\n--- Merging ---")
    merged, added, changed = merge_offers(existing_offers, excel_urls, excel_aliases)

    if added:
        print(f"  Added   ({len(added)}): {', '.join(added)}")
    if changed:
        print(f"  Changed ({len(changed)}): {', '.join(changed)}")
    if not added and not changed:
        print("  No URL changes.")
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
