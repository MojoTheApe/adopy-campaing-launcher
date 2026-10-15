# -*- coding: utf-8 -*-
"""ACL-1613 (PR-1): ai_rescue — сводка страницы, чистка, белый список, режимы, лимиты.

Модель подменена (`FakeClient`), сети нет; страницы — статический HTML в
офлайн-Chromium (все запросы прерваны). Снимки: свёрнутая карточка Audience
(05.10), окно «Meta AI-guided setup» (живой снимок fb358fda8aab, вырезан
`capture_to_fixture.py`) и окно «Ad set name — Template» из Doc21 (по
скриншотам 308c4f7c8a5a/254ee3b3e0c7; живого page.html с этим окном нет,
разметка — та же, что в tests/test_foreign_windows_are_closed_or_named.py).
"""
from __future__ import annotations

import re
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from src.fb_ads_rpa import ai_rescue as ar
from tests.bench_support import FIXTURES, browser, fragment  # noqa: F401

AUDIENCE = FIXTURES / "audience_card_collapsed_drawer_2026_10_05.html"
GUIDED = FIXTURES / "ai_guided_setup_dialog_2026_10_05.html"

NAMING = """
<div role="dialog" id="naming"><div><span role="heading">Ad set name</span>
  <div aria-label="Close" role="button" style="width:16px;height:16px">x</div></div>
  <div>Name your ad sets consistently by creating a template based on your naming convention.</div>
  <div>Template</div><div><div role="button">Add component</div><a href="#">Choose existing template</a></div>
  <div><div role="button">Cancel</div><div role="button" aria-disabled="true">Save</div></div></div>
"""

#: Журнал нажатий: что именно нажато (по id или тексту).
CLICK_LOG = """<script>window.clicked = [];
document.addEventListener('click', (e) => { const b = e.target.closest('button,[role=button],a,input');
  window.clicked.push(b ? (b.id || b.textContent.trim()) : '?'); }, true);</script>"""

DIRTY = """
<h2 role="heading">Settings</h2>
<div><button id="pub">Publish</button></div>
<div><button id="del">Delete draft</button></div>
<div><button id="form">Create instant form</button></div>
<div><button id="acc">Accept terms</button></div>
<div><button id="out">Log out</button></div>
<div><button id="pay">Add payment method</button></div>
<div><button id="cws">Close without saving</button></div>
<div><button id="leave">Leave</button></div>
<div><button id="rem">Remove</button></div>
<div><button id="ok">Show more</button></div>
"""


def _html(body: str) -> str:
    return f"<!doctype html><html><body>{body}{CLICK_LOG}</body></html>"


def mutate_on_mark(label: str, body: str) -> str:
    """Скрипт страницы: как только элемент с такой меткой получил `data-acl-ai`, выполнить `body(el)`.

    Страница меняется сама, между сводкой и действием, — как перерисовка Meta.
    Из подставной модели page.evaluate звать нельзя: клиент крутится в другом потоке,
    а sync-API Playwright небезопасен между потоками.
    """
    return ("<script>new MutationObserver((recs, obs) => {"
            f" const el = [...document.querySelectorAll('[data-acl-ai]')].find(e => e.textContent.trim() === {label!r});"
            f" if (el) {{ obs.disconnect(); (function (el) {{ {body} }})(el); }} }})"
            ".observe(document.documentElement, {attributes: true, subtree: true, attributeFilter: ['data-acl-ai']});"
            "</script>")


@pytest.fixture
def page(browser):  # noqa: F811
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    ctx.route("**/*", lambda route: route.abort())
    p = ctx.new_page()
    try:
        yield p
    finally:
        ctx.close()


class FakeTelemetry:
    def __init__(self):
        self.rows = []

    def record(self, step, ok=None, **kw):
        self.rows.append((step, ok, kw))


class FakeClient:
    """Модель-пустышка: `choose(prompt, page)` возвращает input инструмента (dict/str/None)."""

    def __init__(self, choose, page=None, tokens=(123, 7)):
        self.choose, self.page, self.calls, self.prompts = choose, page, 0, []
        self.messages = SimpleNamespace(create=self._create)
        self.tokens = tokens
        self.last_kwargs = {}

    def _create(self, **kw):
        self.calls += 1
        self.last_kwargs = kw
        prompt = kw["messages"][0]["content"]
        self.prompts.append(prompt)
        answer = self.choose(prompt, self.page)
        content = [] if answer is None else [SimpleNamespace(type="tool_use", input=answer)]
        usage = SimpleNamespace(input_tokens=self.tokens[0], output_tokens=self.tokens[1])
        return SimpleNamespace(content=content, usage=usage)


def index_of(prompt: str, label: str, role: str = "") -> int:
    r = role or r"\w+"
    m = (re.search(rf'^\[(\d+)\] {r} "{re.escape(label)}"', prompt, re.M)
         or re.search(rf'^\[(\d+)\] {r} "{re.escape(label)}', prompt, re.M))
    assert m, f"{label!r} not in summary: {prompt}"
    return int(m.group(1))


def pick(label, action="click", role=""):
    return lambda prompt, page: {"action": action, "index": index_of(prompt, label, role), "reason": "test"}


def run(page, client, *, mode="act", budget=None, step="s1", tel=None, error="row not found", **kw):
    return ar.rescue(page, step=step, goal="open the Audience section", error=error,
                     budget=budget or ar.RescueBudget(), telemetry=tel or FakeTelemetry(),
                     client=client, mode=mode, **kw)


def clicked(page):
    return page.evaluate("window.clicked")


# ── сводка на настоящих снимках ──────────────────────────────────────────────

def test_summary_of_the_collapsed_audience_card(page):
    page.set_content(_html(fragment(AUDIENCE, "card_run_a")))
    s = ar.build_summary(page)
    assert "Audience" in s.text
    card = [e for e in s.elements.values() if e["label"].startswith("Audience") and "not-pressed" in e["state"]]
    assert card, s.text
    assert all(e["role"] == "button" for e in card)
    assert page.evaluate("document.querySelectorAll('[data-acl-ai]').length") == len(s.elements)


def test_summary_of_the_doc21_naming_dialog_puts_the_dialog_first(page):
    page.set_content(_html("<h2 role='heading'>Ad set</h2><button id='bg'>Behind</button>" + NAMING))
    s = ar.build_summary(page)
    assert s.text.startswith('DIALOG "Ad set name"')
    first = s.elements[0]
    assert first["inDialog"] and first["label"] == "Close"
    labels = [e["label"] for e in s.elements.values()]
    assert labels.index("Cancel") < labels.index("Behind")
    assert "disabled" in next(e for e in s.elements.values() if e["label"] == "Save")["state"]


def test_summary_of_the_ai_guided_setup_dialog(page):
    page.set_content(_html(fragment(GUIDED, "dialog")))
    s = ar.build_summary(page)
    assert "Meta AI-guided setup" in s.text
    radios = [e for e in s.elements.values() if e["role"] == "radio"]
    assert any(e["label"] == "Meta AI-guided setup" and "checked" in e["state"] for e in radios), s.text
    assert any(e["label"] == "Manual campaign setup" for e in s.elements.values())


def test_the_summary_follows_the_anchor_after_the_dialogs(page):
    btns = "".join(f'<div style="height:60px"><button>B{i}</button></div>' for i in range(30))
    page.set_content(_html(btns + "<p id='a'>Anchor text here</p>" + "<button>Near</button>"))
    s = ar.build_summary(page, anchor="Anchor text")
    assert s.elements[0]["label"] == "Near"


def test_a_big_page_stays_under_the_caps(page):
    page.set_content(_html("".join(f"<button>Button number {i}</button>" for i in range(1500))))
    s = ar.build_summary(page)
    assert len(s.text) <= ar.MAX_CHARS
    assert len(s.elements) <= ar.MAX_ELEMENTS and s.truncated > 0
    assert "more elements not shown" in s.text
    page.set_content(_html("".join(f"<button>{'word ' * 400}{i}</button>" for i in range(200))))
    assert len(ar.build_summary(page).text) <= ar.MAX_CHARS


# ── чистка ───────────────────────────────────────────────────────────────────

# Синтетические идентификаторы собираются на лету: сканер секретов не должен видеть их в файле.
ACT_A = "act" + "=" + "1234567890123"
ACT_B = "act" + "=" + "998877665544"
ACT_C = "act" + "=" + "123456789012"
UUID = "-".join(["123e4567", "e89b", "12d3", "a456", "426614174000"])

LEAKY = ("Hi evgeni.s@adopy.com see https://adsmanager.facebook.com/adsmanager/manage?" + ACT_A + "&x=1 "
         "" + ACT_B + " id 12345678901234 token EAABsbCS1iHgBO0ZBabcdef012345 "
         "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.sig_abcdefg "
         "uuid " + UUID + " KW_TRL_All_Video_Mix_35_05.10 Facebook_9403A UL921 "
         "call +1 (415) 555-0132 Evgeni's account abcDEF123456abcDEF123456abcDEF www.example.com/x")


def test_the_scrubber_leaves_no_ids_urls_or_emails():
    out = ar.scrub_text(LEAKY, names=("Mojo Page",))
    assert not re.search(r"\d{6,}", out), out
    assert "@" not in out and "http" not in out and "www." not in out and ".com" not in out, out
    for needle in ("KW_TRL", "9403A", "UL921", "EAAB", "eyJ", "Evgeni", "415", "adopy"):
        assert needle not in out, (needle, out)
    assert "act=<id>" in out


