"""Live LLM-driven Ads Manager publisher (Option B — replaces the static
recipe_player.py flow).

Why: recipe_player.py replays a pre-recorded YAML script blindly. Every fix this
session (label disambiguation, wizard-open false positives, dialog-close guards,
click fallback cascades) was needed because a *static* script cannot tell the
difference between "I clicked Next and it worked" and "I clicked Next and Meta's
UI silently ignored it" — it has no way to look at the result and adapt.

This module asks Claude to look at the actual accessibility tree after every
action and decide the next one, the same way a human (or the way this session's
debugging worked: read a screenshot, decide what's wrong, try something) would.
Slower and has a real per-run API cost, but far more resilient to Meta's UI
drift and the "click succeeded, nothing happened" failure class.

Requires ANTHROPIC_API_KEY in .env (a normal Anthropic API key — separate
billing from any Claude Code subscription).
"""
from __future__ import annotations

import base64
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any

from playwright.sync_api import Page

from src.fb_ads_rpa import meta_dom as md
from src.fb_ads_rpa import wizard_guard as wg
from src.fb_ads_rpa.draft import CampaignDraft, RpaPublishResult
from src.fb_ads_rpa.intent_runner import capture_aria_snapshot
from src.fb_ads_rpa.screenshots import capture as capture_screenshot

log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-sonnet-4-5"
DEFAULT_MAX_STEPS = 70

TOOLS = [
    {
        "name": "read_page",
        "description": (
            "Get the current page's accessibility tree (roles, names, values of "
            "visible interactive elements) plus the current URL. Call this first, "
            "and again after every click/fill to see what actually changed — do "
            "not assume an action worked just because it didn't error."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "click",
        "description": "Click an element identified by its accessibility role and exact visible name/text.",
        "input_schema": {
            "type": "object",
            "properties": {
                "role": {"type": "string", "description": "ARIA role, e.g. 'button', 'radio', 'tab', 'heading', 'option', 'link'"},
                "name": {"type": "string", "description": "Exact visible text / accessible name of the element"},
            },
            "required": ["role", "name"],
        },
    },
    {
        "name": "fill",
        "description": "Type a value into a text input/textarea, found near its visible label.",
        "input_schema": {
            "type": "object",
            "properties": {
                "label": {"type": "string", "description": "The field's visible label, e.g. 'Campaign name', 'Daily budget'"},
                "value": {"type": "string"},
            },
            "required": ["label", "value"],
        },
    },
    {
        "name": "press_key",
        "description": (
            "Focus an element by role+name and press a single key (e.g. ' ' for "
            "Space, 'Enter'). Some Meta controls (radio-like option rows) only "
            "respond to a keyboard Space/Enter after focus, not a mouse click."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "role": {"type": "string"},
                "name": {"type": "string"},
                "key": {"type": "string"},
            },
            "required": ["role", "name", "key"],
        },
    },
    {
        "name": "screenshot",
        "description": (
            "Take a screenshot when the accessibility tree alone doesn't explain "
            "the page (e.g. layout/visual questions, or read_page looks empty/"
            "stale). Use sparingly — prefer read_page, it's cheaper and usually enough."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "wait",
        "description": "Wait briefly for the page to finish an animation/transition, then check again with read_page.",
        "input_schema": {
            "type": "object",
            "properties": {"seconds": {"type": "number", "description": "0.5-3 seconds"}},
            "required": ["seconds"],
        },
    },
    {
        "name": "finish",
        "description": (
            "Call this when every field has been set and verified (campaign left "
            "as Draft — you must NOT click Publish), or when you are genuinely "
            "stuck after multiple different attempts at the same step."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "success": {"type": "boolean"},
                "summary": {"type": "string", "description": "What was completed, or exactly where/why you're stuck"},
            },
            "required": ["success", "summary"],
        },
    },
]


@dataclass
class _ToolOutcome:
    content: Any  # str or list[content-block-dict] (for images)


