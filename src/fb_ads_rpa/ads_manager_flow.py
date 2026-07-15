"""Playwright RPA flow for Facebook Ads Manager campaign creation."""
from __future__ import annotations

import re
import time
from typing import Any

RPA_FLOW_VERSION = "v29"

from playwright.sync_api import Frame, Locator, Page, TimeoutError as PlaywrightTimeout

from src.fb_ads_rpa.draft import AdDraft, AdSetDraft, CampaignDraft, RpaPublishResult
from src.fb_ads_rpa.page_diagnostics import diagnose_and_recover
from src.fb_ads_rpa.meta_dom import (
    _COUNTRY_NAME_TO_ISO,
    _COUNTRY_SEARCH,
    click_conversion_location_dropdown,
    click_conversion_event_dropdown,
    click_sidebar_tree_index,
    click_text_button,
    click_wizard_breadcrumb_index,
    click_wizard_back_button,
    click_wizard_sidebar_level,
    close_open_dropdown,
    conversion_menu_is_open,
    conversion_location_menu_visible,
    dismiss_open_popovers,
    expand_conversion_location_single,
    fill_audience_location,
    fill_audience_age,
    fill_input_near_label,
    fill_input_near_label_verified,
    fill_targeting_interest,
    fill_targeting_search,
    is_lead_conversion_event_selected,
    is_website_conversion_selected,
    main_panel_has_label,
    open_detailed_targeting_field,
    pick_first_suggestion,
    scroll_to_section,
    select_conversion_event_lead,
    select_conversion_location_website,
    select_manual_placements,
    select_option_near_section,
    select_placement_device,
    wait_conversion_menu_open,
    wait_editor_saved,
    wizard_editor_level,
    _capture_website_locator_to_recipe,
)
from src.fb_ads_rpa.pacing import (
    pause_after_navigation,
    pause_medium,
    pause_short,
    begin_teach_wait,
    wait_for_teach_resume,
    end_teach_wait,
    get_teach_state,
)
from src.fb_ads_rpa.screenshots import capture
from src.fb_ads_rpa.selectors import load_selectors


class AdsManagerFlowError(RuntimeError):
    def __init__(self, message: str, *, screenshot: str | None = None):
        super().__init__(message)
        self.screenshot = screenshot


def _act_numeric(ad_account_id: str) -> str:
    return re.sub(r"^act_", "", (ad_account_id or "").strip())


def _selector_list(sels: dict[str, Any], key: str) -> list[str]:
    """Return selector list for *key* from an already-extracted selectors dict."""
    raw = sels.get(key) or []
    return [str(s) for s in raw]


def _parse_role_name(raw: str) -> re.Pattern[str]:
    """Parse /pattern/flags or plain string into a regex for get_by_role name=."""
    m = re.match(r"^/(.+)/([gimsuy]*)$", raw.strip())
    if m:
        pattern, flags_str = m.group(1), m.group(2)
        flags = 0
        if "i" in flags_str:
            flags |= re.IGNORECASE
        return re.compile(pattern, flags)
    return re.compile(re.escape(raw.strip()), re.IGNORECASE)


PageLike = Page | Frame


def _ads_roots(page: Page) -> list[PageLike]:
    """Search main page and embedded frames (Ads Manager sometimes nests UI)."""
    roots: list[PageLike] = [page]
    for frame in page.frames:
        if frame != page.main_frame:
            roots.append(frame)
    return roots


def _try_locator(root: PageLike, selector: str) -> Locator | None:
    if selector.startswith("role="):
        m = re.match(r"role=(\w+)\[name=([^\]]+)\]", selector)
        if m:
            role, name_raw = m.group(1), m.group(2)
            loc = root.get_by_role(role, name=_parse_role_name(name_raw))
            return loc.first if loc.count() else None
    if selector.startswith("label="):
        label_raw = selector[6:].strip()
        if label_raw.startswith("/"):
            loc = root.get_by_label(_parse_role_name(label_raw))
        else:
            loc = root.get_by_label(label_raw, exact=False)
        return loc.first if loc.count() else None
    if selector.startswith("placeholder="):
        ph_raw = selector[12:].strip()
        if ph_raw.startswith("/"):
            loc = root.get_by_placeholder(_parse_role_name(ph_raw))
        else:
            loc = root.get_by_placeholder(ph_raw, exact=False)
        return loc.first if loc.count() else None
    if selector.startswith("text="):
        text = selector[5:]
        loc = root.get_by_text(re.compile(re.escape(text), re.I))
        return loc.first if loc.count() else None
    loc = root.locator(selector)
    return loc.first if loc.count() else None


