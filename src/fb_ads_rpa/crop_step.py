"""Meta's Crop step: Original on both cards, no Meta AI expansion, story on Vertical (ACL-1426).

The creative wizard after a library pick or an upload walks Trim → **Crop** →
Text → Creative/Image generation → Enhancements (→ Translation). Until ACL-1426
the robot pressed Next on Crop and Meta's defaults stayed. Captured live
2026-10-01 (phase A, scrubbed fixtures `tests/fixtures/ads_manager/
crop_step_*_2026_10_01.html`):

* heading "Select media crop for placements", two cards with `role=heading`
  **Square** and **Vertical**;
* each card: a `radiogroup` "Aspect Ratio" with `role=radio` **Original** and
  `1:1` / `1:1 (recommended)` (Square) or `9:16 (recommended)` (Vertical), plus
  **Crop** and **Replace** buttons;
* defaults seen: video in the single-screen editor — Original on both cards;
  image in the classic editor — Square Original, **Vertical 9:16**;
* the "Crop and expand (Meta AI)" checkbox from the owner's screenshot was not on
  any captured card; when it is there, it is unchecked here.

What `apply` does on the Crop step, before the walk presses Next:

1. Original on each card that is not on Original (one guarded click each);
2. "Crop and expand (Meta AI)" unchecked on each card where it is checked;
3. for a post/story pair (`creative_pairing.CropPlan.replace_vertical`): Replace
   on the Vertical card, the story file searched and picked in the media
   library that opens, the library's own confirm button, back on Crop;
4. read-back: Original on both cards, no expansion checked and, for a pair,
   the Vertical card's media changed while the Square card's did not.

Any mismatch is a named blocking reason (`crop_*`); the caller fails the creative
step with it, so the ad is held for review instead of published with the wrong
crop. Never presses Publish, Next or anything outside the wizard/picker.
"""
from __future__ import annotations

import os
import re
import threading
import time
from typing import Any

from src.fb_ads_rpa import run_telemetry as _telemetry

#: Machine switch for the whole Crop step handling. Off → the walk presses Next
#: on Crop as before ACL-1426 (Meta's defaults stay).
STEP_SETTING = "FB_RPA_CROP_STEP"

#: Machine switch for the Replace part (pairs). Off → a pair is blocked with
#: `crop_replace_off` rather than published without the story file.
REPLACE_SETTING = "FB_RPA_CROP_REPLACE"

CARDS = ("Square", "Vertical")

#: Library picker's confirm labels, in order. Publish is never among them.
_CONFIRM_LABELS = ("Done", "Select", "Confirm", "Next", "Save", "Continue")

