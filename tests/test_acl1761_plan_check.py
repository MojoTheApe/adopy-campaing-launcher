"""ACL-1761: «Check plan (no writes)» в билдере — план через Graph API, ни одной записи.

Сервер: /publish-plan только с dry_run=1 и только при ACL_FB_BACKEND=api;
план идёт путём чтения ACL-1760 (клиент без права записи), у поддельной Meta
не остаётся ни одной записи. Страница: отрисовка плана и отправка dry_run=1
проверяются в Node (браузерный тест — при свободной памяти больше 6 ГБ).
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_api import graph as g  # noqa: E402
from tests.test_acl1761_api_create import ACT, FakeMeta, _draft  # noqa: E402


# ───────────────────────── «Check plan (no writes)» ─────────────────────────

class _FormReq:
    """Запрос с формой — эндпоинт зовётся напрямую, без TestClient."""

    def __init__(self, fields):
        self._fields = dict(fields)

    async def form(self, **_limits):  # read_form передаёт пределы разбора
        return self._fields


def _plan_endpoint(monkeypatch, fields, backend="api"):
    import asyncio

    from src.web import app as web
    from src.web import publish_api

    calls = []
    monkeypatch.setenv("ACL_FB_BACKEND", backend)
    monkeypatch.setattr(web, "_parse_build_upload_form", lambda form: ("MX", "Acc", [{"name": "C1"}]))
    monkeypatch.setattr(publish_api, "check_plan", lambda slots, **kw: calls.append((slots, kw)) or {
        "account": kw["account_label"], "results": [], "dry_run": True, "error": ""})
    resp = asyncio.run(web.publish_plan(_FormReq(fields)))
    return resp.status_code, json.loads(resp.body), calls


def test_plan_endpoint_runs_only_as_a_dry_run(monkeypatch):
    code, body, calls = _plan_endpoint(monkeypatch, {"dry_run": "1"})
    assert code == 200 and body["dry_run"] is True and calls == [([{"name": "C1"}],
                                                                  {"account_label": "Acc", "country": "MX"})]
    code, body, calls = _plan_endpoint(monkeypatch, {"dry_run": "0"})
    assert code == 400 and calls == []
    code, body, calls = _plan_endpoint(monkeypatch, {})
    assert code == 400 and calls == []


def test_plan_endpoint_needs_the_api_backend(monkeypatch):
    code, body, calls = _plan_endpoint(monkeypatch, {"dry_run": "1"}, backend="rpa")
    assert code == 409 and "ACL_FB_BACKEND=api" in body["error"] and calls == []


def _read_session(monkeypatch, meta, *, writes=False):
    from contextlib import contextmanager
    from types import SimpleNamespace

    from src.analysis_agent import account_spend as _as
    from src.analysis_agent import account_spend_api as _api

    opened = []

    @contextmanager
    def session(account):
        client = g.GraphClient(meta, allow_writes=writes, sleep=lambda s: None, min_interval_s=0.0,
                               max_calls=24)
        opened.append(client)
        yield SimpleNamespace(client=client, act=ACT, account=account, notes={}, keep_next=False)

    monkeypatch.setattr(_api, "_session", session)
    monkeypatch.setattr(_as, "_profile_key", lambda account, *a, **k: "p-" + account)
    return opened


def test_check_plan_uses_the_read_session_and_writes_nothing(tmp_path, monkeypatch):
    from src.web import publish_api

    meta = FakeMeta()
    d = _draft(tmp_path, video=True, ads=2)
    d.warnings.append("Mixed budget in 'MX Leads 1' — using first slot")
    monkeypatch.setattr(publish_api, "build_drafts", lambda slots, **kw: [d])
    opened = _read_session(monkeypatch, meta)
    monkeypatch.setattr(publish_api, "ApiPublisher", publish_api.ApiPublisher)
    out = publish_api.check_plan([{"name": "x"}], account_label="Test", country="MX")
    assert out["error"] == "" and out["dry_run"] is True
    assert meta.posts == [] and opened[0].writes == 0 and opened[0].allow_writes is False
    (res,) = out["results"]
    assert res["dry_run"] is True and res["success"] is True and res["publishable"] is False
    assert "Mixed budget in 'MX Leads 1' — using first slot" in res["warnings"]
    summary = res["plan"]["summary"]
    assert summary["status"] == "PAUSED" and summary["daily_budget"] == 20.0 and summary["currency"] == "USD"
    assert summary["page_id"] == "111" and summary["pixel_id"] == "222"
    (aset,) = summary["ad_sets"]
    assert aset["countries"] == ["MX"] and aset["age"] == "25-55" and aset["language"] == "Spanish"
    assert aset["interests"] == [["Day trading", "Swing trading"]]
    assert aset["placements"].startswith("manual: Facebook Feed, Facebook Stories, Facebook Reels")
    assert "devices mobile/desktop" in aset["placements"]
    assert aset["creative_enhancements"] == "off: text_optimizations"
    assert [(a["format"], a["cta"], a["link"]) for a in aset["ads"]] == [
        ("image", "LEARN_MORE", "https://example.invalid/l"), ("video", "LEARN_MORE", "https://example.invalid/l")]
    assert aset["ads"][0]["file"] == "1000_img.png" and aset["ads"][0]["headline"] == "Head 0"


def test_check_plan_shows_plan_errors_per_campaign(tmp_path, monkeypatch):
    from src.web import publish_api

    meta = FakeMeta()
    bad = _draft(tmp_path, ads=1)
    bad.link = ""
    broken = _draft(tmp_path, ads=1)
    broken.name = broken.ad_sets[0].name = "MX Leads 2"
    broken.errors.append("Creative file not mapped for 1000")
    monkeypatch.setattr(publish_api, "build_drafts", lambda slots, **kw: [bad, broken])
    _read_session(monkeypatch, meta)
    out = publish_api.check_plan([{"name": "x"}], account_label="Test", country="MX")
    assert meta.posts == []
    assert "Tracking link is empty" in out["results"][0]["error"]
    assert "Creative file not mapped" in out["results"][1]["error"]


def test_check_plan_refuses_a_session_that_could_write(tmp_path, monkeypatch):
    from src.web import publish_api

    meta = FakeMeta()
    monkeypatch.setattr(publish_api, "build_drafts", lambda slots, **kw: [_draft(tmp_path, ads=1)])
    _read_session(monkeypatch, meta, writes=True)
    out = publish_api.check_plan([{"name": "x"}], account_label="Test", country="MX")
    assert out["results"] == [] and "must not be able to write" in out["error"] and meta.posts == []


# ── отрисовка плана и отправка dry_run=1 (Node; браузерный тест — при свободной памяти) ──

def _template_js(start: str, end: str) -> str:
    text = (ROOT / "src" / "web" / "templates" / "report.html").read_text(encoding="utf-8")
    i = text.index(start)
    return text[i:text.index(end, i)]


_PLAN_NODE = r"""
ESCAPER
const RealFD = FormData;
let lastSkip = null;
function cbFormDataSkipping(form, skip) { lastSkip = skip; const b = new RealFD(); b.set('camp_count', '1'); return { body: b }; }
function cbSlotTitle(slot) { return slot.title; }
function cbPickedIn(form, picked) { return picked.filter((s) => form.slots.indexOf(s) !== -1); }
function cbFormTitle(form) { return form.title; }
const fetched = [];
globalThis.fetch = async (url, init) => {
  fetched.push({ url: url, dry_run: init.body.get('dry_run'), activate: init.body.get('activate') });
  return { json: async () => DATA };
};
const fields = { camp_count: '2', camp_0_name: 'MX Leads 1', camp_1_name: 'MX Leads 2' };
const slotA = { title: 'MX Leads 1' }, slotB = { title: 'MX Leads 2' };
const form = { title: 'Test', slots: [slotA, slotB],
  querySelector: (sel) => { const m = /name="([^"]+)"/.exec(sel); return m && m[1] in fields ? { value: fields[m[1]] } : null; },
  querySelectorAll: (sel) => sel === '.cb-slot' ? form.slots : [] };
