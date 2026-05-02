# Knowledge Base Context Pack Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the first working increment of the knowledge base context system: SQLite storage, import from current KB, context pack generation, and Markdown export.

**Architecture:** Add a focused `src/knowledge_agent/context_pack/` package with separate modules for schema/storage, import, context building, and Markdown rendering. This plan does not implement vision analysis or morning report integration; it creates stable interfaces those later increments can consume.

**Tech Stack:** Python standard library (`sqlite3`, `json`, `dataclasses`, `pathlib`), existing project paths, `pytest`.

---

## File Structure

- Create `src/knowledge_agent/context_pack/__init__.py`
  - Public exports for the context pack package.
- Create `src/knowledge_agent/context_pack/models.py`
  - Small dataclasses for context pack inputs/outputs.
- Create `src/knowledge_agent/context_pack/store.py`
  - SQLite schema creation, upserts, and read queries.
- Create `src/knowledge_agent/context_pack/importers.py`
  - Import current `config/knowledge_base.json` and enriched performance rows.
- Create `src/knowledge_agent/context_pack/builder.py`
  - Build combo, creative, and offer-country context packs from the store.
- Create `src/knowledge_agent/context_pack/markdown.py`
  - Render context packs to Markdown.
- Create `scripts/build_context_packs.py`
  - CLI for initializing the DB and exporting Markdown context packs.
- Modify `.env.example`
  - Add `KB_CONTEXT_DB` and `KB_CONTEXT_OUTPUT_DIR`.
- Create `tests/test_context_pack_store.py`
  - Unit tests for schema and upsert/query behavior.
- Create `tests/test_context_pack_importers.py`
  - Unit tests for importing current KB-style dictionaries and performance rows.
- Create `tests/test_context_pack_builder.py`
  - Unit tests for combo, creative, and offer-country context pack behavior.
- Create `tests/test_build_context_packs_cli.py`
  - Smoke test for the CLI with a temp DB/output directory.

---

## Task 1: SQLite Store Foundation

**Files:**
- Create: `src/knowledge_agent/context_pack/__init__.py`
- Create: `src/knowledge_agent/context_pack/models.py`
- Create: `src/knowledge_agent/context_pack/store.py`
- Test: `tests/test_context_pack_store.py`

- [ ] **Step 1: Create the package initializer**

Create `src/knowledge_agent/context_pack/__init__.py`:

```python
"""Context pack system for the knowledge base."""

from src.knowledge_agent.context_pack.builder import ContextPackBuilder
from src.knowledge_agent.context_pack.store import ContextPackStore

__all__ = ["ContextPackBuilder", "ContextPackStore"]
```

- [ ] **Step 2: Define dataclasses**

Create `src/knowledge_agent/context_pack/models.py`:

```python
"""Typed data objects for knowledge base context packs."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ComboKey:
    country: str
    offer: str
    target: str
    creative: str
    ad_text_key: str = ""


@dataclass
class ContextPack:
    kind: str
    title: str
    sections: dict[str, str] = field(default_factory=dict)
    missing_fields: list[str] = field(default_factory=list)
    source_ids: dict[str, Any] = field(default_factory=dict)

    def section(self, name: str) -> str:
        return self.sections.get(name, "").strip()
```

- [ ] **Step 3: Write failing store tests**

Create `tests/test_context_pack_store.py`:

```python
from __future__ import annotations

from pathlib import Path

from src.knowledge_agent.context_pack.store import ContextPackStore


def test_store_creates_schema_and_upserts_offer_target_creative(tmp_path: Path) -> None:
    store = ContextPackStore(tmp_path / "kb_context.db")
    store.initialize()

    store.upsert_offer(
        name="Favorita",
        payload={
            "aliases": ["Corporacion Favorita"],
            "audience": "Ecuador retail investors",
            "usp": "Local trusted brand",
            "top_countries": ["EC"],
            "notes": "Known Ecuador winner",
        },
    )
    store.upsert_target(
        code="Trading",
        payload={
            "label": "Trading",
            "aliases": ["trade"],
            "audience": "Active traders",
            "fb_interest": "Day trading",
            "top_countries": ["EC"],
            "notes": "Works in EC",
        },
    )
    store.upsert_creative(
        name="Video3509 The app",
        payload={
            "creative_type": "video",
            "aliases": ["3509 - the app"],
            "offer_hints": ["Favorita"],
            "deposits_summary": 1,
        },
    )

    assert store.get_offer("favorita")["name"] == "Favorita"
    assert store.get_target("trade")["code"] == "Trading"
    assert store.get_creative("3509 - the app")["name"] == "Video3509 The app"


def test_manual_override_wins_over_creative_analysis(tmp_path: Path) -> None:
    store = ContextPackStore(tmp_path / "kb_context.db")
    store.initialize()
    store.upsert_creative("Blue_6t", {"creative_type": "video", "aliases": []})
    store.upsert_creative_analysis(
        "Blue_6t",
        {
            "visual_summary": "Auto summary",
            "hook": "Auto hook",
            "confidence": 0.55,
            "provider": "test",
        },
    )
    store.upsert_manual_override(
        entity_type="creative",
        entity_key="Blue_6t",
        field_name="visual_summary",
        value="Manual summary",
    )

    merged = store.get_creative_context("Blue_6t")
    assert merged["analysis"]["visual_summary"] == "Manual summary"
    assert merged["analysis"]["hook"] == "Auto hook"
```