def test_the_scrubber_hides_known_names_and_the_run_campaign(monkeypatch):
    from src.fb_ads_rpa import run_telemetry

    monkeypatch.setattr(run_telemetry, "current_campaign", lambda: "Summer Sale Bundle")
    out = ar.scrub_text("Page Mojo Page ran Summer Sale Bundle", names=("Mojo Page",))
    assert "Mojo" not in out and "Summer" not in out


def test_everything_in_the_summary_and_the_prompt_is_scrubbed(page):
    page.set_content(_html(
        "<h2 role='heading'>KW_TRL_All_Video_Mix_35_05.10 - Facebook_9403A</h2>"
        "<button aria-label='Switch to evgeni.s@adopy.com'>Open 1234567890123</button>"
        "<a href='https://x.example.com/?" + ACT_C + "'>Account 123456789012 settings</a>"))
    cl = FakeClient(lambda p, pg: {"action": "give_up", "reason": "x"})
    run(page, cl, error="boom for evgeni.s@adopy.com " + ACT_A)
    sent = cl.prompts[0]
    assert not re.search(r"\d{6,}", sent) and "@" not in sent and "http" not in sent, sent
    assert "KW_TRL" not in sent and "9403A" not in sent


@pytest.mark.parametrize("fixture,frag", [(AUDIENCE, "card_run_a"), (AUDIENCE, "card_run_b"), (GUIDED, "dialog")])
def test_scrubbed_fixtures_give_a_clean_summary(page, fixture, frag):
    page.set_content(_html(fragment(fixture, frag)))
    t = ar.build_summary(page).text
    assert not re.search(r"\d{6,}", t) and "@" not in t and "http" not in t


# ── запрещённое: всё в give_up, ничего не нажато ────────────────────────────

@pytest.mark.parametrize("label", ["Publish", "Delete draft", "Create instant form", "Accept terms", "Log out",
                                   "Add payment method", "Close without saving", "Leave", "Remove"])
def test_a_forbidden_pick_becomes_give_up_and_nothing_is_clicked(page, label):
    page.set_content(_html(DIRTY))
    tel = FakeTelemetry()
    res = run(page, FakeClient(pick(label)), tel=tel)
    assert res.ok is False and res.action == "give_up" and res.reason.startswith("forbidden")
    assert clicked(page) == []
    assert tel.rows[-1][0] == "ai_rescue" and tel.rows[-1][1] is False


def test_a_harmless_button_in_a_destructive_dialog_is_refused(page):
    page.set_content(_html('<div role="dialog"><div role="heading">Delete this draft?</div>'
                           '<button>Continue</button><button>Cancel</button></div>'))
    res = run(page, FakeClient(pick("Cancel", "close_dialog")))
    assert res.action == "give_up" and "forbidden_dialog" in res.reason
    assert clicked(page) == []


def test_escape_is_not_pressed_over_a_discard_dialog(page):
    page.set_content(_html('<div role="dialog"><div role="heading">Discard changes?</div>'
                           '<button>Keep editing</button></div>'
                           "<script>window.esc=0;document.addEventListener('keydown',"
                           "e=>{if(e.key==='Escape')window.esc++})</script>"))
    res = run(page, FakeClient(lambda p, pg: {"action": "press_escape"}))
    assert res.action == "give_up" and res.reason.startswith("escape_blocked")
    assert page.evaluate("window.esc") == 0


@pytest.mark.parametrize("answer", [
    {"action": "click", "index": 9999},
    {"action": "click", "index": -1},
    {"action": "click"},
    {"action": "click", "index": True},
    {"action": "click", "index": "3"},
    {"action": "type_text", "text": "hello"},
    {"action": "eval", "code": "document.body.remove()"},
    "this is not json",
    '{"action": ',
    ["click", 1],
    None,
])
def test_an_unknown_index_or_a_malformed_answer_is_give_up(page, answer):
    page.set_content(_html(DIRTY))
    res = run(page, FakeClient(lambda p, pg: answer))
    assert res.ok is False and res.action == "give_up"
    assert clicked(page) == []


def test_close_dialog_needs_a_closing_button_inside_a_dialog(page):
    page.set_content(_html("<div><button id='x'>Show more</button></div>" + NAMING))
    assert run(page, FakeClient(pick("Show more", "close_dialog"))).reason == "behind_dialog"
    assert run(page, FakeClient(pick("Add component", "close_dialog"))).reason.startswith("not_allowlisted")
    res = run(page, FakeClient(pick("Cancel", "close_dialog")))
    assert res.ok is True and clicked(page) == ["Cancel"]


def test_expand_needs_a_collapsed_element(page):
    page.set_content(_html("<button aria-expanded='true'>Open card</button><button>Plain</button>"))
    assert run(page, FakeClient(pick("Open card", "expand"))).reason == "not_collapsed"
    assert run(page, FakeClient(pick("Plain", "expand"))).reason == "not_collapsed"
    assert clicked(page) == []


def test_the_element_changed_between_summary_and_click_is_not_clicked(page):
    """Индекс остался, а под ним уже «Publish»: метку перечитываем перед кликом."""
    page.set_content(_html(DIRTY + mutate_on_mark("Show more", "el.textContent = 'Publish';")))
    res = run(page, FakeClient(pick("Show more")))
    assert res.action == "give_up" and res.reason == "dom_changed"
    assert clicked(page) == []


def test_a_relabelled_but_harmless_element_is_not_clicked_either(page):
    page.set_content(_html("<div><button>One</button></div><div><button>Two</button></div>"
                           + mutate_on_mark("One", "el.textContent = 'Other';")))
    res = run(page, FakeClient(pick("One")))
    assert res.reason == "dom_changed" and clicked(page) == []


def test_a_new_forbidden_dialog_around_the_element_blocks_the_click(page):
    page.set_content(_html("<div id='w'><button>Continue</button></div>" + mutate_on_mark(
        "Continue", "const w = el.parentElement; w.setAttribute('role','dialog');"
                    " w.insertAdjacentHTML('afterbegin', '<span>Delete everything</span>');")))
    res = run(page, FakeClient(pick("Continue")))
    assert res.action == "give_up" and clicked(page) == []


def test_a_removed_element_is_give_up(page):
    page.set_content(_html("<div><button>Gone soon</button></div><div><button>Stay</button></div>"
                           + mutate_on_mark("Gone soon", "el.remove();")))
    assert run(page, FakeClient(pick("Gone soon"))).reason == "element_gone_or_ambiguous"
    assert clicked(page) == []


# ── режимы ───────────────────────────────────────────────────────────────────

def test_suggest_never_touches_the_page(page):
    page.set_content(_html("<div role='dialog'><span role='heading'>Hi</span><button id='c'>Close</button></div>"))
    before = page.evaluate("document.documentElement.outerHTML")
    tel = FakeTelemetry()
    res = run(page, FakeClient(pick("Close", "close_dialog")), mode="suggest", tel=tel)
    assert res.ok is True and res.action == "close_dialog" and res.mode == "suggest"
    assert clicked(page) == []
    assert page.evaluate("document.documentElement.outerHTML") == before  # даже data-acl-ai убраны
    assert tel.rows[-1][2]["diag"]["mode"] == "suggest"
    for a in ({"action": "press_escape"}, {"action": "give_up", "reason": "no"}):
        run(page, FakeClient(lambda p, pg, a=a: a), mode="suggest")
    assert page.evaluate("document.documentElement.outerHTML") == before


def test_act_clicks_exactly_the_chosen_element(page):
    page.set_content(_html(DIRTY))
    res = run(page, FakeClient(pick("Show more")), mode="act")
    assert res.ok is True and res.action == "click" and res.label == "Show more"
    assert clicked(page) == ["ok"]
    assert page.evaluate("document.querySelectorAll('[data-acl-ai]').length") == 0


def test_act_expands_the_collapsed_audience_card(page):
    page.set_content(_html(fragment(AUDIENCE, "card_run_a") + """<script>
      document.addEventListener('click', (ev) => { const b = ev.target.closest('[role=button][aria-pressed=false]');
        if (b) { const id = b.getAttribute('aria-labelledby');
          document.querySelectorAll('[role=button][aria-pressed]').forEach(x => {
            if (x.getAttribute('aria-labelledby') === id) x.setAttribute('aria-pressed','true'); }); } });
      </script>"""))
    s = ar.build_summary(page)
    ar.clear_marks(page)
    first = next(i for i, e in s.elements.items() if e["label"].startswith("Audience") and "not-pressed" in e["state"])
    res = run(page, FakeClient(lambda p, pg: {"action": "expand", "index": first}))
    assert res.ok is True and res.action == "expand"
    assert page.evaluate("[...document.querySelectorAll('[role=button][aria-pressed]')]"
                         ".filter(b => b.getAttribute('aria-pressed') === 'true').length") >= 1
    assert len(clicked(page)) == 1


