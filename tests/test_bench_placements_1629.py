# -*- coding: utf-8 -*-
"""Placements on the layouts of 2026-10-05/06, run through the robot's `fill_placements` (ACL-1629).

Bench. Two captures of the owner's live build (the placements step failed on
several accounts, every failed campaign ended as do-not-publish):

1. `placements_button_expanders_2026_10_06.html` — KLD_BFL_B90 (run
   b40ba4e8c2d6, 00_placements, 08:14 UTC; same layout in 34bf0d6c2262, and in
   ~30 failed runs since 2026-10-05 13:55). The classic ad set editor, manual
   placements behind the collapsed "Placements" node. The category rows are NOT
   `[role=button][aria-expanded]` here: each `<li>` carries a plain
   `<button aria-label="Expand|Collapse">`, and the state is read from that
   label. The robot set every checkbox, but its "Meta does not offer this
   placement" proof (`_CATEGORY_END_JS`, ACL-1445) only knew the aria-expanded
   expanders, so it answered "collapsed" for ever and "Messenger Stories"
   (not offered by Meta since 01.10) stayed a missing placement.
   The capture is the page at the END of the step (all three lists open); the
   page script is emulated by `_SIM_B_JS` from the real rows.

2. `placements_collapsed_card_standalone_2026_10_06.html` — Facebook_9403A (run
   ab33293af6d7, 05_placements, 09:33 UTC, truncated capture, the card is whole).
   The standalone ad set editor shows the Placements card COLLAPSED: a summary
   ("Account controls: None set, Platforms, Placements: Advantage+ placements",
   `role=button aria-pressed=false`) while the body is `<div hidden>`. The step
   only opened such a card on the single-screen editor path
   (`simple_create`), found no categories, and the account-level exclusions it
   then ran read their summary ("Excluded placements: None") from the hidden
   body: `summary_still_none` although the dialog had been confirmed.
"""
from __future__ import annotations

import json
import time

import pytest

from src.fb_ads_rpa import meta_dom as md
from src.fb_ads_rpa import run_telemetry
from tests.bench_support import FIXTURES, browser, fragment  # noqa: F401

BUTTONS = FIXTURES / "placements_button_expanders_2026_10_06.html"
COLLAPSED = FIXTURES / "placements_collapsed_card_standalone_2026_10_06.html"

#: Real ids of the category blocks of the capture.
CATEGORY_IDS = {
    "Feeds": "ads_placement_expandable_list_item_feed",
    "Stories, Status, Reels": "ads_placement_expandable_list_item_story",
    "In-stream ads for reels": "ads_placement_expandable_list_item_stream",
}


@pytest.fixture
def page(browser, monkeypatch, tmp_path):
    monkeypatch.setattr(md, "_sleep_h", lambda base, spread=0.3: time.sleep(min(base, 0.05)))
    monkeypatch.setattr(md, "_human_jitter", lambda base, spread: 0.0)
    monkeypatch.setattr(md, "_PLACEMENTS_CARD_LOAD_WAIT_S", 1.5)
    monkeypatch.setattr(md, "_SUBITEMS_SETTLE_QUIET_S", 0.4)
    monkeypatch.setattr(md, "_PLACEMENTS_RECONCILE_MAX_S", 3.0)
    monkeypatch.setattr(run_telemetry, "TELEMETRY_PATH", tmp_path / "telemetry.jsonl")
    import src.fb_ads_rpa.page_capture as pc
    monkeypatch.setattr(pc, "capture", lambda *a, **k: "")
    monkeypatch.chdir(tmp_path)          # output/debug_placements_fill.txt stays out of the repo
    (tmp_path / "output").mkdir()
    ctx = browser.new_context(viewport={"width": 1000, "height": 800})
    ctx.route("**/*", lambda route: route.abort())
    p = ctx.new_page()
    try:
        yield p
    finally:
        ctx.close()


# ── layout 1: category rows with `button[aria-label=Expand|Collapse]` ─────────

