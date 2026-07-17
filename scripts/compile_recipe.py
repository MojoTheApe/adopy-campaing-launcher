"""Compile a raw Click Recorder recording into a semantic golden recipe.

Input:  output/rpa_recipes/<recording>.json   (dashboard Click Recorder output)
Output: config/rpa_recipes/campaign_golden.yaml (semantic, parameterized steps)

The golden recipe is the canonical "how to open a campaign" path. The Recipe
Player (Phase 1) executes each step with a ranked cascade of click strategies,
preferring the SEMANTIC matcher (own_text/role) and falling back to the raw
recorded css_path / coordinates only as a last resort.

Re-run this whenever you record a cleaner pass:
    python scripts/compile_recipe.py --in output/rpa_recipes/<file>.json

Design ref: docs/superpowers/specs/2026-07-15-agent10-rpa-variations-design.md
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_IN = _ROOT / "output" / "rpa_recipes" / "Saudi_Arabia_campaign_20260715_182143.json"
_DEFAULT_OUT = _ROOT / "config" / "rpa_recipes" / "campaign_golden.yaml"

# --- Parameterization -------------------------------------------------------
# Maps the concrete literals captured in THIS recording to recipe parameters.
# The Recipe Player resolves each {{param}} from the CampaignDraft at runtime.
# When you re-record, update these literals (or the value heuristics below).
PARAM_LITERALS: dict[str, str] = {
    "SA_ArF_All_Image_Day_30_15.07": "{{campaign_name}}",
    "saudi arabia": "{{geo}}",
    "Saudi Arabia": "{{geo}}",
    "Foreign exchange market, Investor, Stock exchange, Stock market": "{{interests}}",
    "2025 - por vida aramco": "{{page_name}}",
    "50": "{{daily_budget}}",
}

# Parameter <-> CampaignDraft source (documented in the YAML header for review).
PARAM_SOURCES: dict[str, str] = {
    "campaign_name": "draft.name",
    "adset_name": "draft.ad_sets[0].name (== draft.name in build_draft)",
    "daily_budget": "draft.ad_sets[0].daily_budget",
    "geo": "draft.country (resolved via meta_dom._COUNTRY_SEARCH)",
    "age_min": "draft.ad_sets[0].age_min",
    "age_max": "draft.ad_sets[0].age_max",
    "interests": "draft.ad_sets[0].targeting_label (comma-split -> add each)",
    "tracking_url": "draft.link",
    "page_name": "GAP: draft has page_id, not page_name (see header notes)",
    "creative_path": "draft.ad_sets[0].ads[i].creative_path (UPLOAD, not library pick)",
    "ad_title": "draft.ad_sets[0].ads[i].ad_title",
    "ad_body": "draft.ad_sets[0].ads[i].ad_body",
}

# Steps that must never auto-promote in the learner (need teach-mode approval).
RISKY_TEXTS = {"website", "lead", "publish"}


def _level_from_url(url: str) -> str:
    if "/campaigns/edit" in url:
        return "campaign"
    if "/adsets/edit" in url:
        return "adset"
    if "/ads/edit" in url:
        return "ad"
    return "create"


def _norm(s: str) -> str:
    return (s or "").replace("\u200b", "").strip()


def _param_for_value(value: str) -> str | None:
    """Map a recorded input value to a {{param}} (literal map -> heuristics)."""
    v = _norm(value)
    if not v:
        return None
    if v in PARAM_LITERALS:
        return PARAM_LITERALS[v]
    low = v.lower()
    if low in PARAM_LITERALS:
        return PARAM_LITERALS[low]
    if v.startswith("http://") or v.startswith("https://"):
        return "{{tracking_url}}"
    if v.replace(".", "", 1).isdigit():
        return "{{daily_budget}}"
    return None


def _is_risky(text: str) -> bool:
    t = _norm(text).lower()
    return any(r == t or t.startswith(r) for r in RISKY_TEXTS)


def _clean(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop noise and abandoned edits; collapse duplicate clicks.

    Filters:
      - keydowns (search-box space presses)
      - checkbox 'on'/'off' change echoes (preceding click already toggled)
      - accidental full-panel clicks (w>=1500 and h>=800)
      - abandoned edits: a change immediately followed by a 'Close' click
        (typed into the wrong field then dismissed) -> drop BOTH
      - consecutive duplicate clicks on the same target within 1.5s
    """
    tmp: list[dict[str, Any]] = []
    for a in actions:
        atype = a.get("type")
        if atype == "keydown":
            continue
        if atype == "change" and _norm(str(a.get("value", ""))).lower() in ("on", "off"):
            continue
        if atype == "click" and (a.get("w") or 0) >= 1500 and (a.get("h") or 0) >= 800:
            continue
        tmp.append(a)

    cleaned: list[dict[str, Any]] = []
    i = 0
    while i < len(tmp):
        a = tmp[i]
        nxt = tmp[i + 1] if i + 1 < len(tmp) else None
        if a.get("type") == "change" and nxt and nxt.get("type") == "click":
            ntext = _norm(nxt.get("own_text") or nxt.get("full_text") or "").lower()
            if ntext == "close":
                i += 2  # abandoned edit + its Close dialog
                continue
        if cleaned and a.get("type") == "click" and cleaned[-1].get("type") == "click":
            prev = cleaned[-1]
            same = (
                a.get("css_path") == prev.get("css_path")
                and _norm(a.get("full_text", "")) == _norm(prev.get("full_text", ""))
            )
            close = abs(a.get("ts", 0) - prev.get("ts", 0)) < 1500
            if same and close:
                i += 1
                continue
        cleaned.append(a)
        i += 1
    return cleaned