def _click_first(page: Page, selectors: list[str], *, timeout_ms: int) -> None:
    last_err: Exception | None = None
    per_try = max(3000, timeout_ms // max(len(selectors), 1))
    for root in _ads_roots(page):
        for sel in selectors:
            try:
                loc = _try_locator(root, sel)
                if loc is None:
                    loc = root.locator(sel).first
                loc.wait_for(state="visible", timeout=per_try)
                loc.scroll_into_view_if_needed(timeout=per_try)
                loc.click(timeout=per_try)
                pause_short()
                return
            except Exception as exc:
                last_err = exc
    raise AdsManagerFlowError(f"Could not click any of: {selectors}") from last_err


_CREATE_BUTTON_RE = re.compile(r"\+?\s*(Create|Crear)", re.I)
_LOADING_OVERLAY_RE = re.compile(r"^(Loading\.\.\.|Cargando\.\.\.)$", re.I)
_OBJECTIVE_MODAL_RE = re.compile(
    r"(Choose a campaign objective|Elige un objetivo|Buying type|Tipo de compra|"
    r"Awareness|Traffic|Engagement|Leads|App promotion|Sales)",
    re.I,
)
_OBJECTIVE_CHOOSER_RE = re.compile(
    r"Choose a campaign objective|Elige un objetivo",
    re.I,
)
_OBJECTIVE_TITLE_RE = re.compile(
    r"(Create new campaign|Crear nueva campa)",
    re.I,
)
_AI_GUIDED_DIALOG_RE = re.compile(
    r"(Meta AI.guided setup|Manual campaign setup|Configuraci[oó]n manual de campa)",
    re.I,
)
_MANUAL_SETUP_RE = re.compile(
    r"Manual campaign setup|Configuraci[oó]n manual de campa",
    re.I,
)
_AI_SETUP_RE = re.compile(r"Meta AI.guided setup", re.I)
_AI_DESCRIBE_RE = re.compile(r"Describe your campaign", re.I)
_CONTINUE_BUTTON_RE = re.compile(r"^(Continue|Continuar)$", re.I)
_NEXT_BUTTON_RE = re.compile(r"^(Next|Siguiente)$", re.I)
_CAMPAIGN_NAME_LABEL_RE = re.compile(r"^Campaign name$|^Nombre de la campaña$", re.I)
_AD_SET_NAME_LABEL_RE = re.compile(r"^Ad set name$|^Nombre del conjunto", re.I)
_DAILY_BUDGET_LABEL_RE = re.compile(r"^Daily budget$|^Presupuesto diario", re.I)


def _ai_describe_textarea_visible(page: Page) -> bool:
    for root in _ads_roots(page):
        try:
            if root.get_by_placeholder(_AI_DESCRIBE_RE).first.is_visible(timeout=500):
                return True
        except Exception:
            pass
        try:
            if root.get_by_text(_AI_DESCRIBE_RE).first.is_visible(timeout=400):
                return True
        except Exception:
            pass
    return False


def _objectives_chooser_visible(page: Page) -> bool:
    """True when Manual is selected and objective list (Leads/Traffic/…) is shown.

    On modern Ads Manager, Manual + objectives appear in the SAME dialog that still
    contains the text 'Meta AI-guided setup' — so we must not treat that as 'stuck'.
    """
    for root in _ads_roots(page):
        try:
            if root.get_by_text(_OBJECTIVE_CHOOSER_RE).first.is_visible(timeout=600):
                return True
        except Exception:
            pass
        # Fallback: several objective names visible together
        try:
            has_leads = root.get_by_text(re.compile(r"^Leads$", re.I)).first.is_visible(timeout=400)
            has_traffic = root.get_by_text(re.compile(r"^Traffic$", re.I)).first.is_visible(timeout=400)
            has_aware = root.get_by_text(re.compile(r"^Awareness$", re.I)).first.is_visible(timeout=400)
            if has_leads and (has_traffic or has_aware):
                return True
        except Exception:
            pass
    return False


def _is_ai_guided_dialog_open(page: Page) -> bool:
    """True only while still on the AI-vs-Manual choice / AI prompt — not when objectives show.

    Screenshot 02c_ai_dialog_stuck showed Manual already selected + objectives list, but
    Continue disabled until Leads is picked. That state is NOT 'AI stuck'.
    """
    if _objectives_chooser_visible(page):
        return False
    if _ai_describe_textarea_visible(page):
        return True
    for root in _ads_roots(page):
        try:
            if root.get_by_text(_AI_SETUP_RE).first.is_visible(timeout=500):
                # Choice screen without objectives yet
                if root.get_by_text(_MANUAL_SETUP_RE).first.is_visible(timeout=400):
                    return True
        except Exception:
            pass
        try:
            loc = root.get_by_text(_AI_GUIDED_DIALOG_RE)
            if loc.count() and loc.first.is_visible():
                # If both Manual text and no objectives → still on choice screen
                return not _objectives_chooser_visible(page)
        except Exception:
            pass
    return False


def _is_objective_modal_open(page: Page) -> bool:
    """True when the Leads/objective picker is visible (including inside Manual dialog)."""
    if _objectives_chooser_visible(page):
        return True
    if _is_ai_guided_dialog_open(page):
        return False
    for root in _ads_roots(page):
        try:
            loc = root.get_by_text(_OBJECTIVE_MODAL_RE)
            if loc.count() and loc.first.is_visible():
                return True
        except Exception:
            pass
        try:
            dlg = root.get_by_role("dialog")
            if dlg.count() and dlg.first.is_visible():
                txt = dlg.first.inner_text(timeout=800) or ""
                if _OBJECTIVE_CHOOSER_RE.search(txt):
                    return True
                if re.search(r"\bLeads\b", txt) and re.search(
                    r"Traffic|Awareness|Engagement|Sales", txt, re.I
                ):
                    return True
        except Exception:
            pass
    return False


def _is_manual_setup_selected(page: Page) -> bool:
    """True when Manual is chosen (objectives visible, AI textarea gone, or radio checked)."""
    if _objectives_chooser_visible(page):
        return True
    if _ai_describe_textarea_visible(page):
        return False
    script = """
    () => {
      const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
      for (const el of document.querySelectorAll(
        '[role=radio], [aria-checked], input[type=radio], label, div'
      )) {
        if (!el.offsetParent) continue;
        const t = norm(el.getAttribute('aria-label') || el.textContent || '');
        if (!t.includes('manual campaign setup') && !t.includes('configuración manual')) continue;
        if (t.includes('meta ai')) continue;
        const checked = el.getAttribute('aria-checked');
        if (checked === 'true') return true;
        if (el.checked === true) return true;
      }
      return false;
    }
    """
    for root in _ads_roots(page):
        try:
            if root.evaluate(script):
                return True
        except Exception:
            pass
    # Choice screen still open but AI prompt gone → Manual selected
    if not _is_ai_guided_dialog_open(page):
        return True
    return not _ai_describe_textarea_visible(page)


def _click_manual_campaign_setup(page: Page) -> bool:
    """Click 'Manual campaign setup' with CDP-resilient strategies."""
    # 1) Semantic / role
    for root in _ads_roots(page):
        for name_re in (
            re.compile(r"Manual campaign setup", re.I),
            re.compile(r"Configuraci[oó]n manual de campa", re.I),
        ):
            try:
                radio = root.get_by_role("radio", name=name_re).first
                if radio.is_visible(timeout=1500):
                    radio.click(timeout=4000, force=True)
                    time.sleep(0.45)
                    if _is_manual_setup_selected(page):
                        return True
            except Exception:
                pass
            try:
                el = root.get_by_text(name_re).first
                if el.is_visible(timeout=1500):
                    el.click(timeout=4000, force=True)
                    time.sleep(0.45)
                    if _is_manual_setup_selected(page):
                        return True
                    # Mouse chain on bbox (CDP-safe)
                    box = el.bounding_box()
                    if box:
                        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                        time.sleep(0.05)
                        page.mouse.down()
                        time.sleep(0.07)
                        page.mouse.up()
                        time.sleep(0.45)
                        if _is_manual_setup_selected(page):
                            return True
            except Exception:
                pass

    # 2) JS: find Manual row and click radio / row
    script = """
    () => {
      const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
      const hits = [];
      for (const el of document.querySelectorAll(
        '[role=radio], label, div, span, li'
      )) {
        if (!el.offsetParent) continue;
        const t = norm(el.getAttribute('aria-label') || '');
        const own = norm(
          Array.from(el.childNodes).filter(n => n.nodeType === 3)
            .map(n => n.textContent).join('') || el.textContent || ''
        );
        const text = t || own;
        if (!text.includes('manual campaign setup') && !text.includes('configuración manual')) continue;
        if (text.includes('meta ai')) continue;
        const r = el.getBoundingClientRect();
        if (r.width < 8 || r.height < 8 || r.height > 120) continue;
        hits.push({ el, area: r.width * r.height });
      }
      if (!hits.length) return false;
      hits.sort((a, b) => a.area - b.area);
      const target = hits[0].el;
      target.scrollIntoView({ block: 'nearest' });
      const radio = target.closest('[role=radio]') || target.querySelector('[role=radio]') || target;
      radio.dispatchEvent(new MouseEvent('mousedown', {bubbles:true, cancelable:true, view:window}));
      radio.dispatchEvent(new MouseEvent('mouseup', {bubbles:true, cancelable:true, view:window}));
      radio.dispatchEvent(new MouseEvent('click', {bubbles:true, cancelable:true, view:window}));
      if (typeof radio.click === 'function') radio.click();
      return true;
    }
    """
    for root in _ads_roots(page):
        try:
            if root.evaluate(script):
                time.sleep(0.5)
                if _is_manual_setup_selected(page):
                    return True
        except Exception:
            pass
    return _is_manual_setup_selected(page)


def _dismiss_ai_guided_setup_dialog(page: Page, *, timeout_ms: int = 20000) -> bool:
    """Select Manual campaign setup until the objective chooser appears.

    Success = Manual selected AND 'Choose a campaign objective' is visible.
    Do NOT click Continue here — Continue stays disabled until Leads is picked.
    """
    if _objectives_chooser_visible(page):
        return True
    if not _is_ai_guided_dialog_open(page) and not _ai_describe_textarea_visible(page):
        return True

    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        if _objectives_chooser_visible(page):
            return True
        _click_manual_campaign_setup(page)
        time.sleep(0.55)
        if _objectives_chooser_visible(page):
            return True
        if _is_manual_setup_selected(page) and not _ai_describe_textarea_visible(page):
            # Manual selected; wait briefly for objectives to render
            time.sleep(0.8)
            if _objectives_chooser_visible(page):
                return True
            # Objectives may already be there under a slightly different label
            if _is_objective_modal_open(page):
                return True
        time.sleep(0.35)

    return _objectives_chooser_visible(page) or (
        _is_manual_setup_selected(page) and not _ai_describe_textarea_visible(page)
    )


def _wait_for_ads_manager_ready(page: Page, *, timeout_ms: int = 60000) -> None:
    """Wait until Meta loading overlay is gone and Create is interactable."""
    import time as _time

    deadline = _time.time() + timeout_ms / 1000
    while _time.time() < deadline:
        loading_visible = False
        for root in _ads_roots(page):
            try:
                loading = root.get_by_text(_LOADING_OVERLAY_RE)
                if loading.count() and loading.first.is_visible():
                    loading_visible = True
                    break
            except Exception:
                pass
        if not loading_visible:
            for root in _ads_roots(page):
                for finder in (
                    lambda r: r.get_by_role("button", name=_CREATE_BUTTON_RE),
                    lambda r: r.get_by_text(_CREATE_BUTTON_RE),
                ):
                    try:
                        loc = finder(root).first
                        loc.wait_for(state="visible", timeout=2000)
                        return
                    except Exception:
                        pass
        _time.sleep(0.5)


def _click_continue_when_enabled(page: Page, *, timeout_ms: int) -> None:
    import time as _time

    deadline = _time.time() + timeout_ms / 1000
    last_err: Exception | None = None
    while _time.time() < deadline:
        for root in _ads_roots(page):
            try:
                btn = root.get_by_role("button", name=_CONTINUE_BUTTON_RE).first
                btn.wait_for(state="visible", timeout=1500)
                if btn.is_enabled():
                    btn.scroll_into_view_if_needed(timeout=3000)
                    btn.click(timeout=5000)
                    pause_short()
                    return
            except Exception as exc:
                last_err = exc
        _time.sleep(0.35)
    raise AdsManagerFlowError("Continue button not enabled/visible") from last_err


def _select_leads_objective(page: Page, selectors: list[str], *, timeout_ms: int) -> None:
    """Pick Leads in the objective modal (radio cards in new Ads Manager UI)."""
    last_err: Exception | None = None
    per_try = max(2500, timeout_ms // max(len(selectors) + 2, 1))

    for root in _ads_roots(page):
        for name_re in (
            re.compile(r"^Leads$", re.I),
            re.compile(r"Clientes potenciales", re.I),
        ):
            # Recorded path: click heading/text 'Leads' then radio
            try:
                heading = root.get_by_role("heading", name=name_re).first
                if heading.is_visible(timeout=1500):
                    box = heading.bounding_box()
                    if box:
                        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                        time.sleep(0.05)
                        page.mouse.down()
                        time.sleep(0.07)
                        page.mouse.up()
                    else:
                        heading.click(timeout=per_try, force=True)
                    pause_short()
                    return
            except Exception as exc:
                last_err = exc
            try:
                radio = root.get_by_role("radio", name=name_re).first
                radio.wait_for(state="visible", timeout=per_try)
                radio.click(timeout=per_try, force=True)
                pause_short()
                return
            except Exception as exc:
                last_err = exc
            try:
                el = root.get_by_text(name_re).first
                if el.is_visible(timeout=1500):
                    box = el.bounding_box()
                    if box:
                        page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                    else:
                        el.click(timeout=per_try, force=True)
                    pause_short()
                    return
            except Exception as exc:
                last_err = exc

    _click_first(page, selectors, timeout_ms=timeout_ms)


def _is_campaign_editor_active(page: Page) -> bool:
    return wizard_editor_level(page) == "campaign" or main_panel_has_label(
        page, ["Campaign name", "Nombre de la campaña"]
    )


def _is_ad_set_level_visible(page: Page) -> bool:
    return wizard_editor_level(page) == "ad_set"


def _is_ad_level_visible(page: Page) -> bool:
    return wizard_editor_level(page) == "ad"


def _click_ad_sidebar(page: Page) -> bool:
    if click_wizard_sidebar_level(page, 2):
        pause_medium()
        return True
    return False


def _click_ad_set_sidebar(page: Page) -> bool:
    if click_wizard_sidebar_level(page, 1):
        pause_medium()
        return True
    return False


def _click_next_step(page: Page, selectors: list[str], *, timeout_ms: int) -> None:
    per_try = max(4000, timeout_ms // 2)
    for root in _ads_roots(page):
        try:
            btn = root.get_by_role("button", name=_NEXT_BUTTON_RE).first
            btn.wait_for(state="visible", timeout=per_try)
            btn.scroll_into_view_if_needed(timeout=per_try)
            if btn.is_enabled():
                btn.click(timeout=per_try)
                pause_short()
                return
        except Exception:
            pass
    _click_first(page, selectors, timeout_ms=timeout_ms)


def _fill_first(page: Page, selectors: list[str], value: str, *, timeout_ms: int) -> None:
    last_err: Exception | None = None
    per_try = max(3000, timeout_ms // max(len(selectors), 1))
    for root in _ads_roots(page):
        for sel in selectors:
            try:
                loc = _try_locator(root, sel)
                if loc is None:
                    loc = root.locator(sel).first
                loc.wait_for(state="visible", timeout=per_try)
                loc.scroll_into_view_if_needed(timeout=per_try)
                loc.click(timeout=per_try)
                loc.fill(value, timeout=per_try)
                pause_short()
                return
            except Exception as exc:
                last_err = exc
    raise AdsManagerFlowError(f"Could not fill {value!r} using: {selectors}") from last_err


def _upload_creative(page: Page, ad: AdDraft, selectors_cfg: dict[str, Any], timeout_ms: int) -> None:
    if not ad.creative_path:
        raise AdsManagerFlowError(f"No creative path for {ad.creative_id or ad.creative_filename}")

    path = ad.creative_path
    file_inputs = page.locator("input[type='file']")
    if file_inputs.count():
        file_inputs.first.set_input_files(path, timeout=timeout_ms)
        pause_medium()
        return

    _click_first(page, _selector_list(selectors_cfg, "add_media_button"), timeout_ms=timeout_ms)
    pause_medium()
    if file_inputs.count():
        file_inputs.first.set_input_files(path, timeout=timeout_ms)
        pause_medium()
        return
    raise AdsManagerFlowError(f"File input not found for creative {path}")


def _fill_ad_copy(page: Page, ad: AdDraft, selectors_cfg: dict[str, Any], timeout_ms: int) -> None:
    if ad.ad_body:
        try:
            _fill_first(
                page,
                _selector_list(selectors_cfg, "primary_text_input"),
                ad.ad_body,
                timeout_ms=timeout_ms,
            )
        except AdsManagerFlowError:
            pass
    if ad.ad_title:
        try:
            _fill_first(
                page,
                _selector_list(selectors_cfg, "headline_input"),
                ad.ad_title,
                timeout_ms=timeout_ms,
            )
        except AdsManagerFlowError:
            pass


class AdsManagerFlow:
    """Navigate Ads Manager UI and create one campaign tree."""

    def __init__(self, page: Page, *, selectors_path: str | None = None):
        self.page = page
        self.cfg = load_selectors(selectors_path)
        timing = self.cfg.get("timing") or {}
        self.nav_timeout = int(timing.get("navigation_timeout_ms") or 90000)
        self.action_timeout = int(timing.get("action_timeout_ms") or 45000)

    def _variations_ranks(self) -> "StrategyRanks | None":
        cached = getattr(self, "_strategy_ranks", None)
        if cached is not None:
            return cached
        try:
            from src.fb_ads_rpa.variations.ranks import StrategyRanks

            ranks = StrategyRanks.load()
        except Exception:
            return None
        self._strategy_ranks = ranks
        return ranks

    def _log_variation_outcome(
        self,
        step_id: str,
        strategy_id: str,
        *,
        ok: bool,
        screenshot: str | None = None,
    ) -> None:
        ranks = self._variations_ranks()
        if ranks is None:
            return
        try:
            ranks.log_outcome(step_id, strategy_id, ok=ok, screenshot=screenshot)
        except Exception:
            pass

    def _mark_variation_next_step_ok(self, step_id: str) -> None:
        ranks = self._variations_ranks()
        if ranks is None:
            return
        try:
            ranks.mark_next_step_ok(step_id)
        except Exception:
            pass

    def _infer_nav_ad_set_strategy(self, steps: list[str]) -> str:
        if any("ad_set_breadcrumb" in step for step in steps):
            return "breadcrumb_ad_set"
        if any("next_from_campaign" in step for step in steps):
            return "next_step"
        return "sidebar_ad_set"

    def _log_nav_ad_set_success(self, result: RpaPublishResult, steps: list[str]) -> None:
        if getattr(result, "_ranks_nav_ad_set_logged", False):
            return
        self._log_variation_outcome(
            "nav_ad_set",
            self._infer_nav_ad_set_strategy(steps),
            ok=True,
        )
        setattr(result, "_ranks_nav_ad_set_logged", True)

    def _infer_conversion_location_website_strategy(self, steps: list[str]) -> str:
        if any("teach_website" in step or "website_teach" in step for step in steps):
            return "teach_mode"
        if any("recipe" in step for step in steps):
            return "recipe_space"
        return "playwright_click"

    def _log_conversion_location_website_outcome(
        self,
        result: RpaPublishResult,
        steps: list[str],
        *,
        ok: bool,
        screenshot: str | None = None,
    ) -> None:
        ranks = self._variations_ranks()
        if ranks is None:
            return
        try:
            from src.fb_ads_rpa.variations.catalog import default_strategies

            ranks.ordered_strategies(
                "conversion_location_website",
                default_strategies("conversion_location_website"),
            )
        except Exception:
            pass
        try:
            ranks.log_outcome(
                "conversion_location_website",
                self._infer_conversion_location_website_strategy(steps),
                ok=ok,
                screenshot=screenshot,
            )
        except Exception:
            pass

    def _shot(self, label: str, result: RpaPublishResult) -> str:
        path = capture(self.page, label)
        result.screenshot_paths.append(path)
        return path

    def _wait_ad_set_level(self, *, timeout_s: float = 12) -> bool:
        import time as _time

        deadline = _time.time() + timeout_s
        while _time.time() < deadline:
            if _is_ad_set_level_visible(self.page):
                return True
            _time.sleep(0.4)
        return False

    def _navigate_to_ad_set_level(self, result: RpaPublishResult) -> bool:
        if _is_ad_set_level_visible(self.page):
            return True

        sels = self.cfg.get("selectors") or {}
        level = wizard_editor_level(self.page)
        if level:
            result.steps_completed.append(f"wizard_level_{level}")

        if level == "ad":
            if click_wizard_back_button(self.page):
                result.steps_completed.append("click_back_from_ad_level")
                pause_medium()
                if self._wait_ad_set_level(timeout_s=12):
                    result.steps_completed.append("ad_set_level_ready")
                    return True
            if click_wizard_breadcrumb_index(self.page, 1):
                result.steps_completed.append("click_ad_set_breadcrumb")
                pause_medium()
                if self._wait_ad_set_level(timeout_s=12):
                    result.steps_completed.append("ad_set_level_ready")
                    return True

        if _click_ad_set_sidebar(self.page):
            result.steps_completed.append("click_ad_set_sidebar")
            pause_medium()
            if self._wait_ad_set_level(timeout_s=12):
                result.steps_completed.append("ad_set_level_ready")
                return True
            result.steps_completed.append("ad_set_sidebar_no_switch")
            self._shot("04b_ad_set_sidebar_wait", result)

        if _is_campaign_editor_active(self.page) and not _is_ad_level_visible(self.page):
            clicked = click_text_button(self.page, ["Next", "Siguiente"])
            if not clicked:
                try:
                    _click_next_step(
                        self.page,
                        _selector_list(sels, "next_button"),
                        timeout_ms=12000,
                    )
                    clicked = True
                except AdsManagerFlowError:
                    clicked = False
            if clicked:
                pause_medium()
                result.steps_completed.append("click_next_from_campaign")
                if self._wait_ad_set_level(timeout_s=15):
                    result.steps_completed.append("ad_set_level_ready")
                    return True
                result.steps_completed.append("next_from_campaign_no_ad_set")
                self._shot("04b_next_from_campaign_wait", result)

        if _is_ad_level_visible(self.page):
            if click_wizard_back_button(self.page):
                result.steps_completed.append("click_back_from_ad_level_retry")
                pause_medium()
                if self._wait_ad_set_level(timeout_s=12):
                    result.steps_completed.append("ad_set_level_ready")
                    return True
            if click_wizard_breadcrumb_index(self.page, 1):
                result.steps_completed.append("click_ad_set_breadcrumb_retry")
                pause_medium()
                if self._wait_ad_set_level(timeout_s=12):
                    result.steps_completed.append("ad_set_level_ready")
                    return True
            if _click_ad_set_sidebar(self.page):
                result.steps_completed.append("click_ad_set_from_ad_level")
                pause_medium()
                if self._wait_ad_set_level(timeout_s=12):
                    result.steps_completed.append("ad_set_level_ready")
                    return True

        try:
            _click_first(
                self.page,
                _selector_list(sels, "ad_set_nav_item"),
                timeout_ms=8000,
            )
            pause_medium()
            result.steps_completed.append("click_ad_set_nav_item")
            if self._wait_ad_set_level(timeout_s=12):
                result.steps_completed.append("ad_set_level_ready")
                return True
        except AdsManagerFlowError:
            pass

        if click_wizard_breadcrumb_index(self.page, 1):
            result.steps_completed.append("click_ad_set_breadcrumb_final")
            pause_medium()
            if self._wait_ad_set_level(timeout_s=12):
                result.steps_completed.append("ad_set_level_ready")
                return True

        return False

    def _ensure_ad_set_level(self, result: RpaPublishResult, *, force: bool = False) -> bool:
        start_idx = len(result.steps_completed)
        already = _is_ad_set_level_visible(self.page)
        if already and not force:
            self._log_nav_ad_set_success(result, result.steps_completed[start_idx:])
            return True
        if self._navigate_to_ad_set_level(result):
            result.steps_completed.append("ensure_ad_set_level_ok")
            self._log_nav_ad_set_success(result, result.steps_completed[start_idx:])
            return True
        result.steps_completed.append("ensure_ad_set_level_failed")
        self._shot("04b_ensure_ad_set_fail", result)
        return False

    def _goto_campaigns(self, draft: CampaignDraft, result: RpaPublishResult) -> None:
        act = _act_numeric(draft.ad_account_id or "")
        if not act:
            raise AdsManagerFlowError("ad_account_id missing on draft")
        url_tpl = (self.cfg.get("urls") or {}).get("campaigns") or (
            "https://adsmanager.facebook.com/adsmanager/manage/campaigns?act={act_id}"
        )
        url = url_tpl.format(act_id=act)
        self.page.goto(url, wait_until="domcontentloaded", timeout=self.nav_timeout)
        pause_after_navigation()
        result.steps_completed.append("open_campaigns")
        self._shot("01_campaigns", result)

    def _start_create_wizard(self, result: RpaPublishResult) -> None:
        sels = self.cfg.get("selectors") or {}
        create_sels = _selector_list(sels, "create_button")

        _wait_for_ads_manager_ready(self.page, timeout_ms=self.nav_timeout)
        result.steps_completed.append("campaigns_ready")

        _click_first(self.page, create_sels, timeout_ms=self.action_timeout)
        pause_medium()
        result.steps_completed.append("click_create")
        self._shot("02_create_clicked", result)

        # New Meta UI: MUST pick Manual before Continue — AI-guided has no Campaign name field.
        if _is_ai_guided_dialog_open(self.page):
            result.steps_completed.append("ai_guided_dialog_open")
            if _dismiss_ai_guided_setup_dialog(self.page, timeout_ms=self.action_timeout):
                result.steps_completed.append("manual_setup_dismissed")
                self._shot("02c_manual_setup_continued", result)
            else:
                result.steps_completed.append("manual_setup_dismiss_failed")
                self._shot("02c_manual_setup_fail", result)

        if _is_objective_modal_open(self.page):
            result.steps_completed.append("objective_modal_open")
        else:
            self._wait_for_campaign_name_field(result, quick=True)

        if (
            "wizard_form_ready" not in result.steps_completed
            and not _is_objective_modal_open(self.page)
            and not _is_ai_guided_dialog_open(self.page)
        ):
            result.steps_completed.append("create_retry")
            try:
                _click_first(self.page, create_sels, timeout_ms=12000)
                pause_medium()
                self._shot("02b_create_retry", result)
                # Retry also lands on AI dialog often — dismiss again
                if _is_ai_guided_dialog_open(self.page):
                    result.steps_completed.append("ai_guided_dialog_open_retry")
                    if _dismiss_ai_guided_setup_dialog(self.page, timeout_ms=self.action_timeout):
                        result.steps_completed.append("manual_setup_dismissed_retry")
                        self._shot("02c_manual_setup_continued", result)
                    else:
                        result.steps_completed.append("manual_setup_dismiss_failed_retry")
            except AdsManagerFlowError:
                result.steps_completed.append("create_retry_skipped")
        elif _is_ai_guided_dialog_open(self.page):
            result.steps_completed.append("create_retry_blocked_by_ai_dialog")
            if _dismiss_ai_guided_setup_dialog(self.page, timeout_ms=self.action_timeout):
                result.steps_completed.append("manual_setup_dismissed_late")
        elif "wizard_form_ready" not in result.steps_completed:
            result.steps_completed.append("create_retry_skipped_modal_open")

        # Legacy UI: Create opens a dropdown — pick Campaign if present
        try:
            _click_first(
                self.page,
                _selector_list(sels, "create_campaign_item"),
                timeout_ms=4000,
            )
            result.steps_completed.append("click_create_campaign")
            pause_medium()
        except AdsManagerFlowError:
            result.steps_completed.append("create_campaign_menu_skipped")

        # Final gate: stuck only if AI prompt still active (not when objectives are shown)
        if _ai_describe_textarea_visible(self.page) and not _objectives_chooser_visible(self.page):
            result.steps_completed.append("ai_guided_still_open")
            self._shot("02c_ai_dialog_stuck", result)
            if not _dismiss_ai_guided_setup_dialog(self.page, timeout_ms=15000):
                raise AdsManagerFlowError(
                    "Stuck on 'Meta AI-guided setup' dialog — could not select "
                    "'Manual campaign setup'. Check screenshot 02c_ai_dialog_stuck."
                )

        if _objectives_chooser_visible(self.page) or _is_objective_modal_open(self.page):
            result.steps_completed.append("objective_chooser_ready")

        try:
            _select_leads_objective(
                self.page,
                _selector_list(sels, "campaign_objective_leads"),
                timeout_ms=self.action_timeout,
            )
            result.steps_completed.append("select_leads_objective")
            pause_short()
        except AdsManagerFlowError:
            result.steps_completed.append("objective_skipped_or_preselected")

        try:
            _click_continue_when_enabled(self.page, timeout_ms=self.action_timeout)
            result.steps_completed.append("continue_after_objective")
        except AdsManagerFlowError:
            result.steps_completed.append("continue_skipped_or_disabled")

        pause_medium()
        # One more check: AI prompt (not objective chooser)
        if _ai_describe_textarea_visible(self.page) and not _objectives_chooser_visible(self.page):
            result.steps_completed.append("ai_guided_after_objective")
            _dismiss_ai_guided_setup_dialog(self.page, timeout_ms=12000)

        self._wait_for_campaign_name_field(result)
        if _ai_describe_textarea_visible(self.page) and not _objectives_chooser_visible(self.page):
            self._shot("03_wizard_started", result)
            raise AdsManagerFlowError(
                "Wizard still on AI-guided setup — Campaign name field does not exist on this screen. "
                "Manual setup was not applied."
            )
        self._shot("03_wizard_started", result)

    def _wait_for_campaign_name_field(self, result: RpaPublishResult, *, quick: bool = False) -> None:
        """Wait until campaign creation form is visible (not still on list view)."""
        sels = self.cfg.get("selectors") or {}
        name_sels = _selector_list(sels, "campaign_name_input")
        import time as _time

        timeout_ms = 8000 if quick else self.action_timeout
        deadline = _time.time() + timeout_ms / 1000
        while _time.time() < deadline:
            for root in _ads_roots(self.page):
                for sel in name_sels:
                    try:
                        loc = _try_locator(root, sel) or root.locator(sel).first
                        loc.wait_for(state="visible", timeout=1500)
                        result.steps_completed.append("wizard_form_ready")
                        return
                    except Exception:
                        pass
                for label_re in (
                    re.compile(r"campaign name", re.I),
                    re.compile(r"nombre de la campa", re.I),
                ):
                    try:
                        root.get_by_label(label_re).first.wait_for(state="visible", timeout=1500)
                        result.steps_completed.append("wizard_form_ready")
                        return
                    except Exception:
                        pass
            _time.sleep(0.4)
        result.steps_completed.append("wizard_form_wait_timeout")

    def _fill_campaign_name(self, draft: CampaignDraft, result: RpaPublishResult) -> None:
        sels = self.cfg.get("selectors") or {}
        selectors = _selector_list(sels, "campaign_name_input")
        per_try = max(4000, self.action_timeout // max(len(selectors), 1))

        for root in _ads_roots(self.page):
            for sel in selectors:
                try:
                    loc = _try_locator(root, sel) or root.locator(sel).first
                    loc.wait_for(state="visible", timeout=per_try)
                    loc.scroll_into_view_if_needed(timeout=per_try)
                    loc.click(timeout=per_try)
                    loc.fill(draft.name, timeout=per_try)
                    pause_short()
                    result.steps_completed.append("campaign_name")
                    return
                except Exception:
                    pass

            for label_re in (
                re.compile(r"^campaign name$", re.I),
                re.compile(r"nombre de la campa", re.I),
            ):
                try:
                    loc = root.get_by_label(label_re).first
                    loc.wait_for(state="visible", timeout=per_try)
                    loc.scroll_into_view_if_needed(timeout=per_try)
                    loc.click(timeout=per_try)
                    loc.fill(draft.name, timeout=per_try)
                    pause_short()
                    result.steps_completed.append("campaign_name")
                    return
                except Exception:
                    pass

            # Meta sometimes shows a clickable title instead of a visible input
            for title_re in (
                re.compile(r"new leads campaign", re.I),
                re.compile(r"untitled campaign", re.I),
                re.compile(r"nueva campa", re.I),
            ):
                try:
                    title = root.get_by_text(title_re).first
                    title.wait_for(state="visible", timeout=3000)
                    title.click(timeout=3000)
                    pause_short()
                    inp = root.locator("input:focus, textarea:focus").first
                    inp.fill(draft.name, timeout=per_try)
                    pause_short()
                    result.steps_completed.append("campaign_name_via_title_click")
                    return
                except Exception:
                    pass

        self._shot("04_campaign_name_fail", result)
        raise AdsManagerFlowError(
            f"Could not fill campaign name {draft.name!r}. "
            f"Tried selectors: {selectors}. "
            "Check screenshot — wizard may not have opened or UI language differs."
        )

    def _fill_daily_budget(
        self,
        amount: float,
        result: RpaPublishResult,
        *,
        step_label: str,
    ) -> bool:
        value = str(int(amount))
        if fill_input_near_label(
            self.page,
            ["Daily budget", "Presupuesto diario"],
            value,
        ):
            pause_short()
            result.steps_completed.append(step_label)
            return True

        sels = self.cfg.get("selectors") or {}
        budget_sels = _selector_list(sels, "budget_input")
        try:
            _fill_first(self.page, budget_sels, value, timeout_ms=12000)
            result.steps_completed.append(step_label)
            return True
        except AdsManagerFlowError:
            return False

    def _advance_to_ad_set(self, result: RpaPublishResult) -> None:
        start_idx = len(result.steps_completed)
        if _is_ad_set_level_visible(self.page):
            result.steps_completed.append("already_on_ad_set_level")
            self._log_nav_ad_set_success(result, result.steps_completed[start_idx:])
            return

        if self._navigate_to_ad_set_level(result):
            self._log_nav_ad_set_success(result, result.steps_completed[start_idx:])
            return

        result.steps_completed.append("ad_set_level_wait_timeout")
        self._shot("04b_ad_set_nav_fail", result)

    def _advance_to_ad_level(self, result: RpaPublishResult) -> bool:
        import time as _time

        sels = self.cfg.get("selectors") or {}
        if _is_ad_level_visible(self.page):
            result.steps_completed.append("ad_level_ready")
            return True

        if _click_ad_sidebar(self.page):
            result.steps_completed.append("click_ad_sidebar")
        else:
            try:
                _click_next_step(
                    self.page,
                    _selector_list(sels, "next_button"),
                    timeout_ms=self.action_timeout,
                )
                result.steps_completed.append("click_next_to_ad")
            except AdsManagerFlowError:
                result.steps_completed.append("click_next_to_ad_failed")
                self._shot("04e_ad_level_nav_fail", result)
                return False

        deadline = _time.time() + self.action_timeout / 1000
        while _time.time() < deadline:
            if _is_ad_level_visible(self.page):
                result.steps_completed.append("ad_level_ready")
                return True
            _time.sleep(0.4)

        result.steps_completed.append("ad_level_wait_timeout")
        self._shot("04e_ad_level_wait_fail", result)
        return False

    def _fill_ad_set_name(self, ad_set: AdSetDraft, result: RpaPublishResult) -> None:
        sels = self.cfg.get("selectors") or {}
        name = ad_set.name

        for root in _ads_roots(self.page):
            for label_re in (
                re.compile(r"^Ad set name$", re.I),
                re.compile(r"nombre del conjunto", re.I),
            ):
                try:
                    loc = root.get_by_label(label_re).first
                    loc.wait_for(state="visible", timeout=8000)
                    loc.scroll_into_view_if_needed(timeout=8000)
                    loc.click(timeout=5000)
                    loc.fill(name, timeout=8000)
                    pause_short()
                    try:
                        current = loc.input_value(timeout=2000)
                        if name in current or current.strip() == name.strip():
                            result.steps_completed.append("adset_name")
                            return
                    except Exception:
                        result.steps_completed.append("adset_name")
                        return
                except Exception:
                    pass

        if fill_input_near_label_verified(
            self.page,
            ["Ad set name", "Nombre del conjunto"],
            name,
        ):
            result.steps_completed.append("adset_name_dom")
            return

        try:
            _fill_first(
                self.page,
                _selector_list(sels, "adset_name_input"),
                name,
                timeout_ms=self.action_timeout,
            )
            result.steps_completed.append("adset_name_selector")
        except AdsManagerFlowError:
            result.steps_completed.append("adset_name_skipped")
            self._shot("04c_adset_name_fail", result)

    def _fill_audience_location(
        self, draft: CampaignDraft, result: RpaPublishResult
    ) -> None:
        raw = (draft.country or "").strip()
        # Normalize full name → ISO-2 ("Costa Rica" → "CR", "Mexico" → "MX").
        # Falls back to 2-char uppercase if no mapping found.
        iso = _COUNTRY_NAME_TO_ISO.get(raw.lower()) or (raw.upper() if len(raw) == 2 else None)
        country = iso or raw.upper()[:2]
        if not country or country == "ALL" or not raw:
            result.steps_completed.append("audience_location_all_skipped")
            return
        if fill_audience_location(self.page, country):
            result.steps_completed.append(f"audience_location:{country}")
            self._shot("05b_location", result)
        else:
            result.steps_completed.append(f"audience_location_failed:{country}")
            self._shot("05b_location_fail", result)

    def _fill_audience_age(
        self, ad_set: AdSetDraft, result: RpaPublishResult
    ) -> None:
        age_min = ad_set.age_min or 18
        age_max = ad_set.age_max or 65
        if age_min == 18 and age_max == 65:
            result.steps_completed.append("audience_age_default_skipped")
            return
        if fill_audience_age(self.page, age_min, age_max):
            result.steps_completed.append(f"audience_age:{age_min}-{age_max}")
        else:
            result.steps_completed.append(f"audience_age_failed:{age_min}-{age_max}")
            self._shot("05c_age_fail", result)

    def _fill_placements_control(
        self, ad_set: AdSetDraft, result: RpaPublishResult
    ) -> None:
        os_val = (ad_set.os or "All").strip()
        if os_val.lower() in ("all", ""):
            result.steps_completed.append("placements_all_skipped")
            return
        # Switch to manual placements first, then select device type
        if select_manual_placements(self.page):
            result.steps_completed.append("placements_manual")
            pause_medium()
            if select_placement_device(self.page, os_val):
                result.steps_completed.append(f"placements_device:{os_val}")
                self._shot("06_placements", result)
            else:
                result.steps_completed.append(f"placements_device_failed:{os_val}")
                self._shot("06_placements_fail", result)
        else:
            result.steps_completed.append("placements_manual_failed")
            self._shot("06_placements_fail", result)

    def _select_conversion_location_website(self, result: RpaPublishResult) -> None:
        start_idx = len(result.steps_completed)
        if not self._ensure_ad_set_level(result):
            result.steps_completed.append("conversion_location_wrong_level")
            self._log_conversion_location_website_outcome(
                result,
                result.steps_completed[start_idx:],
                ok=False,
            )
            return
        close_open_dropdown(self.page)
        wait_editor_saved(self.page)
        scroll_to_section(self.page, "Conversion location", "Ubicación de la conversión")
        self._mark_variation_next_step_ok("nav_ad_set")
        if is_website_conversion_selected(self.page):
            result.steps_completed.append("conversion_location_website_already")
            self._log_conversion_location_website_outcome(
                result,
                result.steps_completed[start_idx:],
                ok=True,
            )
            return

        menu_shot: str | None = None
        menu_open = False
        if click_conversion_location_dropdown(self.page):
            wait_conversion_menu_open(self.page)
            time.sleep(0.5)
            menu_open = conversion_menu_is_open(self.page)
            if menu_open and conversion_location_menu_visible(self.page):
                result.steps_completed.append("conversion_location_menu_opened")
                menu_shot = self._shot("04d_conversion_menu_open", result)
                if expand_conversion_location_single(self.page):
                    result.steps_completed.append("conversion_location_single_clicked")
                    pause_short()
                    self._shot("04d_conversion_single_expanded", result)
                    result.steps_completed.append("conversion_location_recipe_path")  # recorded: Single → Website
            elif menu_open:
                result.steps_completed.append("conversion_location_wrong_menu")
                close_open_dropdown(self.page)

        if select_conversion_location_website(self.page, skip_open=menu_open):
            pause_medium()
            if is_website_conversion_selected(self.page):
                result.steps_completed.append("conversion_location_website")
                shot = self._shot("04d_conversion_website", result)
                self._log_conversion_location_website_outcome(
                    result,
                    result.steps_completed[start_idx:],
                    ok=True,
                    screenshot=shot,
                )
                return
            result.steps_completed.append("conversion_location_website_unverified")
            if not menu_shot:
                menu_shot = self._shot("04d_conversion_menu_open", result)

        sels = self.cfg.get("selectors") or {}
        try:
            _click_first(
                self.page,
                [
                    "text=Website and instant forms",
                    "text=Website and Instant Forms",
                    *_selector_list(sels, "conversion_location_dropdown"),
                ],
                timeout_ms=8000,
            )
            pause_short()
            _click_first(
                self.page,
                _selector_list(sels, "conversion_location_website"),
                timeout_ms=8000,
            )
            pause_medium()
            if is_website_conversion_selected(self.page):
                result.steps_completed.append("conversion_location_website_selector")
                shot = self._shot("04d_conversion_website", result)
                self._log_conversion_location_website_outcome(
                    result,
                    result.steps_completed[start_idx:],
                    ok=True,
                    screenshot=shot,
                )
                return
        except AdsManagerFlowError:
            pass

        result.steps_completed.append("conversion_location_website_skipped")
        fail_shot = self._shot("04d_conversion_location_fail", result)
        if menu_shot:
            result.steps_completed.append(f"screenshot_menu_open:{menu_shot}")
        result.steps_completed.append(f"screenshot_conversion_fail:{fail_shot}")

        # Self-healing: run diagnostics and attempt recovery
        diag = diagnose_and_recover(self.page, failed_step="conversion_location")
        result.steps_completed.append(f"diag:{diag.summary()}")
        if diag.conversion_location == "website":
            result.steps_completed.append("conversion_location_website_recovered")
            shot = self._shot("04d_conversion_website_recovered", result)
            self._log_conversion_location_website_outcome(
                result,
                result.steps_completed[start_idx:],
                ok=True,
                screenshot=shot,
            )
            return

        # Teach-mode: pause so the user can click Website manually.
        # The RPA waits up to 10 minutes, then re-checks and captures the locator.
        result.steps_completed.append("teach_mode_website_waiting")
        begin_teach_wait("website_click")
        resumed = wait_for_teach_resume(max_wait_s=600.0)
        if resumed:
            # After user click: capture what changed and verify
            time.sleep(0.8)
            if is_website_conversion_selected(self.page):
                result.steps_completed.append("conversion_location_website_teach")
                shot = self._shot("04d_conversion_website_teach", result)
                _capture_website_locator_to_recipe(self.page)
                result.steps_completed.append("teach_website_captured")
                self._log_conversion_location_website_outcome(
                    result,
                    result.steps_completed[start_idx:],
                    ok=True,
                    screenshot=shot,
                )
                return
            else:
                result.steps_completed.append("teach_mode_website_still_failed")

        if not self._conversion_location_ok(result):
            self._log_conversion_location_website_outcome(
                result,
                result.steps_completed[start_idx:],
                ok=False,
                screenshot=fail_shot,
            )

    def _conversion_location_ok(self, result: RpaPublishResult) -> bool:
        return any(
            s in result.steps_completed
            for s in (
                "conversion_location_website",
                "conversion_location_website_already",
                "conversion_location_website_selector",
                "conversion_location_website_recovered",
            )
        )

    def _conversion_event_ok(self, result: RpaPublishResult) -> bool:
        return any(
            s in result.steps_completed
            for s in (
                "conversion_event_lead",
                "conversion_event_lead_already",
                "conversion_event_lead_selector",
                "conversion_event_lead_recovered",
            )
        )

    def _select_conversion_event_lead(self, result: RpaPublishResult) -> None:
        if not self._ensure_ad_set_level(result):
            result.steps_completed.append("conversion_event_wrong_level")
            return
        level = wizard_editor_level(self.page)
        if level and level != "ad_set":
            result.steps_completed.append(f"conversion_event_level_{level}")
            if not self._ensure_ad_set_level(result):
                result.steps_completed.append("conversion_event_wrong_level")
                return
        scroll_to_section(self.page, "Conversion event", "Evento de conversión")
        if is_lead_conversion_event_selected(self.page):
            result.steps_completed.append("conversion_event_lead_already")
            return

        if select_conversion_event_lead(self.page):
            pause_medium()
            if is_lead_conversion_event_selected(self.page):
                result.steps_completed.append("conversion_event_lead")
                self._shot("04e_conversion_event_lead", result)
                close_open_dropdown(self.page)
                wait_editor_saved(self.page)
                return
            result.steps_completed.append("conversion_event_lead_unverified")
            self._shot("04e_conversion_event_menu_open", result)

        sels = self.cfg.get("selectors") or {}
        try:
            _click_first(
                self.page,
                _selector_list(sels, "conversion_event_dropdown"),
                timeout_ms=8000,
            )
            pause_short()
            _click_first(
                self.page,
                _selector_list(sels, "conversion_event_lead"),
                timeout_ms=8000,
            )
            pause_medium()
            wait_editor_saved(self.page)
            if is_lead_conversion_event_selected(self.page):
                result.steps_completed.append("conversion_event_lead_selector")
                self._shot("04e_conversion_event_lead", result)
                close_open_dropdown(self.page)
                return
        except AdsManagerFlowError:
            pass

        result.steps_completed.append("conversion_event_lead_skipped")
        self._shot("04e_conversion_event_fail", result)

        # Self-healing: run diagnostics and attempt recovery
        diag = diagnose_and_recover(self.page, failed_step="conversion_event")
        result.steps_completed.append(f"diag:{diag.summary()}")
        if diag.conversion_event == "lead":
            result.steps_completed.append("conversion_event_lead_recovered")
            self._shot("04e_conversion_event_recovered", result)

    def _fill_targeting_group(
        self,
        terms: list[str],
        input_sels: list[str],
        suggest_sels: list[str],
        group_label: str,
        result: RpaPublishResult,
        *,
        raw_group: str = "",
    ) -> None:
        """Paste the full comma-separated targeting string at once into the targeting input.

        Facebook Ads Manager accepts a comma-separated list pasted into the Detailed
        Targeting field — it surfaces all matches simultaneously.  Pasting all terms at
        once is both faster and more reliable than typing them one-by-one.

        Falls back to the term-by-term approach if the paste path fails.
        """
        # Build the full group string (use raw_group if provided, else rejoin terms)
        full_string = raw_group.strip() if raw_group.strip() else ", ".join(terms)

        if not full_string:
            return

        # ── DOM interest search (Ad Set level) ────────────────────────────────
        open_detailed_targeting_field(self.page)
        if fill_targeting_interest(self.page, full_string):
            pause_medium()
            if pick_first_suggestion(self.page):
                pause_short()
            else:
                self.page.keyboard.press("Enter")
            pause_short()
            result.steps_completed.append(f"{group_label}_dom_interest:{len(terms)}_terms")
            return

        # ── Per-term DOM interest search ──────────────────────────────────────
        open_detailed_targeting_field(self.page)
        if len(terms) == 1 and fill_targeting_interest(self.page, terms[0]):
            pause_medium()
            if pick_first_suggestion(self.page):
                pause_short()
            else:
                self.page.keyboard.press("Enter")
            pause_short()
            result.steps_completed.append(f"{group_label}_dom_interest:{terms[0]}")
            return

        # ── Try selector-based fill (visible inputs only) ─────────────────────
        try:
            # Click the targeting input to focus it
            loc = None
            for sel in input_sels:
                candidate = _try_locator(self.page, sel)
                if candidate is None:
                    continue
                try:
                    candidate.wait_for(state="visible", timeout=2000)
                    loc = candidate
                    break
                except Exception:
                    continue
            if loc is None:
                raise AdsManagerFlowError("Targeting input not found")

            loc.click(timeout=self.action_timeout)
            pause_short()

            loc.fill(full_string, timeout=self.action_timeout)
            pause_medium()

            # Confirm first suggestion if dropdown appears
            try:
                if suggest_sels:
                    _click_first(self.page, suggest_sels, timeout_ms=8000)
                else:
                    self.page.keyboard.press("Enter")
            except AdsManagerFlowError:
                self.page.keyboard.press("Enter")
            pause_medium()

            result.steps_completed.append(f"{group_label}_paste_all:{len(terms)}_terms")
            return

        except Exception as exc:
            result.steps_completed.append(f"{group_label}_paste_failed_fallback:{exc}")
            self._shot(f"{group_label}_paste_fail", result)

        # ── Fallback: enter terms one by one ──────────────────────────────────
        for i, term in enumerate(terms):
            step = f"{group_label}_t{i + 1}"
            try:
                open_detailed_targeting_field(self.page)
                if fill_targeting_interest(self.page, term):
                    pause_medium()
                    if pick_first_suggestion(self.page):
                        pause_short()
                        result.steps_completed.append(f"{step}_dom:{term}")
                        continue
                _fill_first(self.page, input_sels, term, timeout_ms=self.action_timeout)
                pause_medium()

                if suggest_sels:
                    _click_first(self.page, suggest_sels, timeout_ms=self.action_timeout)
                    pause_short()
                    result.steps_completed.append(f"{step}:{term}")
                else:
                    self.page.keyboard.press("Enter")
                    pause_short()
                    result.steps_completed.append(f"{step}_enter:{term}")
            except AdsManagerFlowError:
                result.steps_completed.append(f"{step}_failed:{term}")
                self._shot(f"{step}_fail", result)

    def _fill_detailed_targeting(
        self,
        ad_set: AdSetDraft,
        result: RpaPublishResult,
    ) -> None:
        """Fill Detailed Targeting; supports [AND] (Narrow further / Define Further) groups.

        Format examples:
          "Amazon, Online Shopping"              → one group, two interests
          "Amazon, Online Shopping [AND] Finance" → two groups (AND logic / Narrow further)
        The [AND] marker is written by sync_targets_from_gsheet.py from sheet rows
        that contain a standalone "V" (Meta's visual AND separator).
        """
        raw = (ad_set.targeting_label or "").strip()
        if not raw:
            result.steps_completed.append("targeting_empty_skipped")
            return

        sels = self.cfg.get("selectors") or {}
        input_sels   = _selector_list(sels, "detailed_targeting_input")
        suggest_sels = _selector_list(sels, "targeting_suggestion_item")
        narrow_sels  = _selector_list(sels, "narrow_further_button")

        if not input_sels:
            result.steps_completed.append("targeting_no_selector_skipped")
            return

        try:
            _click_first(
                self.page,
                _selector_list(sels, "audience_manual_options"),
                timeout_ms=8000,
            )
            pause_medium()
            result.steps_completed.append("audience_manual_opened")
        except AdsManagerFlowError:
            result.steps_completed.append("audience_manual_skipped")

        if not open_detailed_targeting_field(self.page):
            scroll_to_section(
                self.page,
                "Audience",
                "Audiencia",
                "Detailed targeting",
                "Segmentación detallada",
            )
            result.steps_completed.append("targeting_scroll_only")
        else:
            result.steps_completed.append("targeting_field_opened")

        # Normalize: `\` → `|` then split by all AND-group separators: [AND], |, \
        raw = raw.replace("\\", "|")
        and_groups_raw = [
            g.strip()
            for g in re.split(r"\[AND\]|\|", raw, flags=re.IGNORECASE)
            if g.strip()
        ]
        # Filter groups to only those with at least one non-empty term
        and_groups = [g for g in and_groups_raw if any(t.strip() for t in g.split(","))]
        if not and_groups:
            result.steps_completed.append("targeting_empty_skipped")
            return

        for g_idx, group_raw in enumerate(and_groups):
            terms = [t.strip() for t in group_raw.split(",") if t.strip()]
            if not terms:
                continue

            group_label = f"targeting_g{g_idx + 1}"

            # If this is a second (or later) group, click "Narrow further" first
            if g_idx > 0:
                if narrow_sels:
                    try:
                        _click_first(self.page, narrow_sels, timeout_ms=self.action_timeout)
                        pause_medium()
                        result.steps_completed.append(f"{group_label}_narrow_further")
                    except AdsManagerFlowError:
                        result.steps_completed.append(f"{group_label}_narrow_further_failed")
                        self._shot(f"{group_label}_narrow_fail", result)
                        # Still try to add interests into whatever input is active
                else:
                    result.steps_completed.append(f"{group_label}_no_narrow_selector")

            self._fill_targeting_group(
                terms, input_sels, suggest_sels, group_label, result,
                raw_group=group_raw,
            )

        self._shot("05_targeting", result)

    def _fill_campaign_and_adset(
        self,
        draft: CampaignDraft,
        ad_set: AdSetDraft,
        result: RpaPublishResult,
    ) -> None:
        sels = self.cfg.get("selectors") or {}
        self._fill_campaign_name(draft, result)

        if ad_set.daily_budget is not None:
            if not self._fill_daily_budget(
                ad_set.daily_budget,
                result,
                step_label="campaign_daily_budget",
            ):
                result.steps_completed.append("campaign_daily_budget_skipped")

        self._shot("04_campaign_level", result)
        self._advance_to_ad_set(result)

        if not self._ensure_ad_set_level(result):
            result.steps_completed.append("conversion_skipped_wrong_level")
            self._shot("04_names_budget", result)
            return

        # Conversion before rename — Meta auto-jumps to Ad level after ad set name save.
        # Location first, then Event (user-specified order).
        self._select_conversion_location_website(result)
        close_open_dropdown(self.page)
        pause_short()

        self._select_conversion_event_lead(result)
        close_open_dropdown(self.page)
        wait_editor_saved(self.page)
        pause_short()

        self._fill_ad_set_name(ad_set, result)
        wait_editor_saved(self.page)
        pause_medium()

        if not self._ensure_ad_set_level(result, force=True):
            result.steps_completed.append("conversion_skipped_after_rename")
            self._shot("04_names_budget", result)
            return

        if not self._conversion_location_ok(result):
            self._select_conversion_location_website(result)
            close_open_dropdown(self.page)
            pause_short()
        if not self._conversion_event_ok(result):
            self._select_conversion_event_lead(result)
            close_open_dropdown(self.page)
            wait_editor_saved(self.page)

        self._shot("04_names_budget", result)

        # ── Audience ──────────────────────────────────────────────────────────
        self._fill_audience_location(draft, result)
        self._fill_audience_age(ad_set, result)
        self._shot("05a_audience", result)

        # ── Detailed targeting (paste all) ────────────────────────────────────
        if self._conversion_location_ok(result) and self._conversion_event_ok(result):
            self._fill_detailed_targeting(ad_set, result)
        else:
            result.steps_completed.append("targeting_skipped_conversion_incomplete")
            self._shot("05_targeting_skipped", result)

        # ── Placements / Account controls ─────────────────────────────────────
        self._fill_placements_control(ad_set, result)

        if ad_set.daily_budget is not None:
            if not self._fill_daily_budget(
                ad_set.daily_budget,
                result,
                step_label="adset_daily_budget",
            ):
                result.steps_completed.append("adset_daily_budget_skipped")

    def _create_ads(
        self,
        ad_set: AdSetDraft,
        result: RpaPublishResult,
        *,
        dry_run: bool,
        skip_creative_upload: bool = False,
    ) -> None:
        sels = self.cfg.get("selectors") or {}
        for idx, ad in enumerate(ad_set.ads):
            if idx > 0:
                try:
                    _click_first(
                        self.page,
                        _selector_list(sels, "add_ad_button"),
                        timeout_ms=self.action_timeout,
                    )
                    pause_medium()
                    result.steps_completed.append(f"add_ad_{idx + 1}")
                except AdsManagerFlowError as exc:
                    result.steps_completed.append(f"add_ad_{idx + 1}_failed:{exc}")
                    break

            if dry_run:
                result.steps_completed.append(f"ad_{idx + 1}_skipped_dry_run")
                continue

            if skip_creative_upload:
                result.steps_completed.append(f"ad_{idx + 1}_creative_skipped_manual")
            else:
                _upload_creative(self.page, ad, sels, self.action_timeout)
                result.steps_completed.append(f"ad_{idx + 1}_creative")

            _fill_ad_copy(self.page, ad, sels, self.action_timeout)
            result.steps_completed.append(f"ad_{idx + 1}_copy")
            self._shot(f"06_ad_{idx + 1}", result)

    def _finish_publish(self, draft: CampaignDraft, result: RpaPublishResult, *, dry_run: bool) -> None:
        # DRAFT status → leave wizard open, don't click Publish
        if draft.status == "DRAFT" or dry_run:
            result.steps_completed.append("left_as_draft_in_ads_manager")
            shot = self._shot("99_draft", result)
            self._log_variation_outcome(
                "finish_draft",
                "hand_off_draft",
                ok=True,
                screenshot=shot,
            )
            return

        sels = self.cfg.get("selectors") or {}
        try:
            _click_first(
                self.page,
                _selector_list(sels, "publish_button"),
                timeout_ms=self.action_timeout,
            )
            pause_medium()
            result.steps_completed.append("clicked_publish")
            self._shot("06_publish", result)
        except AdsManagerFlowError as exc:
            shot = capture(self.page, "publish_failed")
            result.screenshot_paths.append(shot)
            raise AdsManagerFlowError(str(exc), screenshot=shot) from exc

    def publish_campaign(
        self,
        draft: CampaignDraft,
        *,
        dry_run: bool = False,
        skip_creative_upload: bool = False,
    ) -> RpaPublishResult:
        result = RpaPublishResult(campaign_name=draft.name)
        if draft.errors:
            result.error = "; ".join(draft.errors)
            return result

        ad_set = draft.ad_sets[0] if draft.ad_sets else None
        if not ad_set or not ad_set.ads:
            result.error = "No ad set or ads on draft"
            return result

        try:
            self._goto_campaigns(draft, result)
            self._start_create_wizard(result)
            result.steps_completed.append(f"rpa_flow_{RPA_FLOW_VERSION}")
            self._fill_campaign_and_adset(draft, ad_set, result)

            ad_set_ready = self._conversion_location_ok(result) and self._conversion_event_ok(result)
            if dry_run:
                result.steps_completed.append("ads_skipped_dry_run")
            elif not ad_set_ready:
                result.steps_completed.append("ads_skipped_before_ad_level")
            elif self._advance_to_ad_level(result):
                self._create_ads(
                    ad_set,
                    result,
                    dry_run=dry_run,
                    skip_creative_upload=skip_creative_upload,
                )
            else:
                result.steps_completed.append("ads_skipped_ad_level_unreachable")
            self._finish_publish(draft, result, dry_run=dry_run)
            # Dry run: success if campaign + ad set basics reached (targeting may fail)
            if dry_run:
                ok_steps = (
                    "campaign_name",
                    "adset_name",
                    "adset_name_dom",
                    "adset_name_selector",
                    "conversion_location_website",
                    "conversion_location_website_already",
                    "conversion_location_website_selector",
                    "conversion_event_lead",
                    "conversion_event_lead_already",
                    "conversion_event_lead_selector",
                )
                result.success = (
                    self._conversion_location_ok(result) and self._conversion_event_ok(result)
                ) or any(s in result.steps_completed for s in ok_steps) or (
                    "wizard_form_ready" in result.steps_completed
                )
            else:
                result.success = ad_set_ready
                if not ad_set_ready:
                    result.error = (
                        "Ad Set не завершён: нужны Conversion location = Website "
                        "и Conversion event = Lead. Перезапустите serve.py (v7) и повторите."
                    )
        except (AdsManagerFlowError, PlaywrightTimeout) as exc:
            shot = capture(self.page, f"error_{draft.name[:20]}")
            result.screenshot_paths.append(shot)
            result.error = str(exc)
            if isinstance(exc, AdsManagerFlowError) and exc.screenshot:
                result.screenshot_paths.append(exc.screenshot)
        finally:
            ranks = getattr(self, "_strategy_ranks", None)
            if ranks is not None:
                try:
                    ranks.save()
                except Exception:
                    pass

        return result