#: What Meta's page script does on the captured rows. Every category starts
#: collapsed (as on arrival), the "Placements" node starts collapsed, a click on
#: a category button toggles its rows, a click on a checkbox flips `aria-checked`
#: a tick later. cfg: `off` — labels that start unchecked; `locked` — labels
#: that start disabled; `collapse` — "once": Meta folds a category 30 ms after
#: its first row click (the redraw after saving, 25.09); `unlock` —
#: {label: ms}: the row is disabled until that many ms after its category opened.
_SIM_B_JS = r"""
(cfg) => {
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const state = {};
  const cats = {};
  for (const [name, id] of Object.entries(cfg.ids)) {
    const block = document.getElementById(id);
    const btn = block.querySelector('button[aria-label]');
    const rows = [...block.querySelectorAll(':scope > ul > div[id$="_check"]')];
    cats[name] = { block, btn, rows, folded: false };
    for (const row of rows) {
      const cb = row.querySelector('input[type=checkbox]');
      const label = norm(row.querySelector('label').textContent);
      cb.dataset.simLabel = label;
      cb.dataset.simCat = name;
      const on = !cfg.off.includes(label);
      cb.checked = on; cb.setAttribute('aria-checked', String(on));
      const locked = cfg.locked.includes(label) || label in cfg.unlock;
      cb.disabled = locked; cb.setAttribute('aria-disabled', String(locked));
      state[label] = { checked: on };
    }
  }
  const fold = (name, folded) => {
    const c = cats[name];
    c.folded = folded;
    c.btn.setAttribute('aria-label', folded ? 'Expand' : 'Collapse');
    for (const r of c.rows) r.style.display = folded ? 'none' : '';
    if (!folded) for (const [label, ms] of Object.entries(cfg.unlock)) {
      const cb = c.block.querySelector('input[data-sim-label="' + label + '"]');
      if (cb) setTimeout(() => { cb.disabled = false; cb.setAttribute('aria-disabled', 'false'); }, ms);
    }
  };
  for (const name of Object.keys(cats)) {
    fold(name, true);
    cats[name].btn.addEventListener('click', () => fold(name, !cats[name].folded));
  }
  // The five category checkboxes start on. As on Meta, the box of a category WITH rows
  // is derived from them: on only when every row is on, `false` while some are off
  // (both B90 captures end like that). A click on it sets every enabled row.
  for (const cb of document.querySelectorAll('[id^="placement-"] input[type=checkbox]')) {
    cb.checked = true; cb.setAttribute('aria-checked', 'true');
  }
  const derived = { 'Feeds': 'placement-feed', 'Stories, Status, Reels': 'placement-story',
    'In-stream ads for reels': 'placement-stream' };
  const recalc = (name) => {
    const box = document.querySelector('#' + derived[name] + ' input[type=checkbox]');
    const on = cats[name].rows.every(r => r.querySelector('input[type=checkbox]').checked);
    box.checked = on; box.setAttribute('aria-checked', String(on));
  };
  for (const name of Object.keys(derived)) recalc(name);
  window.__boxClicks = []; window.__rowsTouched = false;
  const foldedOnce = new Set();
  // Category boxes Meta keeps disabled and still `aria-checked=true` while it saves
  // the click (cfg.catHold = {name: {ms, mode}}): after `ms` the box is free and,
  // mode "off" - shows the new value, "revert" - is back on and needs another
  // click. `again` re-arms the hold on every click (a page that never lets go).
  const catIds = { 'Feeds': 'placement-feed', 'Stories, Status, Reels': 'placement-story',
    'In-stream ads for reels': 'placement-stream', 'Search results': 'placement-search',
    'Apps and sites': 'placement-external' };
  const held = new Set();
  document.addEventListener('click', (e) => {
    const cb = e.target;
    if (!(cb instanceof HTMLInputElement) || cb.type !== 'checkbox') return;
    const dname = Object.keys(derived).find(n => cb.closest('#' + derived[n]));
    if (dname) {
      const was = cb.getAttribute('aria-checked') === 'true';
      window.__boxClicks.push({ name: dname, afterRows: window.__rowsTouched });
      for (const r of cats[dname].rows) {
        const rc = r.querySelector('input[type=checkbox]');
        if (rc.disabled) continue;
        rc.checked = !was; rc.setAttribute('aria-checked', String(!was));
        state[rc.dataset.simLabel].checked = !was;
      }
      recalc(dname);
      return;
    }
    const catName = Object.keys(catIds).find(n => cb.closest('#' + catIds[n]));
    const hold = catName && (cfg.catHold || {})[catName];
    if (hold && (hold.again || !held.has(catName))) {
      held.add(catName);
      cb.disabled = true; cb.setAttribute('aria-disabled', 'true');
      setTimeout(() => {
        cb.disabled = false; cb.setAttribute('aria-disabled', 'false');
        if (hold.mode === 'off') cb.setAttribute('aria-checked', String(cb.checked));
        else { cb.checked = true; cb.setAttribute('aria-checked', 'true'); }
      }, hold.ms);
      return;
    }
    setTimeout(() => cb.setAttribute('aria-checked', String(cb.checked)), 0);
    const label = cb.dataset.simLabel;
    if (!label) return;
    state[label].checked = cb.checked;
    const cat = cb.dataset.simCat;
    window.__rowsTouched = true;
    setTimeout(() => recalc(cat), 5);
    if (cfg.collapse === 'once' && !foldedOnce.has(cat)) {
      foldedOnce.add(cat);
      setTimeout(() => fold(cat, true), 30);
    }
  }, true);
  // The "Placements" node: its list is behind the collapsed node.
  const node = [...document.querySelectorAll('[role=button][aria-expanded]')]
    .find(b => /^Placements$/.test(norm(b.textContent)));
  const region = node.parentElement.querySelector(':scope > [role=region]');
  node.setAttribute('aria-expanded', 'false');
  region.style.display = 'none';
  node.addEventListener('click', () => {
    const open = node.getAttribute('aria-expanded') === 'true';
    node.setAttribute('aria-expanded', String(!open));
    region.style.display = open ? 'none' : '';
  });
  window.__sim = { state, cats, flip: (label) => {
    const cb = document.querySelector('input[data-sim-label="' + label + '"]');
    cb.checked = !cb.checked; cb.setAttribute('aria-checked', String(cb.checked));
    state[label].checked = cb.checked;
    recalc(cb.dataset.simCat);
  } };
}
"""


