"""
Self-healing diagnostic engine for Meta Ads Manager RPA.

Usage inside the flow:
    from src.fb_ads_rpa.page_diagnostics import diagnose_and_recover, PageDiagnosis

    diag = diagnose_and_recover(page, failed_step="conversion_location")
    result.steps_completed.append(f"diag:{diag.summary()}")
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Callable

from playwright.sync_api import Page

# ── Known-state regexes ────────────────────────────────────────────────────────

_WEBSITE_FORMS_RE = re.compile(r"website and instant forms", re.I)
_WEBSITE_ONLY_RE = re.compile(r"^website$|^sitio web$", re.I)
_LEAD_EVENT_RE = re.compile(r"^leads?$|^lead generation$", re.I)
_MANUAL_SETUP_RE = re.compile(r"manual campaign setup|configuraci[oó]n manual", re.I)
_AI_GUIDED_RE = re.compile(r"meta ai.guided setup", re.I)
_OBJECTIVE_MODAL_RE = re.compile(
    r"create new campaign|choose a campaign objective|elige un objetivo", re.I
)
_BANNER_CLOSE_SELS = [
    '[aria-label="Close"]', '[aria-label="Cerrar"]',
    '[aria-label="Dismiss"]', '[aria-label="Descartar"]',
    'button[data-testid="close_button"]',
]

_WIZARD_CLOSE_GUARD_JS = """
  function _isWizardClose(el) {
    let node = el;
    for (let i = 0; i < 8; i++) {
      if (!node || node === document.body) break;
      const role = (node.getAttribute && node.getAttribute('role') || '').toLowerCase();
      const id = (node.id || '').toLowerCase();
      const label = (node.getAttribute && node.getAttribute('aria-label') || '').toLowerCase();
      if (role === 'alert' || role === 'alertdialog' || role === 'status') return false;
      if (id.indexOf('banner') !== -1 || id.indexOf('toast') !== -1 || id.indexOf('notice') !== -1) return false;
      if (label.indexOf('banner') !== -1 || label.indexOf('notice') !== -1) return false;
      if (node.scrollHeight > 600 && node.scrollWidth > 400 && node !== document.documentElement) return false;
      node = node.parentElement;
    }
    const rect = el.getBoundingClientRect();
    if (rect && rect.top < 60 && rect.top >= 0) return true;
    return false;
  }
