"""
Click Recorder — learn campaign creation by watching human clicks over CDP.

Usage from dashboard:
    POST /rpa-record/start  {account_label?: str, recipe_name?: str}
    POST /rpa-record/stop
    GET  /rpa-record/status
    GET  /rpa-record/recipes

The recorder connects to an already-open Multilogin browser (CDP), injects a
click listener, and writes a JSON recipe under output/rpa_recipes/.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright

_RECIPES_DIR = Path(__file__).resolve().parents[2] / "output" / "rpa_recipes"

_INJECT_JS = """
() => {
  if (window.__rpaRecorderInstalled) return true;
  window.__rpaRecorderInstalled = true;
  window.__rpaActions = [];
  window.__rpaInputs = [];

  const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();

  const cssPath = (el) => {
    if (!el || el.nodeType !== 1) return '';
    const parts = [];
    let cur = el;
    for (let depth = 0; cur && cur.nodeType === 1 && depth < 6; depth++) {
      let part = cur.tagName.toLowerCase();
      if (cur.id) { parts.unshift('#' + cur.id); break; }
      const role = cur.getAttribute('role');
      if (role) part += `[role="${role}"]`;
      const cls = (cur.className && typeof cur.className === 'string')
        ? cur.className.trim().split(/\\s+/).slice(0, 2).join('.')
        : '';
      if (cls) part += '.' + cls;
      const parent = cur.parentElement;
      if (parent) {
        const siblings = Array.from(parent.children).filter(c => c.tagName === cur.tagName);
        if (siblings.length > 1) part += `:nth-of-type(${siblings.indexOf(cur) + 1})`;
      }
      parts.unshift(part);
      cur = parent;
    }
    return parts.join(' > ');
  };

  // ACL-1342 S05: пароль и одноразовый код не должны попадать в JSON
  // рекордера — recent_actions уезжает наружу через /rpa-record/status.
  // Признаём поле секретным по type=password, autocomplete=one-time-code
  // и по характерным словам в имени/id/placeholder/aria-label — значение
  // такого поля не читаем вовсе, только фиксируем сам факт ввода.
  //
  // PR #343, ревью: имя поля 2FA-чекпоинта Meta — `approvals_code`
  // (задокументировано публично) — не подходило ни под один из этих слов:
  // "code" в нём есть, но без нужного префикса (verification/security/
  // auth/otp/passcode), и такое поле нередко `type="text"` без
  // `autocomplete="one-time-code"` в легаси-формах чекпоинта. Добавлены
  // approvals?_code/login_code/confirmation_code и написания без
  // разделителя/с пробелом или дефисом вместо подчёркивания.
  const isSecretField = (el) => {
    if (!el) return false;
    const type = (el.getAttribute('type') || el.type || '').toLowerCase();
    if (type === 'password') return true;
    const autocomplete = (el.getAttribute('autocomplete') || '').toLowerCase();
    if (autocomplete === 'one-time-code') return true;
    const hay = [
      el.getAttribute('name'), el.getAttribute('id'), el.getAttribute('placeholder'),
      el.getAttribute('aria-label'),
    ].filter(Boolean).join(' ').toLowerCase();
    return /(otp|one[-_ ]?time|verification[-_ ]?code|security[-_ ]?code|auth(entication)?[-_ ]?code|passcode|2fa|mfa|approvals?[-_ ]?code|login[-_ ]?code|confirmation[-_ ]?code)/.test(hay);
  };

  const describe = (el, eventType) => {
    if (!el || !el.getBoundingClientRect) return null;
    const r = el.getBoundingClientRect();
    const ownText = norm(
      Array.from(el.childNodes || [])
        .filter(n => n.nodeType === 3)
        .map(n => n.textContent).join('')
      || el.getAttribute('aria-label')
      || ''
    );
    const fullText = norm(el.innerText || el.textContent || '').slice(0, 120);
    return {
      ts: Date.now(),
      type: eventType,
      tag: el.tagName,
      role: el.getAttribute('role') || '',
      aria_label: el.getAttribute('aria-label') || '',
      aria_checked: el.getAttribute('aria-checked') || '',
      data_testid: el.getAttribute('data-testid') || '',
      name: el.getAttribute('name') || '',
      own_text: ownText.slice(0, 80),
      full_text: fullText,
      css_path: cssPath(el),
      x: Math.round(r.x + r.width / 2),
      y: Math.round(r.y + r.height / 2),
      w: Math.round(r.width),
      h: Math.round(r.height),
      url: location.href,
    };
  };

  document.addEventListener('click', (e) => {
    try {
      const info = describe(e.target, 'click');
      if (info) window.__rpaActions.push(info);
    } catch (_) {}
  }, true);

  document.addEventListener('change', (e) => {
    try {
      const el = e.target;
      if (!el) return;
      const info = describe(el, 'change');
      if (!info) return;
      if (isSecretField(el)) {
        info.value = '<hidden>';
        info.secret = true;
      } else {
        info.value = (el.value || '').slice(0, 200);
      }
      window.__rpaInputs.push(info);
      window.__rpaActions.push(info);
    } catch (_) {}
  }, true);

  document.addEventListener('keydown', (e) => {
    if (!['Enter', 'Escape', 'Tab', 'ArrowDown', 'ArrowUp', ' '].includes(e.key)) return;
    try {
      const info = describe(e.target || document.activeElement, 'keydown');
      if (!info) return;
      info.key = e.key;
      window.__rpaActions.push(info);
    } catch (_) {}
  }, true);

  return true;
}
"""

_DRAIN_JS = """
() => {
  const actions = window.__rpaActions || [];
  window.__rpaActions = [];
  return actions;
}
"""


@dataclass
class RecorderState:
    running: bool = False
    recipe_name: str = ""
    recipe_path: str | None = None
    cdp_url: str | None = None
    actions: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    started_at: str | None = None
    message: str = ""


_state = RecorderState()
_lock = threading.Lock()
_thread: threading.Thread | None = None
_stop_event = threading.Event()


def _find_cdp_url() -> str | None:
    """Scan common CDP ports for a running Multilogin / Chrome instance."""
    import urllib.request

    ports = [
        60361,  # MLX automation port (known current)
        19222,
        *range(56000, 56500, 10),
        *range(60000, 60500, 10),
        *range(9222, 9260),
        45000, 45001, 35000, 58888,
    ]
    for port in ports:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=0.6) as resp:
                if resp.status == 200:
                    return f"http://127.0.0.1:{port}"
        except Exception:
            pass
    return None


def _find_cdp_url_for_account(account_label: str) -> str | None:
    """Look up the real CDP port for an already-open Multilogin profile.

    Live evidence (2026-07-23): _find_cdp_url()'s fixed port list missed the
    port an active RPA test session's browser was actually running on. The
    same account's port is found reliably via browser.py's
    _discover_debug_port(), which reads the profile's own DevToolsActivePort
    file rather than guessing -- prefer this when an account_label is given.
    """
    try:
        from config.paths import get_ad_account_map_path
        from src.meta_campaign_agent.ad_account_map import (
            load_ad_account_map,
            resolve_account_entry,
        )
        from src.fb_ads_rpa.browser import (
            MultiloginDirectCDPProvider,
            _find_mlx_profile_dir,
        )

        account_map = load_ad_account_map(get_ad_account_map_path())
        entry = resolve_account_entry(account_map, account_label)
        if not entry or not entry.profile_id:
            return None
        user_data_dir = _find_mlx_profile_dir(entry.profile_id)
        if user_data_dir is None:
            return None
        provider = MultiloginDirectCDPProvider()
        port = provider._discover_debug_port(str(user_data_dir), entry.profile_id)
        if port is None:
            return None
        return f"http://127.0.0.1:{port}"
    except Exception:
        return None


def get_status() -> dict[str, Any]:
    with _lock:
        return {
            "ok": True,
            "running": _state.running,
            "recipe_name": _state.recipe_name,
            "recipe_path": _state.recipe_path,
            "cdp_url": _state.cdp_url,
            "actions_count": len(_state.actions),
            "error": _state.error,
            "started_at": _state.started_at,
            "message": _state.message,
            "recent_actions": _state.actions[-8:],
        }


def list_recipes() -> list[dict[str, Any]]:
    _RECIPES_DIR.mkdir(parents=True, exist_ok=True)
    out = []
    for p in sorted(_RECIPES_DIR.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            out.append({
                "path": str(p),
                "name": p.stem,
                "actions": len(data.get("actions") or []),
                "created_at": data.get("created_at"),
                "cdp_url": data.get("cdp_url"),
            })
        except Exception:
            out.append({"path": str(p), "name": p.stem, "actions": 0})
    return out


def _save_recipe() -> Path:
    _RECIPES_DIR.mkdir(parents=True, exist_ok=True)
    name = _state.recipe_name or "campaign"
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = _RECIPES_DIR / f"{name}_{stamp}.json"
    payload = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "recipe_name": name,
        "cdp_url": _state.cdp_url,
        "actions_count": len(_state.actions),
        "actions": _state.actions,
        "notes": (
            "Recorded human clicks over CDP. Use own_text / role / css_path / x,y "
            "to harden meta_dom selectors. Prefer semantic text+role over raw css_path."
        ),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _recorder_loop(cdp_url: str) -> None:
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(cdp_url)
            ctx = browser.contexts[0] if browser.contexts else None
            if not ctx or not ctx.pages:
                raise RuntimeError("No open pages in browser — open Ads Manager first")
            page = ctx.pages[0]
            page.bring_to_front()
            page.evaluate(_INJECT_JS)
            with _lock:
                _state.message = "Recording… create the campaign manually, then click Stop"
            while not _stop_event.is_set():
                try:
                    # Re-inject if page navigated
                    page.evaluate(_INJECT_JS)
                    batch = page.evaluate(_DRAIN_JS) or []
                    if batch:
                        with _lock:
                            _state.actions.extend(batch)
                            _state.message = f"Recording… {len(_state.actions)} actions"
                except Exception as exc:
                    # Page may have navigated — try to reattach
                    try:
                        if ctx.pages:
                            page = ctx.pages[0]
                            page.evaluate(_INJECT_JS)
                    except Exception:
                        with _lock:
                            _state.error = f"Recorder lost page: {exc}"
                time.sleep(0.45)
    except Exception as exc:
        with _lock:
            _state.error = str(exc)
            _state.message = f"Recorder error: {exc}"
    finally:
        with _lock:
            _state.running = False
            if _state.actions and not _state.recipe_path:
                try:
                    path = _save_recipe()
                    _state.recipe_path = str(path)
                    _state.message = f"Saved {len(_state.actions)} actions → {path.name}"
                except Exception as exc:
                    _state.error = f"Save failed: {exc}"


def start_recording(
    *,
    recipe_name: str = "campaign",
    cdp_url: str | None = None,
    account_label: str | None = None,
) -> dict[str, Any]:
    global _thread
    with _lock:
        if _state.running:
            return {"ok": False, "error": "Recorder already running", **get_status()}

    url = cdp_url
    if not url and account_label:
        url = _find_cdp_url_for_account(account_label)
    if not url:
        url = _find_cdp_url()
    if not url:
        return {
            "ok": False,
            "error": "No open browser with CDP found. Start Multilogin profile / dry-run first.",
        }

    with _lock:
        _state.running = True
        _state.recipe_name = (recipe_name or "campaign").strip().replace(" ", "_") or "campaign"
        _state.recipe_path = None
        _state.cdp_url = url
        _state.actions = []
        _state.error = None
        _state.started_at = datetime.now().isoformat(timespec="seconds")
        _state.message = "Connecting…"

    _stop_event.clear()
    _thread = threading.Thread(target=_recorder_loop, args=(url,), daemon=True)
    _thread.start()
    time.sleep(0.8)
    return get_status() | {"ok": _state.error is None}


def stop_recording() -> dict[str, Any]:
    global _thread
    with _lock:
        if not _state.running and not _state.actions:
            return {"ok": False, "error": "Recorder is not running", **get_status()}

    _stop_event.set()
    if _thread and _thread.is_alive():
        _thread.join(timeout=8)

    with _lock:
        if _state.actions and not _state.recipe_path:
            try:
                path = _save_recipe()
                _state.recipe_path = str(path)
                _state.message = f"Saved {len(_state.actions)} actions → {path.name}"
            except Exception as exc:
                _state.error = f"Save failed: {exc}"
                return {"ok": False, "error": _state.error, **asdict(_state)}
        _state.running = False
        return {
            "ok": True,
            "recipe_path": _state.recipe_path,
            "actions_count": len(_state.actions),
            "message": _state.message,
            "error": _state.error,
            "recent_actions": _state.actions[-12:],
        }