def test_scroll_to_and_escape_act(page):
    page.set_content(_html("<div style='height:3000px'></div><div><button id='far'>Far away</button></div>"))
    res = run(page, FakeClient(pick("Far away", "scroll_to")))
    assert res.ok and page.evaluate("window.scrollY") > 1000 and clicked(page) == []
    page.set_content(_html("<div role='dialog'><h2>Heads up</h2><span>Something</span></div>"
                           "<script>window.esc=0;document.addEventListener('keydown',"
                           "e=>{if(e.key==='Escape')window.esc++})</script>"))
    assert run(page, FakeClient(lambda p, pg: {"action": "press_escape"})).ok
    assert page.evaluate("window.esc") == 1


def test_off_is_the_default_and_never_asks(page, monkeypatch):
    monkeypatch.delenv("ACL_AI_RESCUE", raising=False)
    page.set_content(_html(DIRTY))
    cl = FakeClient(pick("Show more"))
    res = ar.rescue(page, step="s", goal="g", error="e", budget=ar.RescueBudget(), client=cl)
    assert res.skipped and res.mode == "off" and cl.calls == 0 and clicked(page) == []
    monkeypatch.setenv("ACL_AI_RESCUE", "bogus")
    assert ar.get_mode() == "off"
    monkeypatch.setenv("ACL_AI_RESCUE", " ACT ")
    assert ar.get_mode() == "act"


# ── лимиты ───────────────────────────────────────────────────────────────────

def test_caps_two_per_step_and_four_per_campaign(page):
    page.set_content(_html(DIRTY))
    budget = ar.RescueBudget()
    cl = FakeClient(lambda p, pg: {"action": "give_up", "reason": "n"})
    tel = FakeTelemetry()
    results = [run(page, cl, budget=budget, step="a", tel=tel) for _ in range(3)]
    assert [r.skipped for r in results] == [False, False, True] and results[2].reason == "budget_exhausted"
    assert cl.calls == 2
    run(page, cl, budget=budget, step="b")
    run(page, cl, budget=budget, step="b")
    assert cl.calls == 4 and budget.total == 4
    res = run(page, cl, budget=budget, step="c")
    assert res.skipped and cl.calls == 4
    assert tel.rows[-1][2]["diag"]["reason"] == "budget_exhausted"


def test_a_failed_call_still_spends_the_budget(page):
    page.set_content(_html(DIRTY))
    budget = ar.RescueBudget()
    run(page, FakeClient(lambda p, pg: None), budget=budget)
    assert budget.total == 1 and budget.by_step == {"s1": 1}


# ── ключ, SDK, сбои ──────────────────────────────────────────────────────────

def test_a_missing_key_is_skipped_with_a_log_line(page, monkeypatch, caplog):
    monkeypatch.setattr(ar, "_load_key", lambda: "")
    page.set_content(_html(DIRTY))
    tel = FakeTelemetry()
    with caplog.at_level("WARNING"):
        res = ar.rescue(page, step="s", goal="g", error="e", budget=ar.RescueBudget(), telemetry=tel, mode="act")
    assert res.skipped and res.reason == "no_api_key" and res.ok is False
    assert "ANTHROPIC_API_KEY" in caplog.text
    assert tel.rows[-1][2]["diag"]["reason"] == "no_api_key"
    assert clicked(page) == []


def test_a_missing_sdk_is_skipped(page, monkeypatch):
    monkeypatch.setattr(ar, "_load_key", lambda: "sk-test-not-real")
    monkeypatch.setitem(sys.modules, "anthropic", None)
    page.set_content(_html(DIRTY))
    res = ar.rescue(page, step="s", goal="g", error="e", budget=ar.RescueBudget(), mode="act")
    assert res.skipped and res.reason == "no_sdk"


def test_the_model_call_never_blocks_longer_than_the_timeout(page, monkeypatch):
    monkeypatch.setattr(ar, "TIMEOUT_S", 0.4)
    page.set_content(_html(DIRTY))
    gate = threading.Event()

    def slow(prompt, pg):
        gate.wait(10)
        return {"action": "click", "index": 0}

    t0 = time.monotonic()
    try:
        res = run(page, FakeClient(slow))
    finally:
        gate.set()
    assert time.monotonic() - t0 < 3
    assert res.action == "give_up" and res.reason == "timeout" and clicked(page) == []


def test_exceptions_never_escape(page, monkeypatch):
    page.set_content(_html(DIRTY))

    def boom(prompt, pg):
        raise RuntimeError("network down")

    res = run(page, FakeClient(boom))
    assert res.action == "give_up" and res.reason.startswith("call_failed")

    class BadTelemetry:
        def record(self, *a, **k):
            raise OSError("disk")

    assert run(page, FakeClient(pick("Show more")), tel=BadTelemetry()).ok is True

    class DeadPage:
        def evaluate(self, *a, **k):
            raise RuntimeError("target closed")

    res = run(DeadPage(), FakeClient(lambda p, pg: {"action": "press_escape"}), mode="act")
    assert isinstance(res, ar.RescueResult)  # не бросило

    monkeypatch.setattr(ar, "build_summary", lambda *a, **k: (_ for _ in ()).throw(ValueError("x")))
    res = run(page, FakeClient(pick("Show more")))
    assert res.skipped and res.reason.startswith("error:")


# ── телеметрия и запрос ──────────────────────────────────────────────────────

def test_telemetry_row_shape(page):
    page.set_content(_html(DIRTY))
    tel = FakeTelemetry()
    res = run(page, FakeClient(pick("Show more"), tokens=(321, 9)), tel=tel, step="further_limit_row")
    step, ok, kw = tel.rows[-1]
    assert (step, ok, kw["note"]) == ("ai_rescue", True, "click")
    d = kw["diag"]
    assert d["step"] == "further_limit_row" and d["mode"] == "act" and d["action"] == "click"
    assert d["label"] == "Show more" and d["input_tokens"] == 321 and d["output_tokens"] == 9
    assert d["latency_s"] >= 0 and len(d["summary_hash"]) == 12 and "goal" in d and "reason" in d
    assert res.label == "Show more"


def test_the_request_carries_the_schema_model_and_timeout(page, monkeypatch):
    monkeypatch.setenv("ACL_AI_RESCUE_MODEL", "claude-test-model")
    page.set_content(_html(DIRTY))
    cl = FakeClient(lambda p, pg: {"action": "give_up", "reason": "no"})
    run(page, cl)
    seen = cl.last_kwargs
    assert seen["model"] == "claude-test-model" and seen["timeout"] == ar.TIMEOUT_S == 20.0
    assert seen["tool_choice"] == {"type": "tool", "name": "rescue_action"}
    enum = seen["tools"][0]["input_schema"]["properties"]["action"]["enum"]
    assert enum == ["click", "expand", "close_dialog", "press_escape", "scroll_to", "give_up"]
    assert "Step: s1" in cl.prompts[0] and "Goal: open the Audience section" in cl.prompts[0]
    assert "Last error: row not found" in cl.prompts[0]
    monkeypatch.delenv("ACL_AI_RESCUE_MODEL")
    assert ar.get_model() == "claude-sonnet-5-5"


def test_the_step_is_known_to_the_progress_model():
    from tests.test_build_progress_model import MODEL, m

    assert m.stage_of("ai_rescue", MODEL["rules"]) == "hold"


# ═══ ревью PR #623: каждая проба ревьюера — регрессионный тест ═══════════════

ESC_LOG = ("<script>window.esc=0;document.addEventListener('keydown',"
           "e=>{if(e.key==='Escape')window.esc++})</script>")


def probe(page, body, choose, *, mode="act", **kw):
    """Страница + подставная модель; возвращает (результат, список нажатий, число Escape)."""
    page.set_content(_html(body + ESC_LOG))
    res = run(page, FakeClient(choose), mode=mode, **kw)
    return res, clicked(page), page.evaluate("window.esc")


def by_label(label, action="click"):
    return lambda prompt, pg: {"action": action, "index": index_of(prompt, label)}


# B1: слова склеиваются без разделителей
def test_b1_text_extraction_keeps_word_boundaries():
    assert "here delete ad set" in ar.norm_text("...length hereDelete ad set")
    assert ar.forbidden_reason(["Placements"], "Cost per goal hereDelete ad set")


def test_b1_a_container_whose_text_ends_in_delete_is_refused(page):
    body = ('<div role="button" aria-expanded="false" style="width:600px;height:80px">'
            'Audience card summary with a long description that goes past eighty characters in total length here'
            '<b>Delete ad set</b></div>')
    res, clk, _ = probe(page, body, by_label("Audience card", "expand"))
    assert res.action == "give_up" and res.reason.startswith("forbidden_label") and clk == []


# B2: клик попадает в вложенный запрещённый элемент
def test_b2_expand_on_a_card_with_a_nested_delete_button_is_refused(page):
    body = ('<div role="button" aria-expanded="false" style="width:600px;height:200px;position:relative;display:block">'
            'Audience card summary with a long description that pushes beyond eighty characters total length here'
            '<button id="DEL" style="position:absolute;left:0;top:0;width:600px;height:200px;opacity:0.99">'
            'Delete ad set</button></div>')
    res, clk, _ = probe(page, body, by_label("Audience card", "expand"))
    assert res.action == "give_up" and clk == []
    assert "descendant_forbidden" in res.reason or "forbidden_label" in res.reason


