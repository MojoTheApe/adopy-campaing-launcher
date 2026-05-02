"""SQLite persistence for the content pipeline.

Uses stdlib sqlite3 to stay dependency-free.  The DB file path comes from
ContentMachineConfig.db_path (default: data/content_machine.db).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Sequence

from src.content_machine.schema import ContentItem, ContentStatus

_COLUMNS = [
    "id", "created_at", "updated_at", "status", "content_type",
    "campaign_goal", "audience_segment", "brief", "topic", "calendar_slot",
    "title", "body", "media_paths", "version", "compliance",
    "approvals", "required_approvers", "publish", "metrics",
    "prompt_hash", "model_used",
]

_CREATE_TABLE = f"""
CREATE TABLE IF NOT EXISTS content_items (
    {', '.join(f'{c} TEXT' if c != 'version' else f'{c} INTEGER DEFAULT 1' for c in _COLUMNS)},
    PRIMARY KEY (id)
);
"""

_CREATE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_status ON content_items (status);
CREATE INDEX IF NOT EXISTS idx_calendar ON content_items (calendar_slot);
CREATE INDEX IF NOT EXISTS idx_type ON content_items (content_type);
"""


class ContentDB:
    """Thin wrapper around SQLite for content items."""

    def __init__(self, db_path: str) -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_CREATE_TABLE + _CREATE_INDEX)
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ── CRUD ─────────────────────────────────────────────────────────────────

    def save(self, item: ContentItem) -> None:
        item.updated_at = datetime.utcnow().isoformat()
        d = item.to_dict()
        cols = [c for c in _COLUMNS if c in d]
        placeholders = ", ".join(["?"] * len(cols))
        upsert = ", ".join(f"{c}=excluded.{c}" for c in cols if c != "id")
        sql = (
            f"INSERT INTO content_items ({', '.join(cols)}) VALUES ({placeholders}) "
            f"ON CONFLICT(id) DO UPDATE SET {upsert}"
        )
        self._conn.execute(sql, [d[c] for c in cols])
        self._conn.commit()

    def get(self, item_id: str) -> ContentItem | None:
        row = self._conn.execute(
            "SELECT * FROM content_items WHERE id = ?", (item_id,)
        ).fetchone()
        if row is None:
            return None
        return ContentItem.from_dict(dict(row))

    def list_by_status(self, *statuses: str) -> list[ContentItem]:
        ph = ", ".join(["?"] * len(statuses))
        rows = self._conn.execute(
            f"SELECT * FROM content_items WHERE status IN ({ph}) ORDER BY created_at DESC",
            statuses,
        ).fetchall()
        return [ContentItem.from_dict(dict(r)) for r in rows]

    def list_by_calendar(self, date_iso: str) -> list[ContentItem]:
        rows = self._conn.execute(
            "SELECT * FROM content_items WHERE calendar_slot = ? ORDER BY created_at",
            (date_iso,),
        ).fetchall()
        return [ContentItem.from_dict(dict(r)) for r in rows]

    def list_scheduled(self) -> list[ContentItem]:
        return self.list_by_status(ContentStatus.SCHEDULED.value)

    def list_recent(self, limit: int = 20) -> list[ContentItem]:
        rows = self._conn.execute(
            "SELECT * FROM content_items ORDER BY updated_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [ContentItem.from_dict(dict(r)) for r in rows]

    def update_status(self, item_id: str, new_status: str) -> None:
        self._conn.execute(
            "UPDATE content_items SET status = ?, updated_at = ? WHERE id = ?",
            (new_status, datetime.utcnow().isoformat(), item_id),
        )
        self._conn.commit()

    def delete(self, item_id: str) -> None:
        self._conn.execute("DELETE FROM content_items WHERE id = ?", (item_id,))
        self._conn.commit()