def _cfg_b(**kw) -> dict:
    cfg = {"ids": CATEGORY_IDS, "off": [], "locked": ["Facebook Business Explore"],
           "unlock": {}, "collapse": "never", "catHold": {}}
    cfg["off"] = kw.pop("off", ["Facebook Business Explore"])
    cfg.update(kw)
    return cfg


def _load_b(page, **kw) -> None:
    page.set_content("<html><body style='margin:0;padding-top:120px'>"
                     f"{fragment(BUTTONS, 'card')}</body></html>")
    page.evaluate(_SIM_B_JS, _cfg_b(**kw))


def _state(page) -> dict[str, bool]:
    return {k: v["checked"] for k, v in page.evaluate("() => window.__sim.state").items()}


def _assert_wanted(page) -> None:
    state = _state(page)
    wrong = [x for x in md._SUBITEMS_ON if x in state and state[x] is not True]
    wrong += [x for x in md._SUBITEMS_OFF if x in state and state[x] is not False]
    assert not wrong, f"not in the wanted state: {wrong}"


def test_layout_b_is_the_captured_one(page):
    """The expanders are plain buttons with an aria-label, not aria-expanded buttons."""
    _load_b(page)
    got = page.evaluate(r"""() => {
      const ids = %s;
      return ids.map(id => {
        const b = document.getElementById(id).querySelector('button[aria-label]');
        return [b.getAttribute('aria-label'), b.hasAttribute('aria-expanded'),
                !!b.closest('[role=button][aria-expanded]')];
      });
    }""" % json.dumps(list(CATEGORY_IDS.values())))
    assert all(label == "Expand" and not has and not anc for label, has, anc in got), got
    assert "Messenger Stories" not in page.content()


def test_layout_b_placements_are_set_and_read_back(page):
    """Every offered placement ends in the wanted state, read back from the page,
    and "Messenger Stories" is named as not offered, not as missing."""
    _load_b(page)
    report: dict = {}
    assert md.fill_placements(page, report) is True, {
        k: report.get(k) for k in ("subitems_missing", "subitems_failed", "not_offered_check")}
    _assert_wanted(page)
    assert report["subitems_missing"] == []
    assert report["subitems_not_offered"] == ["Messenger Stories"]
    check = report["not_offered_check"]["Stories, Status, Reels"]
    assert check["complete"] and check["stable"] and check["end"]["end"] is True, check
    # Wanted-off categories read false; the wanted-on ones are partial on the page and
    # are judged by their rows (R2-1), so their box is not asserted.
    off = {n: report["categories"][n]["checked"] for n, w in md._PLACEMENT_CATEGORIES.items() if not w}
    assert off == {n: False for n in off}


def test_layout_b_a_category_folded_after_a_click_is_opened_again(page):
    """Meta folds a category after saving the first click: the tail is set after re-opening."""
    _load_b(page, collapse="once")
    report: dict = {}
    assert md.fill_placements(page, report) is True, report.get("subitems_missing")
    _assert_wanted(page)
    assert report["subitems_missing"] == []


