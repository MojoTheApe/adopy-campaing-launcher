"""Orchestrate RPA publish: browser session + Ads Manager flow."""
from __future__ import annotations

import logging
from typing import Any

from config.paths import get_ad_account_map_path
from src.fb_ads_rpa.ads_manager_flow import AdsManagerFlow
from src.fb_ads_rpa.browser import (
    BrowserConnection,
    PlaywrightProfileProvider,
    get_browser_provider,
)
from src.fb_ads_rpa.build_draft import slots_to_campaign_drafts
from src.fb_ads_rpa.config import get_browser_provider as get_browser_provider_name
from src.fb_ads_rpa.draft import CampaignDraft, RpaPublishResult
from src.fb_ads_rpa.enrich_draft import enrich_campaign_drafts
from src.meta_campaign_agent.ad_account_map import load_ad_account_map, resolve_account_entry

log = logging.getLogger(__name__)


class RpaPublisher:
    """Publish dashboard slots via Playwright + antidetect browser."""

    def __init__(
        self,
        *,
        browser_provider: str | None = None,
        dry_run: bool = False,
        hand_off: bool = False,
        selectors_path: str | None = None,
    ):
        self.browser_provider_name = browser_provider or get_browser_provider_name()
        self.dry_run = dry_run
        self.hand_off = hand_off  # fill form, disconnect, leave browser open for user
        self.selectors_path = selectors_path
        self._provider = get_browser_provider(self.browser_provider_name)
        self._conn: BrowserConnection | None = None
        self._entry = None
        self._cancelled = False

    def cancel(self) -> None:
        """Stop RPA and close the browser immediately."""
        self._cancelled = True
        self.close(force=True)

    def build_drafts(
        self,
        slots: list[dict[str, Any]],
        *,
        account_label: str,
        country: str,
        activate: bool = False,
        preview_mode: bool = False,
    ) -> list[CampaignDraft]:
        drafts = slots_to_campaign_drafts(
            slots,
            account_label=account_label,
            country=country,
            activate=activate,
        )
        return enrich_campaign_drafts(
            drafts,
            account_label=account_label,
            preview_mode=preview_mode,
        )

    def _resolve_entry(self, account_label: str):
        account_map = load_ad_account_map(get_ad_account_map_path())
        return resolve_account_entry(account_map, account_label)

    def _reset_browser(self) -> None:
        if self._conn:
            try:
                self._conn.close(keep_browser_open=False)
            except Exception:
                pass
            self._conn = None
        if self._entry is not None:
            try:
                self._provider.close(self._entry, keep_browser_open=False)
            except Exception:
                pass

    @staticmethod
    def _browser_closed_error(exc: BaseException) -> bool:
        err = str(exc).lower()
        return (
            "target page" in err
            or "browser has been closed" in err
            or "browser closed" in err
            or "context or browser has been closed" in err
        )

    def _open_multilogin_direct(self, entry, account_label: str) -> BrowserConnection:
        from src.fb_ads_rpa.browser import MultiloginDirectCDPProvider

        log.warning("Falling back to MultiloginDirectCDP (Mimic + CDP, no MLX API token).")
        self._provider = MultiloginDirectCDPProvider()
        return self._provider.open(entry, account_label)

    def _open_playwright_profile(self, entry, account_label: str) -> BrowserConnection:
        log.warning(
            "Using PlaywrightProfileProvider — vanilla Chromium + MLX profile cookies "
            "(NOT Mimic/antidetect). Refresh MLX automation token to use Multilogin API."
        )
        self._provider = PlaywrightProfileProvider()
        return self._provider.open(entry, account_label)

    def _ensure_browser(self, account_label: str) -> BrowserConnection:
        if self._conn is not None:
            return self._conn
        entry = self._resolve_entry(account_label)
        if not entry:
            raise RuntimeError(f"Account {account_label!r} not in ad_account_map.csv")
        self._entry = entry
        try:
            self._conn = self._provider.open(entry, account_label)
            log.info("Browser opened via %s", self._provider.__class__.__name__)
        except Exception as exc:
            err_str = str(exc)
            self._reset_browser()
            if "401" in err_str or "Authorization" in err_str or "authorization" in err_str:
                log.warning("MLX API returned 401 (token expired). Trying DirectCDP.")
                try:
                    self._conn = self._open_multilogin_direct(entry, account_label)
                except Exception as direct_exc:
                    log.warning("DirectCDP failed (%s). Falling back to PlaywrightProfile.", direct_exc)
                    self._reset_browser()
                    self._conn = self._open_playwright_profile(entry, account_label)
            elif (
                "license check failed" in err_str.lower()
                or "mimic browser exited" in err_str.lower()
                or "econnrefused" in err_str.lower()
                or self._browser_closed_error(exc)
            ):
                log.warning("Multilogin launch failed (%s). Trying DirectCDP.", exc)
                try:
                    self._conn = self._open_multilogin_direct(entry, account_label)
                except Exception as direct_exc:
                    log.warning("DirectCDP failed (%s). Falling back to PlaywrightProfile.", direct_exc)
                    self._reset_browser()
                    self._conn = self._open_playwright_profile(entry, account_label)
            else:
                raise
        return self._conn

    def close(self, *, force: bool = False) -> None:
        # Dry run and hand-off: leave the browser open so the user can inspect the form.
        # force=True on cancel always closes the browser.
        keep_open = (self.hand_off or self.dry_run) and not force and not self._cancelled
        if self._conn:
            try:
                self._conn.close(keep_browser_open=keep_open)
            except Exception:
                pass
            self._conn = None
        if self._entry is not None:
            try:
                self._provider.close(self._entry, keep_browser_open=keep_open)
            except Exception:
                pass
            self._entry = None

    def publish_draft(self, draft: CampaignDraft) -> RpaPublishResult:
        if draft.errors:
            return RpaPublishResult(
                campaign_name=draft.name,
                success=False,
                error="; ".join(draft.errors),
            )
        # hand_off: fill everything but leave browser open; treat as DRAFT status
        if self.hand_off:
            draft.status = "DRAFT"
        conn = self._ensure_browser(draft.account_label)
        flow = AdsManagerFlow(conn.page, selectors_path=self.selectors_path)
        try:
            result = flow.publish_campaign(draft, dry_run=self.dry_run)
        except Exception as exc:
            if not self._browser_closed_error(exc):
                raise
            log.warning("Browser closed during RPA (%s). Retrying with PlaywrightProfileProvider.", exc)
            self._reset_browser()
            entry = self._entry or self._resolve_entry(draft.account_label)
            self._conn = self._open_playwright_profile(entry, draft.account_label)
            flow = AdsManagerFlow(self._conn.page, selectors_path=self.selectors_path)
            result = flow.publish_campaign(draft, dry_run=self.dry_run)
        if self.hand_off and result.success:
            result.error = "🖐 Browser left open — select your Facebook Page and click Publish"
            # Keep success=True so the UI shows it as complete, not failed
        elif self.dry_run and result.success:
            provider = getattr(self._conn, "provider", "unknown") if self._conn else "unknown"
            result.error = (
                "🔬 Dry run complete — browser left open, Publish NOT clicked. "
                f"Browser: {provider}. Check the form in Ads Manager."
            )
        return result

    def publish_slots(
        self,
        slots: list[dict[str, Any]],
        *,
        account_label: str,
        country: str,
        activate: bool = False,
    ) -> list[RpaPublishResult]:
        drafts = self.build_drafts(
            slots,
            account_label=account_label,
            country=country,
            activate=activate,
            preview_mode=not self.dry_run,
        )
        results: list[RpaPublishResult] = []
        try:
            for draft in drafts:
                if draft.errors:
                    results.append(
                        RpaPublishResult(
                            campaign_name=draft.name,
                            success=False,
                            error="; ".join(draft.errors),
                        )
                    )
                    continue
                results.append(self.publish_draft(draft))
        finally:
            self.close()
        return results

    def __enter__(self) -> "RpaPublisher":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()