def test_b2_a_harmless_nested_button_covering_the_card_is_refused_by_the_hit_test(page):
    body = ('<div role="button" aria-expanded="false" style="width:600px;height:200px;position:relative;display:block">'
            'Audience card title'
            '<button id="INNER" style="position:absolute;left:0;top:0;width:600px;height:200px">More details</button></div>')
    res, clk, _ = probe(page, body, by_label("Audience card title", "expand"))
    assert res.action == "give_up" and res.reason == "hit_interactive_descendant" and clk == []


def test_b2_click_on_an_element_with_interactive_descendants_is_refused(page):
    body = '<div role="button" style="width:300px;height:60px">Row <button>Details</button></div>'
    res, clk, _ = probe(page, body, by_label("Row", "click"))
    assert res.reason == "has_interactive_descendants" and clk == []


def test_b2_the_click_lands_on_the_verified_point_not_on_a_link_in_the_card(page):
    body = ('<div role="button" aria-expanded="false" style="width:600px;height:100px;position:relative;display:block">'
            'Audience summary<a href="#" id="LNK" style="position:absolute;left:250px;top:0;width:100px;height:100px">'
            'About audiences</a></div>')
    res, clk, _ = probe(page, body, by_label("Audience summary", "expand"))
    assert res.ok is True, res.reason
    assert len(clk) == 1 and clk[0] != "LNK"


def test_n2_the_click_goes_through_locator_click_with_position_and_a_short_timeout(page, monkeypatch):
    page.set_content(_html("<div><button id='b'>Show more</button></div>"))
    seen = {}
    orig = page.locator

    class Spy:
        def __init__(self, loc):
            self.loc = loc

        def click(self, **kw):
            seen.update(kw)
            return self.loc.click(**kw)

    monkeypatch.setattr(page, "locator", lambda sel: Spy(orig(sel)))
    assert run(page, FakeClient(pick("Show more"))).ok is True
    assert clicked(page) == ["b"]
    assert seen["timeout"] <= 1000 and set(seen["position"]) == {"x", "y"}


# B3: источники подписи
def test_b3_a_benign_aria_label_over_a_publish_text_is_refused(page):
    body = ('<div><span>' + "Lorem ipsum filler text. " * 10 +
            '</span><button id="PUB" aria-label="Continue">Publish</button></div>')
    res, clk, _ = probe(page, body, by_label("Continue"))
    assert res.reason.startswith("forbidden_label") and clk == []


def test_b3_a_title_with_delete_is_refused(page):
    res, clk, _ = probe(page, '<div><button id="T" title="Delete" aria-label="Options">...</button></div>',
                        by_label("Options"))
    assert res.reason.startswith("forbidden_label") and clk == []


def test_b3_an_icon_button_whose_child_says_delete_is_refused(page):
    body = '<div><div role="button" id="TRASH"><span aria-label="Delete"></span><img alt="Delete" src="x"></div></div>'
    res, clk, _ = probe(page, body, by_label(""))
    assert res.reason.startswith("forbidden_label") and clk == []


def test_b3_labelledby_and_value_are_label_sources(page):
    body = ('<span id="lb">Publish now</span><div><button id="A" aria-labelledby="lb" aria-label="Go">Go</button></div>'
            '<div><input type="button" id="B" value="Delete"></div>')
    page.set_content(_html(body))
    assert run(page, FakeClient(by_label("Go"))).reason.startswith("forbidden_label")
    assert run(page, FakeClient(lambda p, pg: {"action": "click", "index": index_of(p, "Delete", "button")})
               ).reason.startswith("forbidden_label")
    assert clicked(page) == []


def test_b3_an_empty_label_is_refused(page):
    res, clk, _ = probe(page, '<div><div role="button" id="X" style="width:20px;height:20px"></div></div>',
                        by_label(""))
    assert res.reason == "empty_label" and clk == []


def test_b3_the_forbidden_check_sees_text_past_the_display_cap(page):
    body = '<div><button id="L">' + "harmless words " * 20 + ' and finally Publish</button></div>'
    res, clk, _ = probe(page, body, lambda p, pg: {"action": "click", "index": 0})
    assert res.reason.startswith("forbidden_label") and clk == []


# B4: полный текст окна
def test_b4_the_dialog_title_with_a_nested_tag_counts(page):
    body = ('<div role="dialog"><h2>Delete 3 ads?<b> </b></h2><div>This cannot be undone <i>.</i></div>'
            '<button id="OK">OK</button></div>')
    res, clk, _ = probe(page, body, by_label("OK"))
    assert res.reason.startswith("forbidden_dialog") and clk == []


def test_b4_a_forbidden_phrase_after_a_long_dialog_text_counts(page):
    body = ('<div role="dialog"><h2>Unsaved</h2>' + "".join(f"<span>Some info line {i}.</span>" for i in range(80)) +
            '<span>Discard changes?</span><button id="YES">Yes</button></div>')
    res, clk, _ = probe(page, body, by_label("Yes"))
    assert res.reason.startswith("forbidden_dialog") and clk == []


# B5: испанский и португальский — каждая запись таблицы
SAMPLES = {
    ("en", "publish"): "Publish", ("en", "delete"): "Delete ad", ("en", "remove"): "Remove",
    ("en", "discard"): "Discard changes", ("en", "terms_accept"): "Accept terms",
    ("en", "lead_form"): "Create instant form", ("en", "logout"): "Log out",
    ("en", "payment"): "Add payment method", ("en", "leave_unsaved"): "Close without saving",
    ("es", "publish"): "Publicar", ("es", "delete"): "Eliminar", ("es", "discard"): "Descartar cambios",
    ("es", "terms_accept"): "Aceptar términos y condiciones", ("es", "lead_form"): "Formulario instantáneo",
    ("es", "logout"): "Cerrar sesión", ("es", "payment"): "Añadir método de pago",
    ("es", "leave_unsaved"): "Salir sin guardar",
    ("pt", "publish"): "Publicar anúncio", ("pt", "delete"): "Excluir", ("pt", "discard"): "Descartar alterações",
    ("pt", "terms_accept"): "Aceitar os termos", ("pt", "lead_form"): "Criar formulário",
    ("pt", "logout"): "Encerrar sessão", ("pt", "payment"): "Forma de pagamento",
    ("pt", "leave_unsaved"): "Sair sem salvar",
    ("en", "duplicate"): "Duplicate", ("en", "edit"): "Edit", ("en", "toggle"): "Turn on",
    ("es", "duplicate"): "Duplicar", ("es", "edit"): "Editar", ("es", "toggle"): "Desactivar",
    ("pt", "duplicate"): "Duplicar", ("pt", "edit"): "Editar", ("pt", "toggle"): "Desativar",
}
EXTRA = ["Borrar", "Quitar", "Eliminar anuncio", "Pago", "Facturación", "Remover", "Apagar", "Cobrança",
         "Faturamento", "Cartão de crédito", "Novo formulário", "Crear formulario", "Acepto", "Concordar",
         "Logout", "Sign out", "On/off", "Turn off", "Ativar", "Ligar", "Encender", "Salir", "Sair", "Don't save",
         "Billing", "Payments", "Terms of service", "Instant forms"]


def _table_rows():
    import yaml
    from src.fb_ads_rpa import wizard_guard as wg

    raw = yaml.safe_load(wg._ZONES_PATH.read_text(encoding="utf-8"))["ai_rescue"]
    return [(r["lang"], r["reason"]) for r in raw["context"] + raw["target_only"]]


def test_b5_every_table_row_has_a_sample_in_its_language():
    missing = [k for k in _table_rows() if k not in SAMPLES]
    assert not missing, f"add a sample for {missing}"


@pytest.mark.parametrize("key", sorted(SAMPLES))
def test_b5_each_pattern_blocks_its_language(key):
    assert ar.forbidden_reason([SAMPLES[key]]), key


@pytest.mark.parametrize("word", EXTRA)
def test_b5_more_variants_are_blocked(word):
    assert ar.forbidden_reason([word]), word


@pytest.mark.parametrize("word", ["Publicar", "Eliminar", "Descartar cambios", "Aceptar", "Cerrar sesión"])
def test_b5_spanish_buttons_are_not_clicked(page, word):
    res, clk, _ = probe(page, f'<div><button id="X">{word}</button></div>', by_label(word))
    assert res.reason.startswith("forbidden") and clk == []


@pytest.mark.parametrize("word", ["Show more", "Continue", "Next", "More options", "Audience details"])
def test_b5_harmless_labels_pass_the_table(word):
    assert ar.forbidden_reason([word]) is None, word


# B6: Close
def test_b6_the_editor_header_close_is_not_clickable(page):
    body = '<div><h2>Ad copy</h2><div role="button" id="CLOSE" aria-label="Close">x</div></div>'
    res, clk, _ = probe(page, body, by_label("Close"))
    assert res.action == "give_up" and clk == []
    res, clk, _ = probe(page, body, by_label("Close", "close_dialog"))
    assert res.reason == "not_a_dialog_close_button" and clk == []


def test_b6_close_works_only_through_close_dialog_inside_a_dialog(page):
    body = '<div role="dialog"><h2>Heads up</h2><span>Something happened</span><div role="button" aria-label="Close">x</div></div>'
    res, clk, _ = probe(page, body, by_label("Close", "click"))
    assert res.reason in ("close_only_via_close_dialog",) or res.reason.startswith("forbidden_zone")
    assert clk == []
    res, clk, _ = probe(page, body, by_label("Close", "close_dialog"))
    assert res.ok is True and len(clk) == 1


