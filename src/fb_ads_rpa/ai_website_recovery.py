"""AI-assisted Conversion location → Website recovery over Multilogin CDP.

Flow:
  1) Deterministic meta_dom.select_conversion_location_website
  2) Optional LLM vision pass (Anthropic computer-style: screenshot → JSON action)
  3) Log outcome into config/rpa_variations/strategy_ranks.json (risky → pending_approval)

Does NOT replace Multilogin — attaches to an already-open profile via CDP.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class RecoveryResult:
    ok: bool = False
    strategy: str = ""
    detail: str = ""
    screenshot: str | None = None
    actions: list[str] = field(default_factory=list)


def discover_cdp_ports() -> list[int]:
    """Find --remote-debugging-port values from running Mimic/Chrome processes."""
    ports: list[int] = []
    try:
        import psutil
    except ImportError:
        return ports
    for proc in psutil.process_iter(["name", "cmdline"]):
        try:
            cmd = proc.info.get("cmdline") or []
            joined = " ".join(cmd)
            if "remote-debugging-port=" not in joined:
                continue
            if "mimic" not in joined.lower() and "chrome" not in (proc.info.get("name") or "").lower():
                continue
            for arg in cmd:
                if isinstance(arg, str) and "--remote-debugging-port=" in arg:
                    ports.append(int(arg.split("=", 1)[1]))
        except Exception:
            continue
    # unique, preserve order
    seen: set[int] = set()
    out: list[int] = []
    for p in ports:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def connect_page(cdp_url: str):
    """Return (playwright, browser, page) attached over CDP. Caller must stop playwright."""
    from playwright.sync_api import sync_playwright

    pw = sync_playwright().start()
    browser = pw.chromium.connect_over_cdp(cdp_url)
    # Prefer Ads Manager edit / campaigns page
    page = None
    for ctx in browser.contexts:
        for p in ctx.pages:
            u = (p.url or "").lower()
            if "adsmanager" in u or "facebook.com" in u:
                if page is None or "/edit/" in u:
                    page = p
    if page is None:
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
    try:
        page.bring_to_front()
    except Exception:
        pass
    return pw, browser, page


def _shot(page, label: str) -> str | None:
    try:
        from src.fb_ads_rpa.screenshots import capture

        return capture(page, label)
    except Exception as exc:
        log.debug("shot failed: %s", exc)
        return None


def try_deterministic_website(page) -> RecoveryResult:
    from src.fb_ads_rpa import meta_dom as md

    actions = ["dismiss_draft", "advantage_plus_off", "select_conversion_location_website"]
    try:
        md.dismiss_publish_draft_dialog(page)
        md._turn_off_advantage_plus_leads(page)
        md.dismiss_publish_draft_dialog(page)
        ok = md.select_conversion_location_website(page)
        verified = bool(ok and md.is_website_conversion_selected(page))
        shot = _shot(page, "ai_recovery_deterministic")
        return RecoveryResult(
            ok=verified,
            strategy="meta_dom_select_conversion_location_website",
            detail="verified Website" if verified else "select returned but not verified",
            screenshot=shot,
            actions=actions,
        )
    except Exception as exc:
        return RecoveryResult(
            ok=False,
            strategy="meta_dom_select_conversion_location_website",
            detail=str(exc)[:200],
            actions=actions,
        )


def _page_a11y_digest(page, limit: int = 80) -> str:
    """Compact accessibility-ish text list for the LLM."""
    script = """
    () => {
      const out = [];
      const push = (t) => {
        t = (t || '').replace(/\\s+/g, ' ').trim();
        if (t && t.length < 80) out.push(t);
      };
      for (const el of document.querySelectorAll(
        'button, [role=button], [role=radio], [role=option], [role=heading], label, a'
      )) {
        if (!el.offsetParent) continue;
        push(el.getAttribute('aria-label') || el.innerText || el.textContent);
        if (out.length >= 80) break;
      }
      return out;
    }
    """
    try:
        items = page.evaluate(script) or []
        return "\n".join(f"- {t}" for t in items[:limit])
    except Exception:
        return ""


def try_llm_vision_website(page, *, max_steps: int = 6) -> RecoveryResult:
    """Screenshot + DOM digest → LLM JSON actions → execute via Playwright/keyboard.

    Requires ANTHROPIC_API_KEY (preferred) or OPENAI_API_KEY.
    """
    api_key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    openai_key = (os.environ.get("OPENAI_API_KEY") or "").strip()
    if not api_key and not openai_key:
        return RecoveryResult(
            ok=False,
            strategy="ai_recovery_cdp",
            detail="No ANTHROPIC_API_KEY or OPENAI_API_KEY set",
        )

    from src.fb_ads_rpa import meta_dom as md

    actions_log: list[str] = []
    shot = _shot(page, "ai_recovery_before")

    for step in range(max_steps):
        if md.is_website_conversion_selected(page):
            return RecoveryResult(
                ok=True,
                strategy="ai_recovery_cdp",
                detail=f"Website selected after {step} LLM steps",
                screenshot=shot,
                actions=actions_log,
            )

        digest = _page_a11y_digest(page)
        png_b64 = None
        try:
            png = page.screenshot(type="png", full_page=False, timeout=8000)
            png_b64 = base64.standard_b64encode(png).decode("ascii")
        except Exception:
            pass

        prompt = (
            "You are recovering a Meta Ads Manager ad-set form.\n"
            "Goal: Conversion location must be exactly 'Website' "
            "(NOT 'Website and instant forms').\n"
            "Plan: open Conversion location dropdown if needed → expand Single → "
            "select Website (prefer keyboard Space on the Website row).\n"
            "Also turn Advantage+ leads Off if On.\n"
            "Return ONLY JSON: "
            '{"action":"click_text|press_key|scroll|done|fail","text":"...",'
            '"key":"Space|Enter|Escape","dy":200,"reason":"..."}\n'
            f"Visible controls:\n{digest}\n"
        )

        try:
            decision = _llm_decide(prompt, png_b64, anthropic_key=api_key, openai_key=openai_key)
        except Exception as exc:
            return RecoveryResult(
                ok=False,
                strategy="ai_recovery_cdp",
                detail=f"LLM error: {exc}"[:200],
                screenshot=shot,
                actions=actions_log,
            )

        action = (decision.get("action") or "fail").lower()
        actions_log.append(json.dumps(decision, ensure_ascii=False)[:160])
        log.info("ai_recovery step %s: %s", step, decision)

        if action == "done":
            break
        if action == "fail":
            break
        if action == "press_key":
            key = decision.get("key") or "Space"
            try:
                page.keyboard.press(key)
                time.sleep(0.5)
            except Exception:
                pass
        elif action == "scroll":
            dy = int(decision.get("dy") or 240)
            try:
                page.mouse.wheel(0, dy)
                time.sleep(0.4)
            except Exception:
                pass
        elif action == "click_text":
            text = (decision.get("text") or "").strip()
            if not text:
                continue
            try:
                # Prefer exact for Website
                exact = text.lower() in ("website", "single", "sitio web")
                loc = page.get_by_text(text, exact=exact).first
                if loc.is_visible(timeout=2000):
                    loc.scroll_into_view_if_needed(timeout=2000)
                    if text.lower() == "website":
                        loc.focus(timeout=1500)
                        page.keyboard.press("Space")
                    else:
                        loc.click(timeout=3000, force=True)
                    time.sleep(0.55)
            except Exception:
                pass

        try:
            md.dismiss_publish_draft_dialog(page)
        except Exception:
            pass

    verified = md.is_website_conversion_selected(page)
    shot2 = _shot(page, "ai_recovery_after")
    return RecoveryResult(
        ok=verified,
        strategy="ai_recovery_cdp",
        detail="verified" if verified else "LLM steps finished without Website",
        screenshot=shot2 or shot,
        actions=actions_log,
    )


def _llm_decide(
    prompt: str,
    png_b64: str | None,
    *,
    anthropic_key: str,
    openai_key: str,
) -> dict[str, Any]:
    if anthropic_key:
        return _anthropic_decide(prompt, png_b64, anthropic_key)
    return _openai_decide(prompt, png_b64, openai_key)


def _anthropic_decide(prompt: str, png_b64: str | None, api_key: str) -> dict[str, Any]:
    import urllib.request

    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    if png_b64:
        content.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": png_b64,
                },
            }
        )
    body = {
        "model": os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-20250514"),
        "max_tokens": 300,
        "messages": [{"role": "user", "content": content}],
    }
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "content-type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    text = ""
    for block in data.get("content") or []:
        if block.get("type") == "text":
            text += block.get("text") or ""
    return _parse_json_action(text)


def _openai_decide(prompt: str, png_b64: str | None, api_key: str) -> dict[str, Any]:
    import urllib.request

    user_content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    if png_b64:
        user_content.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{png_b64}"},
            }
        )
    body = {
        "model": os.environ.get("OPENAI_MODEL", "gpt-4.1-mini"),
        "messages": [{"role": "user", "content": user_content}],
        "max_tokens": 300,
    }
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "content-type": "application/json",
            "authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    text = data["choices"][0]["message"]["content"]
    return _parse_json_action(text)


def _parse_json_action(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        return {"action": "fail", "reason": "no json"}
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return {"action": "fail", "reason": "bad json", "raw": text[:120]}


def run_website_recovery(*, cdp_url: str) -> RecoveryResult:
    """Full recovery: deterministic then LLM; log ranks."""
    from src.fb_ads_rpa.variation_store import record_outcome

    pw, browser, page = connect_page(cdp_url)
    try:
        det = try_deterministic_website(page)
        record_outcome(
            "conversion_location_website",
            det.strategy,
            ok=det.ok,
            risk="risky",
            note=det.detail,
            evidence=det.screenshot,
        )
        if det.ok:
            return det

        llm = try_llm_vision_website(page)
        record_outcome(
            "conversion_location_website",
            llm.strategy or "ai_recovery_cdp",
            ok=llm.ok,
            risk="risky",
            note=llm.detail,
            evidence=llm.screenshot,
        )
        return llm
    finally:
        # Disconnect Playwright only — never kill the Multilogin Chrome process.
        try:
            pw.stop()
        except Exception:
            pass