- [ ] **Step 4: Run store tests and verify they fail**

Run:

```powershell
python -m pytest tests/test_context_pack_store.py -v
```

Expected: FAIL because `ContextPackStore` does not exist yet.

- [ ] **Step 5: Implement the store**

Create `src/knowledge_agent/context_pack/store.py`:

```python
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

    def get_performance(self, *, country: str = "", offer: str = "", target: str = "", creative: str = "") -> list[dict[str, Any]]:
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
```

- [ ] **Step 6: Run store tests and verify they pass**

Run:

```powershell
python -m pytest tests/test_context_pack_store.py -v
```

Expected: 2 passed.

- [ ] **Step 7: Commit Task 1**

```bash
git add src/knowledge_agent/context_pack/__init__.py src/knowledge_agent/context_pack/models.py src/knowledge_agent/context_pack/store.py tests/test_context_pack_store.py
git commit -m "Add context pack SQLite store"
```

---

## Task 2: Import Current Knowledge and Performance Rows

**Files:**
- Create: `src/knowledge_agent/context_pack/importers.py`
- Test: `tests/test_context_pack_importers.py`

- [ ] **Step 1: Write failing importer tests**

Create `tests/test_context_pack_importers.py`:

```python
from __future__ import annotations

from pathlib import Path

import pandas as pd

from src.knowledge_agent.context_pack.importers import import_knowledge_json, import_performance_rows
from src.knowledge_agent.context_pack.store import ContextPackStore


def test_import_knowledge_json_imports_offers_and_targets(tmp_path: Path) -> None:
    store = ContextPackStore(tmp_path / "kb_context.db")
    store.initialize()
    import_knowledge_json(
        store,
        {
            "offers": {
                "Favorita": {
                    "aliases": ["Corporacion Favorita"],
                    "audience": "EC shoppers",
                    "usp": "Known local brand",
                    "top_countries": ["EC"],
                    "notes": "Strong local trust",
                }
            },
            "targets": {
                "Trading": {
                    "label": "Trading",
                    "aliases": ["trade"],
                    "fb_interest": "Day trading",
                    "audience": "Active traders",
                    "top_countries": ["EC"],
                    "notes": "Works in EC",
                }
            },
        },
    )

    assert store.get_offer("Corporacion Favorita")["audience"] == "EC shoppers"
    assert store.get_target("trade")["fb_interest"] == "Day trading"


def test_import_performance_rows_creates_creatives_texts_and_snapshots(tmp_path: Path) -> None:
    store = ContextPackStore(tmp_path / "kb_context.db")
    store.initialize()
    df = pd.DataFrame(
        [
            {
                "Country Final": "EC",
                "Offer": "Favorita",
                "Interest": "Trading",
                "Creative": "Video3509 The app",
                "TextSegment": "ES Benefit",
                "$ Spend": 120.0,
                "# Leads": 12,
                "# FTDs": 1,
                "CPA": 120.0,
                "ROI": 36.0,
            }
        ]
    )

    import_performance_rows(store, df, source="fixture")

    assert store.get_creative("Video3509 The app")["creative_type"] == "video"
    assert store.get_ad_text("ES Benefit")["source"] == "performance_text_segment"
    rows = store.get_performance(country="EC", offer="Favorita", target="Trading")
    assert len(rows) == 1
    assert rows[0]["deposits"] == 1
```