def test_b6_a_small_dialog_with_a_name_field_is_a_real_dialog(page):
    """Поле имени внутри маленького окна — не редактор: Publish в тексте окна блокирует всё."""
    body = ('<div role="dialog"><input placeholder="Enter your campaign name" value=""><div>Publish</div>'
            '<div role="button" aria-label="Close">x</div><div><button>Show more</button></div></div>')
    res, clk, _ = probe(page, body, by_label("Close", "close_dialog"))
    assert res.action == "give_up" and clk == []
    res, clk, _ = probe(page, body, by_label("Show more"))
    assert res.reason.startswith("forbidden_dialog") and clk == []


def test_b6_a_full_screen_editor_dialog_is_the_editor_not_a_foreign_dialog(page):
    body = ('<div role="dialog" style="width:100vw;height:100vh"><input placeholder="Enter your campaign name" value="x">'
            '<div>Publish</div><div role="button" aria-label="Close">x</div><div><button>Show more</button></div></div>')
    res, clk, _ = probe(page, body, by_label("Close", "close_dialog"))
    assert res.action == "give_up" and clk == []
    res, clk, _ = probe(page, body, by_label("Show more"))
    assert res.ok is True and clk == ["Show more"]


# B7 / N3: переключатели, строки списков, страница списка
def test_b7_switches_checkboxes_and_radios_are_never_clicked(page):
    body = ('<div><input type="checkbox" role="switch" id="SW" aria-label="Status toggle"></div>'
            '<div><input type="checkbox" id="CB" aria-label="Placement option"></div>'
            '<div><input type="radio" id="RD" aria-label="Budget type"></div>')
    page.set_content(_html(body))
    for label in ("Status toggle", "Placement option", "Budget type"):
        res = run(page, FakeClient(by_label(label)))
        assert res.reason.startswith("role_not_allowed"), (label, res.reason)
    assert clicked(page) == []


def test_b7_row_controls_are_hidden_from_the_model_and_refused(page):
    body = ('<table><tr><td><input type="checkbox" role="switch" id="SW" aria-label="On/off" checked></td>'
            '<td><a href="#">Some campaign link</a></td><td><button>Duplicate</button></td></tr></table>'
            '<div><button>Show more</button></div>')
    page.set_content(_html(body))
    s = ar.build_summary(page)
    assert "On/off" not in s.text and "Duplicate" not in s.text and "Some campaign" not in s.text
    res = run(page, FakeClient(lambda p, pg: {"action": "click", "index": 0}))
    assert res.ok is True and clicked(page) == ["Show more"]
    page.evaluate("document.querySelector('button').setAttribute('data-acl-ai', '77')")
    res = run(page, FakeClient(lambda p, pg: {"action": "click", "index": 77}))
    assert res.reason == "unknown_index"


def test_b7_duplicate_edit_on_off_are_forbidden_words(page):
    for word in ("Duplicate", "Edit", "Turn on", "On/off", "Turn off"):
        res, clk, _ = probe(page, f'<div><button id="X">{word}</button></div>', by_label(word))
        assert res.reason.startswith("forbidden") and clk == [], word


LIST_TABLE = ('<table style="width:1300px;height:600px">' +
              "".join(f'<tr><td><a href="#">Campaign row {i}</a></td><td><input type="checkbox" role="switch" '
                      f'aria-label="On/off"></td></tr>' for i in range(5)) + "</table><div><button>Create</button></div>")


def test_n3_a_list_page_gives_up_without_calling_the_model(page):
    page.set_content(_html(LIST_TABLE))
    cl = FakeClient(by_label("Create"))
    tel = FakeTelemetry()
    budget = ar.RescueBudget()
    res = run(page, cl, tel=tel, budget=budget)
    assert res.action == "give_up" and res.reason == "list_page" and not res.skipped
    assert cl.calls == 0 and clicked(page) == [] and budget.total == 0
    assert tel.rows[-1][0] == "ai_rescue" and tel.rows[-1][2]["diag"]["reason"] == "list_page"


def test_n3_a_dialog_over_the_list_is_handled_but_the_list_behind_it_is_not(page):
    dlg = ('<div role="dialog" style="position:fixed;left:300px;top:100px;width:400px;height:300px;background:#fff">'
           '<h2>Choose how to create</h2><div role="button" id="MAN">Not now</div></div>')
    page.set_content(_html(LIST_TABLE + dlg))
    res = run(page, FakeClient(by_label("Not now", "close_dialog")))
    assert res.ok is True and clicked(page) == ["MAN"]
    res = run(page, FakeClient(by_label("Create")))
    assert res.reason == "behind_dialog"


# N1: правки не теряются
def test_n1_escape_without_a_dialog_is_refused(page):
    res, clk, esc = probe(page, '<div><h2>Ad editor</h2><textarea>edited</textarea></div>',
                          lambda p, pg: {"action": "press_escape"})
    assert res.reason == "no_dialog" and esc == 0


def test_n1_a_dialog_with_edited_text_is_not_closed_or_escaped(page):
    body = ('<div role="dialog"><h2>Details</h2><span>Primary text</span><textarea>new text</textarea>'
            '<div role="button" id="CLS" aria-label="Close">x</div></div>')
    res, clk, esc = probe(page, body, by_label("Close", "close_dialog"))
    assert res.reason == "dirty_dialog" and clk == []
    res, clk, esc = probe(page, body, lambda p, pg: {"action": "press_escape"})
    assert res.reason == "dirty_dialog" and esc == 0
    clean = body.replace("new text", "")
    res, clk, esc = probe(page, clean, lambda p, pg: {"action": "press_escape"})
    assert res.ok is True and esc == 1


def test_j_a_plain_expand_button_still_works(page):
    res, clk, _ = probe(page, '<div><button id="MORE" aria-expanded="false">Audience</button></div>',
                        by_label("Audience", "expand"))
    assert res.ok is True and clk == ["MORE"]


def test_l_a_publish_overlay_over_the_target_is_not_clicked(page):
    body = ('<div><button id="SAFE" style="width:200px;height:50px">Show more</button></div>'
            '<div style="position:absolute;left:0;top:0;width:400px;height:100px"><button id="PUBOVER" '
            'style="width:400px;height:100px;opacity:0.99">Publish</button></div>')
    res, clk, _ = probe(page, body, by_label("Show more"))
    assert res.action == "give_up" and res.reason.startswith("hit_") and clk == []


def test_suggest_applies_the_same_checks_as_act(page):
    body = '<div><button id="PUB">Publicar</button></div>'
    res, clk, _ = probe(page, body, by_label("Publicar"), mode="suggest")
    assert res.action == "give_up" and clk == []


# N7: клиент, созданный нами, закрывается
def test_n7_the_client_we_built_is_closed(page, monkeypatch):
    page.set_content(_html("<div><button>Show more</button></div>"))
    cl = FakeClient(pick("Show more"))
    closed = []
    cl.close = lambda: closed.append(1)
    monkeypatch.setattr(ar, "_make_client", lambda: (cl, ""))
    res = ar.rescue(page, step="s", goal="g", error="e", budget=ar.RescueBudget(), telemetry=FakeTelemetry(), mode="act")
    assert res.ok is True and closed == [1]


def test_n5_the_anchor_search_has_a_time_cap(page, monkeypatch):
    monkeypatch.setattr(ar, "ANCHOR_SCAN_MS", 1)
    page.set_content(_html("".join(f"<p>text line {i}</p>" for i in range(5000)) + "<button>Show more</button>"))
    t0 = time.monotonic()
    s = ar.build_summary(page, anchor="no such anchor anywhere")
    assert time.monotonic() - t0 < 5 and "Show more" in s.text


# B8: чистка — настоящие утечки с ревью (имена заменены синтетическими)
LEAKS = ["B96", "Story14", "Doc120", "Investment_35_06.10", "FacebookDoc120", "facebookdoc120", "9403A",
         "Pixel KLD_BFL_B96", "Pixel B90", "Pixel FacebookDoc120", "06.10", "ES Pro Meli 2"]


@pytest.mark.parametrize("text", LEAKS)
def test_b8_no_token_with_a_digit_survives(text):
    assert not re.search("[0-9]", ar.scrub_text(text)), ar.scrub_text(text)


def test_b8_account_heading_and_campaign_tail_are_scrubbed():
    out = ar.scrub_text("1234567890123 - ES Pro Meli")
    assert out == "<account>", out
    assert ar.scrub_text("ES Pro Meli - 1234567890123") == "<account>"
    out = ar.scrub_text("Mix_A_B_C|Investment_35_06.10")
    assert "Investment" not in out and out.endswith("| <name>"), out
    assert ar.scrub_text("Pixel Something") == "Pixel <name>"


def test_b8_the_summary_of_a_page_with_those_leaks_is_clean(page):
    body = ('<h2 role="heading">1234567890123 - ES Pro Meli</h2><h2 role="heading">Story14</h2>'
            '<h2 role="heading">Mix_A_B_C|Investment_35_06.10</h2>'
            '<div role="combobox" aria-label="Facebook Page">Sample Page Name - XY</div>'
            '<div role="combobox">Another Page Name</div>'
            '<button>Pixel KLD_BFL_B96</button><button>Pixel B90</button><button>Pixel FacebookDoc120</button>'
            '<table><tr><td><a href="#">sample engagement campaign</a></td></tr></table>'
            '<div><a href="#">sample traffic campaign</a></div>'
            '<div><button>Show more</button></div>')
    page.set_content(_html(body))
    s = ar.build_summary(page)
    flat = re.sub(r"^\[\d+\] ", "", s.text, flags=re.M)
    assert not re.search("[0-9]", flat), flat
    assert "Sample Page Name" not in flat and "Another Page Name" not in flat and "engagement" not in flat and "traffic" not in flat
    assert "<field>" in flat


