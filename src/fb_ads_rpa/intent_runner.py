"""IVHP Intent → Verify → Heal → Promote runner (Campaign name first).

Click/fill alone is not success. Success = DOM verify. Failures write an
evidence pack and update strategy_ranks.json.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from playwright.sync_api import Page

from src.fb_ads_rpa import meta_dom as md
from src.fb_ads_rpa.variation_store import load_ranks, record_outcome

log = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parents[2]
_INTENTS_DIR = _ROOT / "config" / "rpa_intents"
_EVIDENCE_ROOT = _ROOT / "output" / "rpa_evidence"
RANKS_PATH = _ROOT / "config" / "rpa_variations" / "strategy_ranks.json"


@dataclass
class IntentOutcome:
    ok: bool
    verified: bool
    strategy: str = ""
    detail: str = ""
    evidence_dir: str | None = None
    tried: list[dict[str, Any]] = field(default_factory=list)


def load_intent(intent_id: str, *, intents_dir: Path | None = None) -> dict[str, Any]:
    base = intents_dir or _INTENTS_DIR
    path = base / f"{intent_id}.yaml"
    if not path.is_file():
        raise FileNotFoundError(f"Intent not found: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not data.get("id"):
        data["id"] = intent_id
    return data


def values_match(expected: str, actual: str) -> bool:
    e = (expected or "").strip()
    a = (actual or "").strip()
    if not e:
        return False
    if not a:
        return False
    return e == a or e in a or a in e


def verify_input_near_label(
    page: Page,
    *,
    labels: list[str],
    expected: str,
) -> tuple[bool, str]:
    current = ""
    try:
        current = md.read_input_near_label(page, labels) or ""
    except Exception as exc:
        log.debug("read_input_near_label failed: %s", exc)
    return values_match(expected, current), current


def _aria_to_markdown(node: Any, *, depth: int = 0) -> str:
    if not isinstance(node, dict):
        return ""
    indent = "  " * depth
    role = node.get("role") or "unknown"
    name = (node.get("name") or "").replace("\n", " ").strip()[:80]
    line = f"{indent}- {role}" + (f": {name}" if name else "")
    kids = node.get("children") or []
    parts = [line]
    if depth < 6:
        for ch in kids[:40]:
            parts.append(_aria_to_markdown(ch, depth=depth + 1))
    return "\n".join(p for p in parts if p)


def capture_aria_snapshot(page: Page) -> str:
    try:
        snap = page.accessibility.snapshot()
        if snap:
            return _aria_to_markdown(snap)
    except Exception as exc:
        log.debug("accessibility.snapshot failed: %s", exc)
    # Fallback: list textboxes / buttons via JS
    try:
        return page.evaluate(
            """() => {
              const lines = [];
              for (const el of document.querySelectorAll(
                'input, textarea, [contenteditable=true], [role=textbox], button, [role=button]'
              )) {
                if (el.offsetParent === null) continue;
                const role = el.getAttribute('role') || el.tagName.toLowerCase();
                const name = (el.getAttribute('aria-label') || el.innerText || el.placeholder || '')
                  .replace(/\\s+/g, ' ').trim().slice(0, 60);
                const val = ('value' in el ? el.value : '') || '';
                lines.push(`- ${role}: ${name}${val ? ' = ' + String(val).slice(0, 40) : ''}`);
                if (lines.length >= 80) break;
              }
              return lines.join('\\n') || '- (empty)';
            }"""
        )
    except Exception:
        return "- (aria unavailable)"


def _capture_screenshot_bytes(page: Page) -> bytes | None:
    try:
        return page.screenshot(type="png", full_page=False)
    except Exception:
        return None


def write_evidence_pack(
    page: Page,
    *,
    step_id: str,
    run_dir: Path,
    strategies_tried: list[dict[str, Any]],
    detail: str = "",
    aria_text: str | None = None,
    screenshot_bytes: bytes | None = None,
) -> Path:
    pack = run_dir / step_id
    pack.mkdir(parents=True, exist_ok=True)
    (pack / "url.txt").write_text((page.url or "")[:500] + "\n", encoding="utf-8")
    (pack / "detail.txt").write_text((detail or "") + "\n", encoding="utf-8")
    (pack / "strategies_tried.json").write_text(
        json.dumps(strategies_tried, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    aria = aria_text if aria_text is not None else capture_aria_snapshot(page)
    (pack / "aria_snapshot.md").write_text(aria + "\n", encoding="utf-8")
    shot = screenshot_bytes if screenshot_bytes is not None else _capture_screenshot_bytes(page)
    if shot:
        (pack / "screenshot.png").write_bytes(shot)
    return pack


def _try_get_by_label_type(page: Page, labels: list[str], value: str) -> bool:
    for lab in labels:
        try:
            el = page.get_by_label(re.compile(rf"^{re.escape(lab)}$", re.I)).first
            if not el.is_visible(timeout=1500):
                continue
            el.click(timeout=2000)
            el.fill("")
            el.type(value, delay=20)
            time.sleep(0.35)
            return True
        except Exception:
            continue
    return False


def run_intent(
    page: Page,
    intent_id: str,
    *,
    value: str,
    run_id: str | None = None,
    intents_dir: Path | None = None,
) -> IntentOutcome:
    """Run IVHP for a fill intent. Currently implements campaign_name strategies."""
    intent = load_intent(intent_id, intents_dir=intents_dir)
    labels = list(intent.get("labels") or intent.get("verify", {}).get("labels") or [])
    risk = str(intent.get("risk") or "safe")
    tried: list[dict[str, Any]] = []
    run_id = run_id or datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    evidence_root = _EVIDENCE_ROOT / run_id

    if not value or not str(value).strip():
        return IntentOutcome(ok=False, verified=False, detail="empty value", tried=tried)

    # Always wait editor first if listed
    for strat in intent.get("strategies") or []:
        sid = str(strat.get("id") or "")
        kind = str(strat.get("kind") or "")
        if kind == "wait" or sid == "wait_editor_saved":
            try:
                md.wait_editor_saved(page, timeout_s=15)
                tried.append({"id": sid or "wait_editor_saved", "ok": True})
            except Exception as exc:
                tried.append({"id": sid or "wait_editor_saved", "ok": False, "err": str(exc)[:80]})
            break

    last_strategy = ""
    for strat in intent.get("strategies") or []:
        sid = str(strat.get("id") or "")
        kind = str(strat.get("kind") or "")
        if kind == "wait" or sid == "wait_editor_saved":
            continue
        last_strategy = sid
        ok_act = False
        try:
            if kind == "fill_verified" or sid == "fill_near_label_verified":
                ok_act = bool(md.fill_input_near_label_verified(page, labels, value))
            elif kind == "fill" or sid == "fill_near_label":
                ok_act = bool(md.fill_input_near_label(page, labels, value))
            elif kind == "get_by_label" or sid == "get_by_label_type":
                ok_act = _try_get_by_label_type(page, labels, value)
            else:
                tried.append({"id": sid, "ok": False, "err": f"unknown kind {kind}"})
                continue
        except Exception as exc:
            tried.append({"id": sid, "ok": False, "err": str(exc)[:100]})
            continue

        verified, current = verify_input_near_label(page, labels=labels, expected=value)
        tried.append(
            {
                "id": sid,
                "ok": bool(ok_act and verified),
                "acted": ok_act,
                "verified": verified,
                "current": (current or "")[:80],
            }
        )
        if ok_act and verified:
            try:
                record_outcome(
                    intent_id,
                    sid,
                    ok=True,
                    risk=risk,
                    note="ivhp_verify_ok",
                    path=RANKS_PATH,
                )
            except Exception as exc:
                log.debug("record_outcome failed: %s", exc)
            return IntentOutcome(
                ok=True,
                verified=True,
                strategy=sid,
                detail=f"value={(current or '')[:60]}",
                tried=tried,
            )

        # Acted but verify failed — count as strategy fail
        try:
            record_outcome(
                intent_id,
                sid,
                ok=False,
                risk=risk,
                note=f"verify_fail current={(current or '')[:40]}",
                path=RANKS_PATH,
            )
        except Exception:
            pass

    # All strategies failed — evidence pack
    verified, current = verify_input_near_label(page, labels=labels, expected=value)
    pack = write_evidence_pack(
        page,
        step_id=intent_id,
        run_dir=evidence_root,
        strategies_tried=tried,
        detail=f"expected={value!r} current={current!r}",
    )
    try:
        record_outcome(
            intent_id,
            last_strategy or "all",
            ok=False,
            risk=risk,
            note="ivhp_all_failed",
            evidence=str(pack),
            path=RANKS_PATH,
        )
    except Exception:
        pass
    return IntentOutcome(
        ok=False,
        verified=False,
        strategy=last_strategy,
        detail=f"expected={value!r} current={current!r}",
        evidence_dir=str(pack),
        tried=tried,
    )
