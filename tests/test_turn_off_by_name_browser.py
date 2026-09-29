"""Turning a campaign off by its exact name on a table without row ids (ACL-1379).

The production flow runs offline against the scrubbed capture of Meta's
id-less campaign table (Doc19, 18.09): five campaigns, "Campaign 1".."5",
1 and 2 switched on. Each test counts every click on the page and which row's
switch received it.
"""
import os
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from playwright.sync_api import sync_playwright

from src.analysis_agent import account_spend as sp

FIXTURE = Path(__file__).parent / "fixtures" / "meta" / "campaign_table_without_row_ids.html"
PERIOD = '<div role="button" aria-label="Date range: Sep 1, 2026">Sep 1, 2026</div>'

# Every click is recorded with the name of the row it landed in; a click on a
# row's own switch flips it the way Meta's optimistic update does.
TRACK = r"""<script>
window.clicks = [];
document.addEventListener('click', e => {
  let r = e.target;
  while (r && !(r.matches && r.matches('[role=presentation]') && r.querySelector('[data-tooltip-content] a'))) r = r.parentElement;
  const tip = r && r.querySelector('[data-tooltip-content]');
  const sw = e.target.closest && e.target.closest('[role=switch]');
  window.clicks.push({row: tip ? tip.getAttribute('data-tooltip-content') : '', sw: !!sw});
  if (sw) sw.setAttribute('aria-checked', sw.getAttribute('aria-checked') === 'true' ? 'false' : 'true');
}, true);
// The capture carries no Meta stylesheet, so its geometry is meaningless for
// hit-testing (a header separator sits on every switch). What is under the
// switch is emulated instead: by default Meta's painted track, the sibling of
// the hidden input (17.09 Doc20), or whatever a test puts there.
window.__hit = 'track';
document.elementFromPoint = () => {
  const sw = document.querySelector('[data-acl-switch]');
  if (!sw) return document.body;
  if (window.__hit === 'dialog') return document.getElementById('dlg');
  if (window.__hit === 'name') return sw.closest('[role=presentation]').querySelector('[data-tooltip-content] a');
  return [...sw.parentElement.children].find(e => e !== sw) || sw;
};
</script><div id="dlg" role="dialog">Publish?</div>"""

# Name a row / set its switch by its current name.
SET_STATE = r"""(states) => {
  for (const tip of document.querySelectorAll('[data-tooltip-content]')) {
    if (!tip.querySelector('a')) continue;
    const name = tip.getAttribute('data-tooltip-content');
    if (!(name in states)) continue;
    const sw = tip.closest('[role=presentation]').querySelector('[role=switch]');
    sw.setAttribute('aria-checked', states[name]);
  }
}"""
RENAME = r"""([from, to]) => {
  const tip = [...document.querySelectorAll('[data-tooltip-content]')]
    .find(t => t.querySelector('a') && t.getAttribute('data-tooltip-content') === from);
  tip.setAttribute('data-tooltip-content', to);
}"""


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=os.environ.get("ACL_BROWSER_EXECUTABLE") or None)
        yield b
        b.close()