globalThis.document = { querySelectorAll: (sel) => sel === 'form.open-account-form' ? [form] : [] };
globalThis.window = globalThis;
JS
(async () => {
  const out = {};
  out.html = cbPlanHtml(DATA);
  out.errHtml = cbPlanHtml({ account: 'Bad', error: 'profile <closed>', results: [] });
  const box = { hidden: true, innerHTML: '' };
  const btn = { disabled: false };
  window.cbCheckPlan([slotB], box, btn);
  await new Promise((r) => setTimeout(r, 20));
  out.fetched = fetched;
  out.skip = lastSkip;
  out.box = box.innerHTML;
  out.btn = btn.disabled;
  process.stdout.write(JSON.stringify(out));
})();
"""

_PLAN_DATA = {
    "account": "Test", "dry_run": True, "error": "",
    "results": [
        {"campaign_name": "MX Leads 1", "dry_run": True, "success": True, "error": None,
         "warnings": ["Mixed budget — using first slot", "Dry run: nothing was created"],
         "plan": {"summary": {
             "campaign": "MX Leads 1", "status": "PAUSED", "objective": "OUTCOME_LEADS",
             "daily_budget": 20.0, "currency": "USD", "page_id": "111", "pixel_id": "222",
             "existing": False, "ad_sets": [{
                 "name": "MX Leads 1", "countries": ["MX"], "language": "Spanish", "age": "25-55",
                 "interests": [["Day trading", "Swing trading"], ["Stocks"]],
                 "placements": "automatic (Advantage+ placements)",
                 "ads": [{"name": "1000 - Text0", "format": "image", "file": "1000_img.png",
                          "headline": "<b>Head</b>", "primary_text": "[Offer] buy now",
                          "description": "Desc", "link": "https://example.invalid/l", "cta": "LEARN_MORE"}]}]}}},
        {"campaign_name": "MX Leads 2", "dry_run": True, "success": False,
         "error": "Tracking link is empty; the API backend needs it for the ads", "warnings": [], "plan": {}},
    ],
}


def _run_plan_node(data: dict) -> dict:
    """Настоящий JS шаблона (план, отправка и `aclEscHtml` самой страницы) в Node."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    js = _template_js("function cbPlanFormData(form, only) {", "/* Имя карточки для подтверждения. */")
    escaper = _template_js("function aclEscHtml(value) {", "  </script>")
    script = (_PLAN_NODE.replace("DATA", "__DATA__").replace("ESCAPER\n", escaper + "\n", 1)
              .replace("JS\n", js + "\n", 1))
    script = "const __DATA__ = " + json.dumps(data) + ";\n" + script
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
        fh.write(script)
        path = fh.name
    try:
        done = subprocess.run([node, path], capture_output=True, text=True, encoding="utf-8", timeout=60)
    finally:
        Path(path).unlink(missing_ok=True)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_the_node_harness_uses_the_templates_own_escaper():
    assert "function aclEscHtml" not in _PLAN_NODE
    assert "function aclEscHtml(value) {" in _template_js("function aclEscHtml(value) {", "  </script>")


def test_plan_html_and_the_button_posting_dry_run_in_node():
    out = _run_plan_node(_PLAN_DATA)
    html = out["html"]
    for part in ("MX Leads 1", "PAUSED", "20.00 USD / day", "page 111", "pixel 222", "MX", "age 25-55",
                 "language Spanish", "Day trading, Swing trading AND Stocks", "automatic (Advantage+",
                 "1000 - Text0", "image 1000_img.png", "&lt;b&gt;Head&lt;/b&gt;", "[Offer] buy now",
                 "Desc", "https://example.invalid/l", "CTA LEARN_MORE", "Mixed budget"):
        assert part in html, part
    assert "Dry run: nothing was created" not in html               # не шум
    assert 'cbd-plan-err" style="color:var(--bad)">✕ Tracking link is empty' in html
    assert "<b>" not in html and "profile &lt;closed&gt;" in out["errHtml"]
    # Кнопка шлёт только отмеченное, всегда dry_run=1 и activate=0, на /publish-plan.
    assert out["fetched"] == [{"url": "/publish-plan", "dry_run": "1", "activate": "0"}]
    assert out["skip"] == {"0": True}
    assert "Plan for <strong>Test</strong>" in out["box"] and out["btn"] is False


def test_the_plan_button_is_rendered_only_for_the_api_backend():
    text = (ROOT / "src" / "web" / "templates" / "report.html").read_text(encoding="utf-8")
    i = text.index("data-act=\"check-plan\"")
    assert "window.ACL_FB_BACKEND === 'api'" in text[i - 400:i]
    assert "window.ACL_FB_BACKEND = {{ (fb_backend or 'rpa') | tojson }}" in text
    j = text.index("act === 'check-plan'")
    assert "window.cbCheckPlan(selectedForPublish()" in text[j:j + 200]
    # «Publish» не тронут: та же кнопка и тот же обработчик.
    assert "window.cbPublishAll(showQueue, selectedForPublish());" in text


# ── ревью #765, раунд 4: каждое поле экранируется, замок чтения, ноль записей ──

#: Каждое поле плана — своя метка с разметкой, `&` и кавычками.
def _hostile(field: str) -> str:
    return f"<b>{field}</b>&" + '"' + "'x"


def _escaped(field: str) -> str:
    return f"&lt;b&gt;{field}&lt;/b&gt;&amp;&quot;&#39;x"


_ESC_FIELDS = ["account", "data_error", "campaign_name", "result_error", "warning", "currency", "page_id",
               "pixel_id", "objective", "adset_name", "country", "age", "language", "interest_a",
               "interest_b", "placements", "ad_name", "format", "file", "headline", "primary_text",
               "description", "link", "cta"]


def test_every_plan_field_is_escaped_by_the_templates_escaper():
    h = _hostile
    data = {
        "account": h("account"), "error": h("data_error"), "dry_run": True,
        "results": [{
            "campaign_name": h("campaign_name"), "error": h("result_error"), "warnings": [h("warning")],
            "plan": {"summary": {
                "daily_budget": 20.0, "currency": h("currency"), "page_id": h("page_id"),
                "pixel_id": h("pixel_id"), "objective": h("objective"), "existing": True,
                "ad_sets": [{
                    "name": h("adset_name"), "countries": [h("country")], "age": h("age"),
                    "language": h("language"), "interests": [[h("interest_a")], [h("interest_b")]],
                    "placements": h("placements"),
                    "ads": [{"name": h("ad_name"), "format": h("format"), "file": h("file"),
                             "headline": h("headline"), "primary_text": h("primary_text"),
                             "description": h("description"), "link": h("link"), "cta": h("cta")}],
                }],
            }},
        }],
    }
    html = _run_plan_node(data)["html"]
    for field in _ESC_FIELDS:
        assert _escaped(field) in html, field
        assert f"<b>{field}" not in html, field
    # Ни одного сырого тега из данных: теги в плане — только свои.
    assert "<b>" not in html and "</b>" not in html


def test_check_plan_holds_the_read_lock_of_the_profile(tmp_path, monkeypatch):
    from src.analysis_agent import account_spend as _as
    from src.web import publish_api

    seen = []
    real = _as.serialized_browser

    def recording(fn=None, *, key=None, reading=False):
        deco = real(key=key, reading=reading)

        def wrap(func):
            inner = deco(func)

            def call(*a, **k):
                seen.append({"key": key, "reading": reading, "args": a})
                return inner(*a, **k)

            return call

        return wrap

    monkeypatch.setattr(_as, "serialized_browser", recording)
    monkeypatch.setattr(publish_api, "build_drafts", lambda slots, **kw: [_draft(tmp_path, ads=1)])
    _read_session(monkeypatch, FakeMeta())
    out = publish_api.check_plan([{"name": "x"}], account_label="Test", country="MX")
    assert out["error"] == ""
    assert len(seen) == 1 and seen[0]["reading"] is True and seen[0]["key"] is _as._profile_key
    assert seen[0]["args"][0] == "Test"


def test_check_plan_refuses_if_anything_was_written(tmp_path, monkeypatch):
    from src.web import publish_api

    meta = FakeMeta()
    monkeypatch.setattr(publish_api, "build_drafts", lambda slots, **kw: [_draft(tmp_path, ads=1)])
    opened = _read_session(monkeypatch, meta)
    real_publish = publish_api.ApiPublisher.publish_draft

    def sneaky(self, draft):
        res = real_publish(self, draft)
        self._client.writes += 1                 # будто что-то ушло записью
        return res

    monkeypatch.setattr(publish_api.ApiPublisher, "publish_draft", sneaky)
    out = publish_api.check_plan([{"name": "x"}], account_label="Test", country="MX")
    assert opened[0].writes == 1
    assert out["results"] == [] and "must not write" in out["error"]


# ── 9095, отзыв владельца: план не закрывает билдер; «PAUSED campaigns» в режиме api ──

_FRAME_NODE = r"""
ESCAPER
const RealFD = FormData;
globalThis.FormData = function () { return new RealFD(); };   // поддельная форма — пустой FormData
function cbFormDataSkipping(form, skip) { return { body: new RealFD() }; }
function cbSlotTitle(slot) { return slot.title; }
function cbPickedIn(form, picked) { return picked; }
function cbFormTitle(form) { return form.title; }
globalThis.window = globalThis;
JS
function makeOut() {
  const listeners = [];
  const body = { hidden: false };
  return {
    hidden: true, innerHTML: '', listeners: listeners, body: body,
    addEventListener: (type, fn) => listeners.push(fn),
    querySelector: (sel) => sel === '.cbd-plan-body' ? body : null,
  };
}
function button(act) {
  const attrs = { 'data-act': act };
  return { textContent: '', getAttribute: (k) => attrs[k], setAttribute: (k, v) => { attrs[k] = v; },
           attrs: attrs, closest: function () { return this; } };
}
(async () => {
  const out = {};
  globalThis.document = { querySelectorAll: () => [] };
  const box = makeOut();
  window.cbCheckPlan(null, box, null);                 // нечего проверять — рамка всё равно с «Close»
  out.frame = box.innerHTML;
  out.bound = box.listeners.length;
  window.cbCheckPlan(null, box, null);                 // второй раз — без второго обработчика
  out.boundAgain = box.listeners.length;
  const fold = button('fold-plan');
  box.listeners[0]({ target: fold, stopPropagation: () => {} });
  out.folded = { body: box.body.hidden, text: fold.textContent, aria: fold.attrs['aria-expanded'] };
  box.listeners[0]({ target: fold, stopPropagation: () => {} });
  out.unfolded = { body: box.body.hidden, text: fold.textContent };
  let stopped = false;
  box.listeners[0]({ target: button('close-plan'), stopPropagation: () => { stopped = true; } });
  out.closed = { hidden: box.hidden, html: box.innerHTML, stopped: stopped };
  // закрыли, пока ждали ответа: панель не открывается заново, кнопка снова доступна
  let release;
  globalThis.fetch = () => new Promise((r) => { release = r; });
  const form = { title: 'Test', querySelectorAll: () => [{}] };
  globalThis.document = { querySelectorAll: (sel) => sel === 'form.open-account-form' ? [form] : [] };
  const box2 = makeOut();
  const btn = { disabled: false };
  window.cbCheckPlan(null, box2, btn);
  out.busy = btn.disabled;
  box2.listeners[0]({ target: button('close-plan'), stopPropagation: () => {} });
  release({ json: async () => ({ account: 'Test', results: [] }) });
  await new Promise((r) => setTimeout(r, 20));
  out.late = { hidden: box2.hidden, html: box2.innerHTML, btn: btn.disabled };
  process.stdout.write(JSON.stringify(out));
})();
"""


def _run_frame_node() -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    js = _template_js("function cbPlanFormData(form, only) {", "/* Имя карточки для подтверждения. */")
    escaper = _template_js("function aclEscHtml(value) {", "  </script>")
    script = _FRAME_NODE.replace("ESCAPER\n", escaper + "\n", 1).replace("JS\n", js + "\n", 1)
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
        fh.write(script)
        path = fh.name
    try:
        done = subprocess.run([node, path], capture_output=True, text=True, encoding="utf-8", timeout=60)
    finally:
        Path(path).unlink(missing_ok=True)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_the_plan_panel_can_be_folded_and_closed():
    out = _run_frame_node()
    assert 'data-act="close-plan"' in out["frame"] and "Close plan ×" in out["frame"]
    assert 'data-act="fold-plan"' in out["frame"] and 'class="cbd-plan-body"' in out["frame"]
    assert out["bound"] == 1 and out["boundAgain"] == 1
    assert out["folded"] == {"body": True, "text": "Show", "aria": "false"}
    assert out["unfolded"] == {"body": False, "text": "Hide"}
    assert out["closed"] == {"hidden": True, "html": "", "stopped": True}
    assert out["busy"] is True
    assert out["late"] == {"hidden": True, "html": "", "btn": False}


def test_the_plan_panel_scrolls_inside_the_drawer():
    text = (ROOT / "src" / "web" / "templates" / "report.html").read_text(encoding="utf-8")
    i = text.index(".cbd-plan-out {")
    css = text[i:text.index("}", i)]
    for rule in ("max-height: 40vh", "overflow: auto", "max-width: 100%", "box-sizing: border-box",
                 "position: relative"):
        assert rule in css, rule
    assert "position: fixed" not in css and "position: absolute" not in css
    assert ".cbd-plan-out[hidden] { display: none; }" in text


_MODE_NODE = r"""
globalThis.window = globalThis;
JS
const out = {};
for (const backend of ['api', 'rpa', undefined]) {
  window.ACL_FB_BACKEND = backend;
  out[String(backend)] = { target: cbPublishTarget(), mode: cbPublishMode(), api: cbApiMode() };
}
process.stdout.write(JSON.stringify(out));
"""


def test_api_mode_says_paused_campaigns_not_drafts():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    js = _template_js("function cbApiMode()", "window.cbPublish = function(form) {")
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
        fh.write(_MODE_NODE.replace("JS\n", js + "\n", 1))
        path = fh.name
    try:
        done = subprocess.run([node, path], capture_output=True, text=True, encoding="utf-8", timeout=60)
    finally:
        Path(path).unlink(missing_ok=True)
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    assert out["api"]["target"] == "PAUSED campaigns" and out["api"]["api"] is True
    assert "PAUSED" in out["api"]["mode"] and "draft" not in out["api"]["mode"].lower()
    for other in ("rpa", "undefined"):
        assert out[other]["target"] == "drafts" and out[other]["mode"].startswith("Draft")
        assert "never clicks Publish" in out[other]["mode"]


def test_publish_all_confirm_uses_the_mode_and_keeps_the_rpa_promise_literally():
    """Ревью R6: вопрос «Publish all» берёт режим из cbPublishMode() в api, а ветка
    робота дословно говорит «Draft» и «never clicks Publish»."""
    text = (ROOT / "src" / "web" / "templates" / "report.html").read_text(encoding="utf-8")
    ask = text[text.index("window.cbPublishAll"):]
    ask = ask[:ask.index("function _runPublishQueue")]
    # ACL-2118: режим стоит в итоговой строке окна (modeHtml помощника).
    i = ask.index("modeHtml: cbApiMode()")
    assert "cbPublishMode()" in ask[i:i + 300]
    rpa_branch = ask[i:ask.index("noticesHtml:", i)]
    assert "Draft" in rpa_branch and "never clicks Publish" in rpa_branch


def test_publish_labels_follow_the_backend():
    text = (ROOT / "src" / "web" / "templates" / "report.html").read_text(encoding="utf-8")
    i = text.index("function publishLabel(t) {")
    label = text[i:text.index("\n  }", i)]
    assert "window.ACL_FB_BACKEND === 'api' ? 'PAUSED campaigns' : 'drafts'" in label
    assert "'Publish selected → ' + what" in label and "'Publish all → ' + what" in label
    assert "confirmLabel: (only ? 'Publish selected → ' : 'Publish all → ') + cbPublishTarget()" in text
    assert "(cbApiMode() ? 'Publish → PAUSED campaign' : 'Publish → draft')" in text
    assert text.count("'Create the selected campaigns as PAUSED campaigns?'") == 2
    # Флаг ставится в голове страницы — раньше любого кода билдера.
    assert text.index("window.ACL_FB_BACKEND = {{") < text.index("function publishLabel(t) {")


def test_mode_helpers_live_inside_the_slice_the_builder_harnesses_load():
    """Ревью R5-B1: срезы страницы в тестах билдера — от cbTrackingWarning до _doCbPublish."""
    text = (ROOT / "src" / "web" / "templates" / "report.html").read_text(encoding="utf-8")
    sliced = text[text.index("window.cbTrackingWarning"):text.index("function _doCbPublish")]
    for name in ("function cbApiMode()", "function cbPublishTarget()", "function cbPublishMode()"):
        assert name in sliced, name
        assert text.count(name) == 1, name
