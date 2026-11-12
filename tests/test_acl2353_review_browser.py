"""ACL-2353: «Before create review» в окне подтверждения Publish (офлайн-Chromium, настоящий шаблон).

/preview-publish, /publish-plan, /api/publish-preflight и /publish-jobs подменены маршрутами; данные выдуманные.
Проверяется глазами пользователя: окно остаётся прежним (рамка, колонки, кнопки), кампания раскрывается,
блокер одной кампании не трогает остальные, снимок уходит на сервер как есть и без второго превью.
С переменной ACL2353_SHOTS=<папка> тот же сценарий снимает экраны (1366×768 и 1920×1080, светлая и тёмная тема).
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_builder_duplicate_buttons import browser  # noqa: E402,F401
from test_acl1819_preflight_browser import LIBRARY, _page_html  # noqa: E402
from test_drawer_review_browser import add  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


class Fake:
    """Подмена сервера: счётчики запросов и ответы, которые тест меняет по ходу."""

    def __init__(self):
        self.previews, self.plans, self.jobs, self.events = [], [], [], []
        self.job_reply = None            # callable(body) -> (status, json) или None
        self.check_fail = False
        self.check_extra = None          # callable(name) -> dict, добавки к строке проверки
        self.ready = True
        self.fp_suffix = ""
        self.review_fp_on_check = None   # callable(name, fp) -> fp, подмена review_fp в строке проверки
        self.currency = "USD"


def _real(errors):
    """Ошибки страницы без фона загрузки: внешние скрипты в офлайн-подмене приходят как JSON."""
    return [e for e in errors if "Unexpected token ':'" not in e]


def _names(post: str) -> list[str]:
    return re.findall(r'name="camp_\d+_name"\r\n\r\n(.*?)\r\n', post or "")


def _open(browser, backend="api", theme="light", size=(1366, 768), fake=None):
    fake = fake or Fake()
    ctx = browser.new_context(color_scheme=theme, viewport={"width": size[0], "height": size[1]})
    p = ctx.new_page()
    errors: list[str] = []
    p.on("pageerror", lambda e: errors.append(str(e)))
    html = _page_html(backend)

    def fp(name):
        return "fp-" + re.sub(r"\W", "", name)[-40:] + fake.fp_suffix

    def route(r):
        url, req = r.request.url, r.request
        if req.resource_type == "document":
            r.fulfill(content_type="text/html", body=html)
        elif "/api/publish-review/event" in url:
            fake.events.append(json.loads(req.post_data or "{}"))
            r.fulfill(json={"ok": True})
        elif "/preview-publish" in url:
            body = req.post_data or ""
            fake.previews.append(body)
            names = _names(body)
            r.fulfill(json={"plan": [{"Campaign Name": n, "Campaign Daily Budget": "$20/day", "Targeting": "Day trading",
                                       "Age Min": 25, "Age Max": 55, "Ad Title": "Head", "Creative Path": "offline-0.png",
                                       "errors": [], "warnings": []} for n in names],
                            "review": {n: fp(n) for n in names}})
        elif "/connection/prepare" in url:
            r.fulfill(json={"ok": True, "code": "rpa_backend"})
        elif "/api/publish-preflight" in url:
            r.fulfill(json={"accounts": [{"account": "Demo", "ready": fake.ready, "reasons": [
                {"message": "Profile is logged out", "hint": "Log in again"}]}]})
        elif "/publish-plan/cached" in url:
            r.fulfill(json={"results": []})
        elif "/publish-plan" in url:
            body = req.post_data or ""
            fake.plans.append(body)
            if fake.check_fail:
                r.fulfill(json={"account": "Demo", "results": [], "error": "Profile busy", "error_kind": "not_checked"})
                return
            rows = []
            for n in _names(body):
                row = {"campaign_name": n, "error": None, "audit": [], "review_fp": fp(n),
                       "plan": {"summary": {"campaign": n, "daily_budget": 20.0, "currency": fake.currency,
                                            "page_id": "111", "pixel_id": "222", "pixel_source": "map", "existing": False,
                                            "ad_sets": [{"name": "Set", "index": 1, "age": "25-55", "interests": [["Day trading"]],
                                                         "placements": "Automatic", "language": "es",
                                                         "ads": [{"index": 1, "name": "Ad 1", "headline": "Head", "primary_text": "Body",
                                                                  "cta": "LEARN_MORE", "link": "https://t.example/c", "file": "offline-0.png"}]}]}}}
                if fake.check_extra:
                    row.update(fake.check_extra(n))
                if fake.review_fp_on_check:
                    row["review_fp"] = fake.review_fp_on_check(n, row["review_fp"])
                rows.append(row)
            r.fulfill(json={"account": "Demo", "results": rows, "error": "", "account_audit": None})
        elif "/publish-jobs" in url and req.method == "POST":
            body = req.post_data or ""
            fake.jobs.append(body)
            if fake.job_reply:
                status, payload = fake.job_reply(body)
                if isinstance(payload, str):
                    r.fulfill(status=status, content_type="text/html", body=payload)
                else:
                    r.fulfill(status=status, json=payload)
            else:
                r.fulfill(json={"job_id": "job-%d" % len(fake.jobs), "total": len(_names(body))})
        elif re.search(r"/publish-jobs/job-[a-z0-9-]+$", url):
            r.fulfill(json={"job_id": "job-1", "status": "done", "errors": [], "total": 1, "done_count": 1,
                            "results": [{"campaign_name": "x", "publishable": True, "success": True}]})
        elif "/api/ad-copy" in url:
            r.fulfill(json={"rows": LIBRARY})
        elif "/api/creatives" in url:
            r.fulfill(json={"files": [{"filename": f"offline-{i}.png", "rel_path": f"offline-{i}.png", "folder": "",
                                       "type": "Image", "size_bytes": 1, "story_label": ""} for i in range(6)]})
        else:
            r.fulfill(json={"files": [], "texts": [], "rows": [], "profiles": [], "waiting": False, "status": "running"})

    p.route("**/*", route)
    p.goto("http://acl.test/")
    p.evaluate("cbDrawer.open()")
    return p, fake, errors, ctx


def _ready(p, count=1):
    """`count` кампаний со всем нужным: ссылка с метками, бюджет, текст из библиотеки, свой файл у каждой."""
    add(p, count=count)
    p.evaluate("""() => {
      const f = document.querySelector('form.open-account-form');
      const set = (el, v) => { el.value = v; el.dispatchEvent(new Event('input', {bubbles: true})); };
      set(f.querySelector('input[name="tracking_link"]'), 'https://track.example.com/c?sub=1');
      f.querySelectorAll('.cb-slot').forEach((s, i) => {
        set(s.querySelector('[name$="_budget"]'), '20');
        set(s.querySelector('input[type="hidden"][name$="_ad_0_copy"]'), 'Text A');
      });
    }""")
    p.wait_for_timeout(700)


def _publish(p):
    p.locator(".cbd-publish-all").click()
    p.wait_for_selector("#safe-confirm-modal .rv-foot")


def _settle(p, timeout=8000):
    """Ждать конца проверки Meta в окне."""
    p.wait_for_function("() => { const m = window.cbReview.model(); return m && !m.checking; }", timeout=timeout)
    p.wait_for_timeout(100)


def _body(p):
    return p.locator("#safe-confirm-modal").inner_text()


def _shots(p, name):
    out = os.environ.get("ACL2353_SHOTS")
    if out:
        Path(out).mkdir(parents=True, exist_ok=True)
        p.screenshot(path=str(Path(out) / f"{name}.png"))


@pytest.mark.parametrize("size,theme", [((1366, 768), "light"), ((1920, 1080), "dark")])
def test_popup_keeps_today_frame_and_a_campaign_expands(browser, size, theme):
    p, fake, errors, ctx = _open(browser, theme=theme, size=size)
    try:
        if theme == "dark":
            p.evaluate("() => document.documentElement.setAttribute('data-theme', 'dark')")
        _ready(p)
        _publish(p)
        _settle(p)
        box = p.locator("#safe-confirm-modal .sfc-box")
        assert box.locator(".sfc-head").inner_text().startswith("Create every campaign as a PAUSED campaign?")
        assert box.locator(".pc-cols .pc-left").count() == 1 and box.locator(".pc-cols .pc-right").count() == 1
        assert box.locator("#sfc-cancel").inner_text() == "Cancel"
        assert box.locator("#sfc-ok").inner_text().startswith("Publish all → PAUSED campaigns")
        lead = box.locator(".sfc-lead").inner_text()
        assert "Publish 1 campaign on 1 account — campaign PAUSED, ad sets and ads active" in lead
        assert "Mode:" in lead and "Budget: 20 USD / day" in lead and "Checked with Meta" in lead
        camp = box.locator("details.rv-camp").first
        assert "· CBO · 20 USD / day" in camp.locator("summary").first.inner_text()
        camp.locator("summary").first.click()
        text = camp.inner_text()
        assert "Ad set 01" in text and "Edit campaign" in text
        camp.locator("details.rv-set summary").click()
        text = camp.inner_text()
        for want in ("Day trading", "25-55", "Primary text", "Body", "Headline", "Head", "LEARN_MORE", "Feed file", "offline-0.png"):
            assert want in text, (want, text)
        assert "On confirmation, this request is frozen." in box.inner_text()
        assert box.locator("#sfc-ok").is_enabled()
        # Кнопки и заголовок стоят на экране.
        b = box.bounding_box()
        assert b["y"] >= 0 and b["y"] + b["height"] <= size[1] + 1
        _shots(p, f"popup-{size[0]}-{theme}")
        assert not _real(errors), errors
    finally:
        ctx.close()


def test_one_budget_total_per_currency_and_unchecked_values_are_marked(browser):
    fake = Fake()
    fake.check_fail = True
    p, fake, errors, ctx = _open(browser, fake=fake)
    try:
        _ready(p)
        _publish(p)
        _settle(p)
        lead = p.locator(".sfc-lead").inner_text()
        assert "Budget: 20 / day (currency not checked with Meta)" in lead, lead
        right = p.locator(".pc-right").inner_text()
        assert "Could not check with Meta (Profile busy)" in right and "Check again" in right
        # Confirm остаётся включённым (D3).
        assert p.locator("#sfc-ok").is_enabled()
        p.locator("details.rv-camp summary").first.click()
        assert "not checked with Meta" in p.locator("details.rv-camp").first.inner_text()
        fake.check_fail = False
        p.locator("[data-rv-recheck]").click()
        _settle(p)
        assert "Budget: 20 USD / day" in p.locator(".sfc-lead").inner_text()
        assert not _real(errors), errors
    finally:
        ctx.close()


def test_warnings_are_listed_with_their_texts(browser):
    fake = Fake()
    fake.check_extra = lambda n: {"audit": [
        {"rule": "media_vertical", "status": "warning", "detail": "", "ads": []}]}
    p, fake, errors, ctx = _open(browser, fake=fake)
    try:
        _ready(p)
        p.evaluate("""() => { const i = document.querySelector('form.open-account-form input[name="tracking_link"]');
          i.value = 'https://track.example.com/c'; i.dispatchEvent(new Event('input', {bubbles: true})); }""")
        _publish(p)
        _settle(p)
        right = p.locator(".pc-right").inner_text()
        assert "Warnings (" in right and "they do not block" in right
        assert "Vertical file for Stories and Reels" in right.replace("\n", " ") or "Stories" in right
        assert "The link is not saved for future builds" in right
        _shots(p, "warnings")
        assert not _real(errors), errors
    finally:
        ctx.close()


def test_a_blocker_removes_only_that_campaign_and_confirm_stays_on(browser):
    fake = Fake()
    p, fake, errors, ctx = _open(browser, fake=fake)
    try:
        _ready(p, count=3)
        first_names = p.evaluate("() => [...document.querySelectorAll('form.open-account-form .cb-name-hidden')].map(x => x.value)")
        bad = first_names[1]
        fake.check_extra = lambda n: ({"audit": [{"rule": "pixel_ok", "status": "error", "ads": [],
                                                  "detail": "The ad account has no pixel."}]} if n == bad else {})
        _publish(p)
        _settle(p)
        lead = p.locator(".sfc-lead").inner_text()
        assert "Publish 2 campaigns on 1 account" in lead, lead
        right = p.locator(".pc-right").inner_text()
        assert "Will not be created (1)" in right and bad in right
        assert p.locator("#sfc-ok").is_enabled()
        p.locator("#sfc-ok").click()
        p.wait_for_function("document.querySelector('#safe-confirm-modal') === null")
        p.wait_for_timeout(300)
        assert len(fake.jobs) == 1
        sent = _names(fake.jobs[0])
        assert bad not in sent and len(sent) == 2, sent
        review = json.loads(re.search(r'name="review_json"\r\n\r\n(.*?)\r\n--', fake.jobs[0], re.S).group(1))
        assert review["v"] == 1 and set(review["campaigns"]) == set(sent)
        assert review["campaigns"][sent[0]]["page_id"] == "111" and review["campaigns"][sent[0]]["pixel_id"] == "222"
        assert review["blocked"] == {"pixel_ok": 1}
        assert not _real(errors), errors
    finally:
        ctx.close()


def test_when_everything_is_blocked_confirm_is_off_with_the_reason(browser):
    fake = Fake()
    fake.check_extra = lambda n: {"audit": [{"rule": "pixel_ok", "status": "error", "ads": [], "detail": "No pixel."}]}
    p, fake, errors, ctx = _open(browser, fake=fake)
    try:
        _ready(p)
        _publish(p)
        _settle(p)
        assert p.locator("#sfc-ok").is_disabled()
        assert "Nothing can be created: fix the campaigns listed under Will not be created." in _body(p)
        p.locator("#sfc-ok").click(force=True)
        assert fake.jobs == []
    finally:
        ctx.close()


def test_edit_campaign_closes_the_popup_and_posts_nothing(browser):
    p, fake, errors, ctx = _open(browser)
    try:
        _ready(p)
        _publish(p)
        _settle(p)
        p.locator("details.rv-camp summary").first.click()
        p.locator(".rv-edit").first.click()
        assert p.locator("#safe-confirm-modal").count() == 0
        assert fake.jobs == []
        p.wait_for_timeout(1300)
        assert {"event": "review.closed", "reason": "edit"}.items() <= fake.events[-1].items()
    finally:
        ctx.close()


def test_confirm_sends_the_snapshot_and_a_later_edit_does_not_change_it(browser):
    p, fake, errors, ctx = _open(browser)
    try:
        _ready(p)
        before = len(fake.previews)
        _publish(p)
        _settle(p)
        assert len(fake.previews) == before + 1, "одно превью на аккаунт при открытии"
        p.locator("#sfc-ok").click()
        assert len(fake.previews) == before + 1, "после Confirm второго превью нет"
        # Правка сразу после Confirm: бюджет уже в форме, снимок ушёл раньше.
        p.evaluate("""() => { const i = document.querySelector('form.open-account-form [name$="_budget"]');
          i.value = '999'; i.dispatchEvent(new Event('input', {bubbles: true})); }""")
        p.wait_for_function("document.querySelector('#safe-confirm-modal') === null")
        p.wait_for_timeout(300)
        assert len(fake.jobs) == 1
        assert re.search(r'name="camp_0_budget"\r\n\r\n20\r\n', fake.jobs[0]), "в задание ушёл бюджет из ревью"
        assert "999" not in fake.jobs[0]
        assert 'name="user_confirmed"' in fake.jobs[0] and 'name="request_id"' in fake.jobs[0]
    finally:
        ctx.close()


def test_a_form_change_while_the_popup_is_open_blocks_confirm_until_reviewed_again(browser):
    p, fake, errors, ctx = _open(browser)
    try:
        _ready(p)
        before = len(fake.previews)
        _publish(p)
        _settle(p)
        # Правка из «другой вкладки»: форма меняется без событий ввода.
        p.evaluate("() => { document.querySelector('form.open-account-form [name$=\"_budget\"]').value = '77'; }")
        p.locator("#sfc-ok").click()
        assert "Campaigns changed since this review:" in p.locator(".rv-stale").inner_text()
        assert fake.jobs == []
        p.locator("[data-rv-again]").click()
        p.wait_for_selector("#safe-confirm-modal .rv-foot")
        _settle(p)
        assert len(fake.previews) == before + 2
        p.locator("#sfc-ok").click()
        p.wait_for_timeout(400)
        assert len(fake.jobs) == 1 and re.search(r'name="camp_0_budget"\r\n\r\n77\r\n', fake.jobs[0])
    finally:
        ctx.close()


def test_a_ticked_card_change_is_caught_without_an_input_event(browser):
    p, fake, errors, ctx = _open(browser)
    try:
        _ready(p, count=2)
        _publish(p)
        _settle(p)
        p.evaluate("() => { const s = document.querySelectorAll('form.open-account-form .cb-slot')[0]; s._cbdPick = true; }")
        # Выбрана только первая карточка, а ревью было про обе.
        p.locator("#sfc-ok").click()
        assert p.locator(".rv-stale").count() == 1 and fake.jobs == []
    finally:
        ctx.close()


def test_a_verdict_with_a_different_fingerprint_is_checked_again(browser):
    fake = Fake()
    calls = {"n": 0}

    def swap(name, fp):
        calls["n"] += 1
        return fp if calls["n"] > 1 else "old-" + fp          # первая проверка вернула устаревший вердикт

    fake.review_fp_on_check = swap
    p, fake, errors, ctx = _open(browser, fake=fake)
    try:
        _ready(p)
        _publish(p)
        _settle(p)
        assert len(fake.plans) == 2, "устаревший вердикт проверили заново"
        assert 'name="force"' in fake.plans[1]
    finally:
        ctx.close()


def test_review_stale_from_the_server_keeps_the_rest_and_offers_review_again(browser):
    fake = Fake()
    p, fake, errors, ctx = _open(browser, fake=fake)
    try:
        _ready(p, count=2)
        names = p.evaluate("() => [...document.querySelectorAll('form.open-account-form .cb-name-hidden')].map(x => x.value)")
        fake.job_reply = lambda body: (200, {"job_id": "job-1", "total": 1, "refused": [{"name": names[1], "code": "review_stale"}],
                                              "refused_text": "Not created: %s changed after the review (library text, form template, "
                                                              "account map or file). Review again." % names[1]})
        _publish(p)
        _settle(p)
        p.locator("#sfc-ok").click()
        p.wait_for_timeout(600)
        queue = p.locator(".cbd-queue").inner_text()
        assert "changed after the review" in queue and "Review again" in queue, queue
        p.locator("[data-cb-review-again]").click()
        p.wait_for_selector("#safe-confirm-modal .rv-foot")
        _settle(p)
        assert "Publish 1 campaign" in p.locator(".sfc-lead").inner_text()
    finally:
        ctx.close()


def test_server_busy_retry_sends_the_same_frozen_body(browser):
    fake = Fake()
    state = {"n": 0}

    def reply(body):
        state["n"] += 1
        if state["n"] == 1:
            return 502, "<html>busy</html>"
        return 200, {"job_id": "job-ok", "total": 1}

    fake.job_reply = reply
    p, fake, errors, ctx = _open(browser, fake=fake)
    try:
        _ready(p)
        before = len(fake.previews)
        _publish(p)
        _settle(p)
        p.locator("#sfc-ok").click()
        p.wait_for_selector("[data-cb-retry]", timeout=8000)
        # Форма за это время изменилась: повтор всё равно везёт замороженное.
        p.evaluate("() => { document.querySelector('form.open-account-form [name$=\"_budget\"]').value = '55'; }")
        p.locator("[data-cb-retry]").click()
        p.wait_for_timeout(500)
        assert len(fake.jobs) == 2
        rid = [re.search(r'name="request_id"\r\n\r\n(.*?)\r\n', j).group(1) for j in fake.jobs]
        assert rid[0] == rid[1] and fake.jobs[0].count("review_json") == fake.jobs[1].count("review_json") == 1
        assert re.search(r'name="camp_0_budget"\r\n\r\n20\r\n', fake.jobs[1])
    finally:
        ctx.close()


def test_not_ready_account_is_left_out_unless_run_anyway(browser):
    fake = Fake()
    fake.ready = False
    p, fake, errors, ctx = _open(browser, fake=fake)
    try:
        _ready(p)
        _publish(p)
        _settle(p)
        assert "Not ready (1)" in _body(p)
        assert "Nothing can be created" in _body(p)
        p.locator("#cb-pf-anyway").check()
        assert "Publish 1 campaign on 1 account" in p.locator(".sfc-lead").inner_text()
        assert p.locator("#sfc-ok").is_enabled()
    finally:
        ctx.close()


def test_rpa_keeps_today_popup_and_never_sends_review_json(browser):
    p, fake, errors, ctx = _open(browser, backend="rpa")
    try:
        _ready(p)
        p.locator(".cbd-publish-all").click()
        p.wait_for_selector("#safe-confirm-modal", timeout=8000)
        text = _body(p)
        assert "Build every campaign as a draft?" in text or "Build" in text
        assert "frozen" not in text and "Will not be created" not in text and "rv-camp" not in p.content().split("<script")[0]
        assert p.locator("#safe-confirm-modal details.rv-camp").count() == 0
        assert not any("review_json" in j for j in fake.jobs)
        assert not [e for e in fake.events if e.get("event", "").startswith("review.")]
    finally:
        ctx.close()


def _ready_form(p, idx):
    """Like `_ready`, but for the form with number `idx` (the second account)."""
    add(p, account=idx, count=1)
    p.evaluate("""(idx) => {
      const f = document.querySelectorAll('form.open-account-form')[idx];
      const set = (el, v) => { el.value = v; el.dispatchEvent(new Event('input', {bubbles: true})); };
      set(f.querySelector('input[name="tracking_link"]'), 'https://track.example.com/c?sub=1');
      f.querySelectorAll('.cb-slot').forEach((s) => {
        set(s.querySelector('[name$="_budget"]'), '20');
        set(s.querySelector('input[type="hidden"][name$="_ad_0_copy"]'), 'Text A');
      });
    }""", idx)
    p.wait_for_timeout(500)


def _close_popup(p):
    p.evaluate("() => { const m = document.getElementById('safe-confirm-modal'); if (m) m.remove(); }")
    p.wait_for_function("() => !window.cbReview.model()")


def _lifetime_summary(n, ad_sets=None):
    return {"plan": {"summary": {
        "campaign": n, "lifetime_budget": 500.0, "budget_type": "lifetime", "currency": "USD", "page_id": "111",
        "page_name": "Page One", "pixel_id": "222", "pixel_source": "map", "existing": False,
        "ad_sets": ad_sets if ad_sets is not None else []}}}


def test_card_country_page_and_retry_publish_all_open_the_review(browser):
    """Spec test 13: every Publish road reaches the review under the API backend and posts nothing by itself."""
    fake = Fake()
    state = {"n": 0}

    def reply(body):
        state["n"] += 1
        if state["n"] == 1:
            return 200, {"error": "The shared Multilogin session was lost", "code": "mlx_session_lost"}
        return 200, {"job_id": "job-%d" % state["n"], "total": 1}

    fake.job_reply = reply
    p, fake, errors, ctx = _open(browser, fake=fake)
    try:
        _ready(p, count=2)
        # 1. Country-page Publish button (cbPublish on the form).
        p.evaluate("() => cbPublish(document.querySelector('form.open-account-form'))")
        p.wait_for_selector("#safe-confirm-modal .rv-foot")
        assert fake.jobs == []
        _close_popup(p)
        # 2. Card Publish: one card opened in the drawer, the same function with a shown card.
        p.evaluate("""() => { const f = document.querySelector('form.open-account-form');
          cbTree.open(f.querySelector('.cb-slot').dataset.cbdId); cbPublish(f); }""")
        p.wait_for_selector("#safe-confirm-modal .rv-foot")
        assert "Create this campaign as a PAUSED campaign?" in _body(p)
        assert fake.jobs == []
        _close_popup(p)
        # 3. ACL-1826 Retry: Publish all, the account is stopped by a lost session, Retry opens the review again.
        p.evaluate("() => cbTree.close && cbTree.close()")
        p.locator(".cbd-publish-all").click()
        p.wait_for_selector("#safe-confirm-modal .rv-foot")
        _settle(p)
        p.locator("#sfc-ok").click()
        p.wait_for_selector("[data-cb-stop-retry]", timeout=8000)
        assert len(fake.jobs) == 1
        p.locator("[data-cb-stop-retry]").click()
        p.wait_for_selector("#safe-confirm-modal .rv-foot")
        assert len(fake.jobs) == 1, "Retry opens the review; nothing is sent before Confirm"
        assert not _real(errors), errors
    finally:
        ctx.close()


def test_two_accounts_an_edit_of_the_second_right_after_confirm_reaches_neither_job(browser):
    """Spec test 7: two accounts, an edit on the second right after Confirm."""
    p, fake, errors, ctx = _open(browser)
    try:
        _ready(p)
        _ready_form(p, 1)
        _publish(p)
        _settle(p)
        p.locator("#sfc-ok").click()
        p.evaluate("""() => { const i = document.querySelectorAll('form.open-account-form')[1].querySelector('[name$="_budget"]');
          i.value = '999'; i.dispatchEvent(new Event('input', {bubbles: true})); }""")
        p.wait_for_function("document.querySelector('#safe-confirm-modal') === null")
        p.wait_for_timeout(500)
        assert len(fake.jobs) == 2, "one job per account"
        for body in fake.jobs:
            assert re.search(r'name="camp_0_budget"\r\n\r\n20\r\n', body) and "999" not in body
            assert body.count("review_json") == 1
        assert not _real(errors), errors
    finally:
        ctx.close()


def test_lifetime_budget_is_shown_as_lifetime_and_kept_out_of_the_daily_total(browser):
    fake = Fake()
    fake.check_extra = lambda n: _lifetime_summary(n)
    p, fake, errors, ctx = _open(browser, fake=fake)
    try:
        _ready(p, count=2)
        _publish(p)
        _settle(p)
        lead = p.locator(".sfc-lead").inner_text()
        assert "Budget: 1000 USD lifetime" in lead and "/ day" not in lead, lead
        p.locator("details.rv-camp summary").first.click()
        text = p.locator("details.rv-camp").first.inner_text()
        assert "500 USD lifetime" in text and "/ day" not in text, text
        assert "Page One (111)" in text, "L1: the Page is shown at campaign level"
        assert not _real(errors), errors
    finally:
        ctx.close()


def test_a_daily_and_a_lifetime_campaign_get_separate_totals(browser):
    fake = Fake()
    p, fake, errors, ctx = _open(browser, fake=fake)
    try:
        _ready(p, count=2)
        names = p.evaluate("() => [...document.querySelectorAll('form.open-account-form .cb-name-hidden')].map(x => x.value)")
        fake.check_extra = lambda n: _lifetime_summary(n) if n == names[0] else {}
        _publish(p)
        _settle(p)
        lead = p.locator(".sfc-lead").inner_text()
        assert "Budget: 20 USD / day · 500 USD lifetime" in lead, lead
        assert not _real(errors), errors
    finally:
        ctx.close()


def test_manual_page_notice_and_instant_form_note_are_shown(browser):
    fake = Fake()
    fake.check_extra = lambda n: {"plan": {"summary": {
        "campaign": n, "daily_budget": 20.0, "currency": "USD", "page_id": "111", "page_name": "Page One",
        "pixel_id": "222", "pixel_source": "map", "existing": False,
        "manual_required": ["Choose the Page by hand in Ads Manager."],
        "ad_sets": [{"name": "Set", "index": 1, "age": "25-55", "interests": [], "placements": "Automatic",
                     "language": "es", "destination": "Instant Form", "ads_not_created": True, "pending_ad_count": 1,
                     "ads": []}]}}}
    p, fake, errors, ctx = _open(browser, fake=fake)
    try:
        _ready(p)
        _publish(p)
        _settle(p)
        right = p.locator(".pc-right").inner_text()
        assert "Manual completion required" in right and "Choose the Page by hand" in right, right
        p.locator("details.rv-camp summary").first.click()
        p.locator("details.rv-set summary").first.click()
        text = p.locator("details.rv-camp").first.inner_text()
        assert "Choose the Page by hand" in text and "not created" in text, text
        assert not _real(errors), errors
    finally:
        ctx.close()


def test_form_note_of_an_instant_form_ad_is_shown(browser):
    fake = Fake()
    fake.check_extra = lambda n: {"plan": {"summary": {
        "campaign": n, "daily_budget": 20.0, "currency": "USD", "page_id": "111", "pixel_id": "222",
        "pixel_source": "map", "existing": False,
        "ad_sets": [{"name": "Set", "index": 1, "age": "25-55", "interests": [], "placements": "Automatic",
                     "language": "es", "destination": "Instant Form",
                     "ads": [{"index": 1, "name": "Ad 1", "headline": "Head", "primary_text": "Body", "cta": "LEARN_MORE",
                              "form_name": "Lead form", "form_note": "form will be created: Lead form",
                              "file": "offline-0.png"}]}]}}}
    p, fake, errors, ctx = _open(browser, fake=fake)
    try:
        _ready(p)
        _publish(p)
        _settle(p)
        p.locator("details.rv-camp summary").first.click()
        p.locator("details.rv-set summary").first.click()
        assert "form will be created: Lead form" in p.locator("details.rv-camp").first.inner_text()
        assert not _real(errors), errors
    finally:
        ctx.close()


def test_twin_cards_are_one_campaign_in_the_count(browser):
    p, fake, errors, ctx = _open(browser)
    try:
        _ready(p, count=2)
        p.evaluate("""() => document.querySelectorAll('form.open-account-form .cb-name-override').forEach(o => {
          o.value = 'Twin'; o.dispatchEvent(new Event('input', {bubbles: true})); })""")
        p.wait_for_function("() => [...document.querySelectorAll('form.open-account-form .cb-name-hidden')].every(h => h.value === 'Twin')")
        _publish(p)
        _settle(p)
        lead = p.locator(".sfc-lead").inner_text()
        assert "1 campaign" in lead and "2 campaigns" not in lead, lead
        assert "Budget: 20 " in lead and "Budget: 40" not in lead, lead
        assert p.locator("#safe-confirm-modal details.rv-camp").count() == 1
        assert not _real(errors), errors
    finally:
        ctx.close()


def test_a_blocked_twin_blocks_the_whole_campaign(browser):
    fake = Fake()
    p, fake, errors, ctx = _open(browser, fake=fake)
    try:
        _ready(p, count=2)
        p.evaluate("""() => document.querySelectorAll('form.open-account-form .cb-name-override').forEach(o => {
          o.value = 'Twin'; o.dispatchEvent(new Event('input', {bubbles: true})); })""")
        p.wait_for_function("() => [...document.querySelectorAll('form.open-account-form .cb-name-hidden')].every(h => h.value === 'Twin')")
        # Only the second card has a blocker: the Preview row of the name is found for the first one only,
        # so give the second card an empty file (a client-side blocker).
        p.evaluate("""() => { const s = document.querySelectorAll('form.open-account-form .cb-slot')[1];
          const f = s.querySelector('[name$="_ad_0_creative_file"]'); f.value = 'missing-file.png'; }""")
        _publish(p)
        _settle(p)
        assert "Will not be created (1)" in p.locator("#safe-confirm-modal").inner_text()
        assert not p.locator("#sfc-ok").is_enabled()
        assert not _real(errors), errors
    finally:
        ctx.close()


def test_the_checked_age_comes_from_the_age_of_the_verdict(browser):
    fake = Fake()
    from datetime import datetime, timedelta, timezone

    old = (datetime.now(timezone.utc) - timedelta(minutes=7)).isoformat()
    fake.check_extra = lambda n: {"checked_at": old, "cached": True}
    p, fake, errors, ctx = _open(browser, fake=fake)
    try:
        _ready(p)
        _publish(p)
        _settle(p)
        assert "Checked with Meta 7 min ago" in p.locator(".sfc-lead").inner_text()
        assert not _real(errors), errors
    finally:
        ctx.close()


def test_the_button_shows_progress_while_the_review_prepares_and_a_second_click_says_so(browser):
    p, fake, errors, ctx = _open(browser)
    try:
        _ready(p)
        p.evaluate("""() => { window.__held = null; const orig = window.fetch;
          window.fetch = function (url, init) { if (String(url).indexOf('/preview-publish') >= 0)
            return new Promise((res) => { window.__held = () => res(orig.call(window, url, init)); });
            return orig.apply(this, arguments); }; }""")
        p.evaluate("() => window.cbPublishAll(window.cbTree.showQueue)")
        p.wait_for_function("() => window.cbPublishPending.active()")
        assert "Checking before publishing" in p.locator(".cbd-publish-all").inner_text()
        p.evaluate("() => window.cbPublishAll(window.cbTree.showQueue)")
        assert "Still preparing the review" in p.locator("body").inner_text()
        p.evaluate("() => window.__held()")
        p.wait_for_selector("#safe-confirm-modal .rv-foot")
        assert p.evaluate("() => window.cbPublishPending.active()") is False
        assert not _real(errors), errors
    finally:
        ctx.close()


def test_refused_campaigns_keep_their_tick_and_get_no_build_dot(browser):
    fake = Fake()
    p, fake, errors, ctx = _open(browser, fake=fake)
    try:
        _ready(p, count=2)
        names = p.evaluate("() => [...document.querySelectorAll('form.open-account-form .cb-name-hidden')].map(x => x.value)")
        fake.job_reply = lambda body: (200, {"job_id": "job-1", "total": 1, "refused": [{"name": names[1], "code": "review_stale"}],
                                              "refused_text": "Not created: %s changed after the review. Review again." % names[1]})
        p.evaluate("() => document.querySelectorAll('form.open-account-form .cb-slot').forEach(s => { s._cbdPick = true; })")
        p.evaluate("() => { window.__watched = []; const w = window.cbBuild && window.cbBuild.watch;"
                   " if (w) window.cbBuild.watch = function (id, acc, names, c) { window.__watched.push(names); return w.apply(this, arguments); }; }")
        _publish(p)
        _settle(p)
        p.locator("#sfc-ok").click()
        p.wait_for_timeout(700)
        watched = p.evaluate("() => window.__watched")
        assert watched and names[1] not in watched[0] and names[0] in watched[0], watched
        assert not _real(errors), errors
    finally:
        ctx.close()