@pytest.fixture
def run(browser, monkeypatch, tmp_path):
    monkeypatch.setattr(sp, "_store_path", lambda: tmp_path / "spend.json")
    contexts = []
    table = FIXTURE.read_text(encoding="utf-8")

    def execute(name="Campaign 1", mode=""):
        ctx = browser.new_context()
        contexts.append(ctx)
        ctx.route("**/*", lambda r: r.fulfill(content_type="text/html", body=""))
        pg = ctx.new_page()
        states = {"Campaign 1": "true", "Campaign 2": "true",
                  "Campaign 3": "false", "Campaign 4": "false", "Campaign 5": "false"}

        class Page:
            visits = 0
            clicks: list = []
            closed = False

            @property
            def url(self):
                return pg.url

            def _harvest(self):
                got = pg.evaluate("window.clicks")
                self.clicks = self.clicks + got
                for c in got:
                    if c["sw"] and mode != "reverted":
                        states[c["row"]] = "false" if states[c["row"]] == "true" else "true"

            def goto(self, url, **kwargs):
                if self.visits:
                    self._harvest()
                self.visits += 1
                pg.goto(url)
                html = table
                if mode == "partial":
                    html = html.replace("Results from 5 campaigns", "Results from 7 campaigns")
                if mode == "with-ids":
                    html = html.replace('data-surface="/am/table/spend_sla"',
                                        'data-surface="/am/table/table_row:111unit/table_cell:spend"', 1)
                extra = ""
                if mode == "page-data-duplicate":
                    extra = ('<script type="application/json">{"a":[{"campaign_structure_tree":'
                             '{"campaign_id":"1","name":"Campaign 1","children":[]}},'
                             '{"campaign_structure_tree":{"campaign_id":"2","name":"Campaign 1",'
                             '"children":[]}}]}</script>')
                pg.set_content(PERIOD + html + extra + TRACK)
                pg.evaluate(SET_STATE, states)
                if mode == "duplicate":
                    pg.evaluate(RENAME, ["Campaign 2", "Campaign 1"])
                if mode in ("covered-by-dialog", "covered-by-name"):
                    pg.evaluate("m => { window.__hit = m; }", mode.split("-")[-1])
                if mode == "disabled":
                    pg.evaluate("""() => document.querySelectorAll('[role=switch]')
                                     .forEach(s => s.setAttribute('aria-disabled', 'true'))""")

            def evaluate(self, js, arg=None):
                if js == sp._BY_NAME_ACTION_JS:
                    if mode == "rename-before-click":
                        pg.evaluate(RENAME, ["Campaign 1", "Campaign 1 (copy)"])
                    if mode == "swap-before-click":
                        pg.evaluate(RENAME, ["Campaign 1", "tmp"])
                        pg.evaluate(RENAME, ["Campaign 2", "Campaign 1"])
                        pg.evaluate(RENAME, ["tmp", "Campaign 2"])
                    if mode == "rerender-before-click":
                        pg.evaluate("""() => {
                          const r = document.querySelector('[data-acl-row]');
                          const c = r.cloneNode(true);
                          c.removeAttribute('data-acl-row');
                          c.querySelectorAll('[data-acl-switch]').forEach(e => e.removeAttribute('data-acl-switch'));
                          r.replaceWith(c);
                        }""")
                    if mode == "off-before-click":
                        pg.evaluate("""() => document.querySelector('[data-acl-switch]')
                                         .setAttribute('aria-checked', 'false')""")
                    if mode == "period-before-click":
                        pg.locator('[aria-label^="Date range"]').evaluate(
                            "e => e.setAttribute('aria-label', 'Date range: Sep 2, 2026')")
                return pg.evaluate(js, arg)

            def locator(self, *args):
                return pg.locator(*args)

            def wait_for_timeout(self, ms):
                pg.wait_for_timeout(1)

            def close(self):
                self.closed = True
                self._harvest()
                pg.close()

        page = Page()
        monkeypatch.setattr(sp, "_entry_for", lambda _: NS(ad_account_id="123", profile_id="p1"))
        monkeypatch.setattr(sp, "_attach_only", lambda *a: NS(page=page, close=lambda **kw: None))
        monkeypatch.setattr(sp, "_wait_for_grid", lambda _: True)
        monkeypatch.setattr(sp, "TURN_OFF_TRIES", 3)
        result = sp._turn_off("FacebookDoc19", "campaign", "", expected_name=name,
                              date_from="2026-09-01", match="name")
        assert page.closed
        return result, page

    yield execute
    for ctx in contexts:
        ctx.close()


def test_a_unique_name_turns_off_exactly_that_row(run):
    result, page = run()
    assert result.state == "off", result
    assert result.matched_by == "name" and result.object_id == "" and result.name == "Campaign 1"
    assert result.delivery == "Active"
    assert page.clicks == [{"row": "Campaign 1", "sw": True}], "pressed something else"
    assert page.visits == 2, "no confirmation after reload"


def test_whitespace_in_the_requested_name_is_normalised(run):
    result, page = run("  Campaign   1 ")
    assert result.state == "off" and page.clicks == [{"row": "Campaign 1", "sw": True}]


@pytest.mark.parametrize("name,mode,words", [
    ("Campaign 1", "duplicate", "2 campaigns in the table have this exact name"),
    ("Campaign 9", "", "no campaign with this exact name"),
    ("campaign 1", "", "no campaign with this exact name"),  # exact, not case-folded
    ("Campaign 1", "partial", "only 5 of 7 campaigns are on screen"),
    ("Campaign 1", "with-ids", "shows row ids"),
    ("Campaign 1", "page-data-duplicate", "more than one campaign has this exact name"),
    ("Campaign 1", "disabled", "switch is disabled"),
    ("Campaign 1", "rename-before-click", "the row changed before action"),
    ("Campaign 1", "swap-before-click", "the row changed before action"),
    ("Campaign 1", "rerender-before-click", "the row changed before action"),
    ("Campaign 1", "period-before-click", "period changed before action"),
    ("Campaign 1", "covered-by-dialog", "switch is covered"),
    ("Campaign 1", "covered-by-name", "switch is covered"),
])
def test_an_unsure_target_is_refused_and_nothing_is_pressed(run, name, mode, words):
    result, page = run(name, mode)
    assert result.state == "failed", result
    assert words in result.error, result.error
    assert page.clicks == [], "something was pressed"
    assert page.visits == 1


@pytest.mark.parametrize("name,mode", [("Campaign 3", ""), ("Campaign 1", "off-before-click")])
def test_an_off_switch_is_reported_and_never_pressed(run, name, mode):
    result, page = run(name, mode)
    assert result.state == "already_off", result
    assert page.clicks == []