def test_b8_names_from_the_caller_are_replaced(page):
    page.set_content(_html("<h2 role='heading'>Quiet Ocean Studio - main</h2><div><button>Show more</button></div>"))
    assert "Quiet Ocean" in ar.build_summary(page).text
    assert "Quiet Ocean" not in ar.build_summary(page, names=("Quiet Ocean Studio",)).text


# ═══ раунд 2 ревью: белый список как основной слой, пробы click_probe2/scrub_probe2/listpage ═══

FOOT = "/am/editor_drawer/editor:ad_set/editor_drawer_footer"


def probe_clicked_nothing(page, body, choose, **kw):
    res, clk, esc = probe(page, body, choose, **kw)
    assert clk == [] and esc == 0, (res, clk, esc)
    return res


# ── позитивный белый список ──────────────────────────────────────────────────

@pytest.mark.parametrize("label", ["Next", "Continue", "OK", "Save", "More options", "Create", "Confirm", "Done",
                                   "Apply", "Add component", "Get started", "Boost", "Audience details"])
def test_allowlist_click_refuses_everything_not_listed(page, label):
    res = probe_clicked_nothing(page, f'<div><button id="X">{label}</button></div>', by_label(label))
    assert res.action == "give_up" and res.reason.startswith("not_allowlisted:"), res.reason


@pytest.mark.parametrize("label", ["Show more", "See more", "Ver más", "Mostrar más", "Ver mais", "Show less"])
def test_allowlist_click_allows_the_short_list(page, label):
    res, clk, _ = probe(page, f'<div><button id="X">{label}</button></div>', by_label(label))
    assert res.ok is True and clk == ["X"], res.reason


@pytest.mark.parametrize("label,ok", [("Audience", True), ("Placements", True), ("Conjuntos de anuncios", True),
                                      ("Posicionamentos", True), ("Billing", False), ("Settings", False)])
def test_allowlist_tabs_by_name(page, label, ok):
    res, clk, _ = probe(page, f'<div><div role="tab" id="T" aria-selected="false">{label}</div></div>', by_label(label))
    assert res.ok is ok and (len(clk) == 1) is ok, (label, res.reason)


@pytest.mark.parametrize("label", ["Close", "Cancel", "Not now", "Skip", "Dismiss", "Got it", "Maybe later", "Later",
                                   "No thanks", "Cerrar", "Cancelar", "Ahora no", "Omitir", "Entendido", "Más tarde",
                                   "No, gracias", "Fechar", "Agora não", "Pular", "Entendi", "Mais tarde", "×", "X"])
def test_allowlist_close_dialog_labels_in_three_languages(page, label):
    body = f'<div role="dialog"><h2>Heads up</h2><span>Something new is here</span><div role="button" id="C" aria-label="{label}">{label}</div></div>'
    res, clk, _ = probe(page, body, lambda p, pg: {"action": "close_dialog", "index": 0})
    assert res.ok is True and clk == ["C"], (label, res.reason)


def test_allowlist_close_dialog_refuses_other_labels(page):
    body = ('<div role="dialog"><h2>Heads up</h2><span>New</span><div role="button" id="A">Details</div>'
            '<div role="button" id="B">Add component</div></div>')
    for label in ("Details", "Add component"):
        res, clk, _ = probe(page, body, by_label(label, "close_dialog"))
        assert res.reason.startswith("not_allowlisted") and clk == [], (label, res.reason)


@pytest.mark.parametrize("text", ["Discard changes?", "You have unsaved changes", "Leave this page?", "You will lose your work",
                                  "Descartar cambios", "Cambios sin guardar", "Salir de la página", "Alterações sem salvar",
                                  "Sair da página", "Se perderán los datos"])
def test_allowlist_cancel_and_close_are_refused_on_dialogs_that_mention_losing_work(page, text):
    for label in ("Cancel", "Close", "Cancelar", "Fechar"):
        body = (f'<div role="dialog"><h2>Careful</h2><span>{text}</span>'
                f'<div role="button" id="C" aria-label="{label}">{label}</div></div>')
        res, clk, _ = probe(page, body, lambda p, pg: {"action": "close_dialog", "index": 0})
        assert res.action == "give_up" and clk == [], (text, label, res.reason)


def test_allowlist_click_on_a_close_word_is_refused_even_inside_a_dialog(page):
    body = '<div role="dialog"><h2>Hi</h2><span>Info</span><div role="button" id="C" aria-label="Skip">Skip</div></div>'
    res, clk, _ = probe(page, body, by_label("Skip", "click"))
    assert res.reason == "close_only_via_close_dialog" and clk == []


def test_allowlist_expand_needs_a_collapsed_state(page):
    page.set_content(_html("<div><button aria-expanded='false' id='A'>Placements</button></div>"
                           "<div><div role='button' aria-pressed='false' id='B'>Audience summary</div></div>"
                           "<div><button id='C'>Plain</button></div><div><button aria-expanded='true'>Open</button></div>"))
    assert run(page, FakeClient(by_label("Placements", "expand"))).ok is True
    assert run(page, FakeClient(by_label("Audience summary", "expand"))).ok is True
    assert clicked(page) == ["A", "B"]
    assert run(page, FakeClient(by_label("Plain", "expand"))).reason == "not_collapsed"
    assert run(page, FakeClient(by_label("Open", "expand"))).reason == "not_collapsed"


def test_allowlist_a_collapsed_element_with_a_forbidden_label_is_still_refused(page):
    res = probe_clicked_nothing(page, '<div><button aria-expanded="false">Delete options</button></div>',
                                by_label("Delete options", "expand"))
    assert res.reason.startswith("forbidden")


def test_allowlist_press_escape_needs_a_real_clean_dialog(page):
    res, clk, esc = probe(page, '<div role="dialog"><h2>Heads up</h2><span>Info</span></div>',
                          lambda p, pg: {"action": "press_escape"})
    assert res.ok is True and esc == 1
    for body in ('<div role="dialog"><h2>Heads up</h2><span>Discard changes?</span></div>',
                 '<div role="dialog"><h2>Heads up</h2><span>Unsaved edits</span></div>',
                 '<div role="dialog"><textarea>typed</textarea></div>', '<div><h2>No dialog</h2></div>'):
        res, clk, esc = probe(page, body, lambda p, pg: {"action": "press_escape"})
        assert res.action == "give_up" and esc == 0, body


def test_allowlist_scroll_to_is_always_allowed_but_never_clicks(page):
    res, clk, _ = probe(page, '<div style="height:3000px"></div><div><button id="P">Publish</button></div>',
                        by_label("Publish", "scroll_to"))
    assert res.ok is True and clk == [] and page.evaluate("window.scrollY") > 1000


def test_allowlist_a_missing_table_refuses_everything(page, monkeypatch):
    monkeypatch.setattr(ar, "_table", lambda: None)
    res = probe_clicked_nothing(page, '<div><button id="X">Show more</button></div>', by_label("Show more"))
    assert res.action == "give_up" and "forbidden_table_missing" in res.reason


def test_the_table_in_the_yaml_has_the_allowlist():
    import yaml
    from src.fb_ads_rpa import wizard_guard as wg

    raw = yaml.safe_load(wg._ZONES_PATH.read_text(encoding="utf-8"))["ai_rescue"]
    assert {"close_dialog", "click", "tab"} <= set(raw["allow"])
    for label in ("Next", "Continue", "OK", "More options"):
        assert ar.norm_text(label) not in {ar.norm_text(x) for x in raw["allow"]["click"]}


# ── R2-B1: «редактор» нельзя подделать меткой data-surface ───────────────────

def test_r2b1_a_discard_confirm_inside_the_footer_surface_is_a_real_dialog(page):
    body = ('<div><input placeholder="Enter your ad set name here..." value="x"><div data-surface="' + FOOT + '">'
            '<div role="dialog" data-surface="' + FOOT + '/confirm"><h2>Discard changes?</h2>'
            '<span>Unsaved edits will be lost.</span><div><div role="button" id="OK" data-surface="' + FOOT +
            '/confirm/lib:ok">OK</div></div></div></div></div>')
    for action in ("click", "close_dialog"):
        res = probe_clicked_nothing(page, body, by_label("OK", action))
        assert res.action == "give_up", (action, res.reason)


def test_r2b1_a_publish_confirm_carrying_a_publish_marker_is_a_real_dialog(page):
    body = ('<div role="dialog"><h2>Publish your campaign?</h2><span>Ads start delivering now.</span>'
            '<div data-surface="/am/editor_drawer/editor:ad/simple_create_publish/confirm"><div>'
            '<div role="button" id="GO">Confirm</div></div></div></div>')
    res = probe_clicked_nothing(page, body, by_label("Confirm"))
    assert res.reason.startswith("forbidden_dialog")


