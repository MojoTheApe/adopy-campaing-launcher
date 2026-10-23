"""ACL-1822 в браузере: страница Rules (Decision flow в Split) и ячейка Rules в таблице кампаний.

Настоящие шаблоны и настоящие обработчики `rules_api`; хранилище правил во временной папке, Meta и
Multilogin не трогаются (команды идут в `FakeMeta` из теста ACL-1800). Тест пропускается, если свободной
памяти меньше 6 ГБ, кроме запуска со снимками экрана (`ACL_SHOTS_DIR`): их владелец просит явно.
Имена снимков: `rules-flow__<состояние>__<тема>__built.png`.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

psutil = pytest.importorskip("psutil")
pytest.importorskip("playwright.sync_api")
SHOTS = os.environ.get("ACL_SHOTS_DIR", "")
if psutil.virtual_memory().available < 6 * 2**30 and not SHOTS:
    pytest.skip("browser tests need more than 6 GB of free RAM", allow_module_level=True)

from playwright.sync_api import sync_playwright  # noqa: E402

from src.analysis_agent import campaign_inventory as ci  # noqa: E402
from src.analysis_agent.rules.store import RulesStore  # noqa: E402
from src.web import campaign_commands, rules_api  # noqa: E402
from tests._retry_once import goto, launch_chromium  # noqa: E402

OWNER = "Buyer One"
STATIC = ROOT / "src" / "web" / "static"
CAMPS = ["120000000000011", "120000000000012"]
SETS = [f"1200000000001{i}" for i in range(1, 7)]
SEED = [{"country": "Mexico", "accounts": ["MX Account 1", "MX Account 2"]},
        {"country": "Chile", "accounts": ["CL Account 1"]}]


def shot(page, name, theme):
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(SHOTS) / f"rules-flow__{name}__{theme}__built.png"), full_page=True)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("ACL_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("ACL_FB_BACKEND", "api")
    monkeypatch.setenv("ACL_BIND_HOST", "127.0.0.1")
    monkeypatch.setattr(ci, "_store_path", lambda: tmp_path / "campaign_inventory.json")
    ci._RUNNING.clear()
    return RulesStore(tmp_path)


def seed_snapshot(account="MX Account 1"):
    snap = ci.Snapshot(account=account, state="ok", currency="USD", finished_at="2026-10-12 07:00:00",
                       finished_at_utc="2026-10-12T07:00:00+00:00")
    snap.campaigns, _ = ci.parse_inventory([
        {"id": CAMPS[0], "name": "MX_Video_Broad_35 01.10", "status": "ACTIVE", "effective_status": "ACTIVE"},
        {"id": CAMPS[1], "name": "MX_Video_Lookalike 05.10", "status": "PAUSED", "effective_status": "PAUSED"}], "campaign")
    snap.adsets, _ = ci.parse_inventory(
        [{"id": s, "name": f"MX_Video_Broad_35_{i + 1:02d}.10", "status": "ACTIVE", "effective_status": "ACTIVE",
          "campaign_id": CAMPS[0 if i < 4 else 1]} for i, s in enumerate(SETS)], "adset")
    snap.ads, snap.insights = [], {"campaign": {}, "adset": {}, "ad": {}}
    snap.steps = {s: "ok" for s in ci.STEPS}
    return snap


def make_rules(st, count=6):
    """Правило Suggest с шестью ad set, одно предложение, одно блокированное, одно без совпадения."""
    rule = st.create_from_preset("stop_loss_new_tests", OWNER, by=OWNER)
    objs = [{"account": "MX Account 1", "level": "adset", "id": s, "name_at_assign": f"MX_Video_Broad_35_{i + 1:02d}.10"}
            for i, s in enumerate(SETS[:count])]
    st.edit_rule(rule["id"], {"name": "Protect LATAM tests", "scope": {"accounts": ["MX Account 1"], "objects": objs,
                                                                         "country": "Mexico"}}, by=OWNER)
    st.set_mode(rule["id"], "suggest", by=OWNER)
    scale = st.create_from_preset("scale_proven", OWNER, by=OWNER)
    st.set_mode(scale["id"], "observe", by=OWNER)
    return rule["id"], scale["id"]


def evaluate(st, rule_id, *, spend=(620.0, 560.0, 510.0, 430.0), blocked=None, no_target=False, stale=False):
    rule = st.get_rule(rule_id)
    version = st.current_version(rule)
    objects = []
    for i, o in enumerate(version["scope"]["objects"]):
        verdict, reasons = ("matched", []) if i < 3 else ("not_matched", [])
        action = {"kind": "pause", "level": "adset", "id": o["id"], "account": o["account"],
                  "requested": {"status": "PAUSED"}, "before": {"status": "ACTIVE"}} if verdict == "matched" else None
        identity = "exact"
        if blocked and i == blocked:
            verdict, reasons, action, identity = "blocked", ["Ambiguous mapping"], None, "duplicate_name"
        if no_target:
            verdict, reasons, action = "blocked", ["No target"], None
        objects.append({"key": f"adset:{o['id']}", "account": o["account"], "level": "adset", "id": o["id"],
                        "name": o["name_at_assign"], "identity": identity, "verdict": verdict, "reasons": reasons,
                        "notes": ["2 ad sets are named like this one"] if reasons == ["Ambiguous mapping"] else [],
                        "conditions_matched": None if reasons else verdict == "matched",
                        "observed": {"spend": spend[min(i, 3)], "ftd": 0, "leads": 12, "target": None if no_target else 60.0},
                        "action": action})
    return st.append_evaluation({
        "trigger": "manual", "rule_id": rule_id, "version": version["version"], "mode": rule["mode"], "strategy": 1,
        "verdict": "blocked" if no_target else "matched", "reasons": ["No target"] if no_target else (["Stale BI"] if stale else []),
        "counts": {}, "bi_through": "2026-10-11",
        "bi_built_at": "2026-10-09T08:00:00+00:00" if stale else "2026-10-12T08:00:00+00:00",
        "snapshot_at": {"MX Account 1": "2026-10-12T07:00:00+00:00"},
        "T": {"value": None if no_target else 60.0, "source": None if no_target else "goal"}, "objects": objects})


def propose_all(st, rule_id):
    rule = st.get_rule(rule_id)
    out = []
    for o in st.current_version(rule)["scope"]["objects"][:3]:
        out.append(st.append_action({
            "state": "proposed", "rule_id": rule_id, "version": rule["current_version"], "account": o["account"],
            "level": "adset", "id": o["id"], "kind": "pause", "before": {"status": "ACTIVE"},
            "requested": {"status": "PAUSED"}, "dedup_key": f"k{o['id']}"}))
    return out


class FakeEngine:
    def __init__(self, st, rule_id):
        self.st, self.rule_id, self.calls = st, rule_id, []

    def enqueue(self, trigger, scope=None):
        self.calls.append((trigger, scope))
        evaluate(self.st, self.rule_id)
        return True

    def status(self):
        return {"queued": 0, "busy": False, "worker": True}


class Stand:
    """Страница-стенд: HTML настоящего шаблона и маршруты `/rules/*` на настоящем `rules_api`."""

    def __init__(self, st, engine=None, live=True, theme="light"):
        self.st, self.engine, self.live, self.theme = st, engine, live, theme
        self.requests, self.fail_overview = [], False

    def html(self):
        from src.web.app import templates

        body = templates.env.get_template("_rules_page.html").render(campaign_table_api=lambda: self.live, rl_seed=SEED)
        dark = "#111720" if self.theme == "dark" else "#f4f5f7"
        return (f"<!doctype html><html data-theme='{self.theme}'><head><meta charset='utf-8'>"
                f"<style>body{{margin:0;padding:24px 32px;background:{dark};font-family:system-ui}}"
                "#buyer-select{position:absolute;left:-9999px}</style></head><body>"
                "<span id='rl-nav-badge' hidden></span>"
                f"<select id='buyer-select'><option value='{OWNER}'>{OWNER}</option></select>{body}</body></html>")

    def handle(self, route):
        req = route.request
        u = urlparse(req.url)
        self.requests.append((req.method, u.path))
        if u.path == "/":
            return route.fulfill(status=200, content_type="text/html", body=self.html())
        if u.path.startswith("/static/"):
            f = STATIC / u.path.rsplit("/", 1)[1]
            return route.fulfill(status=200, content_type="application/javascript", body=f.read_text(encoding="utf-8"))
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        body = json.loads(req.post_data or "{}") if req.method == "POST" else {}
        st = self.st
        table = {
            ("GET", "/rules/overview"): lambda: rules_api.overview(q.get("owner", ""), store=st, engine_status=lambda: {}),
            ("GET", "/rules/activity"): lambda: rules_api.activity(q.get("owner", ""), store=st),
            ("GET", "/rules/scope-options"): lambda: rules_api.scope_options(q.get("account", "")),
            ("POST", "/rules/save"): lambda: rules_api.save_rule(body, store=st),
            ("POST", "/rules/mode"): lambda: rules_api.set_rule_mode(body, store=st),
            ("POST", "/rules/duplicate"): lambda: rules_api.duplicate_rule(body, store=st),
            ("POST", "/rules/dismiss"): lambda: rules_api.dismiss_proposal(body, store=st),
            ("POST", "/rules/action-result"): lambda: rules_api.record_action_result(body, store=st),
            ("POST", "/rules/evaluate-now"): lambda: rules_api.evaluate_now(body, engine=self.engine),
        }
        fn = table.get((req.method, u.path))
        if fn is None:
            return route.fulfill(status=404, body="{}")
        if u.path == "/rules/overview" and self.fail_overview:
            return route.fulfill(status=500, content_type="application/json",
                                 body=json.dumps({"ok": False, "error": "The rules store could not be read."}))
        data, code = fn()
        return route.fulfill(status=code, content_type="application/json", body=json.dumps(data))


@pytest.fixture
def open_page(env, monkeypatch):
    state = {}

    def opener(stand, start=True, width=1280, rule=None):
        if not state:   # один браузер на тест: второй sync_playwright в том же потоке не стартует
            pw = sync_playwright().start()
            state.update(pw=pw, browser=launch_chromium(pw))
        pg = state["browser"].new_page(viewport={"width": width, "height": 900})
        pg.route("http://acl.test/**", stand.handle)
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.errors = errors
        goto(pg, "http://acl.test/")
        if start:
            pg.evaluate("window.aclRulesPage.start()")
            pg.wait_for_function("window.aclRulesPage.state.status !== 'loading'")
            if rule:   # правила создаются в одну секунду, порядок в списке не задан: выбираем явно
                pg.click(f'.rl-item[data-rule="{rule}"]')
        return pg

    yield opener
    if state:
        state["browser"].close()
        state["pw"].stop()


def no_page_scroll(page):
    return page.evaluate("document.documentElement.scrollWidth <= innerWidth")


def filled(env, **kw):
    rid, scale = make_rules(env)
    evaluate(env, rid, **kw)
    propose_all(env, rid)
    return rid, scale


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_filled_page_shows_the_decision_flow_and_never_scrolls_sideways(env, open_page, theme):
    rid, _ = filled(env)
    page = open_page(Stand(env, theme=theme), rule=rid)
    assert page.locator(".rl-item").count() == 2
    assert page.inner_text("#rl-count") == "2"
    text = page.inner_text("#rl-editor")
    assert page.input_value("#rl-name") == "Protect LATAM tests"
    for needle in ("Evidence basis", "Why would this rule act?", "01 · Scope", "02 · Evidence",
                   "03 · Conditions", "04 · Action", "6 ad sets", "Condition match is not execution permission"):
        assert needle in text, needle
    counts = page.locator(".rl-count b").all_inner_texts()
    assert counts == ["6", "3", "3"]
    assert "Spend ≥ 3× T ($180.00) AND FTDs = 0" in page.inner_text(".rl-sentence")
    assert page.locator(".rl-table tbody tr").count() == 6
    assert "$620.00" in page.inner_text(".rl-table")
    assert no_page_scroll(page)
    shot(page, "filled", theme)
    assert page.errors == []


def test_modes_automatic_is_disabled_with_the_reason_and_cohort_lifetime_are_off(env, open_page):
    rid, _ = filled(env)
    page = open_page(Stand(env), rule=rid)
    auto = page.locator('input[name="rl-mode"][value="automatic"]')
    assert auto.is_disabled()
    assert "needs owner settings" in page.inner_text(".rl-mode.off")
    assert page.locator('.rl-seg:has-text("Cohort")').is_disabled() and page.locator('.rl-seg:has-text("Lifetime")').is_disabled()
    assert page.locator('input[name="rl-mode"][value="suggest"]').is_checked()


def test_save_makes_a_new_version_and_never_activates_anything(env, open_page):
    rid, _ = filled(env)
    page = open_page(Stand(env), rule=rid)
    before = env.get_rule(rid)["current_version"]
    page.fill('[data-val="0.0"]', "4")
    assert "Unsaved changes" in page.inner_text(".rl-acts")
    assert page.locator("#rl-eval").is_disabled()
    page.click("#rl-save")
    page.wait_for_selector("text=Saved")
    rule = env.get_rule(rid)
    assert rule["current_version"] == before + 1 and rule["mode"] == "suggest" and rule["grant"] is None
    assert env.current_version(rule)["conditions"][0]["items"][0]["value"] == 4
    assert "≥ 4× T" in page.inner_text("#rl-sentence")


def test_mode_change_saves_only_observe_or_suggest(env, open_page):
    rid, _ = filled(env)
    page = open_page(Stand(env), rule=rid)
    page.check('input[name="rl-mode"][value="observe"]')
    page.click("#rl-save")
    page.wait_for_selector("text=Saved")
    assert env.get_rule(rid)["mode"] == "observe"
    page.click("#rl-toggle")
    page.wait_for_function("document.querySelector('#rl-toggle').textContent === 'Enable'")
    assert env.get_rule(rid)["mode"] == "disabled"


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_create_from_template_duplicate_and_toggle(env, open_page, theme):
    filled(env)
    page = open_page(Stand(env, theme=theme))
    page.click("#rl-templates")      # кнопка открывает вкладку Templates; карточка - диалог создания
    page.wait_for_selector("#rl-tab-templates [data-use]")
    page.click('#rl-tab-templates [data-use="stop_loss_new_tests"]')
    page.wait_for_selector("dialog.rl-dialog[open]")
    assert "Stop-loss · new tests" in page.inner_text("#rl-tpls") and "Scale proven" in page.inner_text("#rl-tpls")
    assert "Disabled" in page.inner_text("#rl-dlg-new")
    shot(page, "templates-dialog", theme)
    page.click('#rl-tpls [data-tpl="scale_proven"]')
    assert page.input_value("#rl-new-name") == "Scale proven"
    page.fill("#rl-new-name", "My scale")
    page.click("#rl-new-ok")
    page.wait_for_function("document.querySelector('#rl-name') && document.querySelector('#rl-name').value === 'My scale'")
    mine = [r for r in env.list_rules(OWNER) if env.current_version(r)["name"] == "My scale"][0]
    assert mine["mode"] == "disabled"
    page.click("#rl-dup")
    page.wait_for_function("document.querySelector('#rl-name').value === 'My scale (copy)'")
    assert page.locator(".rl-item").count() == 4


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_choose_campaigns_reads_the_snapshot_and_saves_the_scope(env, open_page, theme):
    rid, _ = filled(env)
    ci.put_result(seed_snapshot())
    page = open_page(Stand(env, theme=theme), rule=rid)
    page.click("#rl-choose")
    page.wait_for_selector("dialog#rl-dlg-scope[open]")
    page.wait_for_selector("#rl-sc-tree input[data-sel=campaign]")
    assert page.inner_text("#rl-sc-count").startswith("0 campaigns / 6 ad sets")
    shot(page, "scope-dialog", theme)
    page.uncheck(f'#rl-sc-tree input[data-sel=adset][data-id="{SETS[0]}"]')
    page.check(f'#rl-sc-tree input[data-sel=campaign][data-id="{CAMPS[1]}"]')
    assert page.inner_text("#rl-sc-count").startswith("1 campaign / 5 ad sets")
    page.click("#rl-sc-ok")
    assert "Unsaved changes" in page.inner_text(".rl-acts")
    page.click("#rl-save")
    page.wait_for_selector("text=Saved")
    objs = env.current_version(env.get_rule(rid))["scope"]["objects"]
    assert len(objs) == 6 and {o["level"] for o in objs} == {"adset", "campaign"} and all(o["account"] == "MX Account 1" for o in objs)


def test_choose_campaigns_without_a_snapshot_says_read_campaigns_first(env, open_page):
    rid, _ = filled(env)
    page = open_page(Stand(env), rule=rid)
    page.click("#rl-choose")
    page.wait_for_selector("#rl-sc-tree .rl-state")
    assert "Read campaigns first" in page.inner_text("#rl-sc-tree")


def test_evaluate_now_only_queues_and_then_shows_the_new_result(env, open_page):
    rid, _ = make_rules(env)
    engine = FakeEngine(env, rid)
    page = open_page(Stand(env, engine=engine), rule=rid)
    assert "Not evaluated" in page.inner_text(".rl-table")
    page.click("#rl-eval")
    page.wait_for_function("document.querySelector('#rl-evnote').textContent.indexOf('Evaluated') === 0", timeout=15000)
    assert engine.calls == [("manual", rid)]
    assert page.locator(".rl-table tbody tr").count() == 6


def test_suggestion_apply_goes_through_the_confirm_flow_and_dismiss_marks_it(env, open_page):
    rid, _ = filled(env)
    acts = [a for a in env.latest_actions().values() if a["state"] == "proposed"]
    page = open_page(Stand(env), rule=rid)
    page.evaluate("""() => { window.__runs = [];
      window.acmCommands.run = (req) => { window.__runs.push(req); return Promise.resolve({ cancelled: true }); }; }""")
    page.locator("[data-apply]").first.click()
    page.wait_for_function("window.__runs.length === 1")
    run = page.evaluate("window.__runs[0]")
    assert run["command"] == "set_status" and run["value"] == "PAUSED" and run["actor"] == OWNER
    assert all(a["state"] == "proposed" for a in env.latest_actions().values() if a["rule_id"] == rid)
    first = page.locator("[data-dismiss]").first.get_attribute("data-dismiss")
    page.locator("[data-dismiss]").first.click()
    page.wait_for_function("document.querySelectorAll('[data-dismiss]').length === 2")
    assert env.latest_actions()[first]["state"] == "dismissed"
    assert len(acts) == 3


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_empty_error_stale_ambiguous_notarget_and_rpa_states(env, open_page, theme):
    shots = theme
    # пусто
    page = open_page(Stand(env, theme=theme))
    assert "No rules yet" in page.inner_text("#rl-editor") and page.locator("#rl-empty-create").count() == 1
    assert no_page_scroll(page)
    shot(page, "empty", shots)
    # загрузка: страница ещё не стартовала
    page.evaluate("document.getElementById('rl-editor').innerHTML = '<div class=\"rl-state\"><div class=\"rl-skel\" style=\"width:40%\"></div><div class=\"rl-skel\"></div><p class=\"rl-hint\">Loading rules…</p></div>'")
    shot(page, "loading", shots)
    # ошибка
    stand = Stand(env, theme=theme)
    stand.fail_overview = True
    page.close()
    page = open_page(stand)
    assert "Rules are unavailable" in page.inner_text("#rl-editor") and page.locator("#rl-retry").count() == 1
    shot(page, "error", shots)
    stand.fail_overview = False
    rid, _ = make_rules(env)
    evaluate(env, rid, stale=True)
    page.click("#rl-retry")
    page.wait_for_selector(".rl-item")
    page.click(f'.rl-item[data-rule="{rid}"]')
    assert "Stale BI" in page.inner_text("#rl-banner")
    shot(page, "stale-bi", shots)
    evaluate(env, rid, blocked=1)
    page.evaluate("window.aclRulesPage.reload(true)")
    page.wait_for_function("document.querySelector('.rl-card b') && document.body.innerText.indexOf('Ambiguous mapping') >= 0")
    page.select_option("#rl-obj", f"adset:{SETS[1]}")
    assert "Ambiguous mapping" in page.inner_text(".rl-cards")
    assert "Unsaved changes" not in page.inner_text(".rl-acts"), "picking an object to inspect is not an edit"
    shot(page, "ambiguous-mapping", shots)
    evaluate(env, rid, no_target=True)
    page.evaluate("window.aclRulesPage.reload(true)")
    page.wait_for_function("document.body.innerText.indexOf('No CPA goal is set') >= 0")
    shot(page, "no-target", shots)
    assert no_page_scroll(page)
    # rpa
    page.close()
    page = open_page(Stand(env, live=False, theme=theme), start=False)
    assert "Rules need the API backend" in page.inner_text("#rl-rpa") and no_page_scroll(page)
    shot(page, "rpa", shots)
    assert page.errors == []


def test_narrow_widths_keep_the_page_free_of_sideways_scroll(env, open_page):
    rid, _ = filled(env)
    page = open_page(Stand(env), width=1024, rule=rid)
    assert no_page_scroll(page)
    page.set_viewport_size({"width": 390, "height": 800})
    assert no_page_scroll(page)


# ───────────────────────── ячейка Rules в таблице кампаний ─────────────────────────

def test_rules_cell_in_the_campaign_table_opens_the_inspector_and_apply_goes_through_confirm(open_cell_page):
    page, world, st, rid = open_cell_page
    cell = page.locator('tr[data-key="adset:2000002"] [data-rules-open]')
    assert cell.inner_text().startswith("1 · Suggest")
    assert "Pause suggested" in page.inner_text('tr[data-key="adset:2000002"]')
    cell.scroll_into_view_if_needed()
    page.evaluate("document.querySelector('.hc-acct-child .hc-scroll').scrollLeft += 220")
    shot(page, "table-rules-cell", "light")
    cell.click()
    page.wait_for_selector("dialog.hc-rinsp[open]")
    text = page.inner_text("dialog.hc-rinsp")
    assert "Stop-loss" in text and "Spend $620.00" in text and "Exact" in text
    shot(page, "table-inspector", "light")
    page.evaluate("document.documentElement.setAttribute('data-theme', 'dark')")
    shot(page, "table-inspector", "dark")
    page.evaluate("document.querySelector('dialog.hc-rinsp').close()")
    shot(page, "table-rules-cell", "dark")
    page.evaluate("document.documentElement.setAttribute('data-theme', 'light')")
    page.click('tr[data-key="adset:2000002"] [data-rules-open]')
    page.wait_for_selector("dialog.hc-rinsp[open]")
    assert world.meta.posts == []
    page.click("dialog.hc-rinsp [data-rapply]")
    page.wait_for_selector("dialog.acm-confirm[open]")
    assert "Switch off 1 object" in page.inner_text("dialog.acm-confirm")
    assert world.meta.posts == []
    page.click('dialog.acm-confirm [data-acm="confirm"]')
    page.wait_for_function(f"document.querySelector('.hc-switch[data-object-id=\"2000002\"][aria-checked=\"false\"]')", timeout=15000)
    assert [p for p, _ in world.meta.posts] == ["/2000002"]
    page.wait_for_timeout(500)
    last = [a for a in st.latest_actions().values() if a.get("id") == "2000002"][0]
    assert last["state"] == "verified" and last["op_id"]
    assert page.errors == []


@pytest.fixture
def open_cell_page(env, monkeypatch, tmp_path):
    from tests.test_acl1800_home_commands_browser import CmdSite, inventory_snapshot, open_acc
    from src.fb_api import commands as cmd
    from tests.test_acl1800_campaign_commands import World
    from src.web import app as web

    cmd._STORES.clear()
    monkeypatch.setattr(cmd, "RECONCILE_PAUSE_S", 0.01)
    w = World(tmp_path)
    w.store = cmd.get_store()
    monkeypatch.setattr(campaign_commands, "_sessions", lambda: w.open_session)
    ci.put_result(inventory_snapshot())

    class RulesSite(CmdSite):
        def html(self, theme="light"):   # в настоящем дешборде селектор байера есть; ячейки Rules требуют owner
            return super().html(theme).replace("<body style='margin:24px'>",
                                               f"<body style='margin:24px'><select id='buyer-select'><option value='{OWNER}'>{OWNER}</option></select>")

        def handle(self, route):
            u = urlparse(route.request.url)
            if u.path == "/static/rules_ui.js":
                return route.fulfill(status=200, content_type="application/javascript",
                                     body=(STATIC / "rules_ui.js").read_text(encoding="utf-8"))
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            if u.path == "/rules/cells":
                data, code = rules_api.cells(q.get("account", ""), q.get("owner", ""), store=env)
                return route.fulfill(status=code, content_type="application/json", body=json.dumps(data))
            if u.path in ("/rules/action-result", "/rules/dismiss"):
                fn = rules_api.record_action_result if u.path.endswith("result") else rules_api.dismiss_proposal
                data, code = fn(json.loads(route.request.post_data or "{}"), store=env)
                return route.fulfill(status=code, content_type="application/json", body=json.dumps(data))
            return super().handle(route)

    rule = env.create_from_preset("stop_loss_new_tests", OWNER, by=OWNER)
    env.edit_rule(rule["id"], {"scope": {"accounts": ["Acc"], "country": "Kuwait", "objects": [
        {"account": "Acc", "level": "adset", "id": "2000002", "name_at_assign": "Set auto"}]}}, by=OWNER)
    env.set_mode(rule["id"], "suggest", by=OWNER)
    version = env.current_version(env.get_rule(rule["id"]))["version"]
    env.append_evaluation({
        "trigger": "manual", "rule_id": rule["id"], "version": version, "mode": "suggest", "strategy": 1,
        "verdict": "matched", "reasons": [], "counts": {}, "bi_through": "2026-10-11",
        "bi_built_at": "2026-10-12T08:00:00+00:00", "snapshot_at": {}, "T": {"value": 60.0, "source": "goal"},
        "objects": [{"key": "adset:2000002", "account": "Acc", "level": "adset", "id": "2000002", "name": "Set auto",
                     "identity": "exact", "verdict": "matched", "reasons": [], "notes": [], "conditions_matched": True,
                     "observed": {"spend": 620.0, "ftd": 0, "target": 60.0},
                     "action": {"kind": "pause", "level": "adset", "id": "2000002", "account": "Acc",
                                "requested": {"status": "PAUSED"}, "before": {"status": "ACTIVE"}}}]})
    env.append_action({"state": "proposed", "rule_id": rule["id"], "version": version, "account": "Acc", "level": "adset",
                       "id": "2000002", "kind": "pause", "before": {"status": "ACTIVE"}, "requested": {"status": "PAUSED"}})
    site = RulesSite(web, monkeypatch, w)
    pw = sync_playwright().start()
    browser = launch_chromium(pw)
    pg = browser.new_page(viewport={"width": 1400, "height": 900})
    pg.route("http://acl.test/**", site.handle)
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.errors = errors
    goto(pg, "http://acl.test/")
    open_acc(pg, "Acc")
    pg.wait_for_selector(".hc-table tbody tr", timeout=10000)
    pg.wait_for_function("document.querySelector('.amap-campaigns')._hc.state.avail !== null")
    pg.click(f'[data-exp="campaign:1000001"]')
    pg.wait_for_selector('tr[data-key="adset:2000002"]')
    yield pg, w, env, rule["id"]
    browser.close()
    pw.stop()


def test_kpis_nav_badge_tabs_filters_and_one_line_result_cells(env, open_page):
    rid, scale = filled(env)
    st = env
    st.edit_rule(scale, {"scope": {"country": "Chile", "accounts": ["MX Account 1"], "objects": []}}, by=OWNER)
    page = open_page(Stand(env), rule=rid)
    kpis = page.locator(".rl-kpi").all_inner_texts()
    flat = [" ".join(k.split()) for k in kpis]
    assert len(flat) == 4 and flat[0].startswith("Observing 1") and flat[1].startswith("Suggesting 1")
    assert flat[2].startswith("Suggestions awaiting review 3")
    assert page.inner_text("#rl-nav-badge") == "3" and not page.locator("#rl-nav-badge").get_attribute("hidden")
    # фильтры: страна и режим
    assert page.locator(".rl-item").count() == 2
    page.select_option('[data-filter="country"]', "Chile")
    assert page.locator(".rl-item").count() == 1 and "Scale proven" in page.inner_text("#rl-items")
    page.select_option('[data-filter="country"]', "")
    page.select_option('[data-filter="mode"]', "suggest")
    assert page.locator(".rl-item").count() == 1 and "Protect LATAM tests" in page.inner_text("#rl-items")
    page.select_option('[data-filter="mode"]', "")
    # вкладки
    page.click('.rl-tab[data-tab="templates"]')
    assert page.locator("#rl-shell").is_hidden() and "Stop-loss" in page.inner_text("#rl-tab-templates")
    page.click('.rl-tab[data-tab="activity"]')
    page.wait_for_selector("#rl-tab-activity table")
    text = page.inner_text("#rl-tab-activity")
    assert "Evaluation (manual)" in text and "Pause" in text and "proposed" in text
    page.click('.rl-tab[data-tab="rules"]')
    assert page.locator("#rl-shell").is_visible()
    # результат в одну строку при 1280
    heights = page.evaluate("[...document.querySelectorAll('.rl-resrow')].map(e => e.getBoundingClientRect().height)")
    assert heights and max(heights) <= 36, heights    # чип, текст и кнопки - в одной строке
    assert page.evaluate("[...document.querySelectorAll('.rl-resrow .t')].every(e => e.title)")
    assert no_page_scroll(page)
    shot(page, "kpis-tabs-filters", "light")
    page.click('.rl-tab[data-tab="activity"]')
    shot(page, "activity", "light")
