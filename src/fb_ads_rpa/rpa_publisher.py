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
from src.fb_ads_rpa.operations import serialized_browser
from src.fb_ads_rpa.enrich_draft import enrich_campaign_drafts
from src.meta_campaign_agent.ad_account_map import load_ad_account_map, resolve_account_entry

log = logging.getLogger(__name__)


def profile_key_for(account_label: str) -> str:
    """Whose browser a publish of this account occupies: the profile, not the label.

    The same rule the spend reader applies (`account_spend._profile_key`), so
    a publish and a read of one account queue on one lock. Two labels can map
    to one profile; two queues to one browser is exactly what the lock is
    against. When the label does not resolve (not in the map, map unreadable)
    the label itself is the key: holding too much is safer than too little.
    """
    label = str(account_label or "").strip()
    try:
        entry = resolve_account_entry(load_ad_account_map(get_ad_account_map_path()), label)
    except Exception:
        entry = None
    profile = (getattr(entry, "profile_id", "") or "").strip() if entry else ""
    return profile or label


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
        """Try DirectCDP, and leave the publisher as it was if it does not work.

        The provider moves only on success. It used to move on the way in, and
        a failed attempt left the publisher holding DirectCDP: the next line of
        `_ensure_browser` — the one the log calls "the normal chain" — then ran
        DirectCDP a second time and failed the same way, while the configured
        provider was never asked at all (measured on DQ501, 2026-09-16).
        """
        from src.fb_ads_rpa.browser import MultiloginDirectCDPProvider

        log.warning("Falling back to MultiloginDirectCDP (Mimic + CDP, no MLX API token).")
        previous = self._provider
        direct = MultiloginDirectCDPProvider()
        try:
            conn = direct.open(entry, account_label)
        except Exception:
            self._provider = previous
            raise
        self._provider = direct
        return conn

    @staticmethod
    def _profile_already_running(entry) -> bool:
        """Is this MLX profile currently open (started by hand from MLX)?

        Detected by a live chrome.exe whose command line carries the
        profile's own user-data-dir. MLX shortens the path on disk
        (…\\mlx\\profiles\\B18D03~1\\3EF7F4~1\\C21D4D~1), so match on the
        profile id's leading characters rather than the full uuid.
        """
        profile_id = (getattr(entry, "profile_id", "") or "").strip()
        if not profile_id:
            return False
        needle = profile_id.replace("-", "")[:6].lower()
        try:
            import psutil  # type: ignore

            for proc in psutil.process_iter(["name", "cmdline"]):
                try:
                    name = (proc.info.get("name") or "").lower()
                    if "chrome" not in name:
                        continue
                    cmd = " ".join(proc.info.get("cmdline") or []).lower()
                    if "mlx" in cmd and needle in cmd.replace("-", ""):
                        return True
                except Exception:
                    continue
        except Exception:
            return False
        return False

    def _open_playwright_profile(self, entry, account_label: str) -> BrowserConnection:
        # SAFETY GATE (user directive 2026-07-30, after this fallback fired
        # for real): opening the account's cookies in vanilla Chromium — no
        # Mimic fingerprint, no profile proxy — is exactly the signature
        # that gets ad accounts burned. It happened live: the DQ204 browser
        # wedged (CDP dead, profile lock held), DirectCDP failed, and the
        # chain silently dropped to a plain browser with the real session.
        # Never do that automatically again. If someone truly wants this
        # path, they must set FB_RPA_ALLOW_PLAIN_BROWSER=1 explicitly.
        import os as _os

        allow = (_os.environ.get("FB_RPA_ALLOW_PLAIN_BROWSER") or "").strip().lower()
        if allow not in ("1", "true", "yes", "on"):
            raise RuntimeError(
                "PlaywrightProfile fallback is DISABLED: it would open the "
                "account session in a vanilla (non-antidetect, non-proxied) "
                "browser. Re-open the profile in the Multilogin Desktop app "
                "and retry. (Override only if you understand the risk: "
                "FB_RPA_ALLOW_PLAIN_BROWSER=1.)"
            )
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

        # If the user already has this profile open in MLX Desktop, go
        # straight to DirectCDP. The MLX automation API refuses to start a
        # profile that is already running ("HTTP 401: Profile Authorization
        # error", seen live 2026-07-31), so trying the API first just burns
        # time and then fails. DirectCDP is built for exactly this: it finds
        # the running profile and relaunches it with a debugging port.
        # Working on an already-open account is the normal case, not an
        # exception — the user should never have to close their browser
        # for a run to start.
        #
        # Not under attach-only, though, which is the default. There the
        # configured provider already attaches to a running profile before it
        # starts one, and when that fails it restarts the profile through the
        # MLX API — keeping the proxy and the cookies, which is precisely what
        # DirectCDP cannot do and rightly refuses to fake. Taking the shortcut
        # there only produced a refusal, and the buyer was told to close a
        # window that the RPA was able to take over by itself.
        if not attach_only and self._profile_already_running(entry):
            log.info("Profile already open in MLX — using DirectCDP instead of the API.")
            try:
                self._conn = self._open_multilogin_direct(entry, account_label)
                return self._conn
            except Exception as direct_exc:
                log.warning("DirectCDP on running profile failed (%s); trying normal chain.", direct_exc)
                self._reset_browser(keep_browser_open=True)

        try:
            self._conn = self._provider.open(entry, account_label)
            log.info("Browser opened via %s", self._provider.__class__.__name__)
        except Exception as exc:
            err_str = str(exc)
            self._reset_browser(keep_browser_open=soft)
            if attach_only:
                # The reason above is the one from the Multilogin automation
                # API, and it is the whole message worth reading. A profile
                # left open in MLX Desktop is no longer a reason to do
                # anything: that path restarts it by itself.
                raise RuntimeError(
                    f"{exc} "
                    "(MLX attach-only: RPA connects through the Multilogin automation API "
                    "on Publish, restarting the profile itself when it has no debug port.)"
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

    @serialized_browser(key=lambda self, draft: profile_key_for(draft.account_label))
    def publish_draft(self, draft: CampaignDraft) -> RpaPublishResult:
        if (draft.status or "").strip().upper() != "DRAFT":
            raise ValueError(
                "ACL is draft-only: ACTIVE campaigns are not accepted by the RPA publisher"
            )
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
            # A dead PAGE usually does not mean a dead BROWSER — confirmed
            # live 2026-07-31: a tab crashed mid-run while Mimic itself kept
            # running with its CDP port alive. Re-attach through the normal
            # provider chain first (which starts with attach-to-running);
            # the plain-browser fallback stays behind its v72 safety gate.
            log.warning("Browser target died mid-run (%s). Re-attaching.", exc)
            self._reset_browser(keep_browser_open=True)
            try:
                conn = self._ensure_browser(draft.account_label)
            except Exception as re_exc:
                raise self._mlx_fallback_error(exc, re_exc) from re_exc
            flow = AdsManagerFlow(conn.page, selectors_path=self.selectors_path)
            result = flow.publish_campaign(draft, dry_run=self.dry_run)
        if self.hand_off and result.publishable:
            result.error = "Draft ready — browser left open, Meta Publish was not clicked"
        elif self.hand_off and result.success:
            # A draft can exist while being unsafe to publish.  Never replace
            # the blocking verdict with a reassuring hand-off message.
            note = "Browser left open for manual review; this draft is NOT publishable."
            result.error = f"{result.error}\n{note}" if result.error else note
        elif self.dry_run and result.publishable:
            provider = getattr(self._conn, "provider", "unknown") if self._conn else "unknown"
            result.error = (
                "Dry run complete — browser left open, Publish NOT clicked. "
                f"Browser: {provider}."
            )
        return result

    @serialized_browser(key=lambda self, slots, *, account_label, **kw: profile_key_for(account_label))
    def publish_slots(
        self,
        slots: list[dict[str, Any]],
        *,
        account_label: str,
        country: str,
        activate: bool = False,
    ) -> list[RpaPublishResult]:
        if activate:
            raise ValueError(
                "ACL is draft-only: activate=True is disabled in the campaign-manager build"
            )
        drafts = self.build_drafts(
            slots,
            account_label=account_label,
            country=country,
            activate=activate,
            # Never weaken validation for a run that drives a browser. This
            # read `not self.dry_run`, which inverted the two: a REAL run got
            # preview leniency — creative paths synthesised without checking
            # the file exists, a missing Multilogin profile_id downgraded from
            # error to warning — while the supposedly safer dry run got the
            # strict checks. A dry run opens the same browser and needs the
            # same real files; only the callers that render a plan without a
            # browser (dashboard preview, `--preview-only`) pass True.
            preview_mode=False,
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
            # Park the tab on the campaigns LIST before letting go of the
            # browser. Confirmed live 2026-07-31 (Test152 server-truth
            # check): an ad editor left open after the run lets Meta's
            # "Instant form (suggested)" re-render eventually COMMIT via the
            # page's own autosave — the last ad of the run kept reverting
            # server-side even after the in-run sweep and tail watch had
            # verified it. The last ad of the FIRST campaign always
            # survived, because starting the next campaign navigated away —
            # this applies the same escape to the final campaign.
            try:
                if self._conn is not None:
                    self._goto_campaigns_list(self._conn.page)
            except Exception:
                pass
            self.close()
        return results

    @staticmethod
    def _goto_campaigns_list(page) -> None:
        import re as _re

        url = page.url or ""
        m = _re.search(r"[?&]act=(\d+)", url)
        if not m or "/adsmanager/" not in url:
            return
        page.goto(
            f"https://adsmanager.facebook.com/adsmanager/manage/campaigns?act={m.group(1)}",
            wait_until="domcontentloaded",
            timeout=30000,
        )
        page.wait_for_timeout(3000)

    def __enter__(self) -> "RpaPublisher":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()