- [ ] **Step 2: Run importer tests and verify they fail**

Run:

```powershell
python -m pytest tests/test_context_pack_importers.py -v
```

Expected: FAIL because `importers.py` does not exist.

- [ ] **Step 3: Implement importers**

Create `src/knowledge_agent/context_pack/importers.py`:

```python
"""Import current project knowledge into the context pack store."""
from __future__ import annotations

from typing import Any

import pandas as pd

from src.knowledge_agent.context_pack.store import ContextPackStore


def import_knowledge_json(store: ContextPackStore, data: dict[str, Any]) -> None:
    for name, payload in (data.get("offers") or {}).items():
        if isinstance(payload, dict):
            store.upsert_offer(str(name), payload)
    for code, payload in (data.get("targets") or {}).items():
        if isinstance(payload, dict):
            store.upsert_target(str(code), payload)


def import_performance_rows(store: ContextPackStore, df: pd.DataFrame, *, source: str) -> None:
    for _, row in df.iterrows():
        country = _text(row.get("Country Final") or row.get("Country"))
        offer = _text(row.get("Offer") or row.get("Ad Asset"))
        target = _text(row.get("Interest"))
        creative = _text(row.get("Creative") or row.get("Ad Title"))
        text_key = _text(row.get("TextSegment"))

        if creative:
            store.upsert_creative(
                creative,
                {
                    "creative_type": _creative_type(creative),
                    "aliases": [_text(row.get("Ad Title"))] if _text(row.get("Ad Title")) and _text(row.get("Ad Title")) != creative else [],
                    "offer_hints": [offer] if offer else [],
                    "deposits_summary": _safe_int(row.get("# FTDs")),
                },
            )

        if text_key:
            store.upsert_ad_text(
                text_key,
                {
                    "body": "",
                    "language": "",
                    "hook": "",
                    "cta": "",
                    "offer": offer,
                    "risk_flags": [],
                    "source": "performance_text_segment",
                },
            )
            if creative:
                store.link_creative_text(creative, text_key)

        store.insert_performance_snapshot(
            {
                "country": country,
                "offer": offer,
                "target": target,
                "creative": creative,
                "ad_text_key": text_key,
                "spend": _safe_float(row.get("$ Spend")),
                "leads": _safe_int(row.get("# Leads")),
                "deposits": _safe_int(row.get("# FTDs")),
                "cpa": _safe_float(row.get("CPA")),
                "roi": _safe_float(row.get("ROI")),
                "source": source,
            }
        )


def _creative_type(value: str) -> str:
    lowered = value.lower()
    if "video" in lowered or "vid" in lowered or lowered.endswith(".mp4"):
        return "video"
    if "image" in lowered or "img" in lowered or lowered.endswith((".jpg", ".jpeg", ".png", ".webp")):
        return "image"
    return ""


def _text(value: Any) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    return str(value).strip()


def _safe_int(value: Any) -> int:
    try:
        if pd.isna(value):
            return 0
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _safe_float(value: Any) -> float:
    try:
        if pd.isna(value):
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0
```

- [ ] **Step 4: Run importer tests and verify they pass**

Run:

```powershell
python -m pytest tests/test_context_pack_importers.py -v
```

Expected: 2 passed.

- [ ] **Step 5: Run store and importer tests together**

Run:

```powershell
python -m pytest tests/test_context_pack_store.py tests/test_context_pack_importers.py -v
```

Expected: 4 passed.

- [ ] **Step 6: Commit Task 2**

```bash
git add src/knowledge_agent/context_pack/importers.py tests/test_context_pack_importers.py
git commit -m "Import knowledge base into context pack store"
```

---

## Task 3: Context Pack Builder and Markdown Renderer

**Files:**
- Create: `src/knowledge_agent/context_pack/builder.py`
- Create: `src/knowledge_agent/context_pack/markdown.py`
- Test: `tests/test_context_pack_builder.py`

- [ ] **Step 1: Write failing context builder tests**

Create `tests/test_context_pack_builder.py`:

