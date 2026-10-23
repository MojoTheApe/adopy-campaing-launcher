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
   while the Square card's did not (ACL-1533: both by positive evidence -
   `card_changed`; a re-minted `blob:` src or missing ids are not a change;
   Vertical without evidence blocks with `crop_vertical_replace_unconfirmed`).

ACL-1533: Meta's in-wizard Replace picker (live 03.10) is left through the
picker's own Save after the "Search media" box - after a pick and, for a story
not in the library, with nothing picked. The step row's "Crop" is the current
step there and does nothing.

The step is recognised by the step row's "Crop" or by its heading.

Any mismatch is a named blocking reason (`crop_*`); the caller fails the creative
step with it, so the ad is held for review instead of published with the wrong
crop. Never presses Publish, Next or anything outside the wizard/picker.

ACL-1476 (owner, 2026-10-01): two pair cases are WARNINGS, not blocks - the ad
keeps the post file and the story placement is left to Meta:

* `story_not_placed:no_crop_step` - the wizard never showed Crop (the
  single-screen editor walks Media -> Text -> Creative generation ->
  Enhancements, runs e4fd79d26e63 and bd7b2f...);
* `story_not_placed:not_in_library` - Crop was shown, but the `_story` file is
  not in Meta's library (runs a88e3789f7ab, 362762a797c3). Original and Meta AI
  expansion off are still applied and read back; only Replace is skipped.

`warning()` names it; `meta_dom` turns it into the ad's warning row.
"""
from __future__ import annotations

import os
import re
import threading
import time
from typing import Any

from src.fb_ads_rpa import job_cancel
from src.fb_ads_rpa import run_telemetry as _telemetry

#: Machine switch for the whole Crop step handling. Off → the walk presses Next
#: on Crop as before ACL-1426 (Meta's defaults stay).
STEP_SETTING = "FB_RPA_CROP_STEP"

#: Machine switch for the Replace part (pairs). Off → a pair is blocked with
#: `crop_replace_off` rather than published without the story file.
REPLACE_SETTING = "FB_RPA_CROP_REPLACE"

CARDS = ("Square", "Vertical")

#: ACL-1476: a pair whose story could not be placed - warnings, not blocks.
NO_CROP_STEP = "story_not_placed:no_crop_step"
NOT_IN_LIBRARY = "story_not_placed:not_in_library"
WARNINGS = (NO_CROP_STEP, NOT_IN_LIBRARY)

#: ACL-1551 (live 04.10, 232d398471bd): Next on Crop did not take the wizard
#: further (Meta busy, then the same Crop again), so the wizard was left with
#: its own Close - the ad's creative is not confirmed; blocking.
WIZARD_STUCK = "crop_wizard_stuck"

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
  // ACL-1476 (a88e3789f7ab): Meta's "Replace media for Stories, Status,
  // Reels" opens INSIDE the wizard dialog - its buttons are the wizard's step
  // row and the wizard's Close/Save, no Cancel. Close there closes the whole
  // wizard.
  const labels = new Set(buttons.map(b => b.label));
  const inWizard = ['Media', 'Text', 'Crop'].every(l => labels.has(l));
  out.in_wizard = inWizard;
  // ACL-1533 (live 03.10, runs d54d68303e57/2e98a75f252d): the step row's
  // "Crop" there is the current step (aria-current=step) - pressing it is a
  // no-op, the picker stayed. The control that brings Crop back is the
  // picker's own Save: the footer's primary button (where Crop has Next),
  // AFTER the "Search media" box in the dialog; the header's Save before the
  // box is the wizard's. Only a button labelled exactly "Save" - never Next,
  // Done or Publish in that slot.
  const after = (b) => !!(search.compareDocumentPosition(b.el) & Node.DOCUMENT_POSITION_FOLLOWING);
  const ownSave = inWizard
    ? (buttons.filter(b => b.label === 'Save' && !b.disabled && after(b)).pop() || null) : null;
  out.own_save = !!ownSave;
  const target = !scoped ? null
               : inWizard ? ((args.want === 'confirm' || args.want === 'cancel') ? ownSave
                             : args.want === 'close' ? pick(['Close']) : null)
               : args.want === 'confirm' ? pick(args.confirm)
               : args.want === 'cancel' ? pick(['Cancel', 'Close'])
               : args.want === 'close' ? pick(['Close']) : null;
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
    _tls.warning = ""
    # ACL-1551: per ad, not per attempt - a Crop whose Next did not advance
    # is not pressed again by a later walk of the same ad.
    _tls.forward_stuck = False


def note_forward_stuck() -> None:
    """ACL-1551: Next on this ad's Crop did not move the wizard on."""
    _tls.forward_stuck = True


def forward_stuck() -> bool:
    """Next on this ad's Crop already failed to move the wizard on."""
    return bool(getattr(_tls, "forward_stuck", False))