def test_layout_b_a_row_disabled_while_meta_saves_is_set_when_it_unlocks(page):
    """B90 / 13 runs of 05.10: "Ads on Facebook Reels" was disabled and still on
    when first met ("недоступна и не та"); a few seconds later it is free. The
    step reads the real state again before it gives up on a placement it saw."""
    _load_b(page, unlock={"Ads on Facebook Reels": 1500})
    report: dict = {}
    assert md.fill_placements(page, report) is True, {
        k: report.get(k) for k in ("subitems_missing", "subitems_failed")}
    assert _state(page)["Ads on Facebook Reels"] is False
    assert report["subitems_missing"] == []


def test_layout_b_a_row_that_stays_locked_on_fails_and_is_named(page):
    """A wanted-off placement Meta keeps locked ON is a failure that names it."""
    _load_b(page, locked=["Facebook Business Explore", "Threads feed"])
    report: dict = {}
    assert md.fill_placements(page, report) is False
    assert "Threads feed" in report["subitems_missing"]
    assert _state(page)["Threads feed"] is True


def test_layout_b_an_unrendered_list_is_not_a_false_not_offered(page):
    """The Stories list never gets its rows: nothing is called "not offered"."""
    _load_b(page)
    page.evaluate(r"""() => {
      const c = window.__sim.cats['Stories, Status, Reels'];
      const btn = c.btn.cloneNode(true);
      c.btn.replaceWith(btn);
      btn.addEventListener('click', () => {
        const folded = btn.getAttribute('aria-label') === 'Collapse';
        btn.setAttribute('aria-label', folded ? 'Expand' : 'Collapse');
      });
    }""")
    report: dict = {}
    assert md.fill_placements(page, report) is False
    assert "subitems_not_offered" not in report
    assert "Instagram Stories" in report["subitems_missing"]


def test_a_slow_first_read_does_not_spend_the_whole_wait(page, monkeypatch):
    """b359855fb534 (B91, 07:25): `reads: 1, waited_s: 10.8` — the page answered
    the first read so late that the 6 s budget was gone before a second read, the
    list counted as "not settled" and "Messenger Stories" as missing. The budget
    is counted from the first answer."""
    slow = md._CATEGORY_END_JS.replace(
        "(name) => {",
        "(name) => { if (!window.__slow) { window.__slow = 1; const t = Date.now();"
        " while (Date.now() - t < 2200) {} }", 1)
    assert slow != md._CATEGORY_END_JS
    monkeypatch.setattr(md, "_CATEGORY_END_JS", slow)
    monkeypatch.setattr(md, "_SUBITEMS_SETTLE_MAX_S", 2.0)
    _load_b(page)
    report: dict = {}
    assert md.fill_placements(page, report) is True, report.get("not_offered_check")
    assert report["subitems_not_offered"] == ["Messenger Stories"]
    assert report["not_offered_check"]["Stories, Status, Reels"]["reads"] >= 2


# ── layout 2: the card is collapsed and its body hidden ───────────────────────

