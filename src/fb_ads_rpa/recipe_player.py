"""Recipe Player — execute the compiled golden recipe with a strategy cascade.

The player is DATA-DRIVEN from config/rpa_recipes/campaign_golden.yaml. It:
  - resolves {{params}} from the CampaignDraft,
  - runs SIMPLE steps (Create, Leads, Continue, names, budget, Next, crops) with a
    generic click/fill cascade (semantic locator -> keyboard -> raw CDP -> css -> coords),
  - DELEGATES the hard ad-set/ad sections (conversion location/event, geo, age,
    targeting, placements, creative upload) to the battle-tested meta_dom handlers,
  - verifies risky steps, logs every outcome, screenshots transitions/failures,
  - ends in hand-off (never clicks Publish).

Why hybrid: the recording's ad-set micro-clicks use dynamic #js_ ids; replaying them
literally is brittle. The recipe supplies order + params + verification; the robust
handlers do the risky clicking. This is the surface Phase 2's learner plugs into.

Enable with FB_RPA_RECIPE_PLAYER=1 (see config.get_use_recipe_player).
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml
from playwright.sync_api import Page

from src.fb_ads_rpa import intent_runner as ir
from src.fb_ads_rpa import meta_dom as md
from src.fb_ads_rpa import wizard_guard as wg
from src.fb_ads_rpa.config import get_recipe_stop_after
from src.fb_ads_rpa.pacing import pause_medium, pause_short
from src.fb_ads_rpa.screenshots import capture as capture_screenshot

log = logging.getLogger(__name__)

_RECIPE_PATH = Path(__file__).resolve().parents[2] / "config" / "rpa_recipes" / "campaign_golden.yaml"

# Sections handled by specialized meta_dom functions (raw sub-steps are skipped).
_DELEGATED_SECTIONS = {
    "conversion_location",
    "conversion_event",
    "audience_geo",
    "audience_age",
    "targeting",
    "placements",
}


@dataclass
class StepOutcome:
    step_id: str
    action: str
    ok: bool
    strategy: str = ""
    detail: str = ""


@dataclass
class RecipeRunResult:
    ok: bool = False
    steps: list[StepOutcome] = field(default_factory=list)
    screenshots: list[str] = field(default_factory=list)
    error: str | None = None

    def log_line(self) -> str:
        return " -> ".join(f"{o.step_id}:{'ok' if o.ok else 'FAIL'}" for o in self.steps)


def load_recipe(path: str | Path | None = None) -> dict[str, Any]:
    p = Path(path) if path else _RECIPE_PATH
    if not p.is_file():
        raise FileNotFoundError(f"Golden recipe not found: {p}")
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


class RecipePlayer:
    def __init__(
        self,
        page: Page,
        draft: Any,
        *,
        recipe_path: str | Path | None = None,
        screenshot_dir: str = "output/rpa_screenshots",
    ) -> None:
        self.page = page
        self.draft = draft
        self.recipe = load_recipe(recipe_path)
        self.steps: list[dict[str, Any]] = self.recipe.get("steps", [])
        self.by_id = {s["id"]: s for s in self.steps}
        self.screenshot_dir = screenshot_dir
        self.result = RecipeRunResult()
        self._ad = self._first_ad()
        self._guard_recover_attempts = 0
        self._run_id = time.strftime("%Y%m%d_%H%M%S")

    # ── params ────────────────────────────────────────────────────────────────
    def _first_ad(self) -> Any:
        try:
            return self.draft.ad_sets[0].ads[0]
        except Exception:
            return None

    def _param_value(self, token: str) -> str | None:
        """Resolve a {{param}} token to a concrete value from the draft."""
        name = token.strip().strip("{}").strip()
        d = self.draft
        aset = d.ad_sets[0] if getattr(d, "ad_sets", None) else None
        if name == "campaign_name":
            return d.name
        if name == "adset_name":
            return aset.name if aset else d.name
        if name == "daily_budget":
            b = aset.daily_budget if aset else None
            if b is None:
                return None
            return str(int(b)) if float(b).is_integer() else str(b)
        if name == "geo":
            return d.country
        if name == "interests":
            return aset.targeting_label if aset else None
        if name == "tracking_url":
            return d.link or None
        if name == "creative_path":
            return self._ad.creative_path if self._ad else None
        if name == "page_name":
            return None  # best-effort; player picks first available page
        return None

    def _resolve(self, step: dict[str, Any]) -> str | None:
        param = step.get("param")
        if param and "{{" in str(param):
            return self._param_value(param)
        return param

    # ── screenshots / logging ──────────────────────────────────────────────────
    def _shot(self, label: str) -> None:
        try:
            path = capture_screenshot(self.page, label, base_dir=self.screenshot_dir)
            self.result.screenshots.append(path)
        except Exception as exc:
            log.debug("screenshot %s failed: %s", label, exc)

    def _record(self, step_id: str, action: str, ok: bool, strategy: str = "", detail: str = "") -> None:
        self.result.steps.append(StepOutcome(step_id, action, ok, strategy, detail))
        log.info("recipe step %s [%s] %s via %s %s", step_id, action, "OK" if ok else "FAIL", strategy, detail)

    def _guards(self) -> None:
        """Global pre-step guards that unblock the UI."""
        try:
            md.dismiss_discard_drafts_dialog(self.page)
        except Exception:
            pass
        try:
            md.dismiss_publish_draft_dialog(self.page)
        except Exception:
            pass
        # Measurement banner: dismiss via X — never "About this update"
        try:
            if wg.dismiss_measurement_banner(self.page):
                pause_short(self.page)
        except Exception:
            pass
        try:
            wg.dismiss_account_switcher(self.page)
        except Exception:
            pass
        try:
            md._dismiss_floating_banners(self.page)
        except Exception:
            pass
        # If a prior mis-click left Ads Manager (news/help), recover before next step
        self._ensure_not_left_ads_manager(context="pre_step")
        # Clear empty-state filters that hide the Create flow context
        try:
            clear_btn = self.page.get_by_role("button", name=re.compile(r"^Clear filter$", re.I)).first
            if clear_btn.is_visible(timeout=400):
                clear_btn.click(timeout=2000, force=True)
                pause_short(self.page)
        except Exception:
            pass

    def _wizard_is_open(self) -> bool:
        """True only when campaign/ad-set edit chrome is visible (not manage list)."""
        try:
            if self._page_has_wizard(self.page):
                return True
        except Exception:
            pass
        try:
            from src.fb_ads_rpa.ads_manager_flow import _is_campaign_wizard_ready

            if _is_campaign_wizard_ready(self.page):
                return True
        except Exception:
            pass
        # NOTE: no longer falls back to "/edit/" in url alone — same class of false
        # positive as elsewhere (Meta can leave a stale /edit/ URL after the wizard
        # closed). Both real checks above already failed, so it's genuinely closed.
        return False

    def _ensure_not_left_ads_manager(self, *, context: str = "") -> bool:
        """If URL is business news/help, back + reopen *edit wizard*. Manage list alone is not OK."""
        cfg = wg.load_forbidden_zones()
        if not wg.url_left_ads_manager(self.page.url, cfg=cfg):
            # Still may be on manage list after a partial recover — caller uses _ensure_on_wizard
            return True
        if self._guard_recover_attempts >= cfg.max_recover_attempts:
            self._shot("player_guard_recover_exhausted")
            self._record(
                "wizard_guard",
                "recover",
                False,
                "exhausted",
                f"{context} url={(self.page.url or '')[:80]}",
            )
            return False
        self._guard_recover_attempts += 1
        self._shot(f"player_guard_left_{context or 'nav'}")
        rec = wg.recover_from_bad_url(
            self.page,
            reopen_fn=self._reopen_wizard_from_list,
            switch_wizard_fn=lambda: self._switch_to_wizard_tab(timeout_s=8),
            cfg=cfg,
        )
        # recover may have switched tabs via context — re-bind if Ads Manager tab exists
        try:
            for p in self.page.context.pages:
                if wg.is_ads_manager_url(p.url) and not wg.url_left_ads_manager(p.url, cfg=cfg):
                    self.page = p
                    break
        except Exception:
            pass
        # Always demand real wizard chrome after leaving news/help
        wizard_ok = False
        if self._switch_to_wizard_tab(timeout_s=6) and self._wizard_is_open():
            wizard_ok = True
        elif self._reopen_wizard_from_list() and self._wizard_is_open():
            wizard_ok = True
            if not rec.ok:
                rec = wg.RecoverResult(ok=True, action="forced_reopen", detail=(self.page.url or "")[:80])
        ok = bool(rec.ok and wizard_ok)
        self._record(
            "wizard_guard",
            "recover",
            ok,
            rec.action if ok else (rec.action or "no_wizard"),
            f"{context} {rec.detail}",
        )
        self._shot("player_guard_recover")
        return ok

    # ── generic executors ──────────────────────────────────────────────────────
    def _semantic_locator(self, match: dict[str, Any]):
        """Build a Playwright locator from the semantic matcher, or None."""
        page = self.page
        own = match.get("own_text")
        text = match.get("text")
        role = (match.get("role") or "").strip()
        css = match.get("css")
        if css:
            return page.locator(css).first
        label = match.get("label")
        if label:
            try:
                return page.get_by_label(label, exact=True).first
            except Exception:
                pass
        if own is not None:
            pat = re.compile(rf"^{re.escape(own)}$", re.I) if match.get("exact") else own
            if role:
                try:
                    return page.get_by_role(role, name=pat).first
                except Exception:
                    pass
            return page.get_by_text(pat).first
        if text:
            # Use role+text when both are available — avoids grabbing unrelated
            # elements (e.g. "Create view" instead of the campaign "Create" button).
            if role:
                try:
                    exact_pat = re.compile(rf"^{re.escape(text)}$", re.I)
                    return page.get_by_role(role, name=exact_pat).first
                except Exception:
                    pass
            return page.get_by_text(text[:40]).first
        if role:
            return page.get_by_role(role).first
        return None

    def _nav_own_text(self, match: dict[str, Any]) -> str | None:
        own = (match.get("own_text") or match.get("text") or "").strip()
        if own.lower() in ("next", "continue"):
            return own
        return None

    def _click_footer_nav(self, own_text: str) -> bool:
        """Click wizard footer Next/Continue via live bbox CDP — never stale recorded coords.

        Multilogin Playwright clicks often miss (#41286) and land on the measurement
        banner 'About this update' → business/news/click-attribution.
        """
        page = self.page
        try:
            wg.dismiss_measurement_banner(page)
        except Exception:
            pass
        try:
            wg.dismiss_account_switcher(page)
        except Exception:
            pass
        # Dismiss value-rules / insight tooltips that steal clicks
        try:
            tip = page.get_by_text(re.compile(r"Value rules", re.I)).first
            if tip.is_visible(timeout=300):
                page.keyboard.press("Escape")
                pause_short(page)
        except Exception:
            pass

        pat = re.compile(rf"^{re.escape(own_text)}$", re.I)
        candidates = []
        for role in ("button", "link"):
            try:
                loc = page.get_by_role(role, name=pat)
                n = min(loc.count(), 10)
                for i in range(n):
                    el = loc.nth(i)
                    try:
                        if not el.is_visible(timeout=300):
                            continue
                        box = el.bounding_box()
                        if not box or box.get("width", 0) < 8 or box.get("height", 0) < 8:
                            continue
                        candidates.append((box["y"], box, el))
                    except Exception:
                        continue
            except Exception:
                pass
        # Also div[role=button] with exact own text (Meta footer pattern)
        try:
            loc = page.locator(f'div[role="button"]:has-text("{own_text}")')
            n = min(loc.count(), 10)
            for i in range(n):
                el = loc.nth(i)
                try:
                    if not el.is_visible(timeout=300):
                        continue
                    txt = (el.inner_text(timeout=300) or "").strip()
                    if not pat.match(txt.split("\n")[0].strip()):
                        continue
                    box = el.bounding_box()
                    if not box or box.get("width", 0) < 8:
                        continue
                    candidates.append((box["y"], box, el))
                except Exception:
                    continue
        except Exception:
            pass

        if not candidates:
            # Last resort: Meta's footer "Next" is sometimes a plain <div> with
            # no role attribute at all — confirmed live (2026-07-20) via DevTools:
            # exactly one element on the Campaign panel has own (direct-text-node)
            # text "Next", tag DIV, role=null. Neither role-based locator above
            # can ever match that. Scan by own-text instead, same pattern already
            # used successfully for Single/Website in meta_dom.py.
            own_text_script = """
            (target) => {
              const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
              const ownText = (el) => norm(
                Array.from(el.childNodes).filter((n) => n.nodeType === 3)
                  .map((n) => n.textContent).join('')
              );
              const out = [];
              for (const el of document.querySelectorAll('div, span, a, button')) {
                if (!el.offsetParent) continue;
                if (ownText(el).toLowerCase() !== target.toLowerCase()) continue;
                const r = el.getBoundingClientRect();
                if (r.width < 8 || r.height < 8) continue;
                out.push({ x: r.x, y: r.y, width: r.width, height: r.height });
              }
              return out;
            }
            """
            for root in (page, *[f for f in page.frames if f != page.main_frame]):
                try:
                    boxes = root.evaluate(own_text_script, own_text)
                except Exception:
                    boxes = []
                for box in boxes or []:
                    candidates.append((box["y"], box, None))

        if not candidates:
            return False
        # Prefer the lowest (footer) control
        candidates.sort(key=lambda t: t[0], reverse=True)
        _y, box, el = candidates[0]
        # Refuse if the control text is a forbidden help link
        if el is not None:
            try:
                t = (el.inner_text(timeout=300) or "").strip().split("\n")[0][:60]
                if wg.forbidden_click_reason(t):
                    return False
            except Exception:
                pass
        cx = box["x"] + box["width"] / 2
        cy = box["y"] + box["height"] / 2
        # Banner strip is usually y < 80 — refuse clicks that high for Next
        if own_text.lower() == "next" and cy < 100:
            log.warning("footer Next bbox too high (y=%.0f) — refusing (banner risk)", cy)
            return False
        try:
            md._cdp_mouse_click(page, cx, cy)
            return True
        except Exception:
            if el is not None:
                try:
                    el.click(timeout=3000)
                    return True
                except Exception:
                    return False
            return False

    def _generic_click(self, step: dict[str, Any]) -> bool:
        """Cascade: semantic -> keyboard focus+Enter -> raw CDP bbox -> css -> coords."""
        match = step.get("match", {})
        rec = step.get("recorded", {})

        blocked = wg.forbidden_match_reason(match)
        if blocked:
            self._record(step["id"], "click", False, "forbidden_zone", blocked)
            self._shot(f"player_forbidden_{step['id']}")
            log.warning("recipe step %s blocked by forbidden zone: %s", step["id"], blocked)
            return False

        nav = self._nav_own_text(match)

        def _after_click(strategy: str) -> bool:
            pause_short(self.page)
            self._shot(f"player_after_{step['id']}_click")
            # Diagnostic: any click strategy can report success while Meta silently
            # ignores it (still showing the same panel) — this left the wizard
            # stuck at Campaign level (current_step=0) with s07 marked ok, no
            # matter which of the click strategies below "succeeded". Check the
            # actual DOM regardless of which strategy fired.
            if nav and nav.lower() == "next" and step.get("left_tab") == "campaign":
                def _campaign_panel_still_showing() -> bool:
                    try:
                        return self.page.get_by_text(
                            "Campaign name", exact=True
                        ).first.is_visible(timeout=1000)
                    except Exception:
                        return False

                # Give Meta's transition a moment before the first check — a check
                # fired too early can catch a fleeting mid-transition state and
                # wrongly conclude the click worked, only for the page to settle
                # back on the Campaign panel a beat later (reproduced live: s07
                # logged "ok" yet the panel was still Campaign moments after).
                self.page.wait_for_timeout(800)
                if _campaign_panel_still_showing():
                    # Rescue attempt: click the "Ad set" item directly in the left
                    # tree instead of relying on Next at all — a different
                    # navigation path than the one that just failed.
                    try:
                        md.click_sidebar_tree_index(self.page, 1)
                        self.page.wait_for_timeout(800)
                    except Exception:
                        pass
                    if _campaign_panel_still_showing():
                        self._record(
                            step["id"], "click", False, f"{strategy}+no_effect",
                            "Next click reported ok but Campaign name panel still visible "
                            "(sidebar-tree rescue also didn't leave the Campaign panel)",
                        )
                        return False
                    self._record(step["id"], "click", True, f"{strategy}+sidebar_tree_rescue")
                    return True
            if wg.url_left_ads_manager(self.page.url):
                self._shot(f"player_left_after_{step['id']}")
                ok = self._ensure_not_left_ads_manager(context=f"after_{step['id']}")
                self._record(
                    step["id"],
                    "click",
                    ok,
                    f"{strategy}+guard_recover",
                    (self.page.url or "")[:80],
                )
                return ok
            self._record(step["id"], "click", True, strategy)
            return True

        if nav:
            # Always clear measurement banner before footer nav
            try:
                wg.dismiss_measurement_banner(self.page)
            except Exception:
                pass

            # Level transitions (Campaign -> Ad set -> Ad): cascade Next -> left
            # wizard tree -> Next again, verifying real panel content after each
            # attempt instead of trusting "the click didn't throw".
            #
            # Two failure modes seen live today, in order:
            #  1) breadcrumb_index's primary strategy locates `[role="navigation"]
            #     a` across the whole page/all frames, unscoped — it matched Ads
            #     Manager's main left icon rail instead of the in-wizard
            #     breadcrumb, and clicking that navigated away from the edit flow
            #     entirely, popping Meta's "Discard drafts?" dialog (run 12:45).
            #     -> breadcrumb dropped from this cascade entirely.
            #  2) sidebar_tree's click_sidebar_tree_index (JS `el.click()`)
            #     reliably highlights the target row in the left tree (aria/CSS
            #     selection state changes) but the main panel kept showing stale
            #     Campaign content — a highlighted tree row is not proof the
            #     panel actually swapped (runs 17:18 and 20:23, confirmed even
            #     after several extra seconds of waiting). So every attempt below
            #     is followed by a real content check (_wait_for_level), and a
            #     click that doesn't produce the destination panel is treated as
            #     failed, not accepted.
            level_target = {"campaign": 1, "adset": 2}.get(step.get("left_tab") or "")
            level_need = {"campaign": "adset", "adset": "ad"}.get(step.get("left_tab") or "")
            if nav.lower() == "next" and level_target is not None and level_need:
                self._shot(f"player_before_{step['id']}_click")

                def _attempt(strategy: str, action: Any) -> bool:
                    try:
                        action()
                    except Exception:
                        pass
                    return self._wait_for_level(level_need, timeout_s=5.0)

                if _attempt("footer_nav_cdp", lambda: self._click_footer_nav(nav)):
                    return _after_click("footer_nav_cdp")
                if _attempt("sidebar_tree", lambda: md.click_sidebar_tree_index(self.page, level_target)):
                    return _after_click("sidebar_tree")
                if _attempt("footer_nav_cdp_retry", lambda: self._click_footer_nav(nav)):
                    return _after_click("footer_nav_cdp_retry")
                log.warning(
                    "Next/sidebar-tree cascade failed to reach level %r — falling back to generic click",
                    level_need,
                )

            # Known React SPA race: Playwright's actionability check (visible,
            # stable, receives events) passes before React finishes re-attaching
            # its event delegation after the last render — the click physically
            # registers on the DOM node but no handler is listening yet, so
            # nothing happens. A short settle pause before the click is the
            # documented workaround (not a blanket waitForTimeout — this is
            # scoped to the one click that kept silently no-op'ing).
            self.page.wait_for_timeout(400)
            self._shot(f"player_before_{step['id']}_click")
            if self._click_footer_nav(nav):
                return _after_click("footer_nav_cdp")
            # NOTE: footer-nav search failing used to be a hard stop here — no other
            # strategy was tried, which left the wizard stuck at the Campaign panel
            # (current_step=0) while every later step (adset name, conversion
            # location) silently ran against the wrong screen. Fall through to the
            # normal semantic/css/coords cascade below instead of giving up.
            log.warning("footer nav for %r not found — falling back to generic click", nav)

        # 1) Semantic locator
        loc = self._semantic_locator(match)
        if loc is not None:
            try:
                # Block if the resolved control's visible text is a forbidden help/exit link
                try:
                    loc_text = (loc.inner_text(timeout=400) or "").strip().split("\n")[0][:80]
                except Exception:
                    loc_text = ""
                if loc_text and wg.forbidden_click_reason(loc_text):
                    self._record(
                        step["id"],
                        "click",
                        False,
                        "forbidden_zone",
                        f"locator:{loc_text[:60]}",
                    )
                    return False
                if loc.is_visible(timeout=2500):
                    loc.scroll_into_view_if_needed(timeout=2000)
                    box = None
                    try:
                        box = loc.bounding_box()
                    except Exception:
                        box = None
                    if box and box.get("width", 0) > 0:
                        md._cdp_mouse_click(
                            self.page,
                            box["x"] + box["width"] / 2,
                            box["y"] + box["height"] / 2,
                        )
                        return _after_click("semantic_cdp_bbox")
                    loc.click(timeout=4000)
                    return _after_click("semantic")
            except Exception:
                pass
            # 2) Semantic + force (avoid — often clicks through to banner)
            try:
                loc.click(timeout=2500, force=True)
                return _after_click("semantic_force")
            except Exception:
                pass

        # 3) Recorded css_path
        css_path = rec.get("css_path")
        if css_path:
            try:
                el = self.page.locator(css_path).first
                if el.is_visible(timeout=2000):
                    el.click(timeout=3000, force=True)
                    return _after_click("css_path")
            except Exception:
                pass

        # 4) Recorded coordinates via raw CDP mouse — skip if banner / About this update visible
        x, y, w, h = rec.get("x"), rec.get("y"), rec.get("w"), rec.get("h")
        if x is not None and y is not None and (w or 0) > 0 and (h or 0) > 0:
            try:
                if self.page.get_by_text(
                    re.compile(r"^About this update$", re.I)
                ).first.is_visible(timeout=300):
                    wg.dismiss_measurement_banner(self.page)
                    self._record(
                        step["id"],
                        "click",
                        False,
                        "blocked_banner",
                        "refusing stale coords while About this update visible",
                    )
                    return False
            except Exception:
                pass
            try:
                md._cdp_mouse_click(self.page, x + (w or 0) / 2, y + (h or 0) / 2)
                return _after_click("cdp_coords")
            except Exception:
                pass

        self._record(step["id"], "click", False, "", "all strategies failed")
        return False

    def _generic_fill(self, step: dict[str, Any]) -> bool:
        value = self._resolve(step)
        if value is None or value == "":
            if step.get("optional"):
                self._record(step["id"], "fill", True, "skip_optional_empty")
                return True
            self._record(step["id"], "fill", False, "", "no value for param")
            return not step.get("required", True)

        match = step.get("match", {}) or {}
        rec = step.get("recorded", {}) or {}

        # 0) Label-based fill — stable vs dynamic Meta #js_* ids
        label = (match.get("label") or "").strip()
        label_aliases = {
            "Campaign name": ["Campaign name", "Nombre de la campaña"],
            "Ad set name": ["Ad set name", "Nombre del conjunto"],
            "Daily budget": ["Daily budget", "Presupuesto diario"],
        }
        labels = label_aliases.get(label, [label] if label else [])
        if labels:
            try:
                if md.fill_input_near_label(self.page, labels, value):
                    self._record(step["id"], "fill", True, "label", value[:40])
                    return True
            except Exception:
                pass
            for lab in labels:
                try:
                    el = self.page.get_by_label(re.compile(rf"^{re.escape(lab)}$", re.I)).first
                    if el.is_visible(timeout=2000):
                        el.click(timeout=2000)
                        el.fill("")
                        el.type(value, delay=25)
                        self._record(step["id"], "fill", True, "get_by_label", value[:40])
                        return True
                except Exception:
                    pass

        # 1) css_path .fill (skip brittle dynamic #js_ ids)
        css_path = rec.get("css_path") or ""
        if css_path and not re.search(r"#js_\w+", css_path):
            try:
                el = self.page.locator(css_path).first
                if el.is_visible(timeout=2000):
                    el.click(timeout=2000)
                    el.fill("")
                    el.type(value, delay=25)
                    self._record(step["id"], "fill", True, "css_path", value[:40])
                    return True
            except Exception:
                pass

        # 2) coords click then keyboard type — only if we can verify the value stuck
        x, y, w, h = rec.get("x"), rec.get("y"), rec.get("w"), rec.get("h")
        if x is not None and y is not None and (w or 0) > 0 and (h or 0) > 0:
            try:
                md._cdp_mouse_click(self.page, x + (w or 0) / 2, y + (h or 0) / 2)
                pause_short(self.page)
                # Guard: recorded coords are from an old recording and may miss the
                # field entirely on today's layout. If focus didn't actually land in
                # an editable element, Control+a selects the WHOLE PAGE instead of
                # the field's text — this derailed a real run (jumped straight past
                # Ad set config to the Ad level). Bail out before that can happen.
                try:
                    focus_ok = bool(
                        self.page.locator(
                            "input:focus, textarea:focus, [contenteditable=true]:focus"
                        ).first.count()
                    )
                except Exception:
                    focus_ok = False
                if not focus_ok:
                    self._record(step["id"], "fill", False, "coords_no_focus", "click missed the field")
                    self._record(step["id"], "fill", False, "", "all strategies failed")
                    return False
                self.page.keyboard.press("Control+a")
                self.page.keyboard.type(value, delay=25)
                verified = False
                try:
                    focused = self.page.locator("input:focus, textarea:focus, [contenteditable=true]:focus").first
                    cur = focused.input_value(timeout=800) if focused.count() else ""
                    verified = bool(cur) and (value in cur or cur.strip() == value.strip())
                except Exception:
                    verified = False
                if verified:
                    self._record(step["id"], "fill", True, "coords_type", value[:40])
                    return True
                self._record(step["id"], "fill", False, "coords_unverified", value[:40])
            except Exception:
                pass
        self._record(step["id"], "fill", False, "", "all strategies failed")
        return False

    def _upload_creative(self, step: dict[str, Any]) -> bool:
        path = self._resolve(step)
        if not path or not Path(path).is_file():
            self._record(step["id"], "upload_creative", False, "", f"missing file: {path}")
            return False
        # Find a file input and set files directly (robust vs. library tile pick).
        for root in md._roots(self.page):
            try:
                inp = root.locator("input[type=file]").last
                inp.set_input_files(path, timeout=8000)
                pause_medium(self.page)
                self._record(step["id"], "upload_creative", True, "set_input_files", Path(path).name)
                return True
            except Exception:
                continue
        self._record(step["id"], "upload_creative", False, "", "no file input found")
        return False

    # ── wizard tab detection ───────────────────────────────────────────────────
    @staticmethod
    def _page_has_wizard(p: "Page") -> bool:
        """Return True if this Playwright page is showing the campaign creation wizard.

        Meta's UI has changed. The wizard no longer always navigates to
        /edit/standalone — it may open as a side panel on the same campaigns
        page. Detect via URL patterns AND DOM content.

        IMPORTANT: only consider pages on Facebook/Meta domains to avoid
        accidentally matching tabs open on unrelated sites (e.g. microsoft.com).
        Never treat draft row title "New Leads Campaign" on the manage list as
        wizard chrome — that false-positive kept RPA on Ads list.
        """
        url = p.url or ""
        # Must be on a Meta/Facebook domain first.
        meta_domains = ("facebook.com", "meta.com", "fb.com", "adsmanager")
        if not any(d in url for d in meta_domains):
            return False
        # NOTE: an /edit/ URL used to be trusted on its own as "wizard open", but
        # Meta's SPA can leave that URL fragment in place after the wizard has
        # already auto-saved and closed back to the manage list — the campaign_name
        # fill step would then run against the list view and silently type nothing
        # (reproduced consistently across multiple days of RPA runs). Every path
        # below now requires an actual visible wizard marker, not just the URL.
        # Manage URL may still host a side-panel editor — require editor labels.
        on_manage_list = bool(
            re.search(r"/adsmanager/manage/(campaigns|adsets|ads)(?:\?|$)", url)
            and "/edit/" not in url
        )
        wizard_texts = [
            "Campaign name",
            "Campaign details",
            "Nombre de la campaña",
            "Ad set name",
            "Conversion location",
            "Advantage+ campaign budget",
        ]
        if on_manage_list:
            for text in wizard_texts:
                try:
                    if p.get_by_text(text, exact=True).first.is_visible(timeout=400):
                        return True
                except Exception:
                    pass
            # Plain Ads/Campaigns/Ad sets table — not the editor
            return False
        try:
            level = md.wizard_editor_level(p)
            if level in ("campaign", "ad_set", "ad"):
                return True
        except Exception:
            pass
        for text in wizard_texts:
            try:
                if p.get_by_text(text, exact=True).first.is_visible(timeout=400):
                    return True
            except Exception:
                pass
        for ph_re in (
            re.compile(r"campaign name", re.I),
            re.compile(r"enter your campaign name", re.I),
            re.compile(r"nombre de la campa", re.I),
        ):
            try:
                if p.get_by_placeholder(ph_re).first.is_visible(timeout=400):
                    return True
            except Exception:
                pass
        return False

    def _bring_page_front(self, p: "Page") -> None:
        try:
            p.bring_to_front()
        except Exception:
            pass

    def _switch_to_wizard_tab(self, *, timeout_s: float = 12) -> bool:
        """Find the campaign wizard tab and make it self.page.

        Older Meta: navigates current tab to /edit/standalone.
        Newer Meta: opens wizard as a side panel or in a new tab.
        Critical: after Next / accidental navigation RPA often lands on the
        manage/campaigns|ads list — must re-select the edit tab.
        """
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                md.dismiss_discard_drafts_dialog(self.page)
            except Exception:
                pass
            try:
                md.dismiss_publish_draft_dialog(self.page)
            except Exception:
                pass

            # Prefer any edit/standalone tab in the context (most reliable)
            try:
                pages = list(self.page.context.pages)
            except Exception:
                pages = [self.page]

            edit_pages = [
                p for p in pages
                if "/edit/" in (p.url or "") and any(
                    d in (p.url or "") for d in ("facebook.com", "adsmanager", "meta.com")
                )
            ]
            for p in edit_pages:
                # NOTE: used to also accept "/edit/" in p.url alone as a fallback —
                # same stale-URL false positive as elsewhere. _page_has_wizard()
                # already does a real visibility check now; trust that only.
                if self._page_has_wizard(p):
                    if p is not self.page:
                        log.info("Switching to wizard edit tab: %s", p.url[:100])
                        self.page = p
                    self._bring_page_front(self.page)
                    return True

            if self._page_has_wizard(self.page):
                self._bring_page_front(self.page)
                log.info("Wizard detected on current page (url=%s)", self.page.url)
                return True

            # Align with AdsManagerFlow readiness (placeholders / campaign level)
            try:
                from src.fb_ads_rpa.ads_manager_flow import _is_campaign_wizard_ready

                if _is_campaign_wizard_ready(self.page):
                    self._bring_page_front(self.page)
                    log.info("Wizard ready via AdsManagerFlow detector (url=%s)", self.page.url)
                    return True
            except Exception:
                pass

            for p in pages:
                if p is self.page:
                    continue
                if self._page_has_wizard(p):
                    log.info("Wizard opened in new tab (switching): %s", p.url)
                    self.page = p
                    self._bring_page_front(self.page)
                    return True

            time.sleep(0.4)

        log.warning("Wizard not found after %.0fs (url=%s)", timeout_s, self.page.url)
        return False

    def _recover_objective_then_continue(self) -> bool:
        """If still on Create objective dialog: Manual → Leads → Continue."""
        try:
            from src.fb_ads_rpa.ads_manager_flow import (
                _click_continue_when_enabled,
                _click_leads_grid_row,
                _dismiss_ai_guided_setup_dialog,
                _is_campaign_wizard_ready,
                _is_objective_modal_open,
                _objectives_chooser_visible,
            )
        except Exception as exc:
            log.debug("objective recover import failed: %s", exc)
            return False

        page = self.page
        if _is_campaign_wizard_ready(page):
            return True
        if not (_objectives_chooser_visible(page) or _is_objective_modal_open(page)):
            return False

        self._record("wizard_recover", "manual", True, "still_on_objective_modal")
        self._shot("player_wizard_recover_objective")
        try:
            if _dismiss_ai_guided_setup_dialog(page, timeout_ms=12000):
                self._record("wizard_recover", "manual", True, "ensure_manual")
        except Exception as exc:
            self._record("wizard_recover", "manual", False, "", str(exc)[:80])

        try:
            if _click_leads_grid_row(page):
                self._record("wizard_recover", "leads", True, "grid_row")
        except Exception:
            pass

        try:
            _click_continue_when_enabled(page, timeout_ms=12000)
            self._record("wizard_recover", "continue", True, "enabled_click")
            pause_medium(page)
        except Exception as exc:
            self._record("wizard_recover", "continue", False, "", str(exc)[:80])
            return False
        return True

    def _wait_for_wizard_after_continue(self, *, timeout_s: float = 28) -> bool:
        """After Continue: wait/recover until Campaign name wizard is open."""
        pause_medium(self.page)
        # First pass — Meta often needs several seconds to open /edit/ or side panel
        if self._switch_to_wizard_tab(timeout_s=min(14.0, timeout_s)):
            return True

        # Still on objective chooser / AI dialog → Manual + Leads + Continue again
        if self._recover_objective_then_continue():
            if self._switch_to_wizard_tab(timeout_s=12):
                return True

        # Draft may already exist on the manage list
        if self._reopen_wizard_from_list():
            return True

        # Final wait window
        remaining = max(4.0, timeout_s - 14.0)
        if self._switch_to_wizard_tab(timeout_s=remaining):
            return True
        return False

    def _reopen_wizard_from_list(self) -> bool:
        """If wizard closed to campaigns list, reopen via Edit / row double-click."""
        page = self.page
        try:
            md.dismiss_discard_drafts_dialog(page)
        except Exception:
            pass
        try:
            md.dismiss_publish_draft_dialog(page)
        except Exception:
            pass
        try:
            wg.dismiss_account_switcher(page)
        except Exception:
            pass
        try:
            wg.dismiss_measurement_banner(page)
        except Exception:
            pass
        # Prefer Ad sets tab when a draft hierarchy is already selected
        for tab_name in (r"^Ad sets$", r"^Conjuntos de anuncios$"):
            try:
                tab = page.get_by_role("tab", name=re.compile(tab_name, re.I)).first
                if tab.is_visible(timeout=800):
                    tab.click(timeout=2500)
                    pause_short(page)
                    break
            except Exception:
                pass
        # Prefer explicit Edit toolbar button when a draft is selected (CDP bbox)
        for name in (r"^Edit$", r"^Editar$"):
            try:
                btn = page.get_by_role("button", name=re.compile(name, re.I)).first
                if btn.is_visible(timeout=1000):
                    box = btn.bounding_box()
                    if box and box.get("width", 0) > 0:
                        md._cdp_mouse_click(
                            page,
                            box["x"] + box["width"] / 2,
                            box["y"] + box["height"] / 2,
                        )
                    else:
                        btn.click(timeout=3000)
                    pause_medium(page)
                    if self._switch_to_wizard_tab(timeout_s=8) and self._wizard_is_open():
                        return True
            except Exception:
                pass
        # Pencil / aria Edit
        for sel in ('[aria-label="Edit"]', '[aria-label="Editar"]'):
            try:
                btn = page.locator(sel).first
                if btn.is_visible(timeout=600):
                    box = btn.bounding_box()
                    if box:
                        md._cdp_mouse_click(
                            page,
                            box["x"] + box["width"] / 2,
                            box["y"] + box["height"] / 2,
                        )
                        pause_medium(page)
                        if self._switch_to_wizard_tab(timeout_s=8) and self._wizard_is_open():
                            return True
            except Exception:
                pass
        # Double-click a draft row titled New Leads Campaign / our campaign name
        names = [
            getattr(self.draft, "name", None),
            "New Leads Campaign",
            "New Leads Ad Set",
            "New Leads Ad",
        ]
        for name in names:
            if not name:
                continue
            try:
                row = page.get_by_text(name, exact=True).first
                if row.is_visible(timeout=1200):
                    row.dblclick(timeout=3000)
                    pause_medium(page)
                    if self._switch_to_wizard_tab(timeout_s=8) and self._wizard_is_open():
                        return True
            except Exception:
                pass
        return False

    _LEVEL_MARKERS: dict[str, list[str]] = {
        "campaign": [
            "Campaign name",
            "Nombre de la campaña",
            "Daily budget",
            "Campaign details",
        ],
        "adset": ["Ad set name", "Conversion location", "Nombre del conjunto"],
        "conversion": ["Conversion location", "Ubicación de la conversión"],
        "ad": [
            "Instant form",
            "Create instant form",
            "Destination",
            "Ad setup",
            "Primary text",
            "Add media",
        ],
    }

    def _level_markers_visible(self, need: str, *, timeout_ms: int = 900) -> bool:
        for text in self._LEVEL_MARKERS.get(need, []):
            try:
                if self.page.get_by_text(text, exact=True).first.is_visible(timeout=timeout_ms):
                    return True
            except Exception:
                pass
        return False

    def _wait_for_level(self, need: str, *, timeout_s: float = 5.0) -> bool:
        """Poll for need-specific panel markers — a highlighted sidebar-tree row or
        an 'ok' click result is not proof the main panel actually swapped content."""
        deadline = time.monotonic() + timeout_s
        while True:
            if self._level_markers_visible(need, timeout_ms=700):
                return True
            if time.monotonic() >= deadline:
                return False
            self.page.wait_for_timeout(400)

    def _ensure_on_wizard(self, *, need: str = "") -> bool:
        """Re-acquire wizard tab before critical steps. need: campaign|adset|conversion."""
        try:
            md.wait_editor_saved(self.page, timeout_s=12)
        except Exception:
            pass
        if self._switch_to_wizard_tab(timeout_s=8):
            markers = self._LEVEL_MARKERS.get(need, [])
            if not markers:
                return True
            # Retry the need-specific markers for a few seconds — live evidence
            # (2026-07-20 17:18 run) showed Meta's main panel taking longer than
            # one ~1s pass to swap from Campaign to Ad set content after a
            # sidebar-tree level switch. A single quick pass fell through to the
            # generic _page_has_wizard() check below, which is level-agnostic
            # (accepts "Campaign name" as proof of *a* wizard being open) — that
            # let the run proceed onto the Ad set panel's fields while the stale
            # Campaign panel was still showing, so the next click missed and
            # closed the wizard back to the manage list.
            for _ in range(4):
                for text in markers:
                    try:
                        if self.page.get_by_text(text, exact=True).first.is_visible(timeout=1000):
                            return True
                    except Exception:
                        pass
                self.page.wait_for_timeout(500)
            # NOTE: do not trust "/edit/" in url alone here — Meta's SPA can leave a
            # stale /edit/ URL fragment after the wizard has already closed back to
            # the manage list, which produced false positives (campaign_name fill
            # then ran against the list view and silently typed nothing).
            # _page_has_wizard() below does a real DOM/visibility check, but it is
            # level-agnostic — only trust it here as a last resort once the
            # need-specific markers above have had a real chance to appear.
            if self._page_has_wizard(self.page):
                return True
        # Last resort: reopen from manage list
        if self._reopen_wizard_from_list():
            return True
        return False

    # ── steps 7-9: conversion location (new Meta panel UI) ────────────────────
    def _select_conversion_location_panel(self) -> bool:
        """Steps 7-9 for the new Meta wizard side-panel UI.

        Step 0 (pre): Turn off 'Advantage+ leads campaign' toggle — it hides
                      the conversion location dropdown when ON.
        Step 7: Click the Conversion location dropdown trigger.
        Step 8: Expand 'Single' in the opened menu.
        Step 9: Select 'Website' (Space key — mouse click often drops over CDP).

        Uses battle-tested meta_dom primitives; plain Playwright clicks alone
        are unreliable on Multilogin CDP (#41286).
        """
        page = self.page

        # Fast-path: already correct
        try:
            if md.is_website_conversion_selected(page):
                log.info("_select_conversion_location_panel: already Website ✓")
                return True
        except Exception:
            pass

        # Must be on the wizard ad-set page — never the manage/ads list
        try:
            wg.dismiss_account_switcher(page)
        except Exception:
            pass
        if not self._ensure_on_wizard(need="conversion") or not self._wizard_is_open():
            log.warning("_select_conversion_location_panel: not on wizard (url=%s)", page.url)
            self._shot("player_conversion_not_on_wizard")
            # Last attempt: reopen edit from manage list
            if not (self._reopen_wizard_from_list() and self._ensure_on_wizard(need="conversion")):
                return False
        page = self.page
        # Hard marker: Conversion location label must be visible
        try:
            if not page.get_by_text("Conversion location", exact=True).first.is_visible(timeout=2500):
                log.warning("_select_conversion_location_panel: Conversion location label missing")
                self._shot("player_conversion_label_missing")
                if not self._reopen_wizard_from_list():
                    return False
                page = self.page
                if not page.get_by_text("Conversion location", exact=True).first.is_visible(timeout=2500):
                    return False
        except Exception:
            self._shot("player_conversion_label_missing")
            return False

        # ── Step 0: turn off Advantage+ (only when ON — never toggle blindly) ─
        try:
            md.dismiss_publish_draft_dialog(page)
            md._turn_off_advantage_plus_leads(page)
            md.dismiss_publish_draft_dialog(page)
            page.wait_for_timeout(600)
        except Exception as exc:
            log.debug("Advantage+ off skipped: %s", exc)

        # Prefer the full battle-tested handler (opens dropdown → Single → Website)
        try:
            if md.select_conversion_location_website(page):
                log.info("_select_conversion_location_panel: Website via meta_dom ✓")
                return True
        except Exception as exc:
            log.warning("meta_dom.select_conversion_location_website failed: %s", exc)

        log.warning("_select_conversion_location_panel: could not select Website")
        return False

    # ── delegated section handlers ─────────────────────────────────────────────
    def _run_delegated(self, section: str) -> bool:
        d = self.draft
        aset = d.ad_sets[0] if getattr(d, "ad_sets", None) else None
        self._guards()
        # Screenshot BEFORE delegation so we can see exact page state
        self._shot(f"player_before_{section}")
        try:
            if section == "conversion_location":
                # Steps 7-9: ensure wizard → Advantage+ off → Single → Website
                ok = self._select_conversion_location_panel()
                if not ok:
                    log.warning("conversion_location retry with full meta_dom handler")
                    self._ensure_on_wizard(need="conversion")
                    ok = md.select_conversion_location_website(self.page)
            elif section == "conversion_event":
                ok = md.select_conversion_event_lead(self.page)
            elif section == "audience_geo":
                ok = md.fill_audience_location(self.page, d.country)
            elif section == "audience_age":
                ok = md.fill_audience_age(self.page, aset.age_min, aset.age_max) if aset else False
            elif section == "targeting":
                ok = self._run_targeting(aset.targeting_label if aset else "")
            elif section == "placements":
                ok = md.select_manual_placements(self.page)
                if aset and aset.os and aset.os.lower() != "all":
                    md.select_placement_device(self.page, aset.os)
            else:
                ok = False
        except Exception as exc:
            self._record(section, "delegate", False, "", str(exc)[:120])
            return False
        self._record(section, "delegate", bool(ok), f"meta_dom.{section}")
        return bool(ok)

    def _run_targeting(self, interests: str) -> bool:
        if not interests:
            return True
        if not md.open_detailed_targeting_field(self.page):
            return False
        ok_any = False
        for term in [t.strip() for t in interests.split(",") if t.strip()]:
            if md.fill_targeting_interest(self.page, term):
                md.pick_first_suggestion(self.page)
                ok_any = True
                pause_short(self.page)
        return ok_any

    # ── page (best-effort) ──────────────────────────────────────────────────────
    def _pick_available_page(self, step: dict[str, Any]) -> bool:
        """Open the Page combobox and select the first available option; skip if none."""
        rec = step.get("recorded", {})
        css_path = rec.get("css_path")
        try:
            box = self.page.locator(css_path).first if css_path else None
            if box and box.is_visible(timeout=2000):
                box.click(timeout=2500)
                pause_short(self.page)
                opt = self.page.get_by_role("option").first
                if opt.is_visible(timeout=2000):
                    opt.click(timeout=2500)
                    self._record(step["id"], "page", True, "first_option")
                    return True
        except Exception:
            pass
        self._record(step["id"], "page", True, "skipped_handoff", "pick page before publishing")
        return True

    # ── main loop ──────────────────────────────────────────────────────────────
    def run(self) -> RecipeRunResult:
        handled_sections: set[str] = set()
        last_level: str | None = None
        stop_after = get_recipe_stop_after()

        for step in self.steps:
            sid = step["id"]
            section = step.get("section") or ""
            level = step.get("left_tab")
            action = step.get("action")

            if level != last_level:
                # Re-bind wizard tab when entering campaign / adset / ad levels
                if level in ("campaign", "adset", "ad"):
                    need = "campaign" if level == "campaign" else ("adset" if level == "adset" else "")
                    if not self._ensure_on_wizard(need=need):
                        self._shot(f"player_lost_wizard_{level}")
                        self.result.error = f"Lost campaign wizard before {level} steps"
                        self.result.ok = False
                        self._shot("player_handoff")
                        return self.result
                self._shot(f"player_{level}")
                last_level = level

            # Skip harmful / optional steps (e.g. layerConfirm that closes wizard)
            if step.get("skip") or (
                (step.get("match") or {}).get("css") == "button.layerConfirm"
                and section != "objective_continue"
            ):
                self._record(sid, action or "click", True, "skipped_optional")
                continue

            # Delegated sections: run the robust handler once, skip raw sub-steps.
            if section in _DELEGATED_SECTIONS:
                if section in handled_sections:
                    # Still honor stop_after on later ids in the same section (e.g. s12).
                    if stop_after and sid == stop_after:
                        return self._finish_gate(stop_after)
                    continue
                handled_sections.add(section)
                if section.startswith("conversion") or section.startswith("audience") or section in (
                    "targeting",
                    "placements",
                ):
                    self._ensure_on_wizard(need="adset" if section != "conversion_location" else "conversion")
                ok = self._run_delegated(section)
                if not ok and section == "conversion_location":
                    # Hard stop — continuing on the manage/ads list only creates noise
                    self._shot(f"player_fail_{section}")
                    self.result.error = (
                        "Conversion location → Website failed. "
                        "Stay on Ad set edit tab; turn Advantage+ Off; pick Single → Website "
                        "(not Website and instant forms)."
                    )
                    self.result.ok = False
                    self._shot("player_handoff")
                    return self.result
                if not ok and step.get("risk") == "risky":
                    self._shot(f"player_fail_{section}")
                    self.result.error = f"Delegated section failed: {section}"
                    # continue for non-conversion sections — hand-off lets user finish
                # Gate may target s12 while delegated work ran on s10 — stop if stop_after
                # belongs to this section.
                if stop_after and self._stop_after_in_section(stop_after, section):
                    return self._finish_gate(stop_after, section_ok=ok)
                if stop_after and sid == stop_after:
                    return self._finish_gate(stop_after, section_ok=ok)
                continue

            self._guards()

            # Campaign name / budget / adset name must run inside the wizard
            if action == "fill" and step.get("param") in (
                "{{campaign_name}}",
                "{{daily_budget}}",
                "{{adset_name}}",
            ):
                need = "adset" if "adset" in str(step.get("param")) else "campaign"
                if not self._ensure_on_wizard(need=need):
                    self._record(sid, "fill", False, "", "wizard tab lost")
                    self.result.error = f"Wizard lost before {sid}"
                    self.result.ok = False
                    self._shot("player_handoff")
                    return self.result

            if action == "fill" and step.get("param") in ("{{campaign_name}}", "{{adset_name}}", "{{daily_budget}}"):
                # IVHP: fill is not enough — value must verify in the field. Also used
                # for adset_name: the old _generic_fill path for this field included a
                # keyboard select-all fallback that, when focus wasn't actually in the
                # input, selected the WHOLE PAGE instead — derailing the rest of the
                # run (jumped straight to Ad level, Conversion location never set).
                # daily_budget was on the same unverified _generic_fill path and was
                # caught live silently reverting to Meta's $25 default (React-
                # controlled input ignoring a non-keyboard value set) with the run
                # still logging s06_campaign as ok.
                _intent_by_param = {
                    "{{campaign_name}}": "campaign_name",
                    "{{adset_name}}": "adset_name",
                    "{{daily_budget}}": "daily_budget",
                }
                intent_id = _intent_by_param[step.get("param")]
                value = self._resolve(step) or ""
                outcome = ir.run_intent(
                    self.page,
                    intent_id,
                    value=value,
                    run_id=self._run_id,
                )
                self._record(
                    sid,
                    "fill",
                    outcome.ok and outcome.verified,
                    outcome.strategy or "ivhp",
                    outcome.detail or (outcome.evidence_dir or ""),
                )
                if outcome.evidence_dir:
                    self.result.screenshots.append(outcome.evidence_dir)
                if not (outcome.ok and outcome.verified):
                    self._shot(f"player_ivhp_{intent_id}_fail")
                    self.result.error = (
                        f"IVHP: {intent_id} verify failed "
                        f"(expected={value!r}). See evidence: {outcome.evidence_dir or 'n/a'}"
                    )
                    self.result.ok = False
                    self._shot("player_handoff")
                    return self.result
            elif action == "fill":
                self._generic_fill(step)
                # Meta often jumps to Ad level after Ad set name save — return to Ad set
                # before Conversion location.
                if step.get("param") == "{{adset_name}}":
                    self._return_to_ad_set_after_rename()
            elif action == "upload_creative":
                self._upload_creative(step)
            elif step.get("param") == "{{page_name}}":
                self._pick_available_page(step)
            else:
                ok = self._generic_click(step)
                if not ok and step.get("optional"):
                    self._record(sid, action or "click", True, "skip_optional_miss")
                elif not ok and step.get("risk") == "risky":
                    self._shot(f"player_fail_{sid}")

            # After Continue → wait for wizard (URL or side panel), recover Manual if needed
            if section == "objective_continue":
                wizard_found = self._wait_for_wizard_after_continue(timeout_s=28)
                detail = "edit_tab" if "/edit/" in (self.page.url or "") else "dom_wizard"
                self._record(
                    "wizard_tab_switch",
                    "navigate",
                    wizard_found,
                    detail,
                    (self.page.url or "")[:80],
                )
                if not wizard_found:
                    self._shot("player_wizard_tab_not_found")
                    self.result.error = (
                        "Campaign wizard did not open after Continue "
                        "(no Campaign name /edit wizard — try Manual → Leads → Continue). "
                        f"url={(self.page.url or '')[:100]}"
                    )
                    self.result.ok = False
                    self._shot("player_handoff")
                    return self.result
                try:
                    md.wait_editor_saved(self.page, timeout_s=20)
                except Exception:
                    pass
                # Never click layerConfirm / Close here — it dismisses the wizard.
                try:
                    md._turn_off_advantage_plus_leads(self.page)
                    md.dismiss_publish_draft_dialog(self.page)
                except Exception:
                    pass
                self._shot("player_wizard_ready")
                pause_medium(self.page)

            # After Next from campaign → must land on ad set wizard (not manage list)
            if sid == "s07_campaign" or (
                action == "click"
                and (step.get("match") or {}).get("own_text") == "Next"
                and level == "campaign"
            ):
                # Coords/mis-click may have opened measurement news — recover first
                if not self._ensure_not_left_ads_manager(context="after_next"):
                    self._shot("player_adset_tab_not_found")
                    self.result.error = (
                        "Left Ads Manager after Next (help/news). Guard recover failed. "
                        f"url={(self.page.url or '')[:100]}"
                    )
                    self.result.ok = False
                    self._shot("player_handoff")
                    return self.result
                if not self._switch_to_wizard_tab(timeout_s=10):
                    # One more recover+reopen pass before failing the gate
                    self._ensure_not_left_ads_manager(context="adset_missing")
                    if not (self._reopen_wizard_from_list() or self._switch_to_wizard_tab(timeout_s=8)):
                        self._shot("player_adset_tab_not_found")
                        self.result.error = "Ad set wizard not found after Next"
                        self.result.ok = False
                        self._shot("player_handoff")
                        return self.result
                # Best-effort: land on ad set panel (do not hard-fail if still painting)
                self._ensure_on_wizard(need="adset")
                try:
                    md._turn_off_advantage_plus_leads(self.page)
                    md.dismiss_publish_draft_dialog(self.page)
                except Exception:
                    pass
                pause_medium(self.page)

            pause_short(self.page)

            if stop_after and sid == stop_after:
                return self._finish_gate(stop_after)

        self.result.ok = self.result.error is None
        self._shot("player_handoff")
        return self.result

    def _stop_after_in_section(self, stop_after: str, section: str) -> bool:
        if not stop_after or not section:
            return False
        for s in self.steps:
            if s.get("id") == stop_after and (s.get("section") or "") == section:
                return True
        return False

    def _return_to_ad_set_after_rename(self) -> None:
        """After Ad set name fill, Meta may switch to Ad — get Conversion location back."""
        try:
            wg.dismiss_account_switcher(self.page)
        except Exception:
            pass
        try:
            md.wait_editor_saved(self.page, timeout_s=12)
        except Exception:
            pass
        if not self._wizard_is_open():
            reopened = self._reopen_wizard_from_list()
            self._record(
                "adset_level_restore",
                "nav",
                reopened and self._wizard_is_open(),
                "reopen_from_list",
            )
            if reopened:
                self._shot("player_adset_after_rename")
            return
        try:
            if md.wizard_editor_level(self.page) == "ad_set":
                if self.page.get_by_text("Conversion location", exact=True).first.is_visible(timeout=800):
                    self._record("adset_level_keep", "nav", True, "still_ad_set")
                    return
        except Exception:
            pass
        # Prefer sidebar Ad set (index 1), then breadcrumb
        restored = False
        try:
            if md.click_wizard_sidebar_level(self.page, 1):
                restored = True
        except Exception:
            pass
        if not restored:
            try:
                if md.click_wizard_breadcrumb_index(self.page, 1):
                    restored = True
            except Exception:
                pass
        try:
            md.wait_editor_saved(self.page, timeout_s=8)
        except Exception:
            pass
        # Only count restore if Conversion location (or Ad set name) is actually visible
        visible = False
        for label in ("Conversion location", "Ad set name"):
            try:
                if self.page.get_by_text(label, exact=True).first.is_visible(timeout=800):
                    visible = True
                    break
            except Exception:
                pass
        ok = bool(restored and visible and self._wizard_is_open())
        self._record(
            "adset_level_restore",
            "nav",
            ok,
            "sidebar_or_breadcrumb" if restored else "missing",
        )
        if ok:
            self._shot("player_adset_after_rename")
        else:
            self._shot("player_adset_restore_fail")

    def _finish_gate(self, stop_after: str, *, section_ok: bool | None = None) -> RecipeRunResult:
        """Evaluate gate success and stop the recipe."""
        name_ok = any(o.step_id == "s05_campaign" and o.ok for o in self.result.steps)
        budget_ok = any(o.step_id == "s06_campaign" and o.ok for o in self.result.steps)
        adset_ok = any(o.step_id == "s09_adset" and o.ok for o in self.result.steps)
        website_ok = False
        try:
            website_ok = bool(md.is_website_conversion_selected(self.page))
        except Exception:
            website_ok = False

        if stop_after == "s06_campaign":
            self.result.ok = self.result.error is None and name_ok and budget_ok
            gate_name = "gate_campaign_open"
        elif stop_after in (
            "s10_conversion_location",
            "s11_conversion_location",
            "s12_conversion_location",
        ):
            if section_ok is False:
                website_ok = False
            self.result.ok = (
                self.result.error is None
                and name_ok
                and budget_ok
                and adset_ok
                and website_ok
            )
            gate_name = "gate_adset_website"
            if not self.result.ok and not self.result.error:
                missing = []
                if not adset_ok:
                    missing.append("ad_set_name")
                if not website_ok:
                    missing.append("conversion_website_exact")
                self.result.error = (
                    f"Gate stop_after={stop_after} incomplete: "
                    + (",".join(missing) or f"name={name_ok} budget={budget_ok}")
                )
        else:
            self.result.ok = self.result.error is None
            if section_ok is False:
                self.result.ok = False
            gate_name = "gate_stop_after"

        if not self.result.ok and not self.result.error:
            self.result.error = (
                f"Gate stop_after={stop_after} incomplete "
                f"(name={name_ok} budget={budget_ok} adset={adset_ok} website={website_ok})"
            )
        self._record(gate_name, "gate", bool(self.result.ok), f"stop_after={stop_after}")
        shot = "player_gate_adset_website" if gate_name == "gate_adset_website" else "player_gate_campaign_open"
        self._shot(shot if self.result.ok else "player_handoff")
        return self.result