def _section_for(level: str, text: str, tag: str, role: str) -> str:
    t = _norm(text).lower()
    if level == "create":
        if "create" == t:
            return "create"
        if "leads" in t:
            return "objective"
        if "continue" in t:
            return "objective_continue"
    if "website and instant forms" in t:
        return "conversion_location"
    if t in ("single", "website", "multiple"):
        return "conversion_location"
    if "select an event" in t or t == "lead":
        return "conversion_event"
    if "location" in t or "saudi" in t:
        return "audience_geo"
    if t.startswith("age") or t == "18":
        return "audience_age"
    if "detailed targeting" in t or "demographics" in t:
        return "targeting"
    if "placement" in t or "facebook, instagram" in t:
        return "placements"
    if "set up creative" in t or "image ad" in t or "upload" in t:
        return "ad_creative"
    return ""


def _step_from_action(a: dict[str, Any]) -> dict[str, Any]:
    level = _level_from_url(a.get("url", ""))
    tag = a.get("tag", "")
    role = a.get("role", "")
    own = _norm(a.get("own_text", ""))
    full = _norm(a.get("full_text", ""))
    text = own or full
    atype = a.get("type")

    # Build semantic matcher (own_text/role preferred; research-backed resilience)
    match: dict[str, Any] = {}
    if own:
        match["own_text"] = own
    elif full:
        match["text"] = full[:60]
    if role:
        match["role"] = role
    if tag:
        match["tag"] = tag
    if a.get("aria_label"):
        match["aria_label"] = a["aria_label"]

    # Stable CSS anchors for Meta's modal buttons (dynamic ids elsewhere).
    css_path = a.get("css_path", "")
    if "layerConfirm" in css_path:
        match["css"] = "button.layerConfirm"
    elif "layerButton" in css_path:
        match["css"] = "button.layerButton"

    # The Website conversion pick: exact own_text, exclude hybrid labels.
    if _norm(text).lower() == "website":
        match = {"own_text": "Website", "exact": True,
                 "exclude": ["instant", "calls", "messenger"]}

    # Recorded fallback (used only if semantic match fails)
    recorded: dict[str, Any] = {
        "css_path": css_path,
        "x": a.get("x"),
        "y": a.get("y"),
        "w": a.get("w"),
        "h": a.get("h"),
    }
    if a.get("key"):
        recorded["key"] = a["key"]

    step: dict[str, Any] = {
        "left_tab": level,
        "section": _section_for(level, text, tag, role),
        "action": "fill" if atype == "change" else "click",
        "risk": "risky" if _is_risky(text) else "safe",
        "match": match,
        "recorded": recorded,
    }

    if atype == "change":
        value = a.get("value", "")
        param = _param_for_value(value)
        # Ad-set level name reuses the campaign-name literal; bind it honestly.
        if param == "{{campaign_name}}" and level == "adset":
            param = "{{adset_name}}"
        if param:
            step["param"] = param
        step["recorded_value"] = value[:120]
        # A fill needs a stable target: prefer the input's css_path
        step["match"] = {"tag": tag or "INPUT", "role": role} if not own else match
        # FB Page pick is best-effort: auto-select if resolvable, else hand-off.
        if param == "{{page_name}}":
            step["optional"] = True
            step["on_fail"] = "skip"

    # Ad-level media tile (big square, ~104x104) -> upload the local creative file.
    if (
        atype == "click"
        and level == "ad"
        and 80 <= (a.get("w") or 0) < 400
        and 80 <= (a.get("h") or 0) < 400
    ):
        step["action"] = "upload_creative"
        step["section"] = "ad_creative"
        step["param"] = "{{creative_path}}"
        step.pop("verify", None)

    if text and _is_risky(text):
        step["verify"] = {"selected": text}

    return step


