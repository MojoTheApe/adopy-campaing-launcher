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

from src.fb_ads_rpa import meta_dom as md
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
            md.dismiss_publish_draft_dialog(self.page)
        except Exception:
            pass
        try:
            md._dismiss_floating_banners(self.page)
        except Exception:
            pass
        # Clear empty-state filters that hide the Create flow context
        try:
            clear_btn = self.page.get_by_role("button", name=re.compile(r"^Clear filter$", re.I)).first
            if clear_btn.is_visible(timeout=400):
                clear_btn.click(timeout=2000, force=True)
                pause_short(self.page)
        except Exception:
            pass

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

    def _generic_click(self, step: dict[str, Any]) -> bool:
        """Cascade: semantic -> keyboard focus+Enter -> raw CDP bbox -> css -> coords."""
        match = step.get("match", {})
        rec = step.get("recorded", {})

        # 1) Semantic locator
        loc = self._semantic_locator(match)
        if loc is not None:
            try:
                if loc.is_visible(timeout=2500):
                    loc.scroll_into_view_if_needed(timeout=2000)
                    loc.click(timeout=4000)
                    self._record(step["id"], "click", True, "semantic")
                    return True
            except Exception:
                pass
            # 2) Semantic + force
            try:
                loc.click(timeout=2500, force=True)
                self._record(step["id"], "click", True, "semantic_force")
                return True
            except Exception:
                pass

        # 3) Recorded css_path
        css_path = rec.get("css_path")
        if css_path:
            try:
                el = self.page.locator(css_path).first
                if el.is_visible(timeout=2000):
                    el.click(timeout=3000, force=True)
                    self._record(step["id"], "click", True, "css_path")
                    return True
            except Exception:
                pass

        # 4) Recorded coordinates via raw CDP mouse (Multilogin #41286 workaround)
        x, y, w, h = rec.get("x"), rec.get("y"), rec.get("w"), rec.get("h")
        if x is not None and y is not None and (w or 0) > 0 and (h or 0) > 0:
            try:
                md._cdp_mouse_click(self.page, x + (w or 0) / 2, y + (h or 0) / 2)
                self._record(step["id"], "click", True, "cdp_coords")
                return True
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
        url = p.url
        # Must be on a Meta/Facebook domain first.
        meta_domains = ("facebook.com", "meta.com", "fb.com", "adsmanager")
        if not any(d in url for d in meta_domains):
            return False
        # Prefer explicit edit URLs (strong signal)
        if "/edit/standalone" in url or "/campaigns/edit/" in url or "/adsets/edit/" in url:
            return True
        # Labels that only appear inside the editor (not campaign table cells).
        wizard_texts = [
            "Campaign name",
            "Campaign details",
            "Nombre de la campaña",
            "Ad set name",
            "Conversion location",
        ]
        on_manage_list = bool(
            re.search(r"/adsmanager/manage/(campaigns|adsets|ads)(?:\?|$)", url)
            and "/edit/" not in url
        )
        for text in wizard_texts:
            try:
                if p.get_by_text(text, exact=True).first.is_visible(timeout=400):
                    return True
            except Exception:
                pass
        # Plain manage list with no wizard chrome
        if on_manage_list:
            return False
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
                if self._page_has_wizard(p) or "/edit/" in p.url:
                    if p is not self.page:
                        log.info("Switching to wizard edit tab: %s", p.url[:100])
                        self.page = p
                    self._bring_page_front(self.page)
                    return True

            if self._page_has_wizard(self.page):
                self._bring_page_front(self.page)
                log.info("Wizard detected on current page (url=%s)", self.page.url)
                return True

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

    def _reopen_wizard_from_list(self) -> bool:
        """If wizard closed to campaigns list, reopen via Edit / row double-click."""
        page = self.page
        try:
            md.dismiss_publish_draft_dialog(page)
        except Exception:
            pass
        # Prefer explicit Edit toolbar button when a draft is selected
        for name in (r"^Edit$", r"^Editar$"):
            try:
                btn = page.get_by_role("button", name=re.compile(name, re.I)).first
                if btn.is_visible(timeout=1000):
                    btn.click(timeout=3000)
                    pause_medium(page)
                    if self._switch_to_wizard_tab(timeout_s=8):
                        return True
            except Exception:
                pass
        # Double-click a draft row titled New Leads Campaign / our campaign name
        names = [getattr(self.draft, "name", None), "New Leads Campaign"]
        for name in names:
            if not name:
                continue
            try:
                row = page.get_by_text(name, exact=True).first
                if row.is_visible(timeout=1200):
                    row.dblclick(timeout=3000)
                    pause_medium(page)
                    if self._switch_to_wizard_tab(timeout_s=8):
                        return True
            except Exception:
                pass
        return False

    def _ensure_on_wizard(self, *, need: str = "") -> bool:
        """Re-acquire wizard tab before critical steps. need: campaign|adset|conversion."""
        try:
            md.wait_editor_saved(self.page, timeout_s=12)
        except Exception:
            pass
        if self._switch_to_wizard_tab(timeout_s=8):
            markers = {
                "campaign": [
                    "Campaign name",
                    "Nombre de la campaña",
                    "Daily budget",
                    "Campaign details",
                ],
                "adset": ["Ad set name", "Conversion location", "Nombre del conjunto"],
                "conversion": ["Conversion location", "Ubicación de la conversión"],
            }.get(need, [])
            if not markers:
                return True
            for text in markers:
                try:
                    if self.page.get_by_text(text, exact=True).first.is_visible(timeout=1000):
                        return True
                except Exception:
                    pass
            if "/edit/" in (self.page.url or ""):
                return True
            # Wizard tab found but labels still painting
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
        if not self._ensure_on_wizard(need="conversion"):
            log.warning("_select_conversion_location_panel: not on wizard (url=%s)", page.url)
            return False
        page = self.page

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
                        "Stay on Ad set edit tab; turn Advantage+ Off; pick Single → Website."
                    )
                    self.result.ok = False
                    self._shot("player_handoff")
                    return self.result
                if not ok and step.get("risk") == "risky":
                    self._shot(f"player_fail_{section}")
                    self.result.error = f"Delegated section failed: {section}"
                    # continue for non-conversion sections — hand-off lets user finish
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

            if action == "fill":
                self._generic_fill(step)
            elif action == "upload_creative":
                self._upload_creative(step)
            elif step.get("param") == "{{page_name}}":
                self._pick_available_page(step)
            else:
                ok = self._generic_click(step)
                if not ok and step.get("risk") == "risky":
                    self._shot(f"player_fail_{sid}")

            # After Continue → bind wizard tab and wait for Meta to finish verifying
            if section == "objective_continue":
                wizard_found = self._switch_to_wizard_tab()
                self._record(
                    "wizard_tab_switch",
                    "navigate",
                    wizard_found,
                    "edit_tab" if "/edit/" in self.page.url else "dom_wizard",
                    self.page.url[:80],
                )
                if not wizard_found:
                    self._shot("player_wizard_tab_not_found")
                    self.result.error = (
                        "Campaign wizard did not open after Continue "
                        "(no /edit/standalone tab found)"
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
                if not self._switch_to_wizard_tab(timeout_s=10):
                    self._shot("player_adset_tab_not_found")
                    self.result.error = "Ad set wizard not found after Next"
                    self.result.ok = False
                    self._shot("player_handoff")
                    return self.result
                try:
                    md._turn_off_advantage_plus_leads(self.page)
                    md.dismiss_publish_draft_dialog(self.page)
                except Exception:
                    pass
                pause_medium(self.page)

            pause_short(self.page)

            # Campaign-open gate: stop after name + budget (s06), before Next / Ad set.
            if stop_after and sid == stop_after:
                name_ok = any(o.step_id == "s05_campaign" and o.ok for o in self.result.steps)
                budget_ok = any(o.step_id == "s06_campaign" and o.ok for o in self.result.steps)
                if stop_after == "s06_campaign":
                    self.result.ok = self.result.error is None and name_ok and budget_ok
                else:
                    self.result.ok = self.result.error is None
                if not self.result.ok and not self.result.error:
                    self.result.error = f"Gate stop_after={stop_after} incomplete (name={name_ok} budget={budget_ok})"
                self._record("gate_campaign_open", "gate", bool(self.result.ok), f"stop_after={stop_after}")
                self._shot("player_gate_campaign_open" if self.result.ok else "player_handoff")
                return self.result

        self.result.ok = self.result.error is None
        self._shot("player_handoff")
        return self.result