```python
from __future__ import annotations

from pathlib import Path

from src.knowledge_agent.context_pack.builder import ContextPackBuilder
from src.knowledge_agent.context_pack.markdown import render_context_pack
from src.knowledge_agent.context_pack.models import ComboKey
from src.knowledge_agent.context_pack.store import ContextPackStore


def _seed_store(tmp_path: Path) -> ContextPackStore:
    store = ContextPackStore(tmp_path / "kb_context.db")
    store.initialize()
    store.upsert_offer(
        "Favorita",
        {
            "aliases": [],
            "audience": "Ecuador residents who know Favorita",
            "usp": "Invest in a trusted local retail brand",
            "top_countries": ["EC"],
            "notes": "Known Ecuador winner",
        },
    )
    store.upsert_target(
        "Trading",
        {
            "label": "Trading",
            "aliases": ["trade"],
            "audience": "Active traders interested in markets",
            "fb_interest": "Day trading",
            "top_countries": ["EC"],
            "notes": "Works in EC",
        },
    )
    store.upsert_creative("Video3509 The app", {"creative_type": "video", "aliases": [], "offer_hints": ["Favorita"]})
    store.upsert_creative_analysis(
        "Video3509 The app",
        {
            "visual_summary": "Mobile investment app demo",
            "hook": "easy start",
            "confidence": 0.82,
            "provider": "test",
        },
    )
    store.upsert_ad_text("ES Benefit", {"body": "", "hook": "benefit angle", "cta": "Solicitar informacion", "offer": "Favorita"})
    store.insert_performance_snapshot(
        {
            "country": "EC",
            "offer": "Favorita",
            "target": "Trading",
            "creative": "Video3509 The app",
            "ad_text_key": "ES Benefit",
            "spend": 120,
            "leads": 12,
            "deposits": 1,
            "cpa": 120,
            "roi": 36,
            "source": "fixture",
        }
    )
    return store


def test_combo_context_pack_combines_offer_target_creative_text_and_performance(tmp_path: Path) -> None:
    builder = ContextPackBuilder(_seed_store(tmp_path))
    pack = builder.build_combo_pack(
        ComboKey(
            country="EC",
            offer="Favorita",
            target="Trading",
            creative="Video3509 The app",
            ad_text_key="ES Benefit",
        )
    )

    rendered = render_context_pack(pack)
    assert "EC / Favorita / Trading / Video3509 The app / ES Benefit" in rendered
    assert "Invest in a trusted local retail brand" in rendered
    assert "Active traders interested in markets" in rendered
    assert "Mobile investment app demo" in rendered
    assert "1 deposits" in rendered
    assert "ad_text.body" in rendered


def test_offer_country_pack_summarizes_best_patterns(tmp_path: Path) -> None:
    builder = ContextPackBuilder(_seed_store(tmp_path))
    pack = builder.build_offer_country_pack(offer="Favorita", country="EC")

    assert pack.kind == "offer_country"
    rendered = render_context_pack(pack)
    assert "Offer + Country: Favorita / EC" in rendered
    assert "Best targets" in rendered
    assert "Trading" in rendered
    assert "Best creative patterns" in rendered
```

- [ ] **Step 2: Run builder tests and verify they fail**

Run:

```powershell
python -m pytest tests/test_context_pack_builder.py -v
```

Expected: FAIL because `builder.py` and `markdown.py` do not exist.

- [ ] **Step 3: Implement context builder**

Create `src/knowledge_agent/context_pack/builder.py`:

