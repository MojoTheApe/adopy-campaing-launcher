"""SQLite storage for context pack knowledge."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any


class ContextPackStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS offers (
                    name TEXT PRIMARY KEY,
                    aliases_json TEXT NOT NULL DEFAULT '[]',
                    audience TEXT NOT NULL DEFAULT '',
                    usp TEXT NOT NULL DEFAULT '',
                    top_countries_json TEXT NOT NULL DEFAULT '[]',
                    notes TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS targets (
                    code TEXT PRIMARY KEY,
                    label TEXT NOT NULL DEFAULT '',
                    aliases_json TEXT NOT NULL DEFAULT '[]',
                    fb_interest TEXT NOT NULL DEFAULT '',
                    audience TEXT NOT NULL DEFAULT '',
                    top_countries_json TEXT NOT NULL DEFAULT '[]',
                    notes TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS creatives (
                    name TEXT PRIMARY KEY,
                    creative_type TEXT NOT NULL DEFAULT '',
                    aliases_json TEXT NOT NULL DEFAULT '[]',
                    offer_hints_json TEXT NOT NULL DEFAULT '[]',
                    deposits_summary INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS ad_texts (
                    key TEXT PRIMARY KEY,
                    body TEXT NOT NULL DEFAULT '',
                    language TEXT NOT NULL DEFAULT '',
                    hook TEXT NOT NULL DEFAULT '',
                    cta TEXT NOT NULL DEFAULT '',
                    offer TEXT NOT NULL DEFAULT '',
                    risk_flags_json TEXT NOT NULL DEFAULT '[]',
                    source TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS creative_text_links (
                    creative_name TEXT NOT NULL,
                    ad_text_key TEXT NOT NULL,
                    PRIMARY KEY (creative_name, ad_text_key)
                );
                CREATE TABLE IF NOT EXISTS creative_analysis (
                    creative_name TEXT PRIMARY KEY,
                    visual_summary TEXT NOT NULL DEFAULT '',
                    on_screen_text TEXT NOT NULL DEFAULT '',
                    language TEXT NOT NULL DEFAULT '',
                    hook TEXT NOT NULL DEFAULT '',
                    cta TEXT NOT NULL DEFAULT '',
                    visual_tags_json TEXT NOT NULL DEFAULT '[]',
                    risk_flags_json TEXT NOT NULL DEFAULT '[]',
                    confidence REAL NOT NULL DEFAULT 0,
                    provider TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS manual_overrides (
                    entity_type TEXT NOT NULL,
                    entity_key TEXT NOT NULL,
                    field_name TEXT NOT NULL,
                    value TEXT NOT NULL,
                    PRIMARY KEY (entity_type, entity_key, field_name)
                );
                CREATE TABLE IF NOT EXISTS performance_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    country TEXT NOT NULL,
                    offer TEXT NOT NULL,
                    target TEXT NOT NULL,
                    creative TEXT NOT NULL,
                    ad_text_key TEXT NOT NULL DEFAULT '',
                    spend REAL NOT NULL DEFAULT 0,
                    leads INTEGER NOT NULL DEFAULT 0,
                    deposits INTEGER NOT NULL DEFAULT 0,
                    cpa REAL NOT NULL DEFAULT 0,
                    roi REAL NOT NULL DEFAULT 0,
                    source TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS test_learnings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    country TEXT NOT NULL DEFAULT '',
                    offer TEXT NOT NULL DEFAULT '',
                    target TEXT NOT NULL DEFAULT '',
                    creative TEXT NOT NULL DEFAULT '',
                    learning TEXT NOT NULL,
                    confidence TEXT NOT NULL DEFAULT 'medium',
                    tags_json TEXT NOT NULL DEFAULT '[]'
                );
                """
            )

    def upsert_offer(self, name: str, payload: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO offers(name, aliases_json, audience, usp, top_countries_json, notes)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    aliases_json=excluded.aliases_json,
                    audience=excluded.audience,
                    usp=excluded.usp,
                    top_countries_json=excluded.top_countries_json,
                    notes=excluded.notes
                """,
                (
                    name,
                    _json(payload.get("aliases", [])),
                    str(payload.get("audience", "") or ""),
                    str(payload.get("usp", payload.get("УТП", "")) or ""),
                    _json(payload.get("top_countries", [])),
                    str(payload.get("notes", "") or ""),
                ),
            )

    def upsert_target(self, code: str, payload: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO targets(code, label, aliases_json, fb_interest, audience, top_countries_json, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(code) DO UPDATE SET
                    label=excluded.label,
                    aliases_json=excluded.aliases_json,
                    fb_interest=excluded.fb_interest,
                    audience=excluded.audience,
                    top_countries_json=excluded.top_countries_json,
                    notes=excluded.notes
                """,
                (
                    code,
                    str(payload.get("label", code) or code),
                    _json(payload.get("aliases", [])),
                    str(payload.get("fb_interest", "") or ""),
                    str(payload.get("audience", "") or ""),
                    _json(payload.get("top_countries", [])),
                    str(payload.get("notes", "") or ""),
                ),
            )

    def upsert_creative(self, name: str, payload: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO creatives(name, creative_type, aliases_json, offer_hints_json, deposits_summary)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    creative_type=excluded.creative_type,
                    aliases_json=excluded.aliases_json,
                    offer_hints_json=excluded.offer_hints_json,
                    deposits_summary=excluded.deposits_summary
                """,
                (
                    name,
                    str(payload.get("creative_type", "") or ""),
                    _json(payload.get("aliases", [])),
                    _json(payload.get("offer_hints", [])),
                    int(payload.get("deposits_summary", 0) or 0),
                ),
            )

    def upsert_ad_text(self, key: str, payload: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO ad_texts(key, body, language, hook, cta, offer, risk_flags_json, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    body=excluded.body,
                    language=excluded.language,
                    hook=excluded.hook,
                    cta=excluded.cta,
                    offer=excluded.offer,
                    risk_flags_json=excluded.risk_flags_json,
                    source=excluded.source
                """,
                (
                    key,
                    str(payload.get("body", "") or ""),
                    str(payload.get("language", "") or ""),
                    str(payload.get("hook", "") or ""),
                    str(payload.get("cta", "") or ""),
                    str(payload.get("offer", "") or ""),
                    _json(payload.get("risk_flags", [])),
                    str(payload.get("source", "") or ""),
                ),
            )

    def link_creative_text(self, creative_name: str, ad_text_key: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO creative_text_links(creative_name, ad_text_key)
                VALUES (?, ?)
                """,
                (creative_name, ad_text_key),
            )

    def upsert_creative_analysis(self, creative_name: str, payload: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO creative_analysis(
                    creative_name, visual_summary, on_screen_text, language, hook, cta,
                    visual_tags_json, risk_flags_json, confidence, provider
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(creative_name) DO UPDATE SET
                    visual_summary=excluded.visual_summary,
                    on_screen_text=excluded.on_screen_text,
                    language=excluded.language,
                    hook=excluded.hook,
                    cta=excluded.cta,
                    visual_tags_json=excluded.visual_tags_json,
                    risk_flags_json=excluded.risk_flags_json,
                    confidence=excluded.confidence,
                    provider=excluded.provider
                """,
                (
                    creative_name,
                    str(payload.get("visual_summary", "") or ""),
                    str(payload.get("on_screen_text", "") or ""),
                    str(payload.get("language", "") or ""),
                    str(payload.get("hook", "") or ""),
                    str(payload.get("cta", "") or ""),
                    _json(payload.get("visual_tags", [])),
                    _json(payload.get("risk_flags", [])),
                    float(payload.get("confidence", 0) or 0),
                    str(payload.get("provider", "") or ""),
                ),
            )

    def upsert_manual_override(self, entity_type: str, entity_key: str, field_name: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO manual_overrides(entity_type, entity_key, field_name, value)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(entity_type, entity_key, field_name) DO UPDATE SET value=excluded.value
                """,
                (entity_type, entity_key, field_name, value),
            )

    def insert_performance_snapshot(self, payload: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO performance_snapshots(
                    country, offer, target, creative, ad_text_key, spend, leads, deposits, cpa, roi, source
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(payload.get("country", "") or ""),
                    str(payload.get("offer", "") or ""),
                    str(payload.get("target", "") or ""),
                    str(payload.get("creative", "") or ""),
                    str(payload.get("ad_text_key", "") or ""),
                    float(payload.get("spend", 0) or 0),
                    int(payload.get("leads", 0) or 0),
                    int(payload.get("deposits", 0) or 0),
                    float(payload.get("cpa", 0) or 0),
                    float(payload.get("roi", 0) or 0),
                    str(payload.get("source", "") or ""),
                ),
            )

    def get_offer(self, name_or_alias: str) -> dict[str, Any] | None:
        return self._lookup_with_alias("offers", "name", name_or_alias)

    def get_target(self, code_or_alias: str) -> dict[str, Any] | None:
        return self._lookup_with_alias("targets", "code", code_or_alias)

    def get_creative(self, name_or_alias: str) -> dict[str, Any] | None:
        return self._lookup_with_alias("creatives", "name", name_or_alias)

    def get_ad_text(self, key: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM ad_texts WHERE lower(key)=lower(?)", (key,)).fetchone()
        return _row_to_dict(row)

    def get_creative_context(self, name_or_alias: str) -> dict[str, Any]:
        creative = self.get_creative(name_or_alias) or {"name": name_or_alias}
        creative_name = creative["name"]
        with self._connect() as conn:
            analysis = _row_to_dict(
                conn.execute(
                    "SELECT * FROM creative_analysis WHERE creative_name=?",
                    (creative_name,),
                ).fetchone()
            ) or {}
            rows = conn.execute(
                """
                SELECT field_name, value FROM manual_overrides
                WHERE entity_type='creative' AND entity_key=?
                """,
                (creative_name,),
            ).fetchall()
        for row in rows:
            analysis[row["field_name"]] = row["value"]
        return {"creative": creative, "analysis": analysis}

    def get_performance(
        self,
        *,
        country: str = "",
        offer: str = "",
        target: str = "",
        creative: str = "",
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        for field, value in (("country", country), ("offer", offer), ("target", target), ("creative", creative)):
            if value:
                clauses.append(f"lower({field})=lower(?)")
                params.append(value)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._connect() as conn:
            rows = conn.execute(f"SELECT * FROM performance_snapshots{where}", params).fetchall()
        return [_row_to_dict(row) for row in rows if row is not None]

    def _lookup_with_alias(self, table: str, key_field: str, value: str) -> dict[str, Any] | None:
        value_norm = (value or "").strip().lower()
        with self._connect() as conn:
            rows = conn.execute(f"SELECT * FROM {table}").fetchall()
        for row in rows:
            data = _row_to_dict(row)
            if not data:
                continue
            if str(data[key_field]).strip().lower() == value_norm:
                return data
            aliases = data.get("aliases", [])
            if any(str(alias).strip().lower() == value_norm for alias in aliases):
                return data
        return None

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn


def _json(value: Any) -> str:
    return json.dumps(value if value is not None else [], ensure_ascii=False)


def _loads(value: Any) -> Any:
    if value is None:
        return None
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return value


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    data = dict(row)
    for key in list(data):
        if key.endswith("_json"):
            data[key[:-5]] = _loads(data.pop(key))
    return data
