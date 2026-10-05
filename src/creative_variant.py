"""ACL-1593: вариант креатива — ведущее число имени файла.

`1_es_2403_post.mp4` — креатив 2403, вариант 1. Номер варианта нужен в двух
местах: робот пишет его в имя объявления (`AdDraft.ad_name`), а дашборд
подставляет его в разбивку креативов для объявлений, построенных ДО этого
изменения, — по собственным записям робота (`ad_creative_pair`).
"""
from __future__ import annotations

import re
from typing import Any, Iterable

import pandas as pd

# Вариант — одно-двузначное число в самом начале имени. Четырёхзначное в начале
# («2403_es_post.mp4») — это уже номер креатива, а не вариант.
_LEADING_VARIANT_RE = re.compile(r"^(\d{1,2})(?=[_\-\s])")


def file_variant(filename: str) -> str:
    """Вариант из имени файла или "" — когда ведущего числа нет."""
    stem = (filename or "").rsplit("/", 1)[-1].rsplit("\\", 1)[-1].strip()
    m = _LEADING_VARIANT_RE.match(stem)
    return m.group(1) if m else ""


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


def load_variant_map() -> dict[tuple[str, str], str]:
    """Карта по журналу этой машины; нет журнала или он битый — пустая карта."""
    try:
        from src.fb_ads_rpa import run_telemetry

        return variant_map(run_telemetry.load())
    except Exception:  # noqa: BLE001 — подстановка не должна ронять отчёт
        return {}


def apply_backfill(df: pd.DataFrame, vmap: dict[tuple[str, str], str] | None = None) -> pd.DataFrame:
    """Заполнить пустой `VariantId` из записей робота; заполненный не трогать."""
    if "VariantId" not in df.columns or "Adset Name" not in df.columns:
        return df
    if vmap is None:
        vmap = load_variant_map()
    if not vmap:
        return df
    df = df.copy()

    def _fill(row) -> str:
        if row["VariantId"]:
            return row["VariantId"]
        key = (str(row["Adset Name"] or "").strip(), str(row["CreativeId"] or "").strip())
        return vmap.get(key, "")

    df["VariantId"] = df.apply(_fill, axis=1)
    return df