"""


@dataclass
class PageDiagnosis:
    # Wizard level
    wizard_level: str = "unknown"          # campaign | ad_set | ad | unknown
    # Conversion state
    conversion_location: str = "unknown"   # website | website_and_forms | other | unknown
    conversion_event: str = "unknown"      # lead | other | unknown | not_visible
    # Dialog state
    ai_guided_dialog: bool = False
    objective_modal: bool = False
    floating_banner: bool = False
    error_notification: bool = False
    # Page meta
    url: str = ""
    page_text_snippet: str = ""
    # Recovery actions taken
    actions_taken: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = [
            f"level={self.wizard_level}",
            f"conv_loc={self.conversion_location}",
            f"conv_ev={self.conversion_event}",
        ]
        if self.ai_guided_dialog: parts.append("ai_dialog")
        if self.objective_modal:  parts.append("obj_modal")
        if self.floating_banner:  parts.append("banner")
        if self.error_notification: parts.append("error_notif")
        if self.actions_taken:    parts.append("fixed:" + "+".join(self.actions_taken))
        return "|".join(parts)

    @property
    def conversion_ok(self) -> bool:
        return (
            self.conversion_location == "website"
            and self.conversion_event == "lead"
        )


# ── Low-level DOM readers ──────────────────────────────────────────────────────

def _roots(page: Page):
    out = [page]
    for f in page.frames:
        if f != page.main_frame:
            out.append(f)
    return out


def _visible_text(page: Page, timeout_ms: int = 1500) -> str:
    try:
        return page.inner_text("body", timeout=timeout_ms)
    except Exception:
        return ""


def _detect_wizard_level(page: Page) -> str:
    """Detect campaign/ad_set/ad from sidebar tree or active panel heading."""
    LEVEL_PATTERNS = {
        "ad_set": re.compile(
            r"ad set name|nombre del conjunto|audience|targeting|budget|presupuesto", re.I
        ),
        "ad": re.compile(
            r"ad setup|ad creative|configuraci[oó]n del anuncio|ad name|nombre del anuncio", re.I
        ),
        "campaign": re.compile(
            r"campaign name|nombre de la campa|campaign objective|buying type", re.I
        ),
    }
    for root in _roots(page):
        # Check sidebar treeitem selection
        try:
            items = root.get_by_role("treeitem").all()
            for i, item in enumerate(items):
                try:
                    if item.get_attribute("aria-selected") == "true":
                        txt = (item.inner_text(timeout=400) or "").lower()
                        if "campaign" in txt:
                            return "campaign"
                        if "ad set" in txt or "conjunto" in txt:
                            return "ad_set"
                        if "ad" in txt:
                            return "ad"
                        # Fallback: sidebar order → 0=campaign, 1=ad_set, 2=ad
                        if i == 0: return "campaign"
                        if i == 1: return "ad_set"
                        if i == 2: return "ad"
                except Exception:
                    pass
        except Exception:
            pass
        # Fallback: scan visible text
        try:
            txt = root.inner_text("body", timeout=1000) or ""
            for level, pat in LEVEL_PATTERNS.items():
                if pat.search(txt):
                    return level
        except Exception:
            pass
    return "unknown"


def _detect_conversion_location(page: Page) -> str:
    for root in _roots(page):
        try:
            # Look for the conversion location dropdown value
            loc_section = root.get_by_text(
                re.compile(r"conversion location|ubicaci[oó]n de la conversi[oó]n", re.I)
            ).first
            if loc_section.is_visible(timeout=800):
                # Get nearby text to read the selected value
                container = loc_section.locator("..").locator("..")
                txt = container.inner_text(timeout=800)
                if _WEBSITE_ONLY_RE.search(txt.split("\n")[0] if txt else ""):
                    return "website"
                if _WEBSITE_FORMS_RE.search(txt):
                    return "website_and_forms"
                # Check dropdown button text
                for sel in ['[role="button"]', '[role="combobox"]', 'button', 'select']:
                    try:
                        btns = container.locator(sel).all()
                        for btn in btns[:5]:
                            btext = (btn.inner_text(timeout=300) or "").strip()
                            if _WEBSITE_ONLY_RE.search(btext):
                                return "website"
                            if _WEBSITE_FORMS_RE.search(btext):
                                return "website_and_forms"
                    except Exception:
                        pass
        except Exception:
            pass
    # Fallback: scan full page text
    try:
        txt = _visible_text(page)
        if "Website and instant forms" in txt or "Sitio web y formularios" in txt:
            return "website_and_forms"
        # Count occurrences — "Website" alone in dropdown
        if re.search(r"\bWebsite\b|\bSitio web\b", txt):
            return "website"
    except Exception:
        pass
    return "unknown"


def _detect_conversion_event(page: Page) -> str:
    LEAD_SECTION_RE = re.compile(r"\* conversion event|\* evento de conversi[oó]n", re.I)
    for root in _roots(page):
        try:
            section = root.get_by_text(LEAD_SECTION_RE).first
            if section.is_visible(timeout=800):
                container = section.locator("..").locator("..")
                txt = container.inner_text(timeout=800)
                if _LEAD_EVENT_RE.search(txt):
                    return "lead"
                if txt.strip():
                    return "other"
        except Exception:
            pass
    # Not on page at all (e.g. wrong level)
    return "not_visible"


def _has_floating_banner(page: Page) -> bool:
    for root in _roots(page):
        try:
            for sel in _BANNER_CLOSE_SELS:
                els = root.locator(sel).all()
                for el in els[:3]:
                    if el.is_visible(timeout=300):
                        return True
        except Exception:
            pass
    return False


def _has_error_notification(page: Page) -> bool:
    for root in _roots(page):
        try:
            for role in ("alert", "alertdialog"):
                alerts = root.get_by_role(role).all()
                for a in alerts[:3]:
                    if a.is_visible(timeout=300):
                        return True
        except Exception:
            pass
        try:
            errs = root.locator('[data-testid*="error"], [class*="error"], [class*="Error"]').all()
            for e in errs[:3]:
                if e.is_visible(timeout=300):
                    return True
        except Exception:
            pass
    return False


def _has_ai_guided_dialog(page: Page) -> bool:
    for root in _roots(page):
        try:
            el = root.get_by_text(_AI_GUIDED_RE).first
            if el.is_visible(timeout=600):
                return True
        except Exception:
            pass
    return False


def _has_objective_modal(page: Page) -> bool:
    for root in _roots(page):
        try:
            el = root.get_by_text(_OBJECTIVE_MODAL_RE).first
            if el.is_visible(timeout=600):
                return True
        except Exception:
            pass
    return False


# ── Recovery actions ───────────────────────────────────────────────────────────

def _dismiss_banners(page: Page) -> bool:
    dismissed = False
    for root in _roots(page):
        script = f"""() => {{
          {_WIZARD_CLOSE_GUARD_JS}
          const sels = ['[aria-label="Close"]','[aria-label="Cerrar"]',
                        '[aria-label="Dismiss"]','[aria-label="Descartar"]',
                        'button[data-testid="close_button"]'];
          for (const s of sels) {{
            for (const el of document.querySelectorAll(s)) {{
              if (el.offsetParent !== null && !_isWizardClose(el)) {{ el.click(); return true; }}
            }}
          }}
          return false;
        }}"""
        try:
            if root.evaluate(script):
                dismissed = True
                time.sleep(0.3)
        except Exception:
            pass
    return dismissed


def _click_manual_setup_and_continue(page: Page) -> bool:
    """Stronger Manual setup path (same idea as ads_manager_flow dismiss)."""
    MANUAL_TEXTS = [
        re.compile(r"Manual campaign setup", re.I),
        re.compile(r"Configuraci[oó]n manual de campa", re.I),
    ]
    for root in _roots(page):
        for pat in MANUAL_TEXTS:
            try:
                el = root.get_by_text(pat).first
                el.wait_for(state="visible", timeout=2500)
                el.click(timeout=4000, force=True)
                time.sleep(0.5)
            except Exception:
                continue
            for csel in (
                "role=button[name=/^Continue$/i]",
                "role=button[name=/^Continuar$/i]",
            ):
                try:
                    cbtn = root.locator(csel).first
                    cbtn.wait_for(state="visible", timeout=3000)
                    cbtn.click(timeout=4000, force=True)
                    time.sleep(1.0)
                    # Success if AI describe box gone
                    try:
                        if not root.get_by_placeholder(
                            re.compile(r"Describe your campaign", re.I)
                        ).first.is_visible(timeout=600):
                            return True
                    except Exception:
                        return True
                except Exception:
                    pass
    return False


def _navigate_to_ad_set(page: Page) -> bool:
    """Try to get back to Ad Set level from wherever we are."""
    from src.fb_ads_rpa.meta_dom import (
        wizard_editor_level, click_wizard_back_button,
        click_wizard_breadcrumb_index, click_wizard_sidebar_level,
    )
    level = wizard_editor_level(page)
    if level == "ad_set":
        return True
    if level == "ad":
        if click_wizard_back_button(page):
            time.sleep(0.8)
            return wizard_editor_level(page) == "ad_set"
        if click_wizard_breadcrumb_index(page, 1):
            time.sleep(0.8)
            return wizard_editor_level(page) == "ad_set"
    if level == "campaign":
        # Try Next
        for root in _roots(page):
            try:
                btn = root.get_by_role("button", name=re.compile(r"^(Next|Siguiente)$", re.I)).first
                if btn.is_visible(timeout=1000) and btn.is_enabled(timeout=1000):
                    btn.click(timeout=3000)
                    time.sleep(1.0)
                    return wizard_editor_level(page) == "ad_set"
            except Exception:
                pass
    if click_wizard_sidebar_level(page, 1):
        time.sleep(0.8)
        return wizard_editor_level(page) == "ad_set"
    return False


def _fix_conversion_location(page: Page) -> bool:
    from src.fb_ads_rpa.meta_dom import select_conversion_location_website
    try:
        return select_conversion_location_website(page)
    except Exception:
        return False


def _fix_conversion_event(page: Page) -> bool:
    from src.fb_ads_rpa.meta_dom import select_conversion_event_lead
    try:
        return select_conversion_event_lead(page)
    except Exception:
        return False


# ── Main diagnostic entry point ───────────────────────────────────────────────

def diagnose_page(page: Page) -> PageDiagnosis:
    """Inspect current DOM and return structured diagnosis — no side effects."""
    diag = PageDiagnosis()
    try:
        diag.url = page.url or ""
    except Exception:
        pass
    diag.wizard_level = _detect_wizard_level(page)
    diag.conversion_location = _detect_conversion_location(page)
    diag.conversion_event = _detect_conversion_event(page)
    diag.ai_guided_dialog = _has_ai_guided_dialog(page)
    diag.objective_modal = _has_objective_modal(page)
    diag.floating_banner = _has_floating_banner(page)
    diag.error_notification = _has_error_notification(page)
    return diag


# Recovery priority table: (condition_fn, action_label, action_fn)
_RECOVERY_RULES: list[tuple[Callable[[PageDiagnosis], bool], str, Callable[[Page], bool]]] = [
    # 1. Dismiss floating banners first — they block clicks
    (
        lambda d: d.floating_banner,
        "dismiss_banners",
        _dismiss_banners,
    ),
    # 2. Handle AI-guided dialog
    (
        lambda d: d.ai_guided_dialog,
        "click_manual_setup",
        _click_manual_setup_and_continue,
    ),
    # 3. Navigate to Ad Set if on wrong level
    (
        lambda d: d.wizard_level in ("campaign", "ad", "unknown")
                  and d.wizard_level != "ad_set",
        "navigate_to_ad_set",
        _navigate_to_ad_set,
    ),
    # 4. Fix conversion location
    (
        lambda d: d.wizard_level == "ad_set"
                  and d.conversion_location != "website",
        "fix_conversion_location",
        _fix_conversion_location,
    ),
    # 5. Fix conversion event
    (
        lambda d: d.wizard_level == "ad_set"
                  and d.conversion_location == "website"
                  and d.conversion_event not in ("lead", "not_visible"),
        "fix_conversion_event",
        _fix_conversion_event,
    ),
]


def diagnose_and_recover(
    page: Page,
    failed_step: str = "",
    *,
    max_attempts: int = 3,
) -> PageDiagnosis:
    """
    Diagnose current page state and apply recovery actions.
    Runs up to max_attempts diagnosis+fix cycles.
    Returns the final diagnosis after all recovery attempts.
    """
    diag = diagnose_page(page)

    for attempt in range(max_attempts):
        applied_any = False
        for condition, label, action_fn in _RECOVERY_RULES:
            if condition(diag):
                success = action_fn(page)
                diag.actions_taken.append(f"{label}={'ok' if success else 'fail'}")
                applied_any = True
                time.sleep(0.5)
                # Re-diagnose after each fix
                fresh = diagnose_page(page)
                fresh.actions_taken = diag.actions_taken
                diag = fresh
                if diag.conversion_ok:
                    break
        if not applied_any or diag.conversion_ok:
            break

    return diag