def test_r2b1_a_dialog_nested_in_the_editor_dialog_is_real_and_checked(page):
    body = ('<div role="dialog" style="width:100vw;height:100vh"><input placeholder="Enter your ad set name here" value="x">'
            '<div role="dialog"><h2>Your draft will be discarded</h2><div role="button" id="OK">Not now</div></div></div>')
    res = probe_clicked_nothing(page, body, by_label("Not now", "close_dialog"))
    assert res.reason.startswith("forbidden_dialog") or res.reason == "close_blocked_by_dialog_text"
    harmless = body.replace("Your draft will be discarded", "Heads up")
    res, clk, _ = probe(page, harmless, by_label("Not now", "close_dialog"))
    assert res.ok is True and clk == ["OK"]


# ── R2-B2: прокрутка и клик ──────────────────────────────────────────────────

def test_r2b2_smooth_scroll_never_lands_on_the_publish_button_below(page):
    body = ('<style>html{scroll-behavior:smooth}</style><div style="height:1300px"></div>'
            '<div><button id="SAFE" style="height:40px;width:300px">Show more</button></div><div style="height:150px"></div>'
            '<div><button id="PUB" style="height:400px;width:300px">Publish</button></div><div style="height:2000px"></div>')
    for _ in range(3):
        res, clk, _ = probe(page, body, by_label("Show more"))
        page.wait_for_timeout(500)
        assert res.ok is True and "PUB" not in page.evaluate("window.clicked") and clk == ["SAFE"], (res.reason, clk)


def test_r2b2_smooth_scroll_in_a_container(page):
    body = ('<style>#sc{scroll-behavior:smooth;overflow:auto;height:800px}</style><div id="sc"><div style="height:1300px"></div>'
            '<div><button id="SAFE" style="height:40px;width:300px">Show more</button></div><div style="height:150px"></div>'
            '<div><button id="PUB" style="height:400px;width:300px">Publish</button></div><div style="height:2000px"></div></div>')
    res, clk, _ = probe(page, body, by_label("Show more"))
    page.wait_for_timeout(500)
    assert "PUB" not in page.evaluate("window.clicked")


def test_r2b2_a_refused_pick_never_scrolls_the_page(page):
    body = '<div style="height:3000px"></div><div><button id="P">Publish</button></div>'
    res = probe_clicked_nothing(page, body, by_label("Publish"))
    assert res.action == "give_up" and page.evaluate("window.scrollY") == 0
    body = '<div style="height:3000px"></div><div><button id="P">Next</button></div>'
    res = probe_clicked_nothing(page, body, by_label("Next"))
    assert res.reason.startswith("not_allowlisted") and page.evaluate("window.scrollY") == 0


def test_r2b2_a_target_that_keeps_moving_is_not_clicked(page):
    body = ('<style>@keyframes mv{from{transform:translateX(0)}to{transform:translateX(600px)}}'
            '#M{animation:mv 0.5s linear infinite alternate}</style><div><button id="M">Show more</button></div>')
    res, clk, _ = probe(page, body, by_label("Show more"))
    assert res.action == "give_up" and res.reason == "rect_unstable" and clk == []


# ── R2-B3: словоформы, мягкий перенос, нулевая ширина ───────────────────────

INFLECTED = ["Your draft will be deleted.", "Changes will be discarded.", "Se eliminará el borrador.",
             "2 ads will be removed from this campaign.", "Deleting", "Unpublished changes", "Publishing now",
             "Removing ads", "Borrando anuncios", "Anuncio eliminado", "Descartando cambios", "Quitaremos el anuncio",
             "Anúncio excluído", "Apagaremos o anúncio", "Descartado", "Publicado", "Pagamento recusado", "Facturación",
             "Aceptaremos los términos", "Aceitar termos", "Cerrar sesión ahora", "Sair da conta", "Payments overdue",
             "Pub­lish", "Del​ete", "Dele⁠te", "Pub﻿lish"]


@pytest.mark.parametrize("text", INFLECTED)
def test_r2b3_inflected_and_hidden_character_forms_are_forbidden(text):
    assert ar.forbidden_reason([text]), text
    assert ar.forbidden_reason(["Show more"], text), text


@pytest.mark.parametrize("text", ["Your draft will be deleted.", "Changes will be discarded.", "Se eliminará el borrador.",
                                  "2 ads will be removed from this campaign."])
def test_r2b3_probes_n3_through_n3d_click_nothing(page, text):
    body = f'<div role="dialog"><h2>Heads up</h2><span>{text}</span><div role="button" id="OK">Continuar</div></div>'
    assert probe_clicked_nothing(page, body, by_label("Continuar")).action == "give_up"


def test_r2b3_probes_n3e_n5_n7_labels(page):
    for label in ("Deleting", "Unpublished changes", "Pub­lish"):
        res = probe_clicked_nothing(page, f'<div><button id="X">{label}</button></div>', by_label(label[:3]))
        assert res.reason.startswith("forbidden"), (label, res.reason)


@pytest.mark.parametrize("text", ["Show more", "Page settings", "Facebook Page", "Audience", "Pagination", "Next step",
                                  "Placements", "Targeting", "Languages", "Budget", "Schedule"])
def test_r2b3_stems_do_not_swallow_ordinary_words(text):
    assert ar.forbidden_reason([text]) is None, text


# ── R2-B4: имена кампаний и кабинетов ────────────────────────────────────────

@pytest.mark.parametrize("text", ["CO_bnc_All_Video_GMBL | extra", "CO_bnc_All_Video_Day", "name | CO_bnc_All_Video_Day",
                                  "2408 - ES Fav pro", "123 - Some Account Name", "Some Account Name - 2408",
                                  "KW_TRL_x"])
def test_r2b4_campaign_and_account_names_are_scrubbed(text):
    out = ar.scrub_text(text)
    for needle in ("CO_", "bnc", "GMBL", "Fav", "Some Account", "TRL", "extra", "Day"):
        assert needle not in out, (text, out)
    assert not re.search("[0-9]", out), out


def test_r2b4_the_underscore_token_is_a_name_before_and_after_the_bar():
    assert ar.scrub_text("CO_bnc_All_Video_GMBL | x") == "<name> | <name>"
    assert ar.scrub_text("hello CO_bnc_All end") == "hello <name> end"


# ── R2-N1: настоящий список Meta ─────────────────────────────────────────────

REAL_LIST = ('<div role="table" style="width:1500px;height:520px"><div role="row">Campaign Delivery Results</div>'
             '<div role="presentation"><div>' + "".join(
                 f'<div><input type="checkbox" role="switch" aria-label="On/off"><button>Open row {i}</button></div>'
                 for i in range(6)) + '</div></div></div><div><button>Show more</button></div>')


def test_r2n1_a_div_role_table_with_one_row_is_a_list_page(page):
    page.set_content(_html(REAL_LIST))
    cl = FakeClient(by_label("Show more"))
    res = run(page, cl)
    assert res.reason == "list_page" and cl.calls == 0 and clicked(page) == []


def test_r2n1_controls_inside_role_table_are_hidden_and_refused_with_a_dialog_open(page):
    dlg = ('<div role="dialog" style="position:fixed;left:300px;top:100px;width:400px;height:300px;background:#fff">'
           '<h2>Hi</h2><span>Info</span><div role="button" id="D">Not now</div></div>')
    page.set_content(_html(REAL_LIST + dlg))
    s = ar.build_summary(page)
    assert "On/off" not in s.text and "Open row" not in s.text and "Not now" in s.text
    res = run(page, FakeClient(by_label("Not now", "close_dialog")))
    assert res.ok is True and clicked(page) == ["D"]


def test_r2n1_a_small_role_table_in_the_editor_is_not_a_list_page_but_its_controls_are_refused(page):
    body = ('<div role="table" style="width:200px;height:60px"><div role="row"><button id="B">Show more</button></div></div>'
            '<div><button id="C">Show more</button></div>')
    page.set_content(_html(body))
    s = ar.build_summary(page)
    assert s.list_page is False and len(s.elements) == 1


# ── R2-N2: shadow DOM ────────────────────────────────────────────────────────

def test_r2n2_a_target_with_a_shadow_root_is_refused(page):
    body = ('<div><button id="NB" style="width:200px;height:40px">Show more<x-s id="XS"></x-s></button></div>'
            '<script>document.getElementById("XS").attachShadow({mode:"open"}).innerHTML="<span>Publish</span>";</script>')
    res = probe_clicked_nothing(page, body, by_label("Show more"))
    assert res.reason == "shadow_dom"


def test_r2_iframe_overlay_is_refused(page):
    body = ('<div><button id="SAFE" style="width:300px;height:60px">Show more</button></div>'
            '<iframe srcdoc="<button>Publish</button>" style="position:absolute;left:0;top:0;width:400px;height:120px;border:0">'
            '</iframe>')
    res = probe_clicked_nothing(page, body, by_label("Show more"))
    assert res.reason.startswith("hit_")


def test_norm_text_strips_invisible_characters_and_maps_cross_glyphs():
    assert ar.norm_text("Pub­lish") == "publish"
    assert ar.norm_text("​Del‌ete⁠") == "delete"
    assert ar.norm_text("×") == "x" and ar.norm_text("✕") == "x"


# ═══ раунд 3 ревью: поля, ярлык expand, ссылки, close_blockers, узкие основы, окно с полем имени ═══