class LLMAgentPlayer:
    """Drop-in alternative to recipe_player.RecipePlayer — same call shape
    (publish_campaign(draft, dry_run=...) -> RpaPublishResult), so
    rpa_publisher.py can select either engine via FB_RPA_ENGINE."""

    def __init__(self, page: Page, *, model: str | None = None, max_steps: int = DEFAULT_MAX_STEPS):
        self.page = page
        self.model = model or os.environ.get("FB_RPA_LLM_MODEL", DEFAULT_MODEL)
        self.max_steps = max_steps
        self._client = None  # lazy — don't require the key until actually used

    def _get_client(self):
        if self._client is None:
            import anthropic

            api_key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
            if not api_key:
                raise RuntimeError(
                    "ANTHROPIC_API_KEY not set in .env — required for FB_RPA_ENGINE=llm_agent. "
                    "Get a key at https://console.anthropic.com (API Keys)."
                )
            self._client = anthropic.Anthropic(api_key=api_key)
        return self._client

    # ── system prompt ───────────────────────────────────────────────────────

    def _build_system_prompt(self, draft: CampaignDraft, *, dry_run: bool) -> str:
        ad_set = draft.ad_sets[0] if draft.ad_sets else None
        ads_desc = ""
        if ad_set:
            for i, ad in enumerate(ad_set.ads, start=1):
                ads_desc += (
                    f"\n  Ad {i}: creative_type={ad.creative_type!r}, "
                    f"creative_filename={ad.creative_filename!r} (pick this file from the "
                    f"account's existing media library — do not try to upload from disk), "
                    f"headline/title={ad.ad_title!r}, primary_text/body={ad.ad_body!r}, "
                    f"link_description={ad.ad_link_description!r}"
                )
        publish_rule = (
            "You must leave the campaign as a DRAFT. Fill every field, verify it, then call "
            "finish(success=True, ...) — do NOT click 'Publish' or 'Review and publish', ever, "
            "in this run."
            if dry_run or draft.status == "DRAFT"
            else "The campaign should end ACTIVE — after every field is filled and verified, "
            "click the 'Publish' button as the very last action."
        )
        return f"""You control a Playwright browser already open on Facebook Ads Manager's
campaigns list page for this ad account (the campaign creation wizard is NOT
open yet — you start from the plain list view).

Your tools only let you act by ARIA role + exact visible text/label — you never
see raw HTML or CSS. Call read_page after every action to see the real result;
Meta's interface sometimes reports a click as successful when nothing actually
changed — always verify by reading the page again, don't assume.

Be efficient — you have a limited number of steps for the whole campaign:
- Prefer read_page over screenshot. screenshot is for when read_page's text
  genuinely doesn't explain the layout — most of the time it does. Don't
  screenshot after every single action "just to check".
- For fill: the *accessible label* Meta exposes is not always the same as the
  visible placeholder text. read_page's output shows each textbox/combobox
  with its real accessible name — use exactly that string for `label`, not a
  guess. If a fill doesn't verify, look at the read_page output again for the
  exact wording rather than guessing a third or fourth variant blind.
- Once something is filled and verified, move on — don't re-click or re-fill
  a field that already shows the right value "to be sure".
- `fill` only works on text inputs/textareas. Conversion location, Locations,
  and similar dropdown/option pickers are NOT fillable — you select them with
  `click` (or `press_key` if click doesn't register), never `fill`.
- Do not click "Next" until the current panel's required fields are actually
  set and verified — if you click Next too early and the panel doesn't
  advance, that's Meta telling you something on this panel is still incomplete;
  read_page to see what, don't just click Next again or click "Back" and retry
  blindly.
- If two attempts at the same sub-step both fail to change anything, STOP —
  don't start clicking nearby buttons/toggles/icons hoping to stumble onto
  progress (this reliably makes things worse, e.g. accidentally toggling an
  unrelated setting or landing on the account toolbar). Instead call
  screenshot, look at it carefully, and reason in plain text about exactly
  what you now see before choosing one specific next action.

Goal — fill these fields in order, verifying each before moving to the next:
1. Click "Create" to start a new campaign.
2. If a chooser/dialog for AI-guided vs manual setup appears, click "Manual campaign setup".
3. Select objective "Leads", then click "Continue" — this opens the Campaign panel.
4. Campaign name: {draft.name!r}
5. Daily budget: {ad_set.daily_budget if ad_set else "?"} (USD)
6. Click Next to advance to the Ad set panel. Verify with read_page that the
   panel actually changed (e.g. "Ad set name" is now visible) — if it still
   shows "Campaign name", the click had no effect; try a different Next-button
   match (there may be more than one element with that text) or wait 1s and retry.
7. Ad set name: {ad_set.name if ad_set else draft.name!r}
8. Conversion location: must end up as exactly "Website" (NOT "Website and instant
   forms" — that's the default, wrong value). This is a known-tricky step, do it
   in this exact order:
   a. Click the dropdown/combobox currently showing "Website and instant forms"
      to open it.
   b. read_page — the menu that opened shows "Multiple" and "Single" as two
      sections/headings (Single is usually below Multiple, may need scrolling —
      if you don't see "Single" in read_page yet, it may just not be rendered
      in the snapshot; try clicking a heading/option literally named "Single" anyway).
   c. Click the "Single" heading/option to expand it. read_page again — you
      should now see a "Website" radio/option (distinct from "Website and
      instant forms", which is a *different* element — don't confuse them).
   d. Click the exact "Website" option. If the click doesn't change anything
      (verify with read_page — is_website_conversion_selected-equivalent: does
      the dropdown/summary now say just "Website"?), try press_key on that same
      "Website" element with key=" " (Space) instead — Meta's radio-like rows
      here are known to sometimes ignore mouse clicks but respond to keyboard.
9. Conversion event: "Lead".
10. Audience: countries = {draft.country!r}, age {ad_set.age_min if ad_set else "?"}-{ad_set.age_max if ad_set else "?"}, targeting/interest = {ad_set.targeting_label if ad_set else "(none — leave broad)"!r}.
11. Click Next to advance to the Ad panel.
12. Ads (this campaign has {len(ad_set.ads) if ad_set else 0} ad(s)):{ads_desc or " (none configured)"}
13. Website URL / tracking link for the ad: {draft.link!r}

{publish_rule}

Never click: "Discard drafts", "Close" on the wizard's own chrome, "Review and
publish", any "About ..." help link, or "Learn more" — these exit the flow or
destroy work. If a click is refused as forbidden, that is working as intended —
find a different, more specific target instead of retrying the same one blindly.

If a step genuinely won't work after 2-3 different approaches, move on to
finish(success=False, summary=...) describing exactly what's stuck and what you
tried — don't loop forever on one field."""

    # ── tool execution ──────────────────────────────────────────────────────

    def _resolve(self, role: str, name: str, *, timeout: int = 2500):
        """Resolve role+name to a single element, tolerant of Meta's real accessible
        names (trailing zero-width spaces, odd whitespace) without falling back to
        blind substring matching that could confuse e.g. "Website" with "Website
        and instant forms". Returns (locator_or_None, note)."""
        # 1) Playwright's own exact match (trims real whitespace; usually enough).
        try:
            loc = self.page.get_by_role(role, name=name, exact=True).first
            if loc.count() and loc.is_visible(timeout=timeout):
                return loc, "exact"
        except Exception:
            pass
        # 2) Substring match, but only auto-pick it if truly unambiguous — Meta's
        # names often carry a trailing U+200B or extra space that breaks exact
        # matching even though the visible text is identical.
        try:
            candidates = self.page.get_by_role(role, name=name, exact=False)
            n = candidates.count()
            if n == 1:
                loc = candidates.first
                if loc.is_visible(timeout=timeout):
                    return loc, "substring"
            elif n > 1:
                texts = []
                for i in range(min(n, 6)):
                    try:
                        texts.append((candidates.nth(i).inner_text(timeout=400) or "").strip()[:60])
                    except Exception:
                        pass
                return None, f"ambiguous: {n} elements match {role} containing {name!r}: {texts}"
        except Exception:
            pass
        return None, "not found"

    def _click(self, role: str, name: str) -> _ToolOutcome:
        reason = wg.forbidden_click_reason(name)
        if reason:
            return _ToolOutcome(f"REFUSED: {reason!r} is a protected element ({reason}). Pick a different target.")
        loc, note = self._resolve(role, name)
        if loc is None:
            return _ToolOutcome(f"FAILED to click {role} {name!r}: {note}. Try read_page for the exact current text.")
        try:
            loc.scroll_into_view_if_needed(timeout=3000)
            loc.click(timeout=5000)
            return _ToolOutcome(f"Clicked {role} {name!r} ({note} match).")
        except Exception as exc:
            return _ToolOutcome(f"FAILED to click {role} {name!r}: {exc}")

    def _fill(self, label: str, value: str) -> _ToolOutcome:
        # Strategy 1: JS value-set + verify (fast, works for plain inputs).
        try:
            if md.fill_input_near_label_verified(self.page, [label], value):
                return _ToolOutcome(f"Filled {label!r} with {value!r} and verified.")
        except Exception:
            pass
        # Strategy 2: real keyboard typing — some Meta fields are React-controlled
        # and silently revert a JS-set value unless input comes from real key
        # events. This is the strategy that reliably worked for campaign_name
        # earlier in this project (see intent_runner.py's get_by_label_type).
        try:
            loc, note = self._resolve("textbox", label)
            if loc is None:
                combo, note2 = self._resolve("combobox", label)
                loc = combo
            if loc is not None:
                loc.click(timeout=2000)
                loc.fill("")
                loc.type(value, delay=20)
                time.sleep(0.4)
                try:
                    current = loc.input_value(timeout=800) or ""
                except Exception:
                    current = ""
                if value.strip() in current or current.strip() == value.strip():
                    return _ToolOutcome(f"Filled {label!r} with {value!r} via keyboard typing and verified.")
        except Exception:
            pass
        return _ToolOutcome(
            f"Fill of {label!r} did not verify with either strategy (JS value-set or keyboard "
            f"typing). The field may not exist with that exact label/placeholder — read_page "
            f"again and check the precise textbox/combobox name shown there."
        )

    def _press_key(self, role: str, name: str, key: str) -> _ToolOutcome:
        reason = wg.forbidden_click_reason(name)
        if reason:
            return _ToolOutcome(f"REFUSED: {name!r} is a protected element ({reason}).")
        loc, note = self._resolve(role, name)
        if loc is None:
            return _ToolOutcome(f"FAILED to press {key!r} on {role} {name!r}: {note}.")
        try:
            loc.scroll_into_view_if_needed(timeout=3000)
            loc.focus(timeout=3000)
            loc.press(key, timeout=3000)
            return _ToolOutcome(f"Focused {role} {name!r} ({note} match) and pressed {key!r}.")
        except Exception as exc:
            return _ToolOutcome(f"FAILED to press {key!r} on {role} {name!r}: {exc}")

    def _read_page(self) -> _ToolOutcome:
        try:
            snapshot = capture_aria_snapshot(self.page)
        except Exception as exc:
            snapshot = f"(snapshot failed: {exc})"
        url = (self.page.url or "")[:200]
        return _ToolOutcome(f"URL: {url}\n\n{snapshot}")

    def _screenshot(self) -> _ToolOutcome:
        # Plain page.screenshot() regularly times out on Meta's heavy Ads Manager
        # pages (documented pain point elsewhere in this codebase) — reuse the
        # same CDP-first capture the rest of the RPA stack relies on.
        try:
            path = capture_screenshot(self.page, "llm_agent_view")
            if not path:
                return _ToolOutcome("Screenshot skipped: the browser window is minimized.")
            with open(path, "rb") as f:
                img_bytes = f.read()
            if not img_bytes:
                return _ToolOutcome("Screenshot capture produced an empty file.")
            b64 = base64.b64encode(img_bytes).decode()
            return _ToolOutcome(
                [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}}]
            )
        except Exception as exc:
            return _ToolOutcome(f"FAILED to screenshot: {exc}")

    def _wait(self, seconds: float) -> _ToolOutcome:
        capped = max(0.2, min(float(seconds), 5.0))
        self.page.wait_for_timeout(capped * 1000)
        return _ToolOutcome(f"Waited {capped}s.")

    def _execute_tool(self, name: str, tool_input: dict[str, Any]) -> Any:
        if name == "read_page":
            out = self._read_page()
        elif name == "click":
            out = self._click(tool_input.get("role", ""), tool_input.get("name", ""))
        elif name == "fill":
            out = self._fill(tool_input.get("label", ""), tool_input.get("value", ""))
        elif name == "press_key":
            out = self._press_key(tool_input.get("role", ""), tool_input.get("name", ""), tool_input.get("key", ""))
        elif name == "screenshot":
            out = self._screenshot()
        elif name == "wait":
            out = self._wait(float(tool_input.get("seconds", 1)))
        else:
            out = _ToolOutcome(f"Unknown tool {name!r}")
        return out.content

    # ── main loop ────────────────────────────────────────────────────────────

    def publish_campaign(self, draft: CampaignDraft, *, dry_run: bool = True) -> RpaPublishResult:
        result = RpaPublishResult(campaign_name=draft.name)
        if draft.errors:
            result.error = "; ".join(draft.errors)
            return result

        client = self._get_client()
        system_prompt = self._build_system_prompt(draft, dry_run=dry_run)
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": "Begin. Call read_page first to see the current state of the wizard."}
        ]

        for step_num in range(self.max_steps):
            try:
                response = client.messages.create(
                    model=self.model,
                    max_tokens=1024,
                    system=system_prompt,
                    messages=messages,
                    tools=TOOLS,
                )
            except Exception as exc:
                result.error = f"Anthropic API call failed at step {step_num}: {exc}"
                result.ok = False
                return result

            messages.append({"role": "assistant", "content": response.content})

            tool_results: list[dict[str, Any]] = []
            finished = False
            for block in response.content:
                if getattr(block, "type", None) != "tool_use":
                    continue
                if block.name == "finish":
                    result.ok = bool(block.input.get("success"))
                    if not result.ok:
                        result.error = block.input.get("summary") or "LLM agent reported failure"
                    result.steps_completed.append(f"finish(success={result.ok}): {block.input.get('summary', '')[:100]}")
                    finished = True
                    tool_results.append(
                        {"type": "tool_result", "tool_use_id": block.id, "content": "Run finished."}
                    )
                    continue
                content = self._execute_tool(block.name, block.input)
                # Log a short outcome tag alongside the action so the step log is
                # readable without opening screenshots — was this actually acted
                # on, refused by the safety guard, or did it fail outright?
                content_str = content if isinstance(content, str) else "(image)"
                if content_str.startswith("REFUSED"):
                    tag = "refused"
                elif content_str.startswith("FAILED"):
                    tag = "failed"
                elif "did not verify" in content_str:
                    tag = "unverified"
                else:
                    tag = "ok"
                result.steps_completed.append(f"{block.name}({block.input}):{tag}")
                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": content})

            if finished:
                break
            if not tool_results:
                messages.append(
                    {
                        "role": "user",
                        "content": "Call one of your tools (read_page/click/fill/press_key/wait/screenshot/finish).",
                    }
                )
                continue
            messages.append({"role": "user", "content": tool_results})
        else:
            result.ok = False
            result.error = f"LLM agent exceeded max_steps ({self.max_steps}) without calling finish()"

        try:
            final = capture_screenshot(self.page, "llm_agent_final")
            if final:
                result.screenshot_paths.append(final)
        except Exception:
            pass
        return result
