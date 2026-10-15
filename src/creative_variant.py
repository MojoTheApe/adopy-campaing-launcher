"""ACL-1593: вариант креатива — ведущее число имени файла.

`1_es_2403_post.mp4` — креатив 2403, вариант 1. Номер варианта нужен в двух
местах: робот пишет его в имя объявления (`AdDraft.ad_name`), а дашборд
подставляет его в разбивку креативов для объявлений, построенных ДО этого
изменения, — по собственным записям робота (`ad_creative_pair`).
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

# Вариант — одно-двузначное число в самом начале имени. Четырёхзначное в начале
# («2403_es_post.mp4») — это уже номер креатива, а не вариант.
_LEADING_VARIANT_RE = re.compile(r"^(\d{1,2})(?=[_\-\s])")


def norm_variant(value: Any) -> str:
    """Число варианта без ведущих нулей: «01» и «1» — один вариант."""
    v = str(value or "").strip()
    return str(int(v)) if v.isdigit() else v


def file_variant(filename: str) -> str:
    """Вариант из имени файла или "" — когда ведущего числа нет."""
    stem = (filename or "").rsplit("/", 1)[-1].rsplit("\\", 1)[-1].strip()
    m = _LEADING_VARIANT_RE.match(stem)
    return norm_variant(m.group(1)) if m else ""


def variant_map(records: Iterable[dict[str, Any]]) -> dict[tuple[str, str], str]:
    """{(кампания, номер креатива): вариант} по записям `ad_creative_pair`.

    Берётся только однозначное соответствие: имя объявления в записях робота не
    сохранено, поэтому если в одной кампании один креатив шёл двумя разными
    вариантами, какое объявление какое — не известно, и пары в карте нет.
    Повтор записи при ретрае (тот же файл) неоднозначностью не считается.
    """
    seen: dict[tuple[str, str], set[str]] = {}
    for rec in records:
        if rec.get("step") != "ad_creative_pair":
            continue
        diag = rec.get("diag") or {}
        campaign = str(rec.get("campaign") or "").strip()
        number = str(diag.get("number") or "").strip()
        if not campaign or not number:
            continue
        # Тот же файл, что лёг в объявление: медиа объявления — карточка Square.
        seen.setdefault((campaign, number), set()).add(file_variant(str(diag.get("square") or "")))
    return {k: next(iter(v)) for k, v in seen.items() if len(v) == 1 and next(iter(v))}


_MARK = "ad_creative_pair"
#: (путь, mtime_ns, размер) -> карта. Дашборд строит отчёт часто, журнал
#: растёт до десятков мегабайт, а менялся он с прошлого раза редко.
_cache: dict[str, Any] = {"sig": None, "map": {}}


def _read_pairs(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            # Дешёвый отсев до разбора JSON: нужных строк — доли процента.
            if _MARK not in line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:  # noqa: BLE001
                continue
    return out


def load_variant_map(path: Path | None = None) -> dict[tuple[str, str], str]:
    """Карта по журналу этой машины; нет журнала или он битый — пустая карта."""
    try:
        if path is None:
            from src.fb_ads_rpa import run_telemetry

            path = run_telemetry.TELEMETRY_PATH
        path = Path(path)
        st = path.stat()
        sig = (str(path), st.st_mtime_ns, st.st_size)
        if _cache["sig"] == sig:
            return _cache["map"]
        vmap = variant_map(_read_pairs(path))
        _cache["sig"], _cache["map"] = sig, vmap
        return vmap
    except Exception:  # noqa: BLE001 — подстановка не должна ронять отчёт
        return {}


def apply_backfill(df: pd.DataFrame, vmap: dict[tuple[str, str], str] | None = None) -> pd.DataFrame:
    """Заполнить пустой `VariantId` из записей робота; заполненный не трогать.

    В записи робота нет аккаунта, только кампания. Если одно имя кампании
    встречается в таблице у нескольких аккаунтов или стран, запись могла
    относиться к любому из них — такие строки не трогаем.
    """
    if "VariantId" not in df.columns or "Adset Name" not in df.columns:
        return df
    if vmap is None:
        vmap = load_variant_map()
    if not vmap:
        return df
    df = df.copy()
    adset = df["Adset Name"].fillna("").astype(str).str.strip()
    cid = df["CreativeId"].fillna("").astype(str).str.strip()
    owners = [c for c in ("Account Alias", "Country Final") if c in df.columns]
    shared = pd.Series(False, index=df.index)
    if owners:
        multi = (df.groupby(adset)[owners].nunique() > 1).any(axis=1)
        shared = adset.map(multi).fillna(False).astype(bool)
    keys = pd.MultiIndex.from_arrays([adset, cid])
    table = pd.Series(vmap)
    table.index = pd.MultiIndex.from_tuples(list(vmap.keys()))
    found = pd.Series(table.reindex(keys).to_numpy(), index=df.index).fillna("")
    cur = df["VariantId"].fillna("").astype(str)
    df["VariantId"] = cur.where((cur != "") | shared, found)
    return df