SEC_DLG = ('<div role="dialog" style="position:fixed;left:300px;top:100px;width:400px;height:300px;background:#fff">'
           '<h2>Hi</h2><span>Info</span><div role="button" id="D">Not now</div></div>')


# R3-B1: подписи полей — только placeholder
def test_r3b1_the_account_picker_never_shows_its_labelledby_text(page):
    body = ('<span id="acc">Sample Beauty Studio - 1234567890 ( )</span>'
            '<div role="combobox" aria-labelledby="acc" aria-expanded="false" style="width:300px;height:30px">picker</div>'
            '<input role="combobox" aria-label="Sample Beauty Studio" value="Sample Beauty Studio">'
            '<select aria-label="Sample Beauty Studio"><option>Sample Beauty Studio</option></select>'
            '<input type="text" aria-label="Sample Beauty Studio" value="Sample Beauty Studio" placeholder="Search here">'
            + SEC_DLG)
    page.set_content(_html(body))
    s = ar.build_summary(page)
    assert "Sample" not in s.text and "Beauty" not in s.text and "picker" not in s.text, s.text
    assert s.text.count('"<field>"') >= 3 and 'textbox "Search here"' in s.text


# R3-N1: expand только по названию раздела
REAL_EXPAND_LABELS = ["More", "More items", "Action Menu", "Share", "Create new", "Open Dropdown", "Toggle", "Include",
                      "Add media", "Set up creative", "Campaign bid strategy Highest volume", "Advantage+ audience",
                      "Advantage+ placements"]
SECTIONS = ["Audience", "Placements", "Budget", "Schedule", "Budget & schedule", "Locations", "Languages",
            "Detailed targeting", "Demographics", "Age", "Gender", "Optimization & delivery", "Conversion",
            "Destination", "Tracking", "Ad setup", "Creative", "Público", "Ubicaciones", "Presupuesto", "Idiomas",
            "Ubicaciones geográficas", "Segmentación detallada", "Conversión", "Destino", "Configuración del anuncio",
            "Posicionamentos", "Orçamento", "Programação", "Segmentação detalhada", "Conversão", "Criativo"]


@pytest.mark.parametrize("label", REAL_EXPAND_LABELS)
@pytest.mark.parametrize("attr", ['aria-expanded="false"', 'aria-pressed="false"'])
def test_r3n1_expand_refuses_every_real_non_section_label(page, label, attr):
    res = probe_clicked_nothing(page, f'<div><div role="button" id="X" {attr}>{label}</div></div>',
                                by_label(label, "expand"))
    assert res.action == "give_up" and res.reason.startswith(("not_allowlisted", "forbidden", "has_interactive")), res.reason


@pytest.mark.parametrize("label", SECTIONS)
def test_r3n1_expand_allows_section_names(page, label):
    res, clk, _ = probe(page, f'<div><div role="button" id="X" aria-pressed="false">{label}</div></div>',
                        lambda p, pg: {"action": "expand", "index": 0})
    assert res.ok is True and clk == ["X"], (label, res.reason)


def test_r3n1_a_section_card_heading_with_a_long_summary_passes_but_a_toggle_does_not(page):
    page.set_content(_html('<div><div role="button" id="C" aria-pressed="false">Audience Custom audiences Locations Age '
                           'Gender Languages</div></div><div><div role="button" id="T" aria-pressed="false">'
                           'Advantage+ audience</div></div>'))
    assert run(page, FakeClient(by_label("Audience", "expand"))).ok is True
    assert run(page, FakeClient(by_label("Advantage+ audience", "expand"))).reason.startswith("not_allowlisted")
    assert clicked(page) == ["C"]


# R3-N2: ссылки
def test_r3n2_a_link_with_role_button_is_never_a_target(page):
    for body in ('<div><a id="SM" href="https://example.org/help" role="button">Show more</a></div>',
                 '<div><a href="#x"><span id="SM" role="button">Show more</span></a></div>'):
        res = probe_clicked_nothing(page, body, by_label("Show more"))
        assert res.reason == "link_target", res.reason
        assert page.url == "about:blank"


# R3-N3: close_blockers
@pytest.mark.parametrize("title,text,label", [
    ("Non-discrimination policy", "By continuing you confirm your ads comply with our advertising policies.", "Got it"),
    ("Review your campaign", "Everything looks good. You can go live now or later.", "Skip"),
    ("Lead Ads Terms", "Please read before continuing.", "Got it"),
    ("Consent", "I consent to the processing.", "Dismiss"),
    ("Certify", "You certify that the ad is yours.", "Got it"),
    ("Please acknowledge", "Acknowledge the notice.", "Got it"),
    ("Política de no discriminación", "Al continuar confirmas que tus anuncios cumplen.", "Entendido"),
    ("Revisar y publicar", "Puedes salir en vivo ahora.", "Omitir"),
    ("Consentimiento", "Das tu consentimiento.", "Entendido"),
    ("Política de não discriminação", "Ao continuar você confirma.", "Entendi"),
    ("Revisar e publicar", "Você pode ir ao vivo agora.", "Pular"),
    ("Declaração", "Você declara que concorda.", "Entendi"),
])
def test_r3n3_got_it_and_skip_never_acknowledge_policies_or_skip_a_review(page, title, text, label):
    body = (f'<div role="dialog"><h2>{title}</h2><span>{text}</span>'
            f'<div><div role="button" id="X">{label}</div></div></div>')
    res = probe_clicked_nothing(page, body, by_label(label, "close_dialog"))
    assert res.action == "give_up", (title, res.reason)
    res = probe_clicked_nothing(page, body, lambda p, pg: {"action": "press_escape"})
    assert res.action == "give_up", (title, res.reason)


def test_r3n3_a_harmless_got_it_still_works(page):
    body = '<div role="dialog"><h2>New</h2><span>We moved a button.</span><div><div role="button" id="X">Got it</div></div></div>'
    res, clk, _ = probe(page, body, by_label("Got it", "close_dialog"))
    assert res.ok is True and clk == ["X"]


# R3-N6: узкие основы — испанский интерфейс остаётся рабочим
@pytest.mark.parametrize("text", ["Público", "Cuenta publicitaria", "publicidad", "Conta de anúncios", "Exclusões",
                                  "Excluded placements", "Anuncios publicitarios", "Publico objetivo"])
def test_r3n6_ordinary_spanish_and_portuguese_words_pass(text):
    assert ar.forbidden_reason([text]) is None, text


@pytest.mark.parametrize("text", ["Publicar", "Publicado", "Publicada", "Publish", "Published", "publíquese",
                                  "Publicando", "Publicación", "Publicar anúncio", "Publicadas", "Excluir", "Exclua",
                                  "Excluído", "Excluidos", "Excluye", "Excluya", "Excluir anúncio"])
def test_r3n6_verb_forms_are_still_refused(text):
    assert ar.forbidden_reason([text]), text


def test_r3n6_the_spanish_audience_card_and_the_publico_tab_work(page):
    res, clk, _ = probe(page, '<div><div role="button" id="C" aria-pressed="false">Público</div></div>',
                        lambda p, pg: {"action": "expand", "index": 0})
    assert res.ok is True and clk == ["C"]
    res, clk, _ = probe(page, '<div><div role="tab" id="T">Público</div></div>', lambda p, pg: {"action": "click", "index": 0})
    assert res.ok is True and len(clk) == 1
    body = ('<div role="dialog"><h2>Cuenta publicitaria</h2><span>Anuncios y publicidad</span>'
            '<div role="button" id="C" aria-label="Cerrar">x</div></div>')
    res, clk, _ = probe(page, body, by_label("Cerrar", "close_dialog"))
    assert res.ok is True and clk == ["C"]


# R3-N4: окно с полем имени кампании не закрывается никогда
def test_r3n4_a_small_editor_like_dialog_with_an_empty_name_is_not_closable(page):
    body = ('<div role="dialog" style="position:fixed;right:0;top:0;width:500px;height:800px">'
            '<input placeholder="Enter your ad set name here..."><h2>Ad set</h2><span>Budget</span>'
            '<div role="button" id="CLS" aria-label="Close">x</div></div>')
    res = probe_clicked_nothing(page, body, by_label("Close", "close_dialog"))
    assert res.reason == "editor_dialog_not_closable"
    res = probe_clicked_nothing(page, body, lambda p, pg: {"action": "press_escape"})
    assert res.reason == "editor_dialog_not_closable"


def test_r3_editor_header_close_is_refused_both_ways_and_tabs_and_naming_escape_work_by_design(page):
    body = ('<div><input placeholder="Enter your campaign name here..." value="c"><h2>Campaign</h2>'
            '<div role="button" id="CLS">Close</div></div>')
    assert probe_clicked_nothing(page, body, by_label("Close")).action == "give_up"
    assert probe_clicked_nothing(page, body, by_label("Close", "close_dialog")).reason == "not_a_dialog_close_button"
    res, clk, _ = probe(page, '<div role="tablist"><div role="tab" id="TAB">Ads</div></div>', by_label("Ads"))
    assert res.ok is True
    res, clk, esc = probe(page, '<div role="dialog"><h2>Ad set name</h2><span>Name your ad sets consistently.</span>'
                                '<div role="button">Cancel</div></div>', lambda p, pg: {"action": "press_escape"})
    assert res.ok is True and esc == 1
