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
from src.fb_ads_rpa.config import get_browser_provider as get_browser_provider_name, get_mlx_attach_only
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
        """Stop RPA work; disconnect CDP but leave Multilogin window open."""
        self._cancelled = True
        self.close(force=False)

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

    def _reset_browser(self, *, keep_browser_open: bool = False) -> None:
        if self._conn:
            try:
                self._conn.close(keep_browser_open=keep_browser_open)
            except Exception:
                pass
            self._conn = None
        if self._entry is not None:
            try:
                self._provider.close(self._entry, keep_browser_open=keep_browser_open)
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
            "(NOT Mimic/antidetect). Prefer opening the profile in MLX Desktop + DirectCDP, "
            "or refresh MULTILOGIN_AUTOMATION_TOKEN_A."
        )
        self._provider = PlaywrightProfileProvider()
        return self._provider.open(entry, account_label)

    @staticmethod
    def _mlx_fallback_error(direct_exc: BaseException, pw_exc: BaseException | None = None) -> RuntimeError:
        parts = [
            "Could not open Multilogin browser for RPA.",
            "1) Open this profile in the Multilogin (MLX) Desktop app.",
            "2) Retry publish (RPA will attach via CDP), or set FB_BROWSER_PROVIDER=multilogin_direct.",
            "3) Or refresh MULTILOGIN_AUTOMATION_TOKEN_A and use the Multilogin API provider.",
            f"DirectCDP error: {direct_exc}",
        ]
        if pw_exc is not None:
            parts.append(f"PlaywrightProfile error: {pw_exc}")
        return RuntimeError(" ".join(parts))

    def _ensure_browser(self, account_label: str) -> BrowserConnection:
        if self._conn is not None:
            return self._conn
        entry = self._resolve_entry(account_label)
        if not entry:
            raise RuntimeError(f"Account {account_label!r} not in ad_account_map.csv")
        self._entry = entry
        attach_only = get_mlx_attach_only()
        # When handing off / keeping the window, never hard-kill on provider switch.
        soft = bool(self.hand_off or self.dry_run)
        try:
            self._conn = self._provider.open(entry, account_label)
            log.info("Browser opened via %s", self._provider.__class__.__name__)
        except Exception as exc:
            err_str = str(exc)
            self._reset_browser(keep_browser_open=soft)
            if attach_only:
                raise RuntimeError(
                    f"{exc} "
                    "(MLX attach-only: RPA connects via Multilogin automation API on Publish. "
                    "If the profile was opened manually in MLX Desktop, close it or retry Publish — "
                    "RPA will restart it with automation enabled.)"
                ) from exc
            if "401" in err_str or "Authorization" in err_str or "authorization" in err_str:
                log.warning("MLX API returned 401 (token expired). Trying DirectCDP.")
                try:
                    self._conn = self._open_multilogin_direct(entry, account_label)
                except Exception as direct_exc:
                    log.warning("DirectCDP failed (%s). Falling back to PlaywrightProfile.", direct_exc)
                    # Keep Multilogin open if it started; only disconnect CDP.
                    self._reset_browser(keep_browser_open=True)
                    try:
                        self._conn = self._open_playwright_profile(entry, account_label)
                    except Exception as pw_exc:
                        raise self._mlx_fallback_error(direct_exc, pw_exc) from pw_exc
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
                    self._reset_browser(keep_browser_open=True)
                    try:
                        self._conn = self._open_playwright_profile(entry, account_label)
                    except Exception as pw_exc:
                        raise self._mlx_fallback_error(direct_exc, pw_exc) from pw_exc
            else:
                raise
        return self._conn

    def close(self, *, force: bool = False) -> None:
        # Always leave Multilogin/Mimic open so the user can verify FB login.
        # force/cancel only stop RPA + disconnect CDP — never stop_profile.
        _ = force  # kept for call-site compat; must not close MLX window
        if self._conn:
            try:
                self._conn.close(keep_browser_open=True)
            except Exception:
                pass
            self._conn = None
        if self._entry is not None:
            try:
                self._provider.close(self._entry, keep_browser_open=True)
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
            self._reset_browser(keep_browser_open=True)
            entry = self._entry or self._resolve_entry(draft.account_label)
            try:
                self._conn = self._open_playwright_profile(entry, draft.account_label)
            except Exception as pw_exc:
                raise self._mlx_fallback_error(exc, pw_exc) from pw_exc
            flow = AdsManagerFlow(self._conn.page, selectors_path=self.selectors_path)
            result = flow.publish_campaign(draft, dry_run=self.dry_run)
        if self.hand_off and result.success:
            result.error = "Draft ready — browser left open, Meta Publish was not clicked"
            # Keep success=True so the UI shows it as complete, not failed
        elif self.dry_run and result.success:
            provider = getattr(self._conn, "provider", "unknown") if self._conn else "unknown"
            result.error = (
                "Dry run complete — browser left open, Publish NOT clicked. "
                f"Browser: {provider}."
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