#: The card and its account-level dialog, as the page behaves. A click on the
#: card (either button of the pair) opens it: `aria-pressed=true`, the hidden
#: body is shown. "Edit placement controls" opens the dialog of the account
#: controls; Review changes → Confirm saves: the switch stays on and the four
#: boxes stay off for the next opening. `cfg.summary` — "live": the card's
#: "Excluded placements" text follows the save; "stale": it keeps saying None
#: (what the capture's run saw); "none_set": only the header summary exists.
_SIM_CARD_JS = r"""
(cfg) => {
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const sec = document.querySelector('[data-surface$="adset_placement_static_pgd_section"]');
  window.__log = [];
  const pair = [...sec.querySelectorAll('[role=button][aria-pressed]')];
  const bodies = [...sec.querySelectorAll('div[hidden]')];
  const open = () => {
    if (pair[0].getAttribute('aria-pressed') === 'true') return;
    window.__log.push('card_opened');
    for (const b of pair) b.setAttribute('aria-pressed', 'true');
    for (const b of bodies) b.hidden = false;
  };
  if (cfg.overlay) {
    // A control that covers the whole card: a hit-test point lands on it, not on the card.
    sec.style.position = 'relative';
    const ov = document.createElement('button');
    ov.style.cssText = 'position:absolute;left:0;top:0;right:0;bottom:0;z-index:50;opacity:0';
    ov.addEventListener('click', () => window.__log.push('inner_clicked'));
    sec.appendChild(ov);
  }
  if (cfg.cardClickable) for (const b of pair) b.addEventListener('click', open);
  const summary = [...sec.querySelectorAll('span')].find(e => norm(e.textContent) === 'None');
  const header = [...sec.querySelectorAll('span')].find(e => norm(e.textContent) === 'None set');
  const edit = [...sec.querySelectorAll('[role=button]')].find(e => norm(e.textContent) === 'Edit placement controls');
  if (cfg.noEdit) edit.remove();
  const dlg = document.createElement('div');
  dlg.innerHTML = `
  <div role="dialog" id="dlg" style="display:none">
    <div>Placement controls for this ad account</div>
    <label>My business can only advertise on specific placements
      <input type="checkbox" role="switch" aria-label="My business can only advertise on specific placements" id="sw"></label>
    <div id="list" style="display:none">
      <div role="button" aria-expanded="true">Apps and sites</div>
      <div><div><span>Audience Network native, banner and interstitial</span></div><input type="checkbox" checked></div>
      <div><div><span>Audience Network rewarded videos</span></div><input type="checkbox" checked></div>
      <div role="button" aria-expanded="true">Feeds</div>
      <div><div><span>Facebook Marketplace</span></div><input type="checkbox" checked></div>
      <div><div><span>Facebook right column</span></div><input type="checkbox" checked></div>
    </div>
    <div role="button" id="cancel">Cancel</div>
    <div role="button" id="review">Review changes</div>
  </div>
  <div role="dialog" id="rev" style="display:none"><div>Review changes</div><div role="button" id="confirm">Confirm</div></div>`;
  document.body.appendChild(dlg);
  const $ = (id) => document.getElementById(id);
  const hide = (id) => { $(id).style.display = 'none'; };
  $('sw').addEventListener('change', () => { $('list').style.display = $('sw').checked ? 'block' : 'none'; });
  $('cancel').addEventListener('click', () => { window.__log.push('cancel'); hide('dlg'); });
  $('review').addEventListener('click', () => { window.__log.push('review'); hide('dlg'); $('rev').style.display = 'block'; });
  $('confirm').addEventListener('click', () => {
    window.__log.push('confirm'); hide('rev');
    if (cfg.switchLost) { window.__log.push('switch_lost'); $('sw').checked = false; $('list').style.display = 'none'; return; }
    if (cfg.dropBox) {
      const sp = [...$('list').querySelectorAll('span')].find(x => x.textContent === cfg.dropBox);
      sp.parentElement.parentElement.remove();
    }
    if (cfg.saveFails) { window.__log.push('save_lost'); $('sw').checked = false; $('list').style.display = 'none';
      for (const c of $('list').querySelectorAll('input')) c.checked = true; return; }
    const names = [...$('list').querySelectorAll('span')].map(x => x.textContent).join(', ');
    if (cfg.summary === 'live') {
      summary.textContent = names; header.textContent = '4 excluded';
    }
  });
  if (edit) edit.addEventListener('click', () => {
    window.__log.push('edit');
    if (!edit.closest('[hidden]')) $('dlg').style.display = 'block';
  });
}
"""


def _load_card(page, **kw) -> None:
    cfg = {"cardClickable": True, "summary": "stale", "noEdit": False, "saveFails": False,
           "switchLost": False, "dropBox": "", "overlay": False}
    cfg.update(kw)
    page.set_content("<html><body style='margin:0;padding-top:120px'>"
                     f"{fragment(COLLAPSED, 'card')}</body></html>")
    page.evaluate(_SIM_CARD_JS, cfg)


def _rows(tmp_path):
    f = tmp_path / "telemetry.jsonl"
    return [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines() if x.strip()] if f.exists() else []


def test_the_collapsed_card_is_the_captured_one(page):
    _load_card(page)
    got = page.evaluate(r"""() => {
      const sec = document.querySelector('[data-surface$="adset_placement_static_pgd_section"]');
      return { pressed: [...sec.querySelectorAll('[role=button][aria-pressed]')].map(b => b.getAttribute('aria-pressed')),
               hidden: sec.querySelectorAll('div[hidden]').length,
               leaf: [...sec.querySelectorAll('[role=heading]')].map(h => h.textContent.replace(/\s+/g, ' ').slice(0, 40)) };
    }""")
    assert got["pressed"] == ["false", "false"] and got["hidden"] >= 1, got
    assert "Account controls" in page.inner_text("body")


def test_a_collapsed_card_on_the_standalone_editor_is_opened_first(page):
    """The standalone editor shows the same collapsed Placements card as the
    single-screen editor: the step opens it (clicks its header) before it looks
    for the categories."""
    _load_card(page)
    report: dict = {}
    md.fill_placements(page, report)
    assert "card_opened" in page.evaluate("window.__log")
    assert report["placements_card_open"] == "opened", report.get("placements_card_open")
    assert page.evaluate("document.querySelector('[role=button][aria-pressed]').getAttribute('aria-pressed')") == "true"


