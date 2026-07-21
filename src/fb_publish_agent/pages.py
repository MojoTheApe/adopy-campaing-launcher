"""Page Object Model for the Ads Manager "create campaign" wizard.

One method per meaningful action (docs/agent10_fb_ads_playwright_upload.md §1.1) —
the run loop (runner.py) calls these in sequence with values taken from a plan row.
All the actual button/field lookups go through locator_utils.resolve() against
selectors.json, so when Facebook changes the UI you fix selectors.json, not this file.

NOTE: exact field/button labels here are a best-effort starting point based on the
typical Ads Manager layout — they are NOT verified against a live account. Calibrate
with `playwright codegen https://adsmanager.facebook.com` before the first live run
(see docs/agent10_fb_ads_playwright_upload.md §2).
"""
from __future__ import annotations

import re

from . import locator_utils as loc


class AdsManagerCreateFlow:
    def __init__(self, page, selectors_map: dict[str, list[dict]]):
        self.page = page
        self.selectors = selectors_map

    # ── navigation ────────────────────────────────────────────────────────

    def open(self, ads_manager_url: str) -> None:
        self.page.goto(ads_manager_url, wait_until="domcontentloaded")
        loc.resolve(self.page, "logged_in_marker", self.selectors, timeout=15000)

    def start_create(self) -> None:
        loc.click_step(self.page, "open_create_button", self.selectors)

    def next_step(self) -> None:
        loc.click_step(self.page, "next_button", self.selectors)

    # ── campaign level ───────────────────────────────────────────────────

    def set_campaign_name(self, name: str) -> None:
        if name:
            loc.fill_step(self.page, "campaign_name_field", name, self.selectors)

    def select_objective(self, objective_label: str) -> None:
        if objective_label:
            loc.click_step(self.page, "objective_option", self.selectors, value=objective_label)

    # ── ad set level ─────────────────────────────────────────────────────

    def set_daily_budget(self, amount: str) -> None:
        if amount:
            loc.fill_step(self.page, "daily_budget_field", str(amount), self.selectors)

    def set_ad_set_name(self, name: str) -> None:
        if name:
            loc.fill_step(self.page, "ad_set_name_field", name, self.selectors)

    def set_age_range(self, age_min: str, age_max: str) -> None:
        if age_min:
            self._select_or_fill("age_min_field", str(age_min))
        if age_max:
            self._select_or_fill("age_max_field", str(age_max))

    def set_countries(self, countries: str) -> None:
        """`countries` is a comma/semicolon-separated list, e.g. from the plan's Countries column."""
        if not countries:
            return
        for country in re.split(r"[;,]", countries):
            country = country.strip()
            if not country:
                continue
            loc.fill_step(self.page, "location_search_field", country, self.selectors)
            loc.click_step(self.page, "location_option", self.selectors, value=country)

    def set_targeting_interest(self, interest: str) -> None:
        if not interest:
            return
        loc.fill_step(self.page, "interest_search_field", interest, self.selectors)
        loc.click_step(self.page, "interest_option", self.selectors, value=interest)

    def set_creative_from_library(self, creative_filename: str) -> None:
        """The plan only carries a filename reference (Video/Image file name from Agent 8's
        lookup sheets) — not a local file path, so this picks existing media from the ad
        account's library by name rather than uploading a fresh file."""
        if not creative_filename:
            return
        loc.click_step(self.page, "creative_library_open_button", self.selectors)
        loc.fill_step(self.page, "creative_library_search_field", creative_filename, self.selectors)
        loc.click_step(self.page, "creative_library_item", self.selectors, value=creative_filename)

    # ── ad level ─────────────────────────────────────────────────────────

    def set_ad_text(self, primary_text: str = "", headline: str = "", link_description: str = "", website_url: str = "") -> None:
        if primary_text:
            loc.fill_step(self.page, "primary_text_field", primary_text, self.selectors)
        if headline:
            loc.fill_step(self.page, "headline_field", headline, self.selectors)
        if link_description:
            loc.fill_step(self.page, "link_description_field", link_description, self.selectors)
        if website_url:
            loc.fill_step(self.page, "website_url_field", website_url, self.selectors)

    # ── publish ──────────────────────────────────────────────────────────

    def publish(self) -> str | None:
        """Clicks Publish, waits for confirmation, and best-effort extracts the FB campaign id
        from a 'View campaign' link href or the resulting URL. Returns None if not found —
        the caller should still record success and fill in the id later from Ads Manager."""
        loc.click_step(self.page, "publish_button", self.selectors)
        marker = loc.resolve(self.page, "publish_confirmation_marker", self.selectors, timeout=20000)
        href = marker.get_attribute("href") if marker else None
        for source in (href, self.page.url):
            if not source:
                continue
            m = re.search(r"campaign_ids?=(\d+)", source) or re.search(r"/campaigns/(\d+)", source)
            if m:
                return m.group(1)
        return None

    # ── helpers ──────────────────────────────────────────────────────────

    def _select_or_fill(self, step_name: str, value: str) -> None:
        """Age fields are usually <select> dropdowns in Ads Manager; fall back to fill for text inputs."""
        locator = loc.resolve(self.page, step_name, self.selectors)
        try:
            locator.select_option(label=value)
        except Exception:
            locator.fill(value)