#: Read the Crop step: cards, radios, expansion boxes, Replace, media identity.
#: `mark` (optional) = {card, what}: marks the element to click with
#: `data-acl-crop` and returns its centre (after scrolling it into view).
CROP_JS = r"""
(mark) => {
  const norm = (s) => (s || '').replace(/[\u200b-\u200d\ufeff]/g, '').replace(/\s+/g, ' ').trim();
  const box = (el) => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  document.querySelectorAll('[data-acl-crop]').forEach(e => e.removeAttribute('data-acl-crop'));
  const title = [...document.querySelectorAll('[role=heading],h1,h2,h3,h4,div,span')]
    .find(e => e.children.length === 0 && box(e) && norm(e.textContent) === 'Select media crop for placements');
  if (!title) return { found: false };
  let root = title.closest('[role=dialog]') || document.body;
  const heads = {};
  for (const h of root.querySelectorAll('[role=heading],h1,h2,h3,h4,h5')) {
    const t = norm(h.textContent);
    if ((t === 'Square' || t === 'Vertical') && box(h) && !heads[t]) heads[t] = h;
  }
  const label = (c) => {
    let t = c.getAttribute('aria-label') || '';
    if (!t) {
      const by = c.getAttribute('aria-labelledby');
      if (by) t = by.split(/\s+/).map(id => { const e = document.getElementById(id); return e ? e.textContent : ''; }).join(' ');
    }
    if (!t) t = c.textContent || '';
    if (!norm(t)) { const l = c.closest('label') || c.parentElement; t = l ? l.textContent : ''; }
    return norm(t);
  };
  // Meta's radios are native <input type=radio> (visually hidden) inside a
  // <label>: the input's own `checked` first, then aria-checked.
  const checked = (c) => {
    if (c.tagName === 'INPUT') return !!c.checked;
    const a = c.getAttribute('aria-checked');
    if (a !== null) return a === 'true';
    const i = c.querySelector('input'); return !!(i && i.checked);
  };
  const target = (c) => c.closest('label') || c;
  const cardOf = (name) => {
    const h = heads[name]; if (!h) return null;
    const other = heads[name === 'Square' ? 'Vertical' : 'Square'];
    let best = null;
    for (let n = h.parentElement, i = 0; n && n !== root.parentElement && i < 14; n = n.parentElement, i++) {
      if (other && n.contains(other)) break;
      if (n.querySelector('[role=radio],input[type=radio]')) best = n;
    }
    return best;
  };
  const media = (card) => {
    const m = [...card.querySelectorAll('img,video')].find(box) || card.querySelector('img,video');
    if (!m) return '';
    return (m.currentSrc || m.getAttribute('src') || m.getAttribute('poster') || '').slice(0, 400);
  };
  const out = { found: true, cards: {}, point: null };
  for (const name of ['Square', 'Vertical']) {
    const card = cardOf(name);
    if (!card) { out.cards[name] = null; continue; }
    const radios = [...card.querySelectorAll('[role=radio],input[type=radio]')]
      .map(r => ({ el: r, label: label(r), checked: checked(r),
                   disabled: r.disabled === true || r.getAttribute('aria-disabled') === 'true' }));
    const expand = [...card.querySelectorAll('[role=checkbox],[role=switch],input[type=checkbox]')]
      .map(c => ({ el: c, label: label(c), checked: checked(c) }))
      .filter(c => /expand|meta ai/i.test(c.label));
    const replace = [...card.querySelectorAll('[role=button],button')]
      .find(b => box(b) && norm(b.textContent) === 'Replace' && b.getAttribute('aria-disabled') !== 'true');
    out.cards[name] = {
      radios: radios.map(r => ({ label: r.label.slice(0, 40), checked: r.checked, disabled: r.disabled })),
      original: (radios.find(r => /^original$/i.test(r.label)) || {}).checked ?? null,
      expand: expand.map(c => ({ label: c.label.slice(0, 60), checked: c.checked })),
      replace: !!replace,
      media: media(card),
    };
    if (mark && mark.card === name) {
      let el = null;
      if (mark.what === 'original') el = (radios.find(r => /^original$/i.test(r.label)) || {}).el;
      if (mark.what === 'expand') el = (expand.find(c => c.checked) || {}).el;
      if (mark.what === 'replace') el = replace;
      if (el && mark.what !== 'replace') el = target(el);
      if (el) {
        el.scrollIntoView({ block: 'center', behavior: 'instant' });
        const r = el.getBoundingClientRect();
        const hit = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2);
        el.setAttribute('data-acl-crop', mark.what);
        out.point = { x: r.x + r.width / 2, y: r.y + r.height / 2,
                      hit: !!hit && (el === hit || el.contains(hit) || hit.contains(el)) };
      }
    }
  }
  return out;
}
"""

#: The library picker opened by Replace: the dialog around "Search media" and
#: its confirm / cancel buttons. `want` = "confirm" | "cancel" | "" (read only).
PICKER_JS = r"""
(args) => {
  const norm = (s) => (s || '').replace(/[\u200b-\u200d\ufeff]/g, '').replace(/\s+/g, ' ').trim();
  const box = (el) => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const search = [...document.querySelectorAll('input,[contenteditable=true]')]
    .find(e => box(e) && /search media/i.test(e.getAttribute('placeholder') || e.getAttribute('aria-label') || ''));
  if (!search) return { open: false };
  const dlg = search.closest('[role=dialog]') || document.body;
  const crop = [...dlg.querySelectorAll('*')].some(e => e.children.length === 0
    && norm(e.textContent) === 'Select media crop for placements' && box(e));
  const buttons = [...dlg.querySelectorAll('[role=button],button')].filter(box)
    .map(b => ({ el: b, label: norm(b.textContent), disabled: b.getAttribute('aria-disabled') === 'true' || b.disabled === true }));
  const out = { open: true, crop_visible: crop,
                buttons: buttons.map(b => ({ label: b.label.slice(0, 30), disabled: b.disabled })).slice(0, 20),
                point: null };
  const pick = (labels) => {
    for (const l of labels) {
      const b = buttons.filter(x => x.label === l && !x.disabled).pop();
      if (b) return b;
    }
    return null;
  };
  const target = args.want === 'confirm' ? pick(args.confirm)
               : args.want === 'cancel' ? pick(['Cancel', 'Close']) : null;
  if (target) {
    target.el.scrollIntoView({ block: 'center', behavior: 'instant' });
    const r = target.el.getBoundingClientRect();
    out.point = { x: r.x + r.width / 2, y: r.y + r.height / 2, label: target.label };
  }
  return out;
}
"""


def _md():
    from src.fb_ads_rpa import meta_dom

    return meta_dom


# ── plan for the walk (set by the upload path, read by the walk) ──────────

_tls = threading.local()


def set_plan(plan) -> None:
    """The crop plan of the creative being attached in this thread (None clears)."""
    _tls.plan = plan
    _tls.failure = None
    _tls.done = False