```python
"""Build agent-ready context packs from the knowledge store."""
from __future__ import annotations

from collections import Counter

from src.knowledge_agent.context_pack.models import ComboKey, ContextPack
from src.knowledge_agent.context_pack.store import ContextPackStore


class ContextPackBuilder:
    def __init__(self, store: ContextPackStore) -> None:
        self.store = store

    def build_combo_pack(self, key: ComboKey) -> ContextPack:
        offer = self.store.get_offer(key.offer) or {}
        target = self.store.get_target(key.target) or {}
        creative_ctx = self.store.get_creative_context(key.creative)
        creative = creative_ctx.get("creative", {})
        analysis = creative_ctx.get("analysis", {})
        ad_text = self.store.get_ad_text(key.ad_text_key) if key.ad_text_key else None
        ad_text = ad_text or {}
        performance = self.store.get_performance(
            country=key.country,
            offer=key.offer,
            target=key.target,
            creative=key.creative,
        )
        missing = _missing_combo_fields(offer, target, creative, analysis, ad_text)
        title = f"{key.country} / {key.offer} / {key.target} / {key.creative} / {key.ad_text_key}".rstrip(" /")
        return ContextPack(
            kind="combo",
            title=title,
            sections={
                "Offer meaning": _join_parts(offer.get("usp"), offer.get("audience"), offer.get("notes")),
                "Target audience": _join_parts(target.get("label"), target.get("audience"), target.get("notes")),
                "Creative understanding": _join_parts(analysis.get("visual_summary"), analysis.get("hook")),
                "Ad text": _join_parts(ad_text.get("body"), ad_text.get("hook"), ad_text.get("cta")),
                "Performance memory": _summarize_performance(performance),
                "Why it may work": _why_it_may_work(offer, target, analysis),
                "Risks": _join_parts(", ".join(ad_text.get("risk_flags", []) or []), ", ".join(analysis.get("risk_flags", []) or [])),
                "Suggested next angles": _suggest_next_angles(offer, target, analysis),
            },
            missing_fields=missing,
            source_ids={"creative": creative.get("name"), "offer": offer.get("name"), "target": target.get("code")},
        )

    def build_creative_pack(self, creative_name: str) -> ContextPack:
        creative_ctx = self.store.get_creative_context(creative_name)
        creative = creative_ctx.get("creative", {})
        analysis = creative_ctx.get("analysis", {})
        performance = self.store.get_performance(creative=creative.get("name", creative_name))
        missing = []
        if not analysis.get("visual_summary"):
            missing.append("creative.visual_summary")
        if not analysis.get("on_screen_text"):
            missing.append("creative.on_screen_text")
        return ContextPack(
            kind="creative",
            title=f"Creative: {creative.get('name', creative_name)}",
            sections={
                "Known aliases": ", ".join(creative.get("aliases", []) or []),
                "Auto vision summary": _join_parts(analysis.get("visual_summary"), analysis.get("hook")),
                "Worked in": _summarize_performance(performance),
                "Similar creatives": "",
            },
            missing_fields=missing,
            source_ids={"creative": creative.get("name", creative_name)},
        )

    def build_offer_country_pack(self, *, offer: str, country: str) -> ContextPack:
        offer_row = self.store.get_offer(offer) or {}
        performance = self.store.get_performance(country=country, offer=offer)
        target_counts: Counter[str] = Counter()
        creative_counts: Counter[str] = Counter()
        for row in performance:
            if int(row.get("deposits", 0) or 0) > 0:
                target_counts[str(row.get("target", ""))] += int(row.get("deposits", 0) or 0)
                creative_counts[str(row.get("creative", ""))] += int(row.get("deposits", 0) or 0)
        return ContextPack(
            kind="offer_country",
            title=f"Offer + Country: {offer} / {country}",
            sections={
                "Positioning": _join_parts(offer_row.get("usp"), offer_row.get("audience"), offer_row.get("notes")),
                "Best targets": _format_counter(target_counts),
                "Best creative patterns": _format_counter(creative_counts),
                "Best ad text angles": "",
                "Weak/failed patterns": "",
                "Compliance/risk notes": "",
                "Open gaps": "",
                "New material directions": "",
            },
            missing_fields=[],
            source_ids={"offer": offer_row.get("name", offer), "country": country},
        )


def _missing_combo_fields(offer: dict, target: dict, creative: dict, analysis: dict, ad_text: dict) -> list[str]:
    missing = []
    if not offer:
        missing.append("offer")
    if not target:
        missing.append("target")
    if not creative:
        missing.append("creative")
    if not analysis.get("visual_summary"):
        missing.append("creative.visual_summary")
    if not ad_text.get("body"):
        missing.append("ad_text.body")
    return missing


def _summarize_performance(rows: list[dict]) -> str:
    if not rows:
        return ""
    deposits = sum(int(row.get("deposits", 0) or 0) for row in rows)
    spend = sum(float(row.get("spend", 0) or 0) for row in rows)
    leads = sum(int(row.get("leads", 0) or 0) for row in rows)
    best_roi = max(float(row.get("roi", 0) or 0) for row in rows)
    return f"{deposits} deposits, {leads} leads, ${spend:,.2f} spend, best ROI {best_roi:.2f}"


def _why_it_may_work(offer: dict, target: dict, analysis: dict) -> str:
    parts = []
    if offer.get("usp"):
        parts.append(f"Offer angle: {offer['usp']}")
    if target.get("audience"):
        parts.append(f"Audience fit: {target['audience']}")
    if analysis.get("hook"):
        parts.append(f"Creative hook: {analysis['hook']}")
    return " | ".join(parts)


def _suggest_next_angles(offer: dict, target: dict, analysis: dict) -> str:
    parts = []
    if offer.get("usp"):
        parts.append("Repeat the offer promise with a fresh proof point.")
    if target.get("audience"):
        parts.append("Adapt copy to the target audience's existing intent.")
    if analysis.get("hook"):
        parts.append("Create a variant around the same visual hook.")
    return " ".join(parts)


def _format_counter(counter: Counter[str]) -> str:
    if not counter:
        return ""
    return "\n".join(f"- {name}: {count} deposits" for name, count in counter.most_common(5) if name)


def _join_parts(*values: object) -> str:
    return " | ".join(str(value).strip() for value in values if str(value or "").strip())
```