def mark_wizard_stuck() -> None:
    """ACL-1551: the wizard was left on Crop with its own Close (or not at all).

    A Crop failure named earlier stays (it is the more specific reason);
    otherwise `WIZARD_STUCK`. The story warning is dropped - the ad blocks.
    """
    if not getattr(_tls, "failure", None):
        _tls.failure = WIZARD_STUCK
    _tls.warning = ""
    _tls.done = True


def current_plan():
    return getattr(_tls, "plan", None)


def warning() -> str:
    """ACL-1476: the pair's story was not placed - `NO_CROP_STEP` / `NOT_IN_LIBRARY`, or ""."""
    return getattr(_tls, "warning", "") or ""


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
        job_cancel.check()
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


def media_key(src: str | None) -> str:
    """A card media's identity that survives Meta re-rendering the card.

    ACL-1533 (live 03.10): the video cards' media is a `blob:` URL, re-minted
    whenever the Crop step is drawn again - after Replace both cards had a new
    `blob:` src while Square still showed the post. Such a URL says nothing
    about which file is shown: "" (no identity). A CDN URL without its query
    (signed, rotating parameters) is the file.
    """
    s = (src or "").strip()
    if not s or s.lower().startswith(("blob:", "data:")):
        return ""
    return s.split("#", 1)[0].split("?", 1)[0]


def _ratios(card: dict) -> tuple:
    """The card's aspect-ratio radios (label, disabled): which ratios Meta
    offers depends on the shown file's own shape (1:1 is disabled on Square
    while a square file is there)."""
    return tuple((r.get("label"), bool(r.get("disabled"))) for r in card.get("radios") or ())


def card_changed(before: dict, state: dict, name: str, picked: list[str] | None = None) -> str:
    """Positive evidence that the card `name` shows another file now; "" = none.

    ACL-1533: only evidence counts - missing ids or a re-minted `blob:` src
    are not a change. Evidence: the picked tile's thumbnail id appeared on the
    card; the card's thumbnail ids replaced by others; its media's stable
    identity (`media_key`) changed; its ratio radios changed (another shape -
    live 03.10: the story on Vertical made "9:16 (recommended)" disabled).
    """
    c0, c1 = _card(before, name), _card(state, name)
    picked_set = set(picked or ())
    t0, t1 = set(c0.get("thumbs") or ()), set(c1.get("thumbs") or ())
    if picked_set & t1 and not picked_set & t0:
        return "picked_thumb"
    if t0 and t1 and not t0 & t1:
        return "thumbs"
    k0, k1 = media_key(c0.get("media")), media_key(c1.get("media"))
    if k0 and k1 and k0 != k1:
        return "media"
    r0, r1 = _ratios(c0), _ratios(c1)
    if r0 and r1 and r0 != r1:
        return "ratios"
    return ""


def square_changed(before: dict, state: dict, picked: list[str] | None = None) -> str:
    """Positive evidence that Replace changed the Square card; "" = none."""
    return card_changed(before, state, "Square", picked)


def vertical_replaced(before: dict, state: dict, picked: list[str] | None = None) -> str:
    """Positive evidence that Replace put another file on Vertical; "" = none.

    ACL-1533 review: the raw src comparison is vacuous for video (a `blob:`
    src is re-minted on every redraw), so a Replace that left the post on
    Vertical read like a success.
    """
    return card_changed(before, state, "Vertical", picked)


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
        v0, v1 = _card(before, "Vertical").get("media"), _card(state, "Vertical").get("media")
        if not v1 or v1 == v0:
            return "crop_vertical_not_replaced"
        if square_changed(before, state, picked):
            return "crop_square_changed"
        if thumb_match(state, picked) == "mismatch":
            # The Vertical card changed, but not to the tile we picked (another
            # tile of a multi-selection, a preselected post tile, ...).
            return "crop_vertical_not_story"
        if not vertical_replaced(before, state, picked):
            # Only a re-minted src (or nothing) says Vertical changed: the
            # post may still be there - not reported as "as planned".
            return "crop_vertical_replace_unconfirmed"
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
        job_cancel.check()


def _back_to_crop(page, log: dict) -> bool:
    """ACL-1476: leave the picker for the Crop step without closing the wizard.

    Cancel, or inside the wizard the picker's own Save (ACL-1533: the step
    row's "Crop" is the current step there and did nothing live; never the
    wizard's Close first, never Next). True = the picker is gone and the Crop
    step reads again.
    """
    got = _picker(page, "cancel")
    pt = got.get("point")
    log["picker_back"] = pt.get("label") if pt else None
    if pt:
        _md()._cdp_mouse_click(page, float(pt["x"]), float(pt["y"]))
    deadline = time.time() + 6.0
    while True:
        if not _picker(page).get("open") and read(page).get("found"):
            return True
        if time.time() >= deadline:
            break
        time.sleep(0.4)
        job_cancel.check()
    if got.get("in_wizard"):
        # Still on the picker: the old way out (the wizard's Close), and the
        # step is blocked - the Crop cards can no longer be read back.
        closing = _picker(page, "close").get("point")
        if closing:
            log["picker_back"] = "Close"
            _md()._cdp_mouse_click(page, float(closing["x"]), float(closing["y"]))
            time.sleep(0.8)
            job_cancel.check()
    return False


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
        job_cancel.check()
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
        job_cancel.check()
        tiles = md._library_tiles(page, filename)
    log["tiles"] = available
    if not available:
        # ACL-1476: not a block - `apply` goes back to Crop and reads back
        # Original / no expansion; the post file stays on Vertical.
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
            job_cancel.check()
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
        job_cancel.check()
        if not _picker(page).get("open") and read(page).get("found"):
            return ""
    _cancel_picker(page)
    return "crop_replace_not_confirmed"


