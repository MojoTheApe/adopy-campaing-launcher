"""Read/write config/rpa_variations/strategy_ranks.json for the hybrid learner."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[2]
RANKS_PATH = _ROOT / "config" / "rpa_variations" / "strategy_ranks.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_ranks(path: Path | None = None) -> dict[str, Any]:
    p = path or RANKS_PATH
    if not p.is_file():
        return {"version": 1, "updated_at": _now(), "steps": {}}
    return json.loads(p.read_text(encoding="utf-8"))


def save_ranks(data: dict[str, Any], path: Path | None = None) -> Path:
    p = path or RANKS_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    data["updated_at"] = _now()
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return p


def record_outcome(
    step_id: str,
    strategy_id: str,
    *,
    ok: bool,
    risk: str = "safe",
    note: str = "",
    evidence: str | None = None,
    path: Path | None = None,
) -> dict[str, Any]:
    """Update wins/fails; risky successes go to pending_approval."""
    data = load_ranks(path)
    steps = data.setdefault("steps", {})
    entry = steps.setdefault(
        step_id,
        {"risk": risk, "order": [], "stats": {}, "pending_approval": []},
    )
    entry["risk"] = entry.get("risk") or risk
    stats = entry.setdefault("stats", {})
    st = stats.setdefault(strategy_id, {"wins": 0, "fails": 0})
    if ok:
        st["wins"] = int(st.get("wins", 0)) + 1
    else:
        st["fails"] = int(st.get("fails", 0)) + 1
    if note:
        st["note"] = note
    if evidence:
        st["last_evidence"] = evidence
    st["last_ts"] = _now()

    if ok and (entry.get("risk") == "risky" or risk == "risky"):
        pending = entry.setdefault("pending_approval", [])
        pending.append(
            {
                "strategy_id": strategy_id,
                "ts": _now(),
                "evidence": evidence,
                "note": note,
            }
        )
    elif ok and risk == "safe":
        order = entry.setdefault("order", [])
        if strategy_id in order:
            order.remove(strategy_id)
        order.insert(0, strategy_id)

    save_ranks(data, path)
    return entry