- [ ] **Step 4: Implement Markdown renderer**

Create `src/knowledge_agent/context_pack/markdown.py`:

```python
"""Markdown rendering for context packs."""
from __future__ import annotations

from src.knowledge_agent.context_pack.models import ContextPack


def render_context_pack(pack: ContextPack) -> str:
    lines = [f"# {pack.title}", ""]
    for name, value in pack.sections.items():
        lines.append(f"## {name}")
        lines.append("")
        lines.append(value.strip() if value.strip() else "_No data._")
        lines.append("")
    lines.append("## Missing fields")
    lines.append("")
    if pack.missing_fields:
        lines.extend(f"- `{field}`" for field in pack.missing_fields)
    else:
        lines.append("_None._")
    lines.append("")
    return "\n".join(lines).rstrip() + "\n"
```

- [ ] **Step 5: Run builder tests and verify they pass**

Run:

```powershell
python -m pytest tests/test_context_pack_builder.py -v
```

Expected: 2 passed.

- [ ] **Step 6: Run all context pack tests**

Run:

```powershell
python -m pytest tests/test_context_pack_store.py tests/test_context_pack_importers.py tests/test_context_pack_builder.py -v
```

Expected: 6 passed.

- [ ] **Step 7: Commit Task 3**

```bash
git add src/knowledge_agent/context_pack/builder.py src/knowledge_agent/context_pack/markdown.py tests/test_context_pack_builder.py
git commit -m "Build Markdown context packs from knowledge store"
```

---

## Task 4: CLI Export and Environment Documentation

**Files:**
- Create: `scripts/build_context_packs.py`
- Modify: `.env.example`
- Test: `tests/test_build_context_packs_cli.py`

- [ ] **Step 1: Write failing CLI test**

Create `tests/test_build_context_packs_cli.py`:

```python
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def test_build_context_packs_cli_exports_offer_country_pack(tmp_path: Path) -> None:
    kb_json = tmp_path / "knowledge_base.json"
    kb_json.write_text(
        json.dumps(
            {
                "offers": {
                    "Favorita": {
                        "audience": "EC shoppers",
                        "usp": "Local trusted brand",
                        "top_countries": ["EC"],
                        "notes": "Known winner",
                    }
                },
                "targets": {
                    "Trading": {
                        "label": "Trading",
                        "audience": "Active traders",
                        "fb_interest": "Day trading",
                        "top_countries": ["EC"],
                    }
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    db_path = tmp_path / "kb_context.db"
    out_dir = tmp_path / "packs"

    result = subprocess.run(
        [
            sys.executable,
            "scripts/build_context_packs.py",
            "--kb-json",
            str(kb_json),
            "--db",
            str(db_path),
            "--output-dir",
            str(out_dir),
            "--offer",
            "Favorita",
            "--country",
            "EC",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    exported = out_dir / "offer_country_Favorita_EC.md"
    assert exported.is_file()
    text = exported.read_text(encoding="utf-8")
    assert "Offer + Country: Favorita / EC" in text
    assert "Local trusted brand" in text
```

- [ ] **Step 2: Run CLI test and verify it fails**

Run:

```powershell
python -m pytest tests/test_build_context_packs_cli.py -v
```

Expected: FAIL because `scripts/build_context_packs.py` does not exist.

- [ ] **Step 3: Implement CLI**

Create `scripts/build_context_packs.py`:

