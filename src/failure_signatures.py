"""Повторяющиеся отказы телеметрии: «N машин падают на одном шаге».

ACL-402. Три машины, упавшие на одном шаге по одной причине, — это правило, а
не три инцидента. Модуль только СВОДИТ отказы в сигнатуры и говорит, насколько
им можно верить. Он ничего не предлагает применить, не правит селекторы и не
раздаёт «исправление»: превращение сводки в правило — отдельный ручной шаг,
который утверждает владелец (решение 2026-10-20).

Сигнатура = (шаг, причина). Причина берётся там же, где её кладут сами шаги
(`note`, затем `diag.why/error/how`), и нормализуется: цифры, uuid и длинные
идентификаторы заменены метками, чтобы «таймаут 30 с» и «таймаут 45 с» были
одной сигнатурой. В вывод идёт только очищенный `run_telemetry.scrub` текст.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import date, timedelta
from typing import Any

from src.fb_ads_rpa.run_telemetry import scrub

#: Сколько примеров с датой/машиной/прогоном хранить на сигнатуру.
MAX_EVIDENCE = 3
#: Длина причины в сводке: достаточно, чтобы узнать отказ, мало, чтобы унести дамп.
REASON_LIMIT = 160
#: Пустая причина — не «нет отказа», а «шаг не объяснил себя».
NO_REASON = "(причина не указана)"

_UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
_HEX = re.compile(r"\b[0-9a-f]{12,}\b", re.I)
_NUM = re.compile(r"\d+(?:[.,]\d+)?")
_SPACE = re.compile(r"\s+")


def _diag(row: dict[str, Any]) -> dict[str, Any]:
    d = row.get("diag")
    if isinstance(d, dict):
        return d
    if isinstance(d, str) and d.strip().startswith("{"):
        try:
            parsed = json.loads(d)
        except ValueError:  # в таблице diag обрезан до 900 знаков — может не разобраться
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def raw_reason(row: dict[str, Any]) -> str:
    """Чем шаг объяснил отказ: note, затем diag.why / error / how."""
    note = str(row.get("note") or "").strip()
    if note:
        return note
    diag = _diag(row)
    for key in ("why", "error", "how"):
        value = diag.get(key)
        if value:
            return str(value)
    return ""


def normalise_reason(text: str) -> str:
    """Очистить от секретов и свести к виду, одинаковому у разных машин."""
    out = scrub(text or "")
    out = _UUID.sub("<id>", out)
    out = _HEX.sub("<id>", out)
    out = _NUM.sub("#", out)
    out = _SPACE.sub(" ", out).strip().lower()
    if len(out) > REASON_LIMIT:
        out = out[:REASON_LIMIT].rstrip() + "…"
    return out or NO_REASON


def confidence(machines: int, incidents: int) -> str:
    """high — 3+ машин; medium — 2; low — одна машина (общий отказ не доказан)."""
    if machines >= 3 and incidents >= 3:
        return "high"
    if machines >= 2:
        return "medium"
    return "low"


def _day(ts: str) -> str:
    return (ts or "")[:10]


def find_repeated(
    rows: list[dict[str, Any]],
    days: int = 7,
    *,
    today: date | None = None,
    min_incidents: int = 2,
) -> dict[str, Any]:
    """Сгруппировать отказы окна в сигнатуры; вернуть только повторившиеся.

    `fleet` — сколько машин вообще присылало данные в окне: «все 3 из 3» и
    «3 из 40» — разные новости, а без знаменателя их не отличить.
    """
    cutoff = ((today or date.today()) - timedelta(days=days)).isoformat()
    window = [r for r in rows if _day(str(r.get("ts", ""))) >= cutoff and r.get("step")]
    fleet = {str(r.get("machine")) for r in window if r.get("machine")}

    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in window:
        if str(r.get("ok")) in ("0", "False", "false"):
            groups[(str(r["step"]), normalise_reason(raw_reason(r)))].append(r)

    signatures = []
    for (step, reason), items in groups.items():
        if len(items) < min_incidents:
            continue
        machines = sorted({str(i.get("machine")) for i in items if i.get("machine")})
        items.sort(key=lambda i: str(i.get("ts", "")))
        signatures.append({
            "step": step,
            "reason": reason,
            "incidents": len(items),
            "runs": len({i.get("run_id") for i in items if i.get("run_id")}),
            "machines": machines,
            "versions": sorted({str(i.get("flow_version_at_upload")) for i in items
                                if i.get("flow_version_at_upload")}),
            "first": _day(str(items[0].get("ts", ""))),
            "last": _day(str(items[-1].get("ts", ""))),
            "confidence": confidence(len(machines), len(items)),
            "evidence": [
                {"ts": str(i.get("ts", "")), "machine": str(i.get("machine", "")),
                 "run_id": str(i.get("run_id", "")),
                 "version": str(i.get("flow_version_at_upload", ""))}
                for i in items[-MAX_EVIDENCE:]
            ],
        })
    order = {"high": 0, "medium": 1, "low": 2}
    signatures.sort(key=lambda s: (order[s["confidence"]], -len(s["machines"]), -s["incidents"], s["step"]))
    return {"days": days, "cutoff": cutoff, "fleet": sorted(fleet), "signatures": signatures}


def render_markdown(result: dict[str, Any]) -> list[str]:
    """Строки раздела для сводки телеметрии. Правил не предлагает."""
    sigs = result["signatures"]
    fleet = result["fleet"]
    lines = [f"## Повторяющиеся отказы за {result['days']} дн.", ""]
    lines += [
        "> Это сводка для разбора, а не правило: что делать с повтором, решает человек.",
        "> Автоматически ничего не применяется и не раздаётся.",
        "",
    ]
    if not sigs:
        return lines + ["Отказов, повторившихся с одной причиной, нет.", ""]
    if len(fleet) < 2:
        lines += ["⚠️ Данные только с одной машины — общий это отказ или местный, сказать нельзя.", ""]
    lines += ["| Уверенность | Шаг | Причина | Машин | Отказов | Прогонов | Версии | Период |",
              "|---|---|---|---|---|---|---|---|"]
    for s in sigs:
        reason = s["reason"].replace("|", "\\|")
        lines.append(
            f"| {s['confidence']} | `{s['step']}` | {reason} | {len(s['machines'])} из {len(fleet)} "
            f"| {s['incidents']} | {s['runs']} | {', '.join(s['versions']) or '—'} "
            f"| {s['first']} — {s['last']} |"
        )
    lines += ["", "Последние примеры (для поиска в журналах):", ""]
    for s in sigs[:10]:
        ev = "; ".join(f"{e['ts'][:16]} {e['machine']} run {e['run_id'] or '—'}" for e in s["evidence"])
        lines.append(f"- `{s['step']}`: {ev}")
    lines.append("")
    return lines
