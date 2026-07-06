"""Playwright RPA flow for Facebook Ads Manager campaign creation."""
from __future__ import annotations

import re
from typing import Any

from playwright.sync_api import Locator, Page, TimeoutError as PlaywrightTimeout

from src.fb_ads_rpa.draft import AdDraft, AdSetDraft, CampaignDraft, RpaPublishResult
from src.fb_ads_rpa.pacing import pause_after_navigation, pause_medium, pause_short
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


def _try_locator(page: Page, selector: str) -> Locator | None:
    if selector.startswith("role="):
        m = re.match(r"role=(\w+)\[name=([^\]]+)\]", selector)
        if m:
            role, name_raw = m.group(1), m.group(2)
            loc = page.get_by_role(role, name=_parse_role_name(name_raw))
            return loc.first if loc.count() else None
    if selector.startswith("text="):
        text = selector[5:]
        loc = page.get_by_text(re.compile(re.escape(text), re.I))
        return loc.first if loc.count() else None
    loc = page.locator(selector)
    return loc.first if loc.count() else None


def _click_first(page: Page, selectors: list[str], *, timeout_ms: int) -> None:
    last_err: Exception | None = None
    for sel in selectors:
        try:
            loc = _try_locator(page, sel)
            if loc is None:
                continue
            loc.click(timeout=timeout_ms)
            pause_short()
            return
        except Exception as exc:
            last_err = exc
    raise AdsManagerFlowError(f"Could not click any of: {selectors}") from last_err


def _fill_first(page: Page, selectors: list[str], value: str, *, timeout_ms: int) -> None:
    for sel in selectors:
        try:
            loc = _try_locator(page, sel)
            if loc is None:
                continue
            loc.click(timeout=timeout_ms)
            loc.fill(value, timeout=timeout_ms)
            pause_short()
            return
        except Exception:
            continue
    raise AdsManagerFlowError(f"Could not fill {value!r} using: {selectors}")


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

    def _shot(self, label: str, result: RpaPublishResult) -> None:
        path = capture(self.page, label)
        result.screenshot_paths.append(path)

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
        _click_first(self.page, _selector_list(sels, "create_button"), timeout_ms=self.action_timeout)
        pause_medium()
        result.steps_completed.append("click_create")
        self._shot("02_create_clicked", result)

        try:
            _click_first(
                self.page,
                _selector_list(sels, "campaign_objective_leads"),
                timeout_ms=self.action_timeout,
            )
            result.steps_completed.append("select_leads_objective")
        except AdsManagerFlowError:
            result.steps_completed.append("objective_skipped_or_preselected")

        try:
            _click_first(
                self.page,
                _selector_list(sels, "continue_button"),
                timeout_ms=self.action_timeout,
            )
            result.steps_completed.append("continue_after_objective")
        except AdsManagerFlowError:
            pass

        self._shot("03_wizard_started", result)

    def _fill_detailed_targeting(
        self,
        ad_set: AdSetDraft,
        result: RpaPublishResult,
    ) -> None:
        """Type each comma-separated targeting term into Detailed Targeting and select the first suggestion."""
        raw = (ad_set.targeting_label or "").strip()
        terms = [t.strip() for t in raw.split(",") if t.strip()]
        if not terms:
            result.steps_completed.append("targeting_empty_skipped")
            return

        sels = self.cfg.get("selectors") or {}
        input_sels = _selector_list(sels, "detailed_targeting_input")
        suggest_sels = _selector_list(sels, "targeting_suggestion_item")

        if not input_sels:
            result.steps_completed.append("targeting_no_selector_skipped")
            return

        for i, term in enumerate(terms):
            label = f"targeting_{i + 1}"
            try:
                # Click the input, type the term
                _fill_first(self.page, input_sels, term, timeout_ms=self.action_timeout)
                pause_medium()

                # Wait for suggestions and click the first one
                if suggest_sels:
                    _click_first(self.page, suggest_sels, timeout_ms=self.action_timeout)
                    pause_short()
                    result.steps_completed.append(f"{label}:{term}")
                else:
                    # No suggestion selector — accept with Enter as fallback
                    self.page.keyboard.press("Enter")
                    pause_short()
                    result.steps_completed.append(f"{label}_enter:{term}")

            except AdsManagerFlowError as exc:
                result.steps_completed.append(f"{label}_failed:{term}")
                self._shot(f"targeting_{i + 1}_fail", result)
                # Continue with remaining terms rather than hard-failing
                _ = exc

        self._shot("05_targeting", result)

    def _fill_campaign_and_adset(
        self,
        draft: CampaignDraft,
        ad_set: AdSetDraft,
        result: RpaPublishResult,
    ) -> None:
        sels = self.cfg.get("selectors") or {}
        # Campaign name — critical; raise if selector not found (hard fail, not silent)
        _fill_first(
            self.page,
            _selector_list(sels, "campaign_name_input"),
            draft.name,
            timeout_ms=self.action_timeout,
        )
        result.steps_completed.append("campaign_name")

        try:
            _fill_first(
                self.page,
                _selector_list(sels, "adset_name_input"),
                ad_set.name,
                timeout_ms=self.action_timeout,
            )
            result.steps_completed.append("adset_name")
        except AdsManagerFlowError:
            result.steps_completed.append("adset_name_skipped")

        if ad_set.daily_budget is not None:
            try:
                _fill_first(
                    self.page,
                    _selector_list(sels, "budget_input"),
                    str(int(ad_set.daily_budget)),
                    timeout_ms=self.action_timeout,
                )
                result.steps_completed.append("daily_budget")
            except AdsManagerFlowError:
                result.steps_completed.append("daily_budget_skipped")
        else:
            result.steps_completed.append("daily_budget_not_set")

        self._shot("04_names_budget", result)

        # Detailed targeting — parse comma-separated terms
        self._fill_detailed_targeting(ad_set, result)

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
            self._shot("99_draft", result)
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
            self._fill_campaign_and_adset(draft, ad_set, result)
            self._create_ads(
                ad_set,
                result,
                dry_run=dry_run,
                skip_creative_upload=skip_creative_upload,
            )
            self._finish_publish(draft, result, dry_run=dry_run)
            result.success = True
        except (AdsManagerFlowError, PlaywrightTimeout) as exc:
            shot = capture(self.page, f"error_{draft.name[:20]}")
            result.screenshot_paths.append(shot)
            result.error = str(exc)
            if isinstance(exc, AdsManagerFlowError) and exc.screenshot:
                result.screenshot_paths.append(exc.screenshot)

        return result