def compile_recipe(in_path: Path, out_path: Path) -> dict[str, Any]:
    data = json.loads(in_path.read_text(encoding="utf-8"))
    actions = _clean(data.get("actions", []))

    steps: list[dict[str, Any]] = []
    for i, a in enumerate(actions):
        step = _step_from_action(a)
        step = {"id": f"s{i:02d}_{step['section'] or step['left_tab']}", **step}
        steps.append(step)

    recipe = {
        "name": "campaign_golden",
        "version": 1,
        "compiled_at": datetime.now().isoformat(timespec="seconds"),
        "source_recording": in_path.name,
        "endpoint": "handoff",  # never auto-Publish
        "param_sources": PARAM_SOURCES,
        "notes": [
            "Semantic-first: player matches by own_text/role; css_path/x,y are last-resort.",
            "GAP page_name: recording selects FB Page by name; draft only has page_id.",
            "GAP creative: recording picked an already-uploaded tile; player should UPLOAD",
            "  draft creative_path via set_input_files instead of library pick.",
            "GAP dataset: 'FacebookElf191' was pre-selected; treat as verify, not fill.",
            "Ad copy (primary text / headline) was NOT typed in this recording.",
        ],
        "steps": steps,
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        yaml.safe_dump(recipe, sort_keys=False, allow_unicode=True, width=100),
        encoding="utf-8",
    )
    return recipe


def main() -> None:
    ap = argparse.ArgumentParser(description="Compile a recording into the golden recipe.")
    ap.add_argument("--in", dest="in_path", default=str(_DEFAULT_IN))
    ap.add_argument("--out", dest="out_path", default=str(_DEFAULT_OUT))
    args = ap.parse_args()

    in_path = Path(args.in_path)
    out_path = Path(args.out_path)
    if not in_path.is_file():
        raise SystemExit(f"Recording not found: {in_path}")

    recipe = compile_recipe(in_path, out_path)
    steps = recipe["steps"]
    risky = [s for s in steps if s["risk"] == "risky"]
    fills = [s for s in steps if s["action"] == "fill"]
    params = sorted({s["param"] for s in steps if s.get("param")})
    print(f"Compiled {len(steps)} steps -> {out_path}")
    print(f"  levels: " + ", ".join(
        f"{lvl}={sum(1 for s in steps if s['left_tab'] == lvl)}"
        for lvl in ("create", "campaign", "adset", "ad")
    ))
    print(f"  fills: {len(fills)}  risky: {len(risky)}")
    print(f"  params used: {', '.join(params) or '(none)'}")


if __name__ == "__main__":
    main()