def test_a_card_that_does_not_open_is_named(page):
    _load_card(page, cardClickable=False)
    report: dict = {}
    assert md.fill_placements(page, report) is False
    assert report["placements_card_open"] == "failed"
    assert report["why"] == "placements_card_not_opened"
    assert "could not be opened" in report["notice"]


def test_exclusions_are_applied_and_the_card_summary_confirms(page, tmp_path):
    _load_card(page, summary="live")
    report: dict = {}
    assert md.fill_placements(page, report) is False        # manual placements are not offered on this card
    assert report["account_exclusions_ok"] is True
    assert report["account_exclusions"] == "excluded"
    assert page.evaluate("window.__log")[:4] == ["card_opened", "edit", "review", "confirm"]
    assert report["why"] == "placement_categories_not_reached"
    row = [r for r in _rows(tmp_path) if r["step"] == "account_placement_exclusions"][-1]
    assert row["ok"] is True


def test_exclusions_are_verified_by_reopening_when_the_card_summary_is_stale(page, tmp_path):
    """ab33293af6d7: four exclusions changed and confirmed, the card still read
    "None" (`summary_still_none`). The robot reopens the account dialog and reads
    the switch and the four boxes back; that is the proof, not the card text."""
    _load_card(page, summary="stale")
    report: dict = {}
    assert md.fill_placements(page, report) is False
    assert report["account_exclusions_ok"] is True, report.get("account_exclusions")
    assert report["account_exclusions"] == "excluded_verified_in_dialog"
    log = page.evaluate("window.__log")
    assert log.count("edit") == 2 and log[-1] == "cancel", log     # reopened, nothing changed, cancelled
    assert "Excluded placements" not in (report.get("notice") or "")
    row = [r for r in _rows(tmp_path) if r["step"] == "account_placement_exclusions"][-1]
    assert row["ok"] is True and row["note"] == "excluded_verified_in_dialog"


def test_exclusions_that_did_not_stick_are_a_failure(page):
    """The save is lost (the dialog reads switch off, boxes on): not verified."""
    _load_card(page, summary="stale", saveFails=True)
    report: dict = {}
    assert md.fill_placements(page, report) is False
    assert report["account_exclusions_ok"] is False
    assert report["account_exclusions"] == "summary_still_none"
    assert "were not applied" in report["notice"]


def test_account_exclusions_that_meta_does_not_offer_are_a_warning(page, tmp_path):
    """No "Edit placement controls" on the card: said so, in the report and in
    the notice that blocks the draft — not a silent pass."""
    _load_card(page, noEdit=True)
    report: dict = {}
    assert md.fill_placements(page, report) is False
    assert report["account_exclusions_ok"] is False
    assert report["account_exclusions"] == "account_controls_not_offered"
    assert "no longer offers" in report["account_exclusions_warning"]
    assert "account-level placement exclusions" in report["notice"]
    assert "no longer offers" in report["notice"]
    row = [r for r in _rows(tmp_path) if r["step"] == "account_placement_exclusions"][-1]
    assert row["ok"] is False and row["note"] == "account_controls_not_offered"


def test_the_9403a_notice_says_meta_no_longer_offers_exclusions(page):
    """Review of PR #636 (F2): the opened 9403A card has no categories and shows
    Meta's notice that excluding placements "will no longer be available for your
    ad sets". The blocking reason says that (ACL-1336), not "categories under
    Show more settings"."""
    _load_card(page, summary="live")
    report: dict = {}
    assert md.fill_placements(page, report) is False
    assert report["notice"].startswith(
        "Meta no longer offers manual placement exclusions on this ad set (ACL-1336); "
        "placements left as Meta set them."), report["notice"]
    assert "Show more settings" not in report["notice"]
    assert report["why"] == "placement_categories_not_reached"


def test_a_page_error_reading_the_card_is_not_reported_as_not_offered(page, monkeypatch):
    """F5: a page call that throws while the Account controls row is looked for is a
    read error, not "Meta no longer offers exclusions"."""
    _load_card(page)
    real = page.evaluate

    def flaky(script, *a, **k):
        if "account-controls-row" in str(a) or "account-controls-row" in script:
            raise RuntimeError("Execution context was destroyed")
        return real(script, *a, **k)
    monkeypatch.setattr(page, "evaluate", flaky)
    report: dict = {}
    assert md.fill_placements(page, report) is False
    assert report["account_exclusions"] == "account_controls_row_read_error"
    assert "no longer offers" not in report["account_exclusions_warning"]
    assert "Could not read" in report["account_exclusions_warning"]


