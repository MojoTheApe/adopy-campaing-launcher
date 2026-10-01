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
4. read-back: Original on both cards, no expansion checked, no other checked
   box or switch on the step (fail closed, `crop_unknown_toggle_on`) and, for a
   pair, the Vertical card's media changed to the picked tile (thumbnail ids)
   while the Square card's did not.

The step is recognised by the step row's "Crop" or by its heading.

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
  // ACL-1426 review M2: where unknown toggles are looked for - the wizard
  // dialog; without one, the nearest block holding the title and both cards
  // (never the whole ad editor page).
  let scope = title.closest('[role=dialog]');
  if (!scope) {
    scope = title.parentElement;
    const hs = Object.values(heads);
    while (scope && scope.parentElement && !hs.every(h => scope.contains(h))) scope = scope.parentElement;
  }
  const shown = (c) => { if (box(c)) return true; const w = c.closest('label') || c.parentElement; return !!w && box(w); };
  const EXPAND = /expan(d|sion)|meta ai|generative|\bai\b/i;
  const THUMB = /\d{6,}_\d{10,}_\d{10,}_n/g;
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
  const thumbs = (card) => {
    const ids = new Set();
    for (const n of [card, ...card.querySelectorAll('img,video,[src],[poster],[style*="background-image"]')]) {
      for (const v of [n.getAttribute('src'), n.getAttribute('poster'), n.getAttribute('style')]) {
        (String(v || '').match(THUMB) || []).forEach(x => ids.add(x));
      }
    }
    return [...ids].slice(0, 10);
  };
  const TOGGLE = '[role=checkbox],[role=switch],input[type=checkbox]';
  // One control per toggle: a native box inside a role=checkbox wrapper is
  // the same toggle.
  const toggles = (el) => [...el.querySelectorAll(TOGGLE)]
    .filter(c => shown(c) && !(c.tagName === 'INPUT' && c.parentElement && c.parentElement.closest('[role=checkbox],[role=switch]')))
    .map(c => ({ el: c, label: label(c), checked: checked(c) }))
    .map(c => ({ ...c, expand: EXPAND.test(c.label) }));
  const out = { found: true, cards: {}, shared: [], point: null };
  const cardEls = [];
  for (const name of ['Square', 'Vertical']) {
    const card = cardOf(name);
    if (!card) { out.cards[name] = null; continue; }
    cardEls.push(card);
    const radios = [...card.querySelectorAll('[role=radio],input[type=radio]')]
      .map(r => ({ el: r, label: label(r), checked: checked(r),
                   disabled: r.disabled === true || r.getAttribute('aria-disabled') === 'true' }));
    const all = toggles(card);
    const expand = all.filter(c => c.expand);
    const other = all.filter(c => !c.expand);
    const replace = [...card.querySelectorAll('[role=button],button')]
      .find(b => box(b) && norm(b.textContent) === 'Replace' && b.getAttribute('aria-disabled') !== 'true');
    out.cards[name] = {
      radios: radios.map(r => ({ label: r.label.slice(0, 40), checked: r.checked, disabled: r.disabled })),
      original: (radios.find(r => /^original$/i.test(r.label)) || {}).checked ?? null,
      expand: expand.map(c => ({ label: c.label.slice(0, 60), checked: c.checked })),
      other: other.map(c => ({ label: c.label.slice(0, 60), checked: c.checked })),
      replace: !!replace,
      media: media(card),
      thumbs: thumbs(card),
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
  // Toggles on the step outside both cards (e.g. one expansion box shared by
  // the cards): recognised expansion boxes are switched off, any other
  // checked one blocks (fail closed, review M2).
  const shared = scope ? toggles(scope).filter(c => !cardEls.some(k => k.contains(c.el))) : [];
  out.shared = shared.map(c => ({ label: c.label.slice(0, 60), checked: c.checked, expand: c.expand }));
  if (mark && mark.card === 'shared' && mark.what === 'expand') {
    let el = (shared.find(c => c.expand && c.checked) || {}).el;
    if (el) {
      el = el.closest('label') || el;
      el.scrollIntoView({ block: 'center', behavior: 'instant' });
      const r = el.getBoundingClientRect();
      const hit = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2);
      el.setAttribute('data-acl-crop', 'expand');
      out.point = { x: r.x + r.width / 2, y: r.y + r.height / 2,
                    hit: !!hit && (el === hit || el.contains(hit) || hit.contains(el)) };
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
  // ACL-1426 review L1: the picker's buttons only from the picker's own
  // dialog - never from the page or from the wizard's dialog (its Next would
  // move the wizard from Crop to Text with the picker still open).
  const own = search.closest('[role=dialog]');
  const dlg = own || document.body;
  const crop = [...dlg.querySelectorAll('*')].some(e => e.children.length === 0
    && norm(e.textContent) === 'Select media crop for placements' && box(e));
  const wizard = !!own && [...own.querySelectorAll('*')].some(e => e.children.length === 0
    && norm(e.textContent) === 'Select media crop for placements');
  const scoped = !!own && !wizard;
  const buttons = [...dlg.querySelectorAll('[role=button],button')].filter(box)
    .map(b => ({ el: b, label: norm(b.textContent), disabled: b.getAttribute('aria-disabled') === 'true' || b.disabled === true }));
  const out = { open: true, crop_visible: crop, scoped,
                buttons: buttons.map(b => ({ label: b.label.slice(0, 30), disabled: b.disabled })).slice(0, 20),
                point: null };
  const pick = (labels) => {
    for (const l of labels) {
      const b = buttons.filter(x => x.label === l && !x.disabled).pop();
      if (b) return b;
    }
    return null;
  };
  const target = !scoped ? null
               : args.want === 'confirm' ? pick(args.confirm)
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
    _tls.unseen_said = False


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


def thumb_match(state: dict, picked: list[str] | None) -> str:
    """Is the picked library tile the Vertical card's media? (review M3)

    "match" / "mismatch" by the CDN thumbnail ids, as the library-pick path
    proves identity (ACL-1284); "unconfirmed" when either side has none.
    """
    shown = set(_card(state, "Vertical").get("thumbs") or ())
    picked_set = set(picked or ())
    if not shown or not picked_set:
        return "unconfirmed"
    return "match" if shown & picked_set else "mismatch"


def readback(state: dict, plan, before: dict | None = None, picked: list[str] | None = None) -> str:
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
        if any(c.get("checked") for c in card.get("other") or ()):
            return "crop_unknown_toggle_on"
    for c in state.get("shared") or ():
        if c.get("checked"):
            return "crop_meta_ai_expand_on" if c.get("expand") else "crop_unknown_toggle_on"
    if plan is not None and getattr(plan, "replace_vertical", False):
        if before is None:
            return "crop_replace_not_done"
        sq0, sq1 = _card(before, "Square").get("media"), _card(state, "Square").get("media")
        v0, v1 = _card(before, "Vertical").get("media"), _card(state, "Vertical").get("media")
        if not v1 or v1 == v0:
            return "crop_vertical_not_replaced"
        if sq0 and sq1 and sq0 != sq1:
            return "crop_square_changed"
        if thumb_match(state, picked) == "mismatch":
            # The Vertical card changed, but not to the tile we picked (another
            # tile of a multi-selection, a preselected post tile, ...).
            return "crop_vertical_not_story"
        picked_set = set(picked or ())
        if picked_set & set(_card(state, "Square").get("thumbs") or ()) \
                and not picked_set & set(_card(before, "Square").get("thumbs") or ()):
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
    if not picker.get("scoped"):
        # No dialog of its own: its confirm cannot be told from the wizard's
        # Next - refuse rather than press something on the page (review L1).
        return "crop_replace_picker_unscoped"
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
    log["selected_before"] = before
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
    if taken:
        log["picked_thumbs"] = md._tile_thumb_ids(tile)[:5]
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
            for _ in range(3):
                if not any(c.get("expand") and c.get("checked") for c in state.get("shared") or ()):
                    break
                if not _click(page, "shared", "expand"):
                    break
                log["clicks"].append("shared:expand_off")
                state = _wait(page, lambda s: not any(
                    c.get("expand") and c.get("checked") for c in s.get("shared") or ()))
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
                why = readback(state, plan, before if getattr(plan, "replace_vertical", False) else None,
                               log.get("picked_thumbs"))
            if getattr(plan, "replace_vertical", False) and "picked_thumbs" in log:
                log["thumb"] = thumb_match(state, log.get("picked_thumbs"))
                log["vertical_thumbs"] = _card(state, "Vertical").get("thumbs")
            log["shared"] = state.get("shared")
            log["after"] = {n: {k: v for k, v in _card(state, n).items() if k != "media"} for n in CARDS}
    except Exception as exc:
        why = "crop_exception:" + type(exc).__name__
    _tls.done = True
    if why:
        _tls.failure = why
    try:
        # A Replace whose identity could not be read (no thumbnail ids on
        # either side) is not blocked - as on the library-pick path - but
        # said: a warning row, not a silent "as planned".
        unconfirmed = not why and log.get("thumb") == "unconfirmed"
        _telemetry.record("ad_creative_crop", False if why else (None if unconfirmed else True),
                          note=why or ("as_planned_thumb_unconfirmed" if unconfirmed else "as_planned"),
                          duration_s=time.time() - started,
                          diag={**log, "square": getattr(plan, "square_file", ""),
                                "vertical": getattr(plan, "vertical_file", ""),
                                "replace": bool(getattr(plan, "replace_vertical", False))})
    except Exception:
        pass
    return why


#: The Crop step's heading - the step is recognised by it as well as by the
#: step row's "Crop" (review M1: `wizard_capture._step_name` falls back to the
#: first heading when the row is not read).
HEADING = "Select media crop for placements"


def step_is_crop(name: str | None) -> bool:
    name = re.sub(r"\s+", " ", (name or "")).strip()
    return bool(re.fullmatch(r"crop", name, re.I)) or name.lower() == HEADING.lower()


def on_crop_step(page, name: str | None = None) -> bool:
    """The wizard shows the Crop step: by the step name, else by its heading."""
    if step_is_crop(name):
        return True
    return bool(read(page).get("found"))


def reset_attempt() -> None:
    """A new attempt at the same ad: the Crop step is to be done again."""
    _tls.failure = None
    _tls.done = False
    _tls.unseen_said = False


def failure_pending() -> bool:
    """A Crop failure is stored and not yet taken (read without taking it)."""
    return bool(getattr(_tls, "failure", None))


def outcome() -> str:
    """After a wizard walk: the named reason the Crop step failed, or ""."""
    why = take_failure()
    if why:
        return why
    plan = current_plan()
    if plan is not None and getattr(plan, "replace_vertical", False) and not applied():
        # A pair needs the Crop step; the wizard never showed it.
        return "crop_step_not_shown"
    if plan is not None and not applied() and not getattr(_tls, "unseen_said", False):
        # A single file: Meta's defaults (Vertical 9:16, an expansion box)
        # stayed as they were. Not blocking (the owner's rule blocks pairs
        # only), but said once per attempt instead of a silent "" (review M1).
        _tls.unseen_said = True
        try:
            _telemetry.record("ad_creative_crop", None, note="crop_step_not_shown",
                              diag={"rule": getattr(plan, "rule", ""),
                                    "square": getattr(plan, "square_file", ""),
                                    "vertical": getattr(plan, "vertical_file", "")})
        except Exception:
            pass
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
