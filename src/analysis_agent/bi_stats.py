"""Статистика из BI (MySQL, таблица `BI_Export`.`UF.Export`) вместо ручной книги.

ACL-103. BI отдаёт те же поля, что и `cursor example.xlsx`, только с кодом
менеджера (`CM_ID`) вместо имени и под своими именами колонок. Здесь строки
приводятся к схеме книги, чтобы дальше работал тот же конвейер
(``compute_metrics`` → ``enrich_structure``).

Настройки — в машинном `.env` (через ``config.paths.load_env``):

- ``BI_MYSQL_HOST``, ``BI_MYSQL_USER``, ``BI_MYSQL_PASSWORD`` — без любого из
  них BI выключен, дешборд читает книгу (адрес и логин в репо не храним);
- ``BI_MYSQL_PORT`` / ``BI_MYSQL_DATABASE`` / ``BI_MYSQL_TABLE`` — по
  умолчанию 3306 / BI_Export / UF.Export;
- ``FB_STATS_SOURCE`` — ``excel`` принудительно возвращает книгу;
- ``BI_STATS_ALL_CMS=1`` — оставить менеджеров вне config/bi_cm_map.json.

Доступ только на чтение: сессия открывается ``READ ONLY``, запросы — SELECT.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import pandas as pd

DEFAULT_PORT = 3306
DEFAULT_DATABASE = "BI_Export"
DEFAULT_TABLE = "UF.Export"

CM_MAP_PATH = Path(__file__).resolve().parents[2] / "config" / "bi_cm_map.json"

# BI-колонка → колонка книги.
COLUMN_MAP = {
    "DATE": "Date",
    "ACCOUNT_ALIAS": "Account Alias",
    "ADSET_NAME": "Adset Name",
    "AD_TITLE": "Ad Title",
    "AD_ASSET": "Ad Asset",
    "COUNTRY": "Country",
    "VARIANT": "Variant",
    "LEADS": "# Leads",
    "FTDS": "# FTDs",
    "SPEND": "$ Spend",
    "NET": "$ Net",
}

UNKNOWN = "Unknown"

# Ревизия данных BI = момент последней загрузки. Спрашивать её у сервера на
# каждый запрос страницы нельзя (peek идёт из цикла событий), поэтому она
# обновляется не чаще раза в REVISION_TTL секунд и только из рабочего потока.
REVISION_TTL = 300.0
_REV_LOCK = threading.Lock()
_REV: Dict[str, Any] = {"value": None, "checked": 0.0}


def bi_enabled() -> bool:
    """BI включён: есть пароль и источник не переключён на книгу."""
    if (os.environ.get("FB_STATS_SOURCE") or "").strip().lower() == "excel":
        return False
    return all(
        (os.environ.get(name) or "").strip()
        for name in ("BI_MYSQL_HOST", "BI_MYSQL_USER", "BI_MYSQL_PASSWORD")
    )


def _settings() -> Dict[str, Any]:
    env = os.environ.get
    return {
        "host": (env("BI_MYSQL_HOST") or "").strip(),
        "port": int((env("BI_MYSQL_PORT") or DEFAULT_PORT)),
        "user": (env("BI_MYSQL_USER") or "").strip(),
        "password": (env("BI_MYSQL_PASSWORD") or "").strip(),
        "database": (env("BI_MYSQL_DATABASE") or DEFAULT_DATABASE).strip(),
        "table": (env("BI_MYSQL_TABLE") or DEFAULT_TABLE).strip(),
    }


def _table_ref(cfg: Dict[str, Any]) -> str:
    # Точка — часть имени таблицы ("UF.Export"), не разделитель схемы.
    quote = lambda name: "`" + name.replace("`", "``") + "`"  # noqa: E731
    return f"{quote(cfg['database'])}.{quote(cfg['table'])}"


def _connect(cfg: Dict[str, Any]):
    import pymysql

    conn = pymysql.connect(
        host=cfg["host"], port=cfg["port"], user=cfg["user"],
        password=cfg["password"], database=cfg["database"],
        connect_timeout=15, read_timeout=120, charset="utf8mb4",
    )
    with conn.cursor() as cur:
        cur.execute("SET SESSION TRANSACTION READ ONLY")
    return conn


def load_cm_map(path: Optional[Path] = None) -> Dict[str, str]:
    """CM_ID → имя менеджера из config/bi_cm_map.json (ключи строками)."""
    path = path or CM_MAP_PATH
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    codes = data.get("codes", data) if isinstance(data, dict) else {}
    return {str(k).strip(): str(v).strip() for k, v in codes.items() if str(v).strip()}


def fetch_raw(cfg: Optional[Dict[str, Any]] = None) -> pd.DataFrame:
    cfg = cfg or _settings()
    conn = _connect(cfg)
    try:
        with conn.cursor() as cur:
            from src.analysis_agent.bi_compare import BI_METADATA_COLUMNS

            # Только явно относящиеся к единицам статистики колонки; валюта
            # аккаунта/одного SPEND не доказывает общую валюту SPEND и NET.
            try:
                cur.execute("SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                            "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s",
                            (cfg["database"], cfg["table"]))
                available = {str(row[0]).upper(): str(row[0]) for row in cur.fetchall()}
            except Exception:  # noqa: BLE001 — отказ метаданных не отменяет сами числа
                available = {}
            optional = [available[name] for name in BI_METADATA_COLUMNS if name in available]
            cols = ["CM_ID", *COLUMN_MAP, *optional]
            cur.execute(f"SELECT {', '.join('`' + c + '`' for c in cols)} FROM {_table_ref(cfg)}")
            rows = cur.fetchall()
    finally:
        conn.close()
    frame = pd.DataFrame(list(rows), columns=cols)
    return frame.rename(columns={name: name.upper() for name in optional})


def normalize(raw: pd.DataFrame) -> pd.DataFrame:
    """Сырые строки BI → колонки и типы книги; ``CM_ID`` остаётся как есть."""
    df = raw.rename(columns=COLUMN_MAP).copy()
    df["CM_ID"] = df["CM_ID"].fillna(UNKNOWN).astype(str).str.strip()
    for col in ("# Leads", "# FTDs"):
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0).astype("int64")
    for col in ("$ Spend", "$ Net"):
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0).astype("float64")
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    for col in ("Country", "Account Alias", "Adset Name", "Ad Title", "Ad Asset", "Variant"):
        df[col] = df[col].fillna("").astype(str).str.strip()
    from src.analysis_agent import bi_compare

    return bi_compare.annotate_source(df, "bi")


def assign_cm(df: pd.DataFrame, cm_map: Dict[str, str], *, keep_unmapped: bool = False) -> pd.DataFrame:
    """``CM_ID`` → имя менеджера (колонка ``CM``), ``CM_ID`` убирается.

    Строке с ``CM_ID = Unknown`` достаётся менеджер, к которому этот аккаунт
    относится в остальных строках BI (чужой код на общем аккаунте — не наш,
    его не присваиваем). Остальные строки — чужие команды и Taboola: книга их
    не содержала, поэтому по умолчанию они отбрасываются; с ``keep_unmapped``
    остаются под именем ``CM <код>``.
    """
    df = df.copy()
    codes = df.pop("CM_ID").fillna(UNKNOWN).astype(str).str.strip()
    names = codes.map(cm_map)

    known = pd.DataFrame({"alias": df["Account Alias"], "cm": names}).dropna()
    if not known.empty:
        by_alias = known.groupby("alias")["cm"].agg(lambda s: s.value_counts().index[0])
        inherit = codes.eq(UNKNOWN) & names.isna()
        names = names.where(~inherit, df["Account Alias"].map(by_alias))
    unresolved = int(names.isna().sum())
    if not keep_unmapped:
        df, codes, names = df[names.notna()].copy(), codes[names.notna()], names[names.notna()]
    df["CM"] = names.fillna("CM " + codes)
    df.attrs["unmapped_cm_rows"] = unresolved if not keep_unmapped else 0
    return df


def to_workbook_frame(
    raw: pd.DataFrame, cm_map: Dict[str, str], *, keep_unmapped: bool = False
) -> pd.DataFrame:
    """Строки BI → схема книги Stats (см. normalize + assign_cm)."""
    return assign_cm(normalize(raw), cm_map, keep_unmapped=keep_unmapped)


def keep_all_cms() -> bool:
    return (os.environ.get("BI_STATS_ALL_CMS") or "").strip().lower() in ("1", "true", "yes")


def read_bi_stats() -> pd.DataFrame:
    return to_workbook_frame(fetch_raw(), load_cm_map(), keep_unmapped=keep_all_cms())


def fetch_revision(cfg: Optional[Dict[str, Any]] = None) -> str:
    """Отпечаток данных: последняя загрузка + число строк (дёшево, один SELECT)."""
    cfg = cfg or _settings()
    conn = _connect(cfg)
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT MAX(`DATE_INSERTED`), COUNT(*) FROM {_table_ref(cfg)}")
            last, count = cur.fetchone()
    finally:
        conn.close()
    return f"{last}|{count}"


def describe_error(exc: BaseException) -> str:
    """Безопасное описание сбоя BI для лога, страницы и error.html.

    Только класс исключения и числовой код драйвера (``OperationalError 2003``).
    Текст ошибки PyMySQL не берётся совсем: он содержит адрес сервера и логин
    (ACL-103). На случай, если описание когда-нибудь дополнят, итог всё равно
    проходит общий скраббер — он прячет настроенные BI_MYSQL_*.
    """
    from src.fb_ads_rpa.run_telemetry import scrub

    text = type(exc).__name__
    code = exc.args[0] if getattr(exc, "args", None) else None
    if isinstance(code, int) and not isinstance(code, bool):
        text += f" {code}"
    return scrub(text)


def redact(text: str) -> str:
    """Текст чужой ошибки (например, архива) через общий скраббер."""
    from src.fb_ads_rpa.run_telemetry import scrub

    return scrub(text)


class BIUnavailable(FileNotFoundError):
    """BI не ответил, а книги-фолбэка нет. FileNotFoundError — чтобы страницы
    показали обычную ошибку «нет данных», а не 500."""


def known_revision() -> Optional[str]:
    """Последняя известная ревизия, без сети."""
    return _REV["value"]


def refresh_revision(*, force: bool = False) -> Optional[str]:
    """Обновить ревизию, если устарела (или force). Ошибка сети — старое значение."""
    with _REV_LOCK:
        if not force and _REV["value"] is not None and time.monotonic() - _REV["checked"] < REVISION_TTL:
            return _REV["value"]
        try:
            _REV["value"] = fetch_revision()
        except Exception:  # noqa: BLE001 — BI недоступен: решит загрузчик (фолбэк на книгу)
            if _REV["value"] is None:
                _REV["value"] = "unavailable"
        _REV["checked"] = time.monotonic()
        return _REV["value"]


_WATCHER: Dict[str, Any] = {"thread": None}


def ensure_revision_watcher() -> None:
    """Фоновый поток: раз в REVISION_TTL обновляет ревизию.

    Страница сама в BI не ходит (иначе запрос после загрузки BI ждал бы
    сеть и холодную сборку — ACL-1399). Поток лишь меняет ключ; peek видит
    новый ключ, и отчёт пересобирается фоном, как при новой книге.
    """
    with _REV_LOCK:
        thread = _WATCHER["thread"]
        if thread is not None and thread.is_alive():
            return

        def run() -> None:
            while bi_enabled():
                time.sleep(REVISION_TTL)
                refresh_revision(force=True)

        thread = threading.Thread(target=run, name="acl-bi-revision", daemon=True)
        _WATCHER["thread"] = thread
        thread.start()


def source_key(excel_key: Optional[Tuple[str, float, int]]) -> Optional[Tuple[Any, ...]]:
    """Ключ кэша отчёта: ревизия BI плюс ключ книги (книга — фолбэк)."""
    rev = known_revision()
    if rev is None:
        return None
    return ("bi", rev, excel_key)
