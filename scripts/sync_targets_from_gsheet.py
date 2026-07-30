"""Синхронизация таргетов из Google Sheet → knowledge_base/targets.md + config/knowledge_base.json.

Формат вкладки — КОЛОНОЧНЫЙ (перестроена пользователем 2026-07-30, задача Д3
плана; старый формат «чередующиеся строки в колонке A» больше не поддерживается —
прогон старого парсера по новой вкладке спаривал бы соседние имена как
(код, интересы) и портил KB):

    A: Targeting name — код таргета ("Day", "FX", "Luxury", ...)
    B: targeting 1    — FB-интересы через запятую (как их печатает RPA)
    C: define further — вторая группа (Narrow further / AND), опциональна
    D: aliases        — алиасы через запятую (опциональна, появится позже)
    E: audience       — описание аудитории (опциональна)
    F: top countries  — топ-страны через запятую (опциональна)
    G: notes          — заметки (опциональна)

Колонки D–G сегодня в вкладке отсутствуют — скрипт это переживает: значения
соответствующих полей сохраняются из текущего knowledge_base.json (как и
раньше). Как только колонки появятся в шите, они станут источником истины
без правок скрипта.

Скрипт:
  1. Читает Sheet через Google Sheets API (service account credentials).
  2. Парсит строки A–G (первая строка-заголовок пропускается).
  3. Мёрджит с текущим knowledge_base.json:
       - Обновляет fb_interest (B) и fb_interest_2 (C) из Sheet
       - aliases/audience/top_countries/notes — из D–G, если заполнены,
         иначе сохраняются существующие
       - Добавляет новые таргеты
  4. Перезаписывает knowledge_base/targets.md
  5. Перезаписывает config/knowledge_base.json (только секцию targets)
  6. Выводит diff добавленных/изменённых таргетов

Использование:
    python scripts/sync_targets_from_gsheet.py [--dry-run]
    python scripts/sync_targets_from_gsheet.py --sheet-id 1xYz... --sheet-name "Detailed targeting"
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

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
    """Создать Google Sheets API сервис.

    Делегирует общему билдеру gsheet_accounts._build_sheets_service —
    той же авторизацией, что живой загрузчик пикера (gsheet_targeting.py):
    OAuth (личный токен), при его отсутствии — service account. Раньше
    здесь был SA-only путь, но сервисному аккаунту доступ к общему шиту
    ещё не выдан (задача Д5 плана) — синк падал с 403, хотя дешборд
    рядом успешно читал ту же вкладку через OAuth.
    """
    try:
        from src.analysis_agent.gsheet_accounts import (
            _build_sheets_service as _shared_builder,
        )
    except ImportError as exc:
        print(f"[ERROR] gsheet_accounts недоступен: {exc}", file=sys.stderr)
        sys.exit(1)

    svc = _shared_builder(credentials_path)
    if svc is None:
        print(
            "[ERROR] Не удалось создать Sheets-сервис (нет ни OAuth-токена, "
            "ни service-account креды). Смотри scripts/README_GSHEET_SETUP.md",
            file=sys.stderr,
        )
        sys.exit(1)
    return svc


def resolve_sheet_name_by_gid(service, sheet_id: str, gid: int) -> str:
    """Resolve a numeric sheet GID to its tab title using the Sheets API."""
    meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    for sheet in meta.get("sheets", []):
        props = sheet.get("properties", {})
        if props.get("sheetId") == gid:
            return props["title"]
    titles = [s["properties"]["title"] for s in meta.get("sheets", [])]
    raise ValueError(
        f"Sheet with gid={gid} not found in spreadsheet {sheet_id!r}.\n"
        f"Available tabs: {titles}"
    )


def fetch_sheet_values(
    service, sheet_id: str, sheet_name: str
) -> List[List[str]]:
    """Вернуть значения колонок A–G листа как список строк."""
    range_notation = f"'{sheet_name}'!A:G"
    result = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=sheet_id, range=range_notation)
        .execute()
    )
    return result.get("values", [])


# ── Parsing ────────────────────────────────────────────────────────────────────

# Заголовок-мусор в ячейках интересов ("Interests: Adventure travel, ...") —
# презентация, не имя интереса. Та же чистка, что в gsheet_targeting.py.
import re as _re

_BUCKET_PREFIX_RE = _re.compile(r"^\s*(interests?|behaviou?rs?)\s*:\s*", _re.I)


def _clean_cell_names(raw: str) -> str:
    """Очистить ячейку с именами интересов: bucket-префикс, пробелы, запятые."""
    raw = _BUCKET_PREFIX_RE.sub("", (raw or "").strip())
    names = [n.strip() for n in raw.split(",")]
    return ", ".join(n for n in names if n)


def _looks_like_header(row: List[str]) -> bool:
    a = (row[0] if row else "").strip().lower()
    b = (row[1] if len(row) > 1 else "").strip().lower()
    return (
        a in ("targeting name", "name", "код", "название", "название таргетинга")
        or b in ("targeting 1", "targeting1", "таргетинг 1", "таргетинг")
    )


def parse_columnar_rows(rows: List[List[str]]) -> List[Dict[str, Any]]:
    """Парсить колоночный формат A–G (см. докстринг модуля).

    Возвращает список dict'ов: code, fb_interest, fb_interest_2, aliases
    (list), audience, top_countries (list), notes. Отсутствующие/пустые
    колонки → пустые значения (мердж сохранит существующие из KB).
    Дубликаты кода: первая строка выигрывает (детерминированно, как в
    gsheet_targeting.load_targeting_profiles).
    """

    def cell(row: List[str], i: int) -> str:
        return (row[i] if len(row) > i and row[i] else "").strip()

    out: List[Dict[str, Any]] = []
    seen: set = set()
    for idx, row in enumerate(rows):
        if idx == 0 and _looks_like_header(row):
            continue
        code = cell(row, 0)
        if not code or code in seen:
            continue
        seen.add(code)
        out.append({
            "code": code,
            "fb_interest": _clean_cell_names(cell(row, 1)),
            "fb_interest_2": _clean_cell_names(cell(row, 2)),
            "aliases": [a.strip() for a in cell(row, 3).split(",") if a.strip()],
            "audience": cell(row, 4),
            "top_countries": [c.strip() for c in cell(row, 5).split(",") if c.strip()],
            "notes": cell(row, 6),
        })
    return out


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
    sheet_rows: List[Dict[str, Any]],
) -> Tuple[Dict[str, Any], List[str], List[str]]:
    """Смержить таргеты из Sheet с существующими.

    Sheet — источник истины для fb_interest / fb_interest_2 всегда, и для
    aliases/audience/top_countries/notes — когда соответствующая колонка
    заполнена. Пустая/отсутствующая колонка НЕ затирает существующее
    значение (сегодня D–G в вкладке нет вовсе — все четыре поля
    сохраняются из KB, включая алиасы, на которых держится Д4-парсер).

    Returns:
        (merged_dict, added_codes, changed_codes)
    """
    merged = dict(existing)
    added: List[str] = []
    changed: List[str] = []

    for srow in sheet_rows:
        code = srow["code"]
        if code not in merged:
            merged[code] = {
                "label": code,
                "aliases": srow["aliases"],
                "fb_interest": srow["fb_interest"],
                "fb_interest_2": srow["fb_interest_2"],
                "audience": srow["audience"],
                "top_countries": srow["top_countries"],
                "notes": srow["notes"],
            }
            added.append(code)
            continue

        entry = merged[code]
        before = json.dumps(entry, ensure_ascii=False, sort_keys=True)
        entry["fb_interest"] = srow["fb_interest"]
        entry["fb_interest_2"] = srow["fb_interest_2"]
        if srow["aliases"]:
            # union: алиасы из шита добавляются к существующим, ничего
            # не удаляется (удаление — осознанное действие руками в KB)
            existing_lower = {a.lower() for a in entry.get("aliases", [])}
            entry.setdefault("aliases", []).extend(
                a for a in srow["aliases"] if a.lower() not in existing_lower
            )
        if srow["audience"]:
            entry["audience"] = srow["audience"]
        if srow["top_countries"]:
            entry["top_countries"] = srow["top_countries"]
        if srow["notes"]:
            entry["notes"] = srow["notes"]
        entry.setdefault("aliases", [])
        if json.dumps(entry, ensure_ascii=False, sort_keys=True) != before:
            changed.append(code)

    return merged, added, changed


# ── Markdown generation ────────────────────────────────────────────────────────

def _md_section(code: str, entry: Dict[str, Any]) -> str:
    label = entry.get("label") or code
    fb_interest = entry.get("fb_interest") or "—"
    audience = entry.get("audience") or "—"
    top_countries = entry.get("top_countries") or []
    notes = entry.get("notes") or ""
    aliases = entry.get("aliases") or []

    fb_interest_2 = entry.get("fb_interest_2") or ""

    lines = [f"## {code} — {label}", ""]
    lines.append(f"- **FB Interest:** {fb_interest}")
    if fb_interest_2:
        lines.append(f"- **FB Interest 2 (Define further / AND):** {fb_interest_2}")
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
        default=os.environ.get("GSHEET_TARGETS_SHEET", ""),
        help="Sheet tab name. Leave empty to auto-resolve via --gid",
    )
    parser.add_argument(
        "--gid",
        type=int,
        default=int(os.environ.get("GSHEET_TARGETS_GID", "0") or 0),
        help="Numeric sheet GID from URL (#gid=...). Used when --sheet-name is not set",
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

    service = _build_sheets_service(args.credentials)

    sheet_name = args.sheet_name
    if not sheet_name:
        if args.gid:
            print(f"[sync_targets] Resolving tab name for gid={args.gid} ...")
            sheet_name = resolve_sheet_name_by_gid(service, args.sheet_id, args.gid)
            print(f"[sync_targets] Resolved tab name: {sheet_name!r}")
        else:
            # Fall back to first sheet
            meta = service.spreadsheets().get(spreadsheetId=args.sheet_id).execute()
            sheets = meta.get("sheets", [])
            sheet_name = sheets[0]["properties"]["title"] if sheets else "Sheet1"
            print(f"[sync_targets] No tab name/gid specified, using first tab: {sheet_name!r}")

    print(f"[sync_targets] Fetching sheet: {args.sheet_id!r} / {sheet_name!r}")
    rows = fetch_sheet_values(service, args.sheet_id, sheet_name)
    print(f"[sync_targets] Got {len(rows)} rows from Sheet")

    sheet_rows = parse_columnar_rows(rows)
    print(f"[sync_targets] Parsed {len(sheet_rows)} target entries")

    kb = load_kb_json()
    existing_targets: Dict[str, Any] = kb.get("targets", {})

    merged, added, changed = merge_targets(existing_targets, sheet_rows)

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
