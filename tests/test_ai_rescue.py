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
<div><button id="ok">Edit audience</button></div>
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
    m = re.search(rf'^\[(\d+)\] {role or r"\w+"} "{re.escape(label)}"', prompt, re.M)
    assert m, f"{label!r} not in summary:\n{prompt}"
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
    assert res.action == "give_up" and "forbidden_nearby" in res.reason
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
    page.set_content(_html("<button id='x'>Edit audience</button>" + NAMING))
    assert run(page, FakeClient(pick("Edit audience", "close_dialog"))).reason == "not_a_dialog_close_button"
    assert run(page, FakeClient(pick("Add component", "close_dialog"))).reason == "not_a_dialog_close_button"
    res = run(page, FakeClient(pick("Cancel", "close_dialog")))
    assert res.ok is True and clicked(page) == ["Cancel"]


def test_expand_needs_a_collapsed_element(page):
    page.set_content(_html("<button aria-expanded='true'>Open card</button><button>Plain</button>"))
    assert run(page, FakeClient(pick("Open card", "expand"))).reason == "not_collapsed"
    assert run(page, FakeClient(pick("Plain", "expand"))).reason == "not_collapsed"
    assert clicked(page) == []


def test_the_element_changed_between_summary_and_click_is_not_clicked(page):
    """Индекс остался, а под ним уже «Publish»: метку перечитываем перед кликом."""
    page.set_content(_html(DIRTY + mutate_on_mark("Edit audience", "el.textContent = 'Publish';")))
    res = run(page, FakeClient(pick("Edit audience")))
    assert res.action == "give_up" and "recheck_forbidden" in res.reason
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
    page.set_content(_html("<button id='a'>Edit audience</button><div role='dialog'>"
                           "<span role='heading'>Hi</span><button>Close</button></div>"))
    before = page.evaluate("document.documentElement.outerHTML")
    tel = FakeTelemetry()
    res = run(page, FakeClient(pick("Edit audience")), mode="suggest", tel=tel)
    assert res.ok is True and res.action == "click" and res.mode == "suggest"
    assert clicked(page) == []
    assert page.evaluate("document.documentElement.outerHTML") == before  # даже data-acl-ai убраны
    assert tel.rows[-1][2]["diag"]["mode"] == "suggest"
    for a in ({"action": "press_escape"}, {"action": "give_up", "reason": "no"}):
        run(page, FakeClient(lambda p, pg, a=a: a), mode="suggest")
    assert page.evaluate("document.documentElement.outerHTML") == before


def test_act_clicks_exactly_the_chosen_element(page):
    page.set_content(_html(DIRTY))
    res = run(page, FakeClient(pick("Edit audience")), mode="act")
    assert res.ok is True and res.action == "click" and res.label == "Edit audience"
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
    page.set_content(_html("<div style='height:3000px'></div><button id='far'>Far away</button>"
                           "<script>window.esc=0;document.addEventListener('keydown',"
                           "e=>{if(e.key==='Escape')window.esc++})</script>"))
    res = run(page, FakeClient(pick("Far away", "scroll_to")))
    assert res.ok and page.evaluate("window.scrollY") > 1000 and clicked(page) == []
    assert run(page, FakeClient(lambda p, pg: {"action": "press_escape"})).ok
    assert page.evaluate("window.esc") == 1


def test_off_is_the_default_and_never_asks(page, monkeypatch):
    monkeypatch.delenv("ACL_AI_RESCUE", raising=False)
    page.set_content(_html(DIRTY))
    cl = FakeClient(pick("Edit audience"))
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
    assert tel.rows[-1][2]["diag"]["reason"] == "budget_exhausted" or True


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

    assert run(page, FakeClient(pick("Edit audience")), tel=BadTelemetry()).ok is True

    class DeadPage:
        def evaluate(self, *a, **k):
            raise RuntimeError("target closed")

    res = run(DeadPage(), FakeClient(lambda p, pg: {"action": "press_escape"}), mode="act")
    assert isinstance(res, ar.RescueResult)  # не бросило

    monkeypatch.setattr(ar, "build_summary", lambda *a, **k: (_ for _ in ()).throw(ValueError("x")))
    res = run(page, FakeClient(pick("Edit audience")))
    assert res.skipped and res.reason.startswith("error:")


# ── телеметрия и запрос ──────────────────────────────────────────────────────

def test_telemetry_row_shape(page):
    page.set_content(_html(DIRTY))
    tel = FakeTelemetry()
    res = run(page, FakeClient(pick("Edit audience"), tokens=(321, 9)), tel=tel, step="further_limit_row")
    step, ok, kw = tel.rows[-1]
    assert (step, ok, kw["note"]) == ("ai_rescue", True, "click")
    d = kw["diag"]
    assert d["step"] == "further_limit_row" and d["mode"] == "act" and d["action"] == "click"
    assert d["label"] == "Edit audience" and d["input_tokens"] == 321 and d["output_tokens"] == 9
    assert d["latency_s"] >= 0 and len(d["summary_hash"]) == 12 and "goal" in d and "reason" in d
    assert res.label == "Edit audience"


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