def test_a_control_over_the_card_header_is_not_clicked(page, tmp_path):
    """The card-open click guard: the point in the heading is covered by another
    control, so no trusted click goes there; the card is opened through its own
    button instead (DOM click)."""
    _load_card(page, overlay=True)
    report: dict = {}
    md.fill_placements(page, report)
    log = page.evaluate("window.__log")
    assert "inner_clicked" not in log, log
    assert report["placements_card_open"] == "opened"
    row = [r for r in _rows(tmp_path) if r["step"] == "placements_card"][-1]
    assert row["diag"]["how"] == "dom", row


def test_exclusions_with_the_switch_lost_are_not_verified(page):
    """The four boxes read off but the dialog's switch is off again: the account is
    not restricted to specific placements, so that is not a verified save."""
    _load_card(page, switchLost=True)
    report: dict = {}
    assert md.fill_placements(page, report) is False
    assert report["account_exclusions_ok"] is False
    assert report["account_exclusions"] == "summary_still_none"


def test_an_exclusion_box_missing_from_the_dialog_is_not_counted_as_excluded(page):
    """After the save the dialog has no row for one of the four placements: it
    cannot be read, so it is not "excluded"."""
    _load_card(page, dropBox="Facebook right column")
    report: dict = {}
    assert md.fill_placements(page, report) is False
    assert report["account_exclusions_ok"] is False


# ── F1/F3 of the review: category boxes disabled while saving, final read-back ─

def _cat_checked(page, name: str) -> bool:
    return page.evaluate(
        "(id) => document.querySelector('#' + id + ' input[type=checkbox]').getAttribute('aria-checked') === 'true'",
        {"Search results": "placement-search", "Apps and sites": "placement-external"}[name])


def test_a_category_box_disabled_while_meta_saves_is_clicked_again_when_free(page, monkeypatch):
    """Review F1 (11 of 29 live failures): "Search results" stays disabled and still
    on for ~2.5 s after it is clicked off, then is free - and still on (the click
    was lost). The step reads it again and clicks it off once it is enabled."""
    monkeypatch.setattr(md, "_PLACEMENTS_RECONCILE_MAX_S", 6.0)
    _load_b(page, catHold={"Search results": {"ms": 2500, "mode": "revert"}})
    report: dict = {}
    assert md.fill_placements(page, report) is True, {
        k: report.get(k) for k in ("categories_failed", "categories", "final_readback")}
    assert _cat_checked(page, "Search results") is False
    assert "Search results" in report["categories_reconciled"]
    assert report["categories_reclicked"]["Search results"] == 1
    _assert_wanted(page)


def test_a_category_box_that_settles_on_its_own_passes(page, monkeypatch):
    """Meta keeps the box disabled and on while saving, then shows it off: no more clicks."""
    monkeypatch.setattr(md, "_PLACEMENTS_RECONCILE_MAX_S", 6.0)
    _load_b(page, catHold={"Search results": {"ms": 2500, "mode": "off"}})
    report: dict = {}
    assert md.fill_placements(page, report) is True, report.get("categories_failed")
    assert _cat_checked(page, "Search results") is False
    assert "Search results" not in report.get("categories_reclicked", {})


def test_a_category_box_that_stays_disabled_and_checked_fails(page):
    """A box Meta never frees is an honest failure that names it."""
    _load_b(page, catHold={"Search results": {"ms": 10 ** 9, "mode": "revert"}})
    report: dict = {}
    assert md.fill_placements(page, report) is False
    assert report["categories_failed"] == ["Search results"]


def test_a_category_box_the_page_reverts_on_every_click_stops_at_the_cap(page, monkeypatch):
    """The page takes every click back: one honest refusal after the shared cap, no loop."""
    monkeypatch.setattr(md, "_PLACEMENTS_RECONCILE_MAX_S", 6.0)
    _load_b(page, catHold={"Search results": {"ms": 100, "mode": "revert", "again": True}})
    report: dict = {}
    assert md.fill_placements(page, report) is False
    assert report["categories_failed"] == ["Search results"]
    assert report["categories_reclicked"]["Search results"] <= md._PLACEMENT_REOPENS