#: Сколько ждать карточки Crop, пока Meta показывает заглушки (ACL-1669).
CARDS_RENDER_WAIT_S = 20.0


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
        if state.get("found") and not any(_card(state, n) for n in CARDS):
            # ACL-1669 (B97, bc4302862ee7): шаг Crop открыт, а карточки ещё не
            # дорисованы — Meta показывает заглушки загрузки (~13 с). Читали
            # сразу, видели пустые карточки и отвечали crop_replace_button_missing.
            state = _wait(page, lambda s: any(_card(s, n) for n in CARDS), CARDS_RENDER_WAIT_S)
            log["cards_waited_s"] = round(time.time() - started, 1)
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
                    if why == "crop_story_not_in_library":
                        # ACL-1476: the story is not in Meta's library - back
                        # to Crop, Replace skipped, the rest read back below.
                        if _back_to_crop(page, log):
                            _tls.warning = NOT_IN_LIBRARY
                            why = ""
                            state = read(page)
                            # ACL-1533: the way back may be the picker's Save
                            # with nothing picked - the Vertical card must
                            # still show the same file (media there, no
                            # evidence of another file: ids, stable media
                            # identity, ratio radios), or the step blocks.
                            v0, v1 = _card(before, "Vertical"), _card(state, "Vertical")
                            if state.get("found") and v0 and (
                                    (v0.get("media") and not v1.get("media"))
                                    or vertical_replaced(before, state)):
                                why = "crop_vertical_changed_without_story"
                        else:
                            why = "crop_replace_picker_not_closed"
                    elif not why:
                        log["clicks"].append("Vertical:replace")
                        state = _wait(page, lambda s: _card(s, "Vertical").get("media")
                                      != _card(before, "Vertical").get("media"), 6.0)
                        # Meta may reset the ratio of a replaced card.
                        if _card(state, "Vertical").get("original") is False and _click(page, "Vertical", "original"):
                            log["clicks"].append("Vertical:original_after_replace")
                            state = _wait(page, lambda s: _card(s, "Vertical").get("original") is True)
            if not why:
                # Replace skipped (story not in library): Original and no
                # expansion are read back as for a single file.
                skipped = warning() == NOT_IN_LIBRARY
                replaced = getattr(plan, "replace_vertical", False) and not skipped
                why = readback(state, None if skipped else plan, before if replaced else None,
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
        _tls.warning = ""
    try:
        # A Replace whose identity could not be read (no thumbnail ids on
        # either side) is not blocked - as on the library-pick path - but
        # said: a warning row, not a silent "as planned". ACL-1476: so is a
        # story that is not in the library.
        unconfirmed = not why and log.get("thumb") == "unconfirmed"
        warned = "" if why else warning()
        _telemetry.record("ad_creative_crop", False if why else (None if unconfirmed or warned else True),
                          note=why or warned or ("as_planned_thumb_unconfirmed" if unconfirmed else "as_planned"),
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
    _tls.warning = ""


def reopen() -> None:
    """ACL-1736: мастер вернулся на Crop через другой шаг - рамки проверить снова."""
    _tls.done = False


def failure_pending() -> bool:
    """A Crop failure is stored and not yet taken (read without taking it)."""
    return bool(getattr(_tls, "failure", None))


def outcome() -> str:
    """After a wizard walk: the named reason the Crop step failed, or ""."""
    why = take_failure()
    if why:
        return why
    plan = current_plan()
    pair = plan is not None and getattr(plan, "replace_vertical", False)
    if plan is not None and not applied() and not getattr(_tls, "unseen_said", False):
        # Meta's defaults (Vertical 9:16, an expansion box) stayed as they
        # were. Not blocking, but said once per attempt instead of a silent ""
        # (review M1). ACL-1476: a pair too - the single-screen editor's wizard
        # (Media, Text, Creative generation, Enhancements) has no Crop step;
        # the ad keeps the post file and the warning `NO_CROP_STEP` is named.
        _tls.unseen_said = True
        if pair:
            _tls.warning = NO_CROP_STEP
        try:
            _telemetry.record("ad_creative_crop", None, note=NO_CROP_STEP if pair else "crop_step_not_shown",
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
