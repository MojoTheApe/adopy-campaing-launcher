"""Сборщики payload для событий журнала.

Чистые функции: ничего не читают у Meta и не открывают браузер, поэтому
тестируются без профиля. Все вызовы из чужих модулей идут через
`src.acl_event_log` ленивым импортом внутри `try/except`.
"""
from __future__ import annotations

import os
import time
from typing import Any

from src.acl_event_log.clean import canonical_hash, link_fingerprint

#: Зеркало `run_telemetry.record`: только шаги API-пути и тайминги.
RAW_RUN_PREFIXES = ("api_", "fb_api_")
RAW_RUN_STEPS = {"job_phase", "graph_retry"}

#: Ключи слота, которые попадают в `slots_brief` (ссылки здесь нет).
_SLOT_KEYS = ("name", "targeting", "copy", "copy2", "copy_more", "creative_id", "creative_type",
              "lead_form_template", "adset_index", "adset_name", "budget")

_RESULT_KEYS = ("campaign_name", "published_as", "status", "success", "publishable", "error",
                "steps_completed", "campaign_id", "adset_id", "adset_ids", "ad_ids", "creative_ids",
                "problems", "warnings", "blocking_failures", "manual_required", "created", "reused",
                "readback")

_MEDIA_KEYS = ("ad_id", "creative_id", "image_hash", "video_id", "vertical_image_hash",
               "vertical_video_id", "reused", "file_md5")

_STATUS_RANK = {"error": 3, "warning": 2, "warn": 2, "ok": 1}


def slots_brief(slots: list[dict]) -> list[dict]:
    """Слоты задания только с разрешёнными ключами: имена записей библиотек, без ссылки."""
    out = []
    for slot in slots or []:
        if isinstance(slot, dict):
            out.append({k: slot[k] for k in _SLOT_KEYS if k in slot})
    return out


def _file_size(path: str) -> int | None:
    try:
        return os.stat(path).st_size   # файл не читаем
    except Exception:
        return None


def spec_payload(plan: Any, facts: Any) -> dict:
    """Полный план кампании для `publish.spec`; `spec_hash` — от всего остального."""
    from src.fb_api.creator import BID_STRATEGY, OBJECTIVE

    adsets, ads = [], []
    for s in plan.ad_sets:
        adsets.append({
            "index": s.index, "name": s.name, "destination": s.destination,
            "age_min": s.age_min, "age_max": s.age_max, "gender": s.gender, "os": s.os,
            "os_min": s.os_min, "device": s.device, "languages": list(s.languages),
            "start_time": s.start_time, "placements": s.placements,
            "interest_groups": s.interest_groups,
        })
        for ad in s.ads:
            ads.append({
                "index": ad.index, "name": ad.name, "kind": ad.kind,
                "file_size": _file_size(ad.path), "title": ad.title, "body": ad.body,
                "description": ad.description, "titles": list(ad.titles),
                "bodies": list(ad.bodies), "descriptions": list(ad.descriptions),
                "cta": ad.cta, "form_template": ad.form_template,
                "vertical": bool(ad.vertical_path),
            })
    spec: dict[str, Any] = {
        "campaign_name": plan.name, "objective": OBJECTIVE, "bid_strategy": BID_STRATEGY,
        "daily_budget": plan.daily_budget, "country": plan.country, "language": plan.language,
        "adsets": adsets, "ads": ads, "link": link_fingerprint(plan.link),
        "currency": getattr(facts, "currency", ""),
    }
    spec["spec_hash"] = canonical_hash(spec)
    return spec


def _media_brief(ad_media: dict) -> dict:
    return {str(idx): {k: rec[k] for k in _MEDIA_KEYS if k in rec}
            for idx, rec in (ad_media or {}).items() if isinstance(rec, dict)}


def stats_payload(name: str, spec_hash: str, *, outcome: Any = None, plan: Any = None,
                  began: float, calls: int, writes: int, raised: str | None = None) -> dict:
    """Итог работы создателя: тайминги, число вызовов и что создано."""
    out: dict[str, Any] = {
        "campaign_name": name, "spec_hash": spec_hash,
        "duration_s": round(time.monotonic() - began, 1),
        "calls": calls, "writes": writes, "raised": raised,
    }
    if outcome is not None:
        out.update({
            "stop_kind": outcome.stop_kind, "published_as": outcome.published_as,
            "tag": getattr(plan, "tag", None), "mismatches": outcome.mismatches,
            "ad_media": _media_brief(outcome.ad_media),
        })
    return out


def result_payload(res: dict) -> dict:
    """Итог кампании как есть, без новых полей."""
    return {k: res.get(k) for k in _RESULT_KEYS}


def check_payload(result: dict, *, plan_hash_value: str, duration_s: float) -> dict:
    """Итог проверки плана: вердикты правил (худший по кампаниям) и блокирующие правила."""
    verdicts: dict[str, str] = {}
    rows = [r for res in result.get("results") or [] for r in (res.get("audit") or [])]
    if result.get("account_audit"):
        rows.append(result["account_audit"])
    for row in rows:
        rule, status = row.get("rule"), str(row.get("status") or "")
        if rule and _STATUS_RANK.get(status, 0) >= _STATUS_RANK.get(verdicts.get(rule, ""), 0):
            verdicts[rule] = status
    return {
        "plan_hash": plan_hash_value, "duration_s": round(duration_s, 1),
        "verdicts": verdicts,
        "blocking_rules": [r for r, s in verdicts.items() if s == "error"],
        "error_kind": result.get("error_kind") or "",
        "campaigns": [res.get("campaign_name") for res in result.get("results") or []],
    }


def account_payload(act: str, row: dict | None, acc: dict | None = None, facts: Any = None) -> dict:
    """Состояние кабинета из того, что `preflight_account` уже прочитал."""
    row = row or {}
    return {
        "act": act, "access": bool(row),
        "account_status": row.get("account_status"), "disable_reason": row.get("disable_reason"),
        "currency": getattr(facts, "currency", None),
        "timezone_name": getattr(facts, "timezone_name", None),
        "tz_offset_h": getattr(facts, "utc_offset_hours", None),
        "pages_n": len(getattr(facts, "pages", []) or []) if facts is not None else None,
        "pixels_n": len(getattr(facts, "pixels", []) or []) if facts is not None else None,
    }
