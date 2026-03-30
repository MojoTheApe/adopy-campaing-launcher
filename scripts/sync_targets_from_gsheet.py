"""Синхронизация таргетов из Google Sheet → knowledge_base/targets.md + config/knowledge_base.json.

Формат таблицы (чередующиеся строки в колонке A):
    Row 1: EF
    Row 2: Exchange trading, Currency exchange, Forex broker
    Row 3: HV
    Row 4: Investing, Wealth management, Financial planning
    ...

Скрипт:
  1. Читает Sheet через Google Sheets API (service account credentials).
  2. Парсит пары (код таргета, список FB-интересов).
  3. Мёрджит с текущим knowledge_base.json:
       - Обновляет fb_interest из Sheet
       - Сохраняет aliases, audience, notes, top_countries
       - Добавляет новые таргеты с пустыми полями
  4. Перезаписывает knowledge_base/targets.md
  5. Перезаписывает config/knowledge_base.json (только секцию targets)
  6. Выводит diff добавленных/изменённых таргетов

Использование:
    python scripts/sync_targets_from_gsheet.py
    python scripts/sync_targets_from_gsheet.py --sheet-id 1xYz... --sheet-name Sheet1
    python scripts/sync_targets_from_gsheet.py --credentials config/google_credentials.json
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
_KB_TARGETS_MD = _PROJ_ROOT / "knowledge_base" / "targets.md"


# ── Google Sheets helpers ──────────────────────────────────────────────────────

def _build_sheets_service(credentials_path: str):
    """Создать Google Sheets API сервис через service account."""
    try:
        from google.oauth2.service_account import Credentials
        from googleapiclient.discovery import build
    except ImportError:
        print(
            "[ERROR] Google API libraries не установлены.\n"
            "Запустите: pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib",
            file=sys.stderr,
        )
        sys.exit(1)

    creds_path = Path(credentials_path)
    if not creds_path.is_absolute():
        creds_path = _PROJ_ROOT / creds_path

    if not creds_path.is_file():
        print(
            f"[ERROR] Credentials file not found: {creds_path}\n"
            "Смотри scripts/README_GSHEET_SETUP.md для инструкций.",
            file=sys.stderr,
        )
        sys.exit(1)

    creds = Credentials.from_service_account_file(
        str(creds_path),
        scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"],
    )
    return build("sheets", "v4", credentials=creds)


def fetch_sheet_values(
    service, sheet_id: str, sheet_name: str
) -> List[List[str]]:
    """Вернуть все значения листа как список строк."""
    range_notation = f"{sheet_name}!A:A"
    result = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=sheet_id, range=range_notation)
        .execute()
    )
    return result.get("values", [])


# ── Parsing ────────────────────────────────────────────────────────────────────

def _normalize_interests_string(raw: str) -> str:
    """Нормализовать строку с FB-интересами.

    Правило: одиночная буква V (на отдельной строке или окружённая запятыми/пробелами)
    означает «Narrow Further» / «Define Further» в Detailed Targeting Facebook —
    т.е. аудитория должна соответствовать ОБЕИМ группам интересов (AND-логика).
    Заменяем V на читаемый маркер [AND].

    Примеры:
      "Finance, Investing, V, Luxury, Travel"  → "Finance, Investing [AND] Luxury, Travel"
      "Finance\nV\nLuxury"                      → "Finance [AND] Luxury"
    """
    import re
    # Replace newline-V-newline patterns
    normalized = re.sub(r'[\n\r]+\s*V\s*[\n\r]+', ' [AND] ', raw)
    # Replace comma-space-V-space-comma patterns
    normalized = re.sub(r',\s*V\s*,', ' [AND] ', normalized)
    # Replace trailing ", V" followed by newline or end
    normalized = re.sub(r',\s*V\s*$', '', normalized, flags=re.MULTILINE)
    # Replace standalone " V " with " [AND] " (V surrounded by spaces/commas)
    normalized = re.sub(r'(?<=[,\s])V(?=[,\s])', '[AND]', normalized)
    # Clean up extra spaces and commas around [AND]
    normalized = re.sub(r'\s*,\s*\[AND\]\s*,?\s*', ' [AND] ', normalized)
    normalized = re.sub(r'\s{2,}', ' ', normalized)
    return normalized.strip()


def parse_alternating_rows(rows: List[List[str]]) -> List[Tuple[str, List[str]]]:
    """Парсить чередующиеся строки: нечётная = код, чётная = интересы через запятую.

    Пустые строки пропускаются (сдвиг не нарушается — они не считаются за пару).

    Особое правило: одиночная буква V в строке интересов означает «Narrow Further»
    (Define Further в FB Detailed Targeting) — AND-логика между двумя группами.
    В результате заменяется на [AND] в строке fb_interest.
    """
    pairs: List[Tuple[str, List[str]]] = []
    pending_code: Optional[str] = None

    for row in rows:
        cell = row[0].strip() if row else ""
        if not cell:
            continue  # skip empty rows — don't break pairing

        if pending_code is None:
            pending_code = cell
        else:
            normalized = _normalize_interests_string(cell)
            interests = [i.strip() for i in normalized.split(",") if i.strip()]
            pairs.append((pending_code, interests))
            pending_code = None

    if pending_code is not None:
        # last target had no interests row
        pairs.append((pending_code, []))

    return pairs


# ── JSON KB helpers ────────────────────────────────────────────────────────────

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


# ── Merge logic ────────────────────────────────────────────────────────────────

def merge_targets(
    existing: Dict[str, Any],
    sheet_pairs: List[Tuple[str, List[str]]],
) -> Tuple[Dict[str, Any], List[str], List[str]]:
    """Смержить таргеты из Sheet с существующими.

    Returns:
        (merged_dict, added_codes, changed_codes)
    """
    merged = dict(existing)
    added: List[str] = []
    changed: List[str] = []

    for code, interests in sheet_pairs:
        fb_interest_str = ", ".join(interests)
        if code not in merged:
            merged[code] = {
                "label": code,
                "aliases": [],
                "fb_interest": fb_interest_str,
                "audience": "",
                "top_countries": [],
                "notes": "",
            }
            added.append(code)
        else:
            old_interest = merged[code].get("fb_interest", "")
            if old_interest != fb_interest_str:
                changed.append(code)
            # Preserve all existing fields; only update fb_interest
            merged[code]["fb_interest"] = fb_interest_str
            # Ensure aliases field exists
            if "aliases" not in merged[code]:
                merged[code]["aliases"] = []

    return merged, added, changed


# ── Markdown generation ────────────────────────────────────────────────────────

def _md_section(code: str, entry: Dict[str, Any]) -> str:
    label = entry.get("label") or code
    fb_interest = entry.get("fb_interest") or "—"
    audience = entry.get("audience") or "—"
    top_countries = entry.get("top_countries") or []
    notes = entry.get("notes") or ""
    aliases = entry.get("aliases") or []

    lines = [f"## {code} — {label}", ""]
    lines.append(f"- **FB Interest:** {fb_interest}")
    lines.append(f"- **Аудитория:** {audience}")
    if top_countries:
        lines.append(f"- **Топ-страны:** {', '.join(top_countries)}")
    if aliases:
        lines.append(f"- **Алиасы:** {', '.join(aliases)}")
    if notes:
        lines.append(f"- **Примечание:** {notes}")
    lines.append("")
    lines.append("---")
    lines.append("")
    return "\n".join(lines)


def generate_targets_md(targets: Dict[str, Any]) -> str:
    header = (
        "# Targets (Interest Codes)\n\n"
        "Справочник таргетов, используемых в названиях адсетов (поле Interest).\n"
        "_Авто-генерируется из Google Sheet. Редактируй aliases, audience, notes и top_countries "
        "в config/knowledge_base.json — они сохраняются при следующей синхронизации._\n\n---\n\n"
    )
    body = "".join(_md_section(code, entry) for code, entry in targets.items())
    return header + body


# ── Main ───────────────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sync targets from Google Sheet → knowledge_base/targets.md + config/knowledge_base.json"
    )
    parser.add_argument(
        "--sheet-id",
        default=os.environ.get("GSHEET_TARGETS_ID", ""),
        help="Google Sheet ID (or set GSHEET_TARGETS_ID in .env)",
    )
    parser.add_argument(
        "--sheet-name",
        default=os.environ.get("GSHEET_TARGETS_SHEET", "Sheet1"),
        help="Sheet tab name (default: Sheet1)",
    )
    parser.add_argument(
        "--credentials",
        default=os.environ.get("GOOGLE_CREDENTIALS_JSON", "config/google_credentials.json"),
        help="Path to service account JSON credentials",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print diff without writing files",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()

    if not args.sheet_id:
        print(
            "[ERROR] GSHEET_TARGETS_ID не задан.\n"
            "Укажи через --sheet-id или добавь в .env: GSHEET_TARGETS_ID=...\n"
            "Смотри scripts/README_GSHEET_SETUP.md",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"[sync_targets] Fetching sheet: {args.sheet_id!r} / {args.sheet_name!r}")
    service = _build_sheets_service(args.credentials)
    rows = fetch_sheet_values(service, args.sheet_id, args.sheet_name)
    print(f"[sync_targets] Got {len(rows)} rows from Sheet")

    sheet_pairs = parse_alternating_rows(rows)
    print(f"[sync_targets] Parsed {len(sheet_pairs)} target entries")

    kb = load_kb_json()
    existing_targets: Dict[str, Any] = kb.get("targets", {})

    merged, added, changed = merge_targets(existing_targets, sheet_pairs)

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

    # Write JSON
    kb["targets"] = merged
    save_kb_json(kb)
    print(f"[sync_targets] Wrote config/knowledge_base.json ({len(merged)} targets)")

    # Write MD
    _KB_TARGETS_MD.parent.mkdir(parents=True, exist_ok=True)
    with open(_KB_TARGETS_MD, "w", encoding="utf-8") as f:
        f.write(generate_targets_md(merged))
    print(f"[sync_targets] Wrote knowledge_base/targets.md")


if __name__ == "__main__":
    main()
