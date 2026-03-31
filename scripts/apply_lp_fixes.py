"""Применить исправления LP URL из broken_lps_to_fix.txt → knowledge_base.json.

Формат файла:
  OFFER | СТАРЫЙ_URL -> НОВЫЙ_URL
  OFFER | СТАРЫЙ_URL -> REMOVE

После обновления KB выводит сводку изменений.
Для перепарсинга запусти: python scripts/parse_landing_pages.py
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

_PROJ_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJ_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJ_ROOT))

sys.stdout.reconfigure(encoding="utf-8")

_KB_JSON = _PROJ_ROOT / "config" / "knowledge_base.json"
_FIX_FILE = _PROJ_ROOT / "output" / "broken_lps_to_fix.txt"


def load_kb_json() -> dict:
    with open(_KB_JSON, encoding="utf-8") as f:
        return json.load(f)


def save_kb_json(data: dict) -> None:
    with open(_KB_JSON, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def parse_fix_file(path: Path) -> list[tuple[str, str, str]]:
    """Returns list of (offer_name, old_url, new_url_or_REMOVE)."""
    fixes = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^(.+?)\s*\|\s*(https?://\S+)\s*->\s*(.+)$", line)
        if not m:
            continue
        fixes.append((m.group(1).strip(), m.group(2).strip(), m.group(3).strip()))
    return fixes


def find_offer_key(offers: dict, name: str) -> str | None:
    for k in offers:
        if k.lower() == name.lower():
            return k
    return None


def main() -> None:
    if not _FIX_FILE.is_file():
        print(f"[ERROR] Fix file not found: {_FIX_FILE}", file=sys.stderr)
        sys.exit(1)

    fixes = parse_fix_file(_FIX_FILE)
    print(f"[apply_fixes] Parsed {len(fixes)} entries from fix file\n")

    kb = load_kb_json()
    offers = kb.setdefault("offers", {})

    updated = 0
    removed = 0
    skipped = 0

    for offer_name, old_url, new_url in fixes:
        key = find_offer_key(offers, offer_name)
        if key is None:
            print(f"  [!] Offer not found in KB: '{offer_name}' — skipping")
            skipped += 1
            continue

        urls: list = offers[key].get("lp_urls") or []
        if old_url not in urls:
            print(f"  [!] URL not in KB for '{offer_name}': {old_url[:60]}... — skipping")
            skipped += 1
            continue

        idx = urls.index(old_url)
        if new_url.upper() == "REMOVE":
            urls.pop(idx)
            offers[key]["lp_urls"] = urls
            print(f"  [REMOVE] {offer_name}: {old_url[:70]}")
            removed += 1
        else:
            urls[idx] = new_url
            offers[key]["lp_urls"] = urls
            print(f"  [UPDATE] {offer_name}")
            print(f"    old: {old_url[:80]}")
            print(f"    new: {new_url[:80]}")
            updated += 1

    print(f"\n--- Summary ---")
    print(f"  Updated: {updated}")
    print(f"  Removed: {removed}")
    print(f"  Skipped: {skipped}")

    if updated + removed == 0:
        print("\nNo changes to write.")
        return

    save_kb_json(kb)
    print(f"\n[apply_fixes] Saved knowledge_base.json")
    print(f"\nЗапусти теперь полный перезапуск парсера:")
    print(f"  python scripts/parse_landing_pages.py --delay 0.3")


if __name__ == "__main__":
    main()