def current_plan():
    return getattr(_tls, "plan", None)


def take_failure() -> str | None:
    """The named blocking reason of the last Crop step, once."""
    why = getattr(_tls, "failure", None)
    _tls.failure = None
    return why


def applied() -> bool:
    return bool(getattr(_tls, "done", False))


def replace_enabled() -> bool:
    value = (os.environ.get(REPLACE_SETTING) or "").strip().lower()
    return value not in ("0", "false", "no", "off")


# ── reading and clicking ──────────────────────────────────────────────────

def read(page, mark: dict | None = None) -> dict:
    try:
        got = page.evaluate(CROP_JS, mark)
    except Exception as exc:
        return {"found": False, "error": type(exc).__name__}
    return got if isinstance(got, dict) else {"found": False}


def _click(page, card: str, what: str) -> bool:
    state = read(page, {"card": card, "what": what})
    pt = state.get("point")
    if not pt or not pt.get("hit"):
        return False
    _md()._cdp_mouse_click(page, float(pt["x"]), float(pt["y"]))
    return True


def _wait(page, ok, wait_s: float = 3.0) -> dict:
    deadline = time.time() + wait_s
    state = read(page)
    while not ok(state) and time.time() < deadline:
        time.sleep(0.25)
        state = read(page)
    return state


def _card(state: dict, name: str) -> dict:
    return (state.get("cards") or {}).get(name) or {}


def readback(state: dict, plan, before: dict | None = None) -> str:
    """"" when the Crop step is as the plan says; otherwise the named reason."""
    if not state.get("found"):
        return "crop_step_not_read"
    for name in CARDS:
        card = _card(state, name)
        if not card:
            return f"crop_{name.lower()}_card_missing"
        if card.get("original") is not True:
            return f"crop_{name.lower()}_not_original"
        if any(c.get("checked") for c in card.get("expand") or ()):
            return f"crop_{name.lower()}_meta_ai_expand_on"
    if plan is not None and getattr(plan, "replace_vertical", False):
        if before is None:
            return "crop_replace_not_done"
        sq0, sq1 = _card(before, "Square").get("media"), _card(state, "Square").get("media")
        v0, v1 = _card(before, "Vertical").get("media"), _card(state, "Vertical").get("media")
        if not v1 or v1 == v0:
            return "crop_vertical_not_replaced"
        if sq0 and sq1 and sq0 != sq1:
            return "crop_square_changed"
    return ""


def _picker(page, want: str = "") -> dict:
    try:
        got = page.evaluate(PICKER_JS, {"want": want, "confirm": list(_CONFIRM_LABELS)})
    except Exception as exc:
        return {"open": False, "error": type(exc).__name__}
    return got if isinstance(got, dict) else {"open": False}


def _cancel_picker(page) -> None:
    got = _picker(page, "cancel")
    pt = got.get("point")
    if pt:
        _md()._cdp_mouse_click(page, float(pt["x"]), float(pt["y"]))
        time.sleep(0.8)


def _capture(page, step: str, diag: dict) -> None:
    try:
        from src.fb_ads_rpa import page_capture as _pages
        from src.fb_ads_rpa import wizard_capture as _wc

        if not _wc.capture_enabled() or not _telemetry.current_run_id():
            return
        _pages.capture(page, step=step, diag=diag, quota=_wc.QUOTA, quota_max=_wc.MAX_PER_RUN + 4,
                       page_html=False, remember=False,
                       blocks={"picker": "(() => { const s = [...document.querySelectorAll('input')]"
                                         ".find(e => /search media/i.test(e.placeholder || ''));"
                                         " return s ? (s.closest('[role=dialog]') || null) : null; })()",
                               "creative_wizard": _pages.CREATIVE_WIZARD_BLOCK_JS})
    except Exception:
        return


