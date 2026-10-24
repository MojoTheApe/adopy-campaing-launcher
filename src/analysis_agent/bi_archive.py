"""Локальный архив статистики: копит дни из BI, книга нужна только один раз.

ACL-103. BI отдаёт скользящее окно в ~8 дней и каждый день перезаливает его.
Архив хранит все дни, которые когда-либо видел, в SQLite в папке машины
(``config.paths.get_local_config_dir()``) — дешборду больше не нужна
ручная годовая книга:

- день, за который в архиве есть строки BI, берётся только из BI; новое окно
  BI целиком заменяет свои дни (BI пересчитывает прошлые дни, FTD дозревают);
- день, которого BI в архиве нет, берётся из книги — книга импортируется
  один раз на версию файла (mtime) и только за такие дни. Когда BI накопит
  свою историю, книга перестаёт что-либо добавлять.

Строки BI хранятся с ``CM_ID`` (имя подставляется при чтении, так что правка
config/bi_cm_map.json действует и на прошлые дни); строки книги — с именем CM.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import closing
from pathlib import Path
from typing import Dict, Optional

import pandas as pd

TABLE = "stats_rows"
FILE_NAME = "bi_stats_archive.sqlite3"

# Колонки строки в схеме книги + код менеджера BI + источник.
TEXT_COLS = ["Country", "Account Alias", "Adset Name", "Ad Title", "Ad Asset", "Variant", "CM"]
NUM_COLS = ["# Leads", "# FTDs", "$ Spend", "$ Net"]
META_COLS = ["_stats_currency_code", "_stats_timezone", "_stats_day_boundary"]

_LOCK = threading.RLock()


def archive_path() -> str:
    override = (os.environ.get("BI_ARCHIVE_PATH") or "").strip()
    if override:
        return override
    from config.paths import get_local_config_dir

    return str(Path(get_local_config_dir()) / FILE_NAME)


def _connect(path: Optional[str] = None) -> sqlite3.Connection:
    path = path or archive_path()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    cols = ", ".join(f'"{c}" TEXT' for c in TEXT_COLS) + ", " + ", ".join(f'"{c}" REAL' for c in NUM_COLS)
    conn.execute(
        f'CREATE TABLE IF NOT EXISTS {TABLE} ("Date" TEXT NOT NULL, {cols}, "CM_ID" TEXT, source TEXT NOT NULL)'
    )
    conn.execute(f'CREATE INDEX IF NOT EXISTS {TABLE}_date ON {TABLE} ("Date", source)')
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    # Добавочная миграция сохраняет все прежние строки/источники/версии книги.
    with _LOCK:
        existing = {r[1] for r in conn.execute(f"PRAGMA table_info({TABLE})")}
        for col in META_COLS:
            if col not in existing:
                try:
                    conn.execute(f'ALTER TABLE {TABLE} ADD COLUMN "{col}" TEXT')
                except sqlite3.OperationalError:
                    # Другой процесс мог закончить ту же добавочную миграцию.
                    if col not in {r[1] for r in conn.execute(f"PRAGMA table_info({TABLE})")}:
                        conn.close()
                        raise
    return conn


def _day(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, errors="coerce").dt.strftime("%Y-%m-%d")


def _rows(df: pd.DataFrame, source: str) -> pd.DataFrame:
    from src.analysis_agent import bi_compare

    df = bi_compare.annotate_source(df, source)
    out = pd.DataFrame({"Date": _day(df["Date"])})
    for c in TEXT_COLS:
        out[c] = df[c].fillna("").astype(str) if c in df.columns else None
    for c in NUM_COLS:
        out[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0) if c in df.columns else 0.0
    out["CM_ID"] = df["CM_ID"].astype(str) if "CM_ID" in df.columns else None
    out["source"] = source
    for col in META_COLS:
        out[col] = df[col].map(bi_compare.text)
    return out[out["Date"].notna()]


def _insert(conn: sqlite3.Connection, rows: pd.DataFrame) -> None:
    cols = list(rows.columns)
    sql = f"INSERT INTO {TABLE} ({', '.join(chr(34) + c + chr(34) for c in cols)}) VALUES ({', '.join('?' * len(cols))})"
    conn.executemany(sql, rows.itertuples(index=False, name=None))


def ingest_bi_window(window: pd.DataFrame, path: Optional[str] = None) -> int:
    """Окно BI (в схеме книги + CM_ID) заменяет в архиве все свои дни."""
    rows = _rows(window, "bi")
    days = sorted(set(rows["Date"]))
    if not days:
        return 0
    with _LOCK, closing(_connect(path)) as conn, conn:
        conn.executemany(f'DELETE FROM {TABLE} WHERE "Date" = ?', [(d,) for d in days])
        _insert(conn, rows)
    return len(days)


def ingest_workbook(book: pd.DataFrame, version: str, path: Optional[str] = None) -> int:
    """Строки книги — только за дни без BI в архиве; одна версия файла — один раз."""
    with _LOCK, closing(_connect(path)) as conn, conn:
        seen = conn.execute("SELECT value FROM meta WHERE key = 'workbook_version'").fetchone()
        if seen and seen[0] == version:
            return 0
        bi_days = {r[0] for r in conn.execute(f"SELECT DISTINCT \"Date\" FROM {TABLE} WHERE source = 'bi'")}
        rows = _rows(book, "workbook")
        rows = rows[~rows["Date"].isin(bi_days)]
        days = sorted(set(rows["Date"]))
        conn.executemany(f"DELETE FROM {TABLE} WHERE \"Date\" = ? AND source = 'workbook'", [(d,) for d in days])
        _insert(conn, rows)
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('workbook_version', ?)", (version,))
    return len(days)


def workbook_version_seen(path: Optional[str] = None) -> Optional[str]:
    if not os.path.isfile(path or archive_path()):
        return None
    with closing(_connect(path)) as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = 'workbook_version'").fetchone()
    return row[0] if row else None


def read_archive(path: Optional[str] = None) -> pd.DataFrame:
    if not os.path.isfile(path or archive_path()):
        return pd.DataFrame(columns=["Date", *TEXT_COLS, *NUM_COLS, "CM_ID", "source"])
    with closing(_connect(path)) as conn:
        df = pd.read_sql_query(f"SELECT * FROM {TABLE}", conn)
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    for c in ("# Leads", "# FTDs"):
        df[c] = df[c].fillna(0).astype("int64")
    return df


def archive_days(path: Optional[str] = None) -> Dict[str, int]:
    """{источник: число дней} — для отчёта и проверки."""
    if not os.path.isfile(path or archive_path()):
        return {}
    with closing(_connect(path)) as conn:
        return dict(conn.execute(f'SELECT source, COUNT(DISTINCT "Date") FROM {TABLE} GROUP BY source').fetchall())