```python
#!/usr/bin/env python3
"""Build Markdown context packs from the local knowledge base."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.knowledge_agent.context_pack.builder import ContextPackBuilder
from src.knowledge_agent.context_pack.importers import import_knowledge_json
from src.knowledge_agent.context_pack.markdown import render_context_pack
from src.knowledge_agent.context_pack.store import ContextPackStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build knowledge base context packs.")
    parser.add_argument("--kb-json", default=str(_ROOT / "config" / "knowledge_base.json"))
    parser.add_argument("--db", default=os.environ.get("KB_CONTEXT_DB", str(_ROOT / "data" / "knowledge_context.db")))
    parser.add_argument("--output-dir", default=os.environ.get("KB_CONTEXT_OUTPUT_DIR", str(_ROOT / "output" / "context_packs")))
    parser.add_argument("--offer", required=True)
    parser.add_argument("--country", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    kb_path = Path(args.kb_json)
    if not kb_path.is_file():
        print(f"Knowledge JSON not found: {kb_path}", file=sys.stderr)
        return 2

    data = json.loads(kb_path.read_text(encoding="utf-8"))
    store = ContextPackStore(args.db)
    store.initialize()
    import_knowledge_json(store, data)

    builder = ContextPackBuilder(store)
    pack = builder.build_offer_country_pack(offer=args.offer, country=args.country)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"offer_country_{_safe_name(args.offer)}_{_safe_name(args.country)}.md"
    output_path.write_text(render_context_pack(pack), encoding="utf-8")
    print(f"Wrote {output_path}")
    return 0


def _safe_name(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in value.strip())


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Update `.env.example`**

Add this block after the existing Knowledge Base settings:

```dotenv
# ── Knowledge Base: Context Packs ──────────────────────────────────────────────
# Local SQLite DB for structured context-pack lookup
KB_CONTEXT_DB=data/knowledge_context.db
# Markdown exports for human-readable context packs
KB_CONTEXT_OUTPUT_DIR=output/context_packs
```

- [ ] **Step 5: Run CLI test and verify it passes**

Run:

```powershell
python -m pytest tests/test_build_context_packs_cli.py -v
```

Expected: 1 passed.

- [ ] **Step 6: Run all context pack tests**

Run:

```powershell
python -m pytest tests/test_context_pack_store.py tests/test_context_pack_importers.py tests/test_context_pack_builder.py tests/test_build_context_packs_cli.py -v
```

Expected: 7 passed.

- [ ] **Step 7: Commit Task 4**

```bash
git add scripts/build_context_packs.py .env.example tests/test_build_context_packs_cli.py
git commit -m "Export knowledge context packs to Markdown"
```

---

## Task 5: Regression Verification

**Files:**
- No new files.
- Verify existing morning report behavior remains unchanged.

- [ ] **Step 1: Run context pack tests**

Run:

```powershell
python -m pytest tests/test_context_pack_store.py tests/test_context_pack_importers.py tests/test_context_pack_builder.py tests/test_build_context_packs_cli.py -v
```

Expected: 7 passed.

- [ ] **Step 2: Run morning report snapshot tests**

Run:

```powershell
python -m pytest tests/test_markdown_unchanged.py -v
```

Expected: existing snapshot tests pass. This confirms the foundation did not change morning report output.

- [ ] **Step 3: Run broader tests if runtime is acceptable**

Run:

```powershell
python -m pytest tests -v
```

Expected: all existing tests pass. If an unrelated existing test fails, capture the failing test name and error before changing anything.

- [ ] **Step 4: Smoke test real KB export**

Run:

```powershell
python scripts/build_context_packs.py --offer Favorita --country EC
```

Expected: command prints `Wrote ...output\context_packs\offer_country_Favorita_EC.md` and the file contains a Markdown context pack.

- [ ] **Step 5: Commit verification fixes if needed**

If verification required any fixes, commit only those fixes:

```bash
git add <fixed-files>
git commit -m "Stabilize context pack foundation"
```

If no fixes were needed, do not create an empty commit.

---

## Out of Scope for This Plan

- Vision analysis of image/video files.
- Morning report integration.
- Brainstorming agent integration.
- Web dashboard for browsing context packs.
- Full ad text body extraction from unknown external sources.

These are separate increments built on the store, importer, and context builder interfaces created here.

---

## Self-Review Notes

- Spec coverage: this plan covers the foundation: SQLite, current KB import, context builder, Markdown preview, missing fields, and manual override precedence.
- Deferred spec areas are explicitly out of scope for this first implementation increment: vision enrichment, morning report integration, and brainstorming integration.
- Type consistency: `ContextPackStore`, `ContextPackBuilder`, `ComboKey`, and `ContextPack` names are defined before use and reused consistently.
- Test coverage: each module has a failing-test-first task and exact verification commands.
