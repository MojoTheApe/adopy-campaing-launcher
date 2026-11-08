"""ACL-2456: builder, copy a campaign to other ad accounts.

Server: what the dialog is told about the targets (account map + cached readings, no Meta call).
Page: the real builder in an offline Chromium: the button is API-only, the copy never carries the source
Page / Instant Form id / link / budget (when the currency differs or is unknown), and it is an ordinary
draft in the target form (Preview and Check plan still apply; nothing is created by the copy).
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.meta_campaign_agent.ad_account_map import AccountMapEntry  # noqa: E402
from src.web import copy_to_accounts as cta  # noqa: E402

MAP = {
    "Src": AccountMapEntry(ad_account_id="act_100", page_id="SRC_PAGE", pixel_id="SRC_PIXEL",
                           tracking_link="https://src.example/lp"),
    "Same": AccountMapEntry(ad_account_id="act_100"),
    "UsdTwo": AccountMapEntry(ad_account_id="act_200", pixel_id="PX200"),
    "Eur": AccountMapEntry(ad_account_id="act_300"),
    "NoAct": AccountMapEntry(ad_account_id=""),
}
FACTS = {"Src": {"currency": "USD"}, "UsdTwo": {"currency": "usd"}, "Eur": {"currency": "EUR"}}


def _describe(targets, *, api=True, facts=FACTS, source="Src"):
    return cta.describe_targets(source, targets, account_map=MAP, facts_for=facts.get, api_backend=api)


# ───────────────────────── server ─────────────────────────

def test_budget_is_kept_only_for_two_known_equal_currencies():
    assert cta.budget_rule("USD", "USD")["mode"] == cta.BUDGET_KEEP
    assert cta.budget_rule("usd", " USD ")["mode"] == cta.BUDGET_KEEP
    diff = cta.budget_rule("USD", "EUR")
    assert diff["mode"] == cta.BUDGET_CONFIRM and "USD" in diff["reason"] and "EUR" in diff["reason"]
    assert "not copied" in diff["reason"]


@pytest.mark.parametrize("src,dst", [("", "USD"), ("USD", ""), ("", "")])
def test_an_unknown_currency_is_never_treated_as_equal(src, dst):
    rule = cta.budget_rule(src, dst)
    assert rule["mode"] == cta.BUDGET_CONFIRM and "not known" in rule["reason"]


def test_rpa_backend_is_refused_with_the_reason():
    with pytest.raises(cta.CopyRefused, match="API backend only"):
        _describe(["Eur"], api=False)


def test_unknown_source_is_refused():
    with pytest.raises(cta.CopyRefused):
        _describe(["Eur"], source="Nope")


def test_targets_are_checked_against_the_account_map():
    out = _describe(["Eur", "UsdTwo", "Same", "NoAct", "Ghost", "Eur", ""])
    rows = {r["label"]: r for r in out["targets"]}
    assert list(rows) == ["Eur", "UsdTwo", "Same", "NoAct", "Ghost"], "duplicates and blanks are dropped"
    assert rows["Eur"]["ok"] and rows["Eur"]["budget"]["mode"] == "confirm"
    assert rows["UsdTwo"]["ok"] and rows["UsdTwo"]["budget"]["mode"] == "keep"
    assert not rows["Same"]["ok"] and "Same ad account" in rows["Same"]["blockers"][0]
    assert not rows["NoAct"]["ok"] and "no ad account id" in rows["NoAct"]["blockers"][0]
    assert not rows["Ghost"]["ok"] and "not in the account map" in rows["Ghost"]["blockers"][0]
    assert out["source_currency"] == "USD"


def test_the_target_pixel_comes_from_its_own_map_row_never_from_the_source():
    rows = {r["label"]: r for r in _describe(["UsdTwo", "Eur"])["targets"]}
    assert rows["UsdTwo"]["pixel"] == "PX200" and "PX200" in " ".join(rows["UsdTwo"]["notes"])
    assert rows["Eur"]["pixel"] == "" and "No pixel in the account map" in " ".join(rows["Eur"]["notes"])
    blob = json.dumps(_describe(["UsdTwo", "Eur"]))
    for source_value in ("SRC_PAGE", "SRC_PIXEL", "src.example"):
        assert source_value not in blob, f"the source {source_value} leaked into the answer for a target"


def test_a_target_with_no_reading_is_a_confirm_not_a_silent_keep():
    out = _describe(["UsdTwo"], facts={"Src": {"currency": "USD"}})
    assert out["targets"][0]["budget"]["mode"] == "confirm"


class _JsonReq:
    def __init__(self, body):
        self._body = body

    async def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def _endpoint(monkeypatch, body, backend="api"):
    from src.web import app as web

    monkeypatch.setenv("ACL_FB_BACKEND", backend)
    monkeypatch.setattr(web, "get_ad_account_map_path", lambda: __file__)  # exists; contents are not read
    monkeypatch.setattr(web, "load_ad_account_map", lambda path: MAP)
    monkeypatch.setattr(web, "_account_facts_for_page", lambda: FACTS)
    resp = asyncio.run(web.api_copy_to_accounts_targets(_JsonReq(body)))
    return resp.status_code, json.loads(resp.body)


def test_endpoint_answers_for_the_api_backend(monkeypatch):
    code, body = _endpoint(monkeypatch, {"source": "Src", "targets": ["Eur", "UsdTwo"]})
    assert code == 200 and body["ok"] and [t["label"] for t in body["targets"]] == ["Eur", "UsdTwo"]


def test_endpoint_refuses_the_rpa_backend_and_bad_bodies(monkeypatch):
    code, body = _endpoint(monkeypatch, {"source": "Src", "targets": ["Eur"]}, backend="rpa")
    assert code == 409 and body["error"] == "API backend only"
    assert _endpoint(monkeypatch, {"source": "Src"})[0] == 400
    assert _endpoint(monkeypatch, ValueError("x"))[0] == 400


def test_the_endpoint_reads_no_meta():
    src = (Path(__file__).resolve().parent.parent / "src/web/copy_to_accounts.py").read_text(encoding="utf-8")
    for banned in ("GraphClient", "publish_api.check_plan", "requests", "creator"):
        assert banned not in src.replace("src.fb_api.creator", ""), banned


# ───────────────────────── page (offline Chromium) ─────────────────────────

REPORT = Path(__file__).resolve().parents[1] / "src/web/templates/report.html"


def test_the_future_stub_is_gone_and_the_control_is_wired():
    page = REPORT.read_text(encoding="utf-8")
    assert "future('copyTo'" not in page and "copyTo:     { task:" not in page
    assert "copyToButtonHtml(window.ACL_FB_BACKEND === 'api')" in page
    assert "/api/copy-to-accounts/targets" in page
    # the copy is the existing cross-account DOM copy (clears the Instant Form id; the Page is not copied)
    assert "doCamp(plan)" in page[page.index("function applyCopy"):]


@pytest.fixture(scope="module")
def browser():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=os.environ.get("ACL_BROWSER_EXECUTABLE") or None)
        yield b
        b.close()


@pytest.fixture
def page(browser):
    from test_builder_per_account import _render
    from test_drawer_review_browser import OFFLINE_FILES

    ctx = browser.new_context()
    p = ctx.new_page()
    errors: list[str] = []
    p.on("pageerror", lambda e: errors.append(str(e)))
    html = _render({"Src": [], "UsdTwo": [], "Eur": []})
    asked: list[dict] = []
    p.asked = asked

    def route(r):
        if r.request.resource_type == "document":
            r.fulfill(content_type="text/html", body=html)
            return
        if r.request.url.endswith("/api/copy-to-accounts/targets"):
            body = json.loads(r.request.post_data or "{}")
            asked.append(body)
            try:
                out = cta.describe_targets(body["source"], body["targets"], account_map=MAP,
                                           facts_for=FACTS.get, api_backend=True)
            except cta.CopyRefused as exc:
                r.fulfill(status=409, json={"ok": False, "error": str(exc)})
                return
            r.fulfill(json=out)
            return
        r.fulfill(json={"files": OFFLINE_FILES, "texts": [], "rows": [], "profiles": [], "waiting": False})

    p.route("**/*", route)
    p.goto("http://acl.test/")
    p.evaluate("cbDrawer.open()")
    yield p
    assert not errors, errors
    ctx.close()


def _seed(p, api=True):
    from test_drawer_review_browser import add

    p.evaluate(f"window.ACL_FB_BACKEND = {json.dumps('api' if api else 'rpa')}")
    # forms are rendered in alphabetical order: Eur, Src, UsdTwo
    add(p, account=1, count=1)
    p.evaluate(
        """() => {
          const f = Array.from(document.querySelectorAll('form.open-account-form'));
          const by = (n) => f.find(x => x.querySelector('[name="account_label"]').value === n);
          by('Src').querySelector('[name="tracking_link"]').value = 'https://src.example/lp';
          by('UsdTwo').querySelector('[name="tracking_link"]').value = 'https://usdtwo.example/lp';
          by('Eur').querySelector('[name="tracking_link"]').value = 'https://eur.example/lp';
          const s = by('Src').querySelector('.cb-slot');
          s.querySelector('input[name$="_page_id"]').value = 'SRC_PAGE_1';
          s.querySelector('input[name$="_ad_0_form_id"]').value = 'SRC_FORM_1';
          s.querySelector('input[name$="_budget"]').value = '50';
        }"""
    )


def _src_button(p):
    return p.locator("#cbd-editor [data-copy-to]").first


def _open_dialog(p):
    p.evaluate("cbTree.open(document.querySelector('.cb-slot').dataset.cbdId)")
    p.evaluate("cbMove.copyTo(Array.from(document.querySelectorAll('form.open-account-form')).find(x => "
               "x.querySelector('[name=\"account_label\"]').value === 'Src').querySelector('.cb-slot'))")
    p.locator(".cbcp").wait_for()


def test_with_the_rpa_backend_the_control_is_disabled_with_the_reason(page):
    _seed(page, api=False)
    btn = page.locator(".cb-slot [data-copy-to]").first
    assert btn.is_disabled() and btn.get_attribute("title") == "API backend only"
    page.evaluate("cbMove.copyTo(document.querySelector('.cb-slot'))")
    assert "API backend only" in page.locator(".cbm-toast").inner_text()
    assert not page.locator(".cbcp").count(), "the dialog must not open on the RPA backend"


def test_the_dialog_blocks_the_same_account_and_asks_the_budget_when_the_currency_differs(page):
    _seed(page)
    _open_dialog(page)
    assert page.asked and page.asked[0]["source"] == "Src" and sorted(page.asked[0]["targets"]) == ["Eur", "UsdTwo"]
    go = page.locator(".cbcp-btns button", has_text="Copy")
    assert go.is_disabled(), "nothing picked yet"
    boxes = page.locator(".cbcp-row input[type=checkbox]")
    assert boxes.count() == 2 and not page.locator(".cbcp-row", has_text="Src").count()
    eur = page.locator(".cbcp-row", has_text="Eur")
    eur.locator("input[type=checkbox]").check()
    assert "USD" in eur.inner_text() and "EUR" in eur.inner_text(), "the mismatch is named"
    assert go.is_disabled(), "an EUR account with no budget typed must not be copyable"
    eur.locator("input.cb-input").fill("45")
    assert go.is_enabled()


def test_the_copy_takes_the_typed_budget_and_none_of_the_source_account_fields(page):
    _seed(page)
    _open_dialog(page)
    eur = page.locator(".cbcp-row", has_text="Eur")
    eur.locator("input[type=checkbox]").check()
    eur.locator("input.cb-input").fill("45")
    page.locator(".cbcp-btns button", has_text="Copy").click()
    got = page.evaluate(
        """() => {
          const f = Array.from(document.querySelectorAll('form.open-account-form'));
          const by = (n) => f.find(x => x.querySelector('[name="account_label"]').value === n);
          const dst = by('Eur'), src = by('Src');
          const copy = dst.querySelector('.cb-slot');
          const vals = Array.from(copy.querySelectorAll('input,select,textarea')).map(i => i.value);
          const fd = new FormData(dst);
          return { dst: dst.querySelectorAll('.cb-slot').length, src: src.querySelectorAll('.cb-slot').length,
                   budget: copy.querySelector('input[name$="_budget"]').value,
                   page: copy.querySelector('input[name$="_page_id"]').value,
                   formId: copy.querySelector('input[name$="_ad_0_form_id"]').value,
                   link: fd.get('tracking_link'), account: fd.get('account_label'),
                   leaked: Array.from(copy.querySelectorAll('input,select,textarea')).filter(i => /src\\.example|SRC_PAGE|SRC_FORM/.test(i.value)).map(i => i.tagName + ':' + (i.name || i.className)),
                   file: copy.querySelector('input[name$="_creative_file"]').value,
                   srcBudget: src.querySelector('.cb-slot input[name$="_budget"]').value };
        }"""
    )
    assert got["dst"] == 1 and got["src"] == 1
    assert got["budget"] == "45" and got["srcBudget"] == "50", "the source amount must not be copied silently"
    assert got["page"] == "" and got["formId"] == "", "Page / Instant Form of the source account are carried over"
    assert got["link"] == "https://eur.example/lp" and got["account"] == "Eur"
    assert got["leaked"] == [], got["leaked"]
    assert got["file"], "the ads are copied"
    toast = page.locator(".cbm-toast").inner_text()
    assert "Copied" in toast and "Preview and Check plan" in toast


def test_same_currency_keeps_the_amount_and_one_undo_removes_every_copy(page):
    _seed(page)
    _open_dialog(page)
    for name in ("UsdTwo", "Eur"):
        row = page.locator(".cbcp-row", has_text=name)
        row.locator("input[type=checkbox]").check()
    page.locator(".cbcp-row", has_text="Eur").locator("input.cb-input").fill("40,5")
    page.locator(".cbcp-btns button", has_text="Copy").click()
    got = page.evaluate(
        """() => Object.fromEntries(Array.from(document.querySelectorAll('form.open-account-form')).map(f => [
            f.querySelector('[name="account_label"]').value,
            Array.from(f.querySelectorAll('.cb-slot')).map(s => s.querySelector('input[name$="_budget"]').value)]))"""
    )
    assert got["UsdTwo"] == ["50"] and got["Eur"] == ["40.5"] and got["Src"] == ["50"]
    page.locator(".cbm-toast .cbm-undo").click()
    left = page.evaluate("Array.from(document.querySelectorAll('form.open-account-form')).map(f => f.querySelectorAll('.cb-slot').length)")
    assert sorted(left) == [0, 0, 1], "Undo must take back all copies of one Copy to accounts"


def test_copy_creates_nothing_in_meta(page):
    """The copy is DOM-only: the page makes no publish / plan request when it copies."""
    _seed(page)
    hits: list[str] = []
    page.on("request", lambda r: hits.append(r.url) if any(x in r.url for x in ("/publish", "/build-upload")) and "/cached" not in r.url else None)
    _open_dialog(page)
    eur = page.locator(".cbcp-row", has_text="Eur")
    eur.locator("input[type=checkbox]").check()
    eur.locator("input.cb-input").fill("45")
    page.locator(".cbcp-btns button", has_text="Copy").click()
    page.wait_for_timeout(300)
    assert hits == [], hits


def test_undo_is_all_or_nothing_when_one_target_is_busy(page):
    _seed(page)
    # a third target: Src -> Eur, UsdTwo and a second copy to Eur are enough to have three copies
    _open_dialog(page)
    for name in ("UsdTwo", "Eur"):
        page.locator(".cbcp-row", has_text=name).locator("input[type=checkbox]").check()
    page.locator(".cbcp-row", has_text="Eur").locator("input.cb-input").fill("40")
    page.locator(".cbcp-btns button", has_text="Copy").click()
    counts = ("Array.from(document.querySelectorAll('form.open-account-form'))"
              ".map(f => f.querySelectorAll('.cb-slot').length)")
    assert sorted(page.evaluate(counts)) == [1, 1, 1]
    # make one target busy (the same signal a running build gives)
    page.evaluate("window.__realBusy = window.cbAccountBusy; "
                  "window.cbAccountBusy = (k) => /Eur$/.test(k) || window.__realBusy(k); 0")
    depth = page.evaluate("cbMove.undoDepth()")
    page.locator(".cbm-toast .cbm-undo").click()
    assert sorted(page.evaluate(counts)) == [1, 1, 1], "nothing may be removed while one copy cannot be undone"
    assert page.evaluate("cbMove.undoDepth()") == depth, "the Undo entry must stay on the stack"
    assert "Can" in page.locator(".cbm-toast").inner_text()
    page.evaluate("window.cbAccountBusy = window.__realBusy; 0")
    assert page.evaluate("cbMove.undoLatest()") is True
    assert sorted(page.evaluate(counts)) == [0, 0, 1]