def test_a_late_revert_by_the_page_fails_the_step(page, monkeypatch):
    """Review F3: after everything is set the page takes one placement back. The final
    read of every row sees it and the step fails."""
    wrapped = ("(label) => { if (label === 'Threads feed' && !window.__rev) {"
               " window.__rev = 1; window.__sim.flip('Threads feed'); }"
               " return (" + md.PLACEMENTS_SUBITEM_READ_JS + ")(label); }")
    monkeypatch.setattr(md, "PLACEMENTS_SUBITEM_READ_JS", wrapped)
    _load_b(page)
    report: dict = {}
    assert md.fill_placements(page, report) is False
    assert {"row": "Threads feed", "want": False, "got": True} in report["final_readback"]["mismatch"]


def test_a_clean_run_has_an_empty_final_readback(page):
    _load_b(page)
    report: dict = {}
    assert md.fill_placements(page, report) is True
    assert report["final_readback"] == {"mismatch": []}


def test_a_category_that_folds_while_the_list_settles_is_opened_again(page, monkeypatch):
    """The "not offered" proof needs the list open: Meta folds it during the wait and
    the settle loop opens it again (without that, "Messenger Stories" stays missing)."""
    wrapped = ("(name) => { if (name === 'Stories, Status, Reels' && !window.__f) {"
               " window.__f = 1; window.__sim.cats[name].btn.click(); }"
               " return (" + md._CATEGORY_END_JS + ")(name); }")
    monkeypatch.setattr(md, "_CATEGORY_END_JS", wrapped)
    _load_b(page)
    report: dict = {}
    assert md.fill_placements(page, report) is True, report.get("not_offered_check")
    assert report["not_offered_check"]["Stories, Status, Reels"]["reopened"] >= 1
    assert report["subitems_not_offered"] == ["Messenger Stories"]


# ── round 2 of the review: partial parents, finish pass, notice scope ─────────

def _box(page, cat_id: str):
    return page.evaluate(
        "(id) => document.querySelector('#' + id + ' input[type=checkbox]').getAttribute('aria-checked')", cat_id)


def test_the_real_b90_end_state_passes_with_partial_parent_boxes(page):
    """R2-1: at the end of a real run Feeds, "Stories, Status, Reels" and In-stream read
    `aria-checked=false` (some of their rows are on, some off). That is the wanted
    end state, and the step passes: those categories are judged by their rows."""
    _load_b(page)
    report: dict = {}
    assert md.fill_placements(page, report) is True, {
        k: report.get(k) for k in ("final_readback", "subitems_missing", "categories_failed")}
    assert [_box(page, i) for i in ("placement-feed", "placement-story", "placement-stream")] == ["false"] * 3
    rows_on = page.evaluate(
        "() => Object.values(window.__sim.state).filter(v => v.checked).length")
    assert 0 < rows_on < len(page.evaluate("() => Object.keys(window.__sim.state)"))
    assert report["final_readback"] == {"mismatch": []}
    _assert_wanted(page)


def test_a_partial_wanted_on_category_is_never_clicked_by_the_finish_pass(page, monkeypatch):
    """R2-2: a click on a partial parent turns all its rows on and undoes the robot's own
    exclusions. After the first row was touched no category box is clicked, even while
    the finish pass runs for another category (Search results held by Meta)."""
    monkeypatch.setattr(md, "_PLACEMENTS_RECONCILE_MAX_S", 6.0)
    _load_b(page, catHold={"Search results": {"ms": 1500, "mode": "revert"}})
    report: dict = {}
    assert md.fill_placements(page, report) is True, report.get("categories_failed")
    late = [c for c in page.evaluate("window.__boxClicks") if c["afterRows"]]
    assert late == [], late
    assert "Search results" in report["categories_reconciled"]
    _assert_wanted(page)


def test_the_acl_1336_notice_is_not_used_where_manual_placements_are_offered(page):
    """R2-3: B90 shows the same Meta notice but still offers manual placements. If the
    categories are missed for another reason (the node never opens), the old message
    is given, not the ACL-1336 one."""
    _load_b(page)
    page.evaluate(r"""() => {
      const node = [...document.querySelectorAll('[role=button][aria-expanded]')]
        .find(b => /^Placements$/.test(b.textContent.replace(/\s+/g, ' ').trim()));
      const dead = node.cloneNode(true);
      node.replaceWith(dead);
      const note = document.createElement('div');
      note.textContent = 'Excluding placements or platforms will no longer be available for your ad sets.';
      dead.parentElement.parentElement.appendChild(note);
    }""")
    report: dict = {}
    assert md.fill_placements(page, report) is False
    assert "ACL-1336" not in report.get("notice", ""), report.get("notice")
    assert "Show more settings" in report["notice"] or "could not be" in report["notice"]
