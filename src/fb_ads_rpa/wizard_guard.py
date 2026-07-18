"""Wizard stay-in-flow guard (phase A): deny-list + URL recover for recipe player.

Blocks known exit clicks (About this update, help links, wizard Close) and
recovers when the tab navigates to Meta Business news/help (e.g. click-attribution).
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from playwright.sync_api import Page

log = logging.getLogger(__name__)

_ZONES_PATH = Path(__file__).resolve().parents[2] / "config" / "rpa_forbidden_zones.yaml"


@dataclass
class GuardConfig:
    bad_url_patterns: list[str] = field(default_factory=list)
    forbidden_click_texts: list[dict[str, Any]] = field(default_factory=list)
    allowed_click_texts: list[str] = field(default_factory=list)
    max_recover_attempts: int = 2
    prefer_close_news_tab: bool = True


@lru_cache(maxsize=1)
def load_forbidden_zones(path: str | None = None) -> GuardConfig:
    p = Path(path) if path else _ZONES_PATH
    if not p.is_file():
        log.warning("forbidden zones file missing: %s", p)
        return GuardConfig()
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    recover = raw.get("recover") or {}
    return GuardConfig(
        bad_url_patterns=[str(x) for x in (raw.get("bad_url_patterns") or [])],
        forbidden_click_texts=list(raw.get("forbidden_click_texts") or []),
        allowed_click_texts=[str(x) for x in (raw.get("allowed_click_texts") or [])],
        max_recover_attempts=int(recover.get("max_attempts") or 2),
        prefer_close_news_tab=bool(recover.get("prefer_close_news_tab", True)),
    )


def reload_forbidden_zones(path: str | None = None) -> GuardConfig:
    load_forbidden_zones.cache_clear()
    return load_forbidden_zones(path)


def _compiled(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern)


def is_allowed_click_text(text: str, cfg: GuardConfig | None = None) -> bool:
    cfg = cfg or load_forbidden_zones()
    t = (text or "").strip()
    if not t:
        return False
    for pat in cfg.allowed_click_texts:
        if _compiled(pat).search(t):
            return True
    return False


def forbidden_click_reason(
    text: str | None,
    *,
    cfg: GuardConfig | None = None,
    scope: str | None = None,
) -> str | None:
    """Return reason string if text is a forbidden click target, else None."""
    cfg = cfg or load_forbidden_zones()
    t = (text or "").strip()
    if not t:
        return None
    if is_allowed_click_text(t, cfg):
        return None
    for rule in cfg.forbidden_click_texts:
        pat = str(rule.get("pattern") or "")
        if not pat:
            continue
        rule_scope = rule.get("scope")
        if scope and rule_scope and rule_scope != scope:
            continue
        if scope is None and rule_scope == "wizard_chrome":
            # Close is only blocked when checking wizard chrome / match own_text Close
            # Explicit Close match still blocked via match dict path below.
            pass
        try:
            if _compiled(pat).search(t):
                return str(rule.get("reason") or pat)
        except re.error:
            if pat.lower() in t.lower():
                return str(rule.get("reason") or pat)
    return None


def forbidden_match_reason(match: dict[str, Any] | None, *, cfg: GuardConfig | None = None) -> str | None:
    """Check recipe step match fields before clicking."""
    if not match:
        return None
    cfg = cfg or load_forbidden_zones()
    for key in ("own_text", "text", "label"):
        val = match.get(key)
        if not val or not isinstance(val, str):
            continue
        # Wizard Close in recipe match is always forbidden (except Cancel in allowed list)
        scope = "wizard_chrome" if val.strip().lower() in ("close", "cerrar") else None
        reason = forbidden_click_reason(val, cfg=cfg, scope=scope)
        if reason:
            return reason
    return None


def url_left_ads_manager(url: str | None, *, cfg: GuardConfig | None = None) -> bool:
    """True if URL is Meta business news/help or clearly outside Ads Manager edit flow."""
    cfg = cfg or load_forbidden_zones()
    u = (url or "").strip().lower()
    if not u or u == "about:blank":
        return False
    for pat in cfg.bad_url_patterns:
        if pat.lower() in u:
            return True
    # Explicit news/help host paths already covered; also catch business marketing site
    if "facebook.com/business/" in u and "adsmanager" not in u:
        # Allow business.facebook.com ads paths if any
        if "/adsmanager" in u or "/ads/manager" in u:
            return False
        return True
    return False


def is_ads_manager_url(url: str | None) -> bool:
    u = (url or "").lower()
    if not u:
        return False
    return any(
        x in u
        for x in (
            "adsmanager",
            "/ads/manager",
            "facebook.com/ads",
            "/campaigns",
            "/edit/",
            "act=",
        )
    )


def dismiss_measurement_banner(page: Page) -> bool:
    """Dismiss the blue measurement banner via X — never click 'About this update'."""
    banner_re = re.compile(
        r"evolving how we measure|About this update|click results will report",
        re.I,
    )
    try:
        banner = page.get_by_text(banner_re).first
        if not banner.is_visible(timeout=500):
            return False
    except Exception:
        return False

    # JS first: find About this update / banner text, click sibling Close — not the link
    try:
        clicked = page.evaluate(
            """() => {
              const matchRe = /About this update|evolving how we measure|click results will report/i;
              const nodes = Array.from(document.querySelectorAll('a,button,div,span'));
              const hit = nodes.find(el => matchRe.test((el.textContent||'').trim().slice(0, 120)));
              if (!hit) return false;
              let root = hit;
              for (let i = 0; i < 8 && root; i++) {
                const closers = root.querySelectorAll(
                  '[aria-label="Close"],[aria-label="Dismiss"],[aria-label="Cerrar"],[aria-label="Descartar"],[aria-label="Dismiss notification"]'
                );
                for (const closer of closers) {
                  const t = (closer.textContent||'').trim();
                  if (/^About this update$/i.test(t)) continue;
                  const r = closer.getBoundingClientRect();
                  // Prefer closers in the top banner strip
                  if (r.top < 120 && r.width > 0 && r.height > 0) {
                    closer.click();
                    return true;
                  }
                }
                root = root.parentElement;
              }
              return false;
            }"""
        )
        if clicked:
            log.info("wizard_guard: dismissed measurement banner via JS near About this update")
            time.sleep(0.3)
            return True
    except Exception as exc:
        log.debug("banner dismiss JS failed: %s", exc)

    for sel in (
        'div[role="banner"] [aria-label="Close"]',
        'div[role="banner"] [aria-label="Dismiss"]',
        '[aria-label="Dismiss notification"]',
        '[aria-label="Dismiss"]',
    ):
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=400):
                try:
                    name = (btn.inner_text(timeout=200) or "").strip()
                except Exception:
                    name = ""
                if forbidden_click_reason(name):
                    continue
                box = btn.bounding_box()
                if box and box.get("y", 999) < 120:
                    from src.fb_ads_rpa import meta_dom as md

                    md._cdp_mouse_click(
                        page,
                        box["x"] + box["width"] / 2,
                        box["y"] + box["height"] / 2,
                    )
                else:
                    btn.click(timeout=1500, force=True)
                log.info("wizard_guard: dismissed measurement banner via %s", sel)
                return True
        except Exception:
            pass
    return False


@dataclass
class RecoverResult:
    ok: bool
    action: str = ""
    detail: str = ""


def recover_from_bad_url(
    page: Page,
    *,
    reopen_fn=None,
    switch_wizard_fn=None,
    cfg: GuardConfig | None = None,
) -> RecoverResult:
    """If on news/help (or left Ads Manager), history.back / close tab / reopen draft.

    reopen_fn() -> bool and switch_wizard_fn() -> bool are optional callables bound
    to RecipePlayer methods (may update player.page).
    """
    cfg = cfg or load_forbidden_zones()
    url = page.url or ""
    if not url_left_ads_manager(url, cfg=cfg):
        return RecoverResult(ok=True, action="still_in_flow", detail=url[:80])

    log.warning("wizard_guard: left Ads Manager → %s", url[:120])

    # 1) Prefer closing a dedicated news tab and switching back to Ads Manager
    if cfg.prefer_close_news_tab:
        try:
            ctx = page.context
            pages = list(ctx.pages)
            ads_pages = [p for p in pages if is_ads_manager_url(p.url) and not url_left_ads_manager(p.url, cfg=cfg)]
            bad_pages = [p for p in pages if url_left_ads_manager(p.url, cfg=cfg)]
            if ads_pages and bad_pages:
                for bp in bad_pages:
                    if bp is not ads_pages[0]:
                        try:
                            bp.close()
                        except Exception:
                            pass
                target = ads_pages[0]
                try:
                    target.bring_to_front()
                except Exception:
                    pass
                if switch_wizard_fn and switch_wizard_fn():
                    return RecoverResult(ok=True, action="close_news_tab", detail=(target.url or "")[:80])
                # Landed on Ads Manager manage list — must reopen edit wizard
                if reopen_fn and reopen_fn():
                    return RecoverResult(ok=True, action="close_news_tab_reopen", detail=(target.url or "")[:80])
        except Exception as exc:
            log.debug("close news tab failed: %s", exc)

    # 2) history.back on current page
    try:
        page.go_back(timeout=8000, wait_until="domcontentloaded")
        time.sleep(1.0)
    except Exception as exc:
        log.debug("go_back failed: %s", exc)

    if switch_wizard_fn and switch_wizard_fn():
        return RecoverResult(ok=True, action="history_back_wizard", detail=(page.url or "")[:80])
    # Manage list after back is NOT success — reopen edit or fail
    if not url_left_ads_manager(page.url, cfg=cfg) and is_ads_manager_url(page.url):
        if reopen_fn and reopen_fn():
            return RecoverResult(ok=True, action="history_back_reopen", detail=(page.url or "")[:80])
        return RecoverResult(
            ok=False,
            action="history_back_no_wizard",
            detail=(page.url or "")[:120],
        )

    # 3) Reopen draft from campaigns list
    if reopen_fn and reopen_fn():
        return RecoverResult(ok=True, action="reopen_draft", detail=(page.url or "")[:80])

    return RecoverResult(ok=False, action="recover_failed", detail=(page.url or "")[:120])


def dismiss_account_switcher(page: Page) -> bool:
    """Close the ad-account / business portfolio picker if it is open."""
    markers = (
        re.compile(r"Search for an ad account", re.I),
        re.compile(r"^Business portfolios$", re.I),
        re.compile(r"^Create a business portfolio$", re.I),
    )
    open_picker = False
    for pat in markers:
        try:
            if page.get_by_text(pat).first.is_visible(timeout=350):
                open_picker = True
                break
        except Exception:
            pass
    if not open_picker:
        return False
    try:
        page.keyboard.press("Escape")
        time.sleep(0.35)
        log.info("wizard_guard: dismissed account switcher via Escape")
        return True
    except Exception as exc:
        log.debug("dismiss account switcher failed: %s", exc)
        return False