def test_a_click_that_meta_did_not_keep_is_not_success(run):
    result, page = run("Campaign 1", "reverted")
    assert page.clicks == [{"row": "Campaign 1", "sw": True}]
    assert result.state == "failed" and "on again" in result.error


def test_the_switch_is_not_covered_in_the_recorded_layout(browser):
    """The real capture: the own-switch check must accept the recorded markup."""
    p = browser.new_page()
    p.route("**/*", lambda r: r.abort())
    p.set_content(PERIOD + FIXTURE.read_text(encoding="utf-8"))
    found = p.evaluate(sp._FIND_BY_NAME_JS, {"name": "Campaign 2", "kind": "CAMPAIGN_GROUP"})
    assert found["found"] and found["on"] and found["rows"] == 5
    assert p.evaluate("() => document.querySelectorAll('[data-acl-row]').length") == 1
    p.close()


# ── outside the browser ────────────────────────────────────────────────

@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(sp, "_store_path", lambda: tmp_path / "spend.json")
    monkeypatch.setattr(sp, "_entry_for", lambda _: NS(ad_account_id="123", profile_id="p1"))
    return tmp_path


def test_ads_without_ids_are_not_turned_off_by_name(store, monkeypatch):
    monkeypatch.setattr(sp, "_attach_only", lambda *a: pytest.fail("the browser was touched"))
    out = sp._turn_off("FacebookDoc19", "ad", "", parent_id="1", expected_name="Ad 1", match="name")
    assert out.state == "failed" and "Ads on an account without ids" in out.error


def test_a_missing_id_without_an_explicit_match_is_not_a_name_search(store, monkeypatch):
    monkeypatch.setattr(sp, "_attach_only", lambda *a: pytest.fail("the browser was touched"))
    out = sp._turn_off("FacebookDoc19", "campaign", "", expected_name="Campaign 1")
    assert out.state == "failed" and "without campaign ids" in out.error


def test_a_busy_profile_is_refused_before_the_browser(store, monkeypatch):
    monkeypatch.setattr(sp, "_turn_off", lambda *a, **k: pytest.fail("the browser was touched"))
    asked = []
    out = sp.turn_off("FacebookDoc19", "campaign", "", expected_name="Campaign 1", match="name",
                      profile_busy=lambda key: asked.append(key) or True)
    assert asked == ["p1"]
    assert out.state == "failed" and out.error == sp.PROFILE_BUSY_ERROR
    assert sp.turn_off_history()[0]["error"] == sp.PROFILE_BUSY_ERROR


def test_an_unanswerable_busy_check_counts_as_busy(store, monkeypatch):
    monkeypatch.setattr(sp, "_turn_off", lambda *a, **k: pytest.fail("the browser was touched"))

    def boom(key):
        raise RuntimeError("no answer")

    out = sp.turn_off("FacebookDoc19", "campaign", "", expected_name="Campaign 1", match="name",
                      profile_busy=boom)
    assert out.error == sp.PROFILE_BUSY_ERROR


def test_a_confirmed_turn_off_by_name_marks_only_that_saved_row(store, monkeypatch):
    objects = [
        {"level": "campaign", "id": "", "name": "Campaign 1", "switch": "on", "delivery": "Active"},
        {"level": "campaign", "id": "", "name": "Campaign 2", "switch": "on", "delivery": "Active"},
        {"level": "ad", "id": "", "name": "Campaign 1", "switch": "on", "delivery": "Active"},
    ]
    sp._save_all({"FacebookDoc19|2026-09-01|2026-09-01": {"objects": objects}})
    monkeypatch.setattr(sp, "_turn_off", lambda *a, **k: sp.TurnOffResult(
        account="FacebookDoc19", level="campaign", object_id="", name="Campaign 1",
        state="off", delivery="Processing", matched_by="name"))
    sp.turn_off("FacebookDoc19", "campaign", "", expected_name="Campaign 1", match="name",
                profile_busy=lambda key: False)
    saved = sp._load_all()["FacebookDoc19|2026-09-01|2026-09-01"]["objects"]
    assert [o["switch"] for o in saved] == ["off", "on", "on"]
    assert saved[0]["delivery"] == "Processing"


def test_a_shared_saved_name_is_not_marked(store):
    objects = [{"level": "campaign", "id": "", "name": "Campaign 1", "switch": "on"},
               {"level": "campaign", "id": "", "name": "Campaign 1", "switch": "on"}]
    sp._save_all({"FacebookDoc19|2026-09-01|2026-09-01": {"objects": objects}})
    assert sp._mark_object_off_by_name("FacebookDoc19", "campaign", "Campaign 1") == 0
    assert [o["switch"] for o in sp._load_all()["FacebookDoc19|2026-09-01|2026-09-01"]["objects"]] == ["on", "on"]