def _replace_vertical(page, filename: str, log: dict) -> str:
    """Replace on Vertical → pick `filename` in the library → confirm. "" = done."""
    md = _md()
    if not _click(page, "Vertical", "replace"):
        return "crop_replace_button_missing"
    deadline = time.time() + 12.0
    picker = _picker(page)
    while not picker.get("open") and time.time() < deadline:
        time.sleep(0.4)
        picker = _picker(page)
    log["picker_buttons"] = picker.get("buttons")
    _capture(page, "wizard_crop_replace", {"file": filename, "picker": picker})
    if not picker.get("open"):
        return "crop_replace_picker_not_opened"
    md._library_search(page, filename)
    tiles = md._library_tiles(page, filename)
    available = 0
    for _ in range(md._LIBRARY_APPEAR_TRIES):
        try:
            available = tiles.count()
        except Exception:
            available = 0
        if available:
            break
        time.sleep(1.0)
        tiles = md._library_tiles(page, filename)
    log["tiles"] = available
    if not available:
        _cancel_picker(page)
        return "crop_story_not_in_library"
    before = md._library_selected_count(page)
    taken = False
    for i in range(min(available, 3)):
        tile = tiles.nth(i)
        if md._library_tile_selected(tile):
            taken = True
            break
        if md._click_library_tile(page, tile, before=before):
            continue
        for _ in range(10):
            time.sleep(0.3)
            count = md._library_selected_count(page)
            if md._library_tile_selected(tile) or (count is not None and before is not None and count > before):
                taken = True
                break
        if taken:
            break
    log["picked"] = taken
    if not taken:
        _cancel_picker(page)
        return "crop_story_pick_did_not_take"
    got = _picker(page, "confirm")
    pt = got.get("point")
    if not pt:
        _cancel_picker(page)
        return "crop_replace_no_confirm"
    log["confirm"] = pt.get("label")
    md._cdp_mouse_click(page, float(pt["x"]), float(pt["y"]))
    deadline = time.time() + 20.0
    while time.time() < deadline:
        time.sleep(0.5)
        if not _picker(page).get("open") and read(page).get("found"):
            return ""
    _cancel_picker(page)
    return "crop_replace_not_confirmed"


def apply(page, plan) -> str:
    """Bring the Crop step to the plan and read it back. "" = as planned.

    Never raises; any surprise is a named reason. Called by the wizard walk on
    the Crop step, before its forward press.
    """
    started = time.time()
    log: dict[str, Any] = {"rule": getattr(plan, "rule", ""), "clicks": []}
    why = ""
    try:
        state = read(page)
        if not state.get("found"):
            why = "crop_step_not_read"
        else:
            log["before"] = {n: {k: v for k, v in _card(state, n).items() if k != "media"} for n in CARDS}
            for name in CARDS:
                card = _card(state, name)
                if card and card.get("original") is False:
                    if _click(page, name, "original"):
                        log["clicks"].append(f"{name}:original")
                        state = _wait(page, lambda s, n=name: _card(s, n).get("original") is True)
                for _ in range(3):
                    card = _card(state, name)
                    if not any(c.get("checked") for c in card.get("expand") or ()):
                        break
                    if not _click(page, name, "expand"):
                        break
                    log["clicks"].append(f"{name}:expand_off")
                    state = _wait(page, lambda s, n=name: not any(
                        c.get("checked") for c in _card(s, n).get("expand") or ()))
            before = state
            if plan is not None and getattr(plan, "replace_vertical", False):
                if not replace_enabled():
                    why = "crop_replace_off"
                else:
                    why = _replace_vertical(page, plan.vertical_file, log)
                    if not why:
                        log["clicks"].append("Vertical:replace")
                        state = _wait(page, lambda s: _card(s, "Vertical").get("media")
                                      != _card(before, "Vertical").get("media"), 6.0)
                        # Meta may reset the ratio of a replaced card.
                        if _card(state, "Vertical").get("original") is False and _click(page, "Vertical", "original"):
                            log["clicks"].append("Vertical:original_after_replace")
                            state = _wait(page, lambda s: _card(s, "Vertical").get("original") is True)
            if not why:
                why = readback(state, plan, before if getattr(plan, "replace_vertical", False) else None)
            log["after"] = {n: {k: v for k, v in _card(state, n).items() if k != "media"} for n in CARDS}
    except Exception as exc:
        why = "crop_exception:" + type(exc).__name__
    _tls.done = True
    if why:
        _tls.failure = why
    try:
        _telemetry.record("ad_creative_crop", False if why else True, note=why or "as_planned",
                          duration_s=time.time() - started,
                          diag={**log, "square": getattr(plan, "square_file", ""),
                                "vertical": getattr(plan, "vertical_file", ""),
                                "replace": bool(getattr(plan, "replace_vertical", False))})
    except Exception:
        pass
    return why


def step_is_crop(name: str | None) -> bool:
    return bool(re.fullmatch(r"crop", (name or "").strip(), re.I))


def reset_attempt() -> None:
    """A new attempt at the same ad: the Crop step is to be done again."""
    _tls.failure = None
    _tls.done = False


def outcome() -> str:
    """After a wizard walk: the named reason the Crop step failed, or ""."""
    why = take_failure()
    if why:
        return why
    plan = current_plan()
    if plan is not None and getattr(plan, "replace_vertical", False) and not applied():
        # A pair needs the Crop step; the wizard never showed it.
        return "crop_step_not_shown"
    return ""


def plan_for(creative_path: str):
    """Crop plan for the creative on disk, or None when the step is switched off."""
    value = (os.environ.get(STEP_SETTING) or "").strip().lower()
    if value in ("0", "false", "no", "off"):
        return None
    from src.fb_ads_rpa import creative_pairing

    try:
        return creative_pairing.plan_for_path(creative_path)
    except Exception:
        return None
