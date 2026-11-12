"""ACL-2557: Home «Open profile» в настоящем браузере: Expanded и Compact, ход открытия, отказы, перерисовка.

Страница — настоящие шаблоны Home и настоящий warm_sessions.js; сервер подменён маршрутами Playwright.
Meta, Multilogin и сеть не трогаются.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

pytest.importorskip("playwright.sync_api")
psutil = pytest.importorskip("psutil")
if psutil.virtual_memory().available < 2 * 2**30:
    pytest.skip("browser tests need more than 2 GB of free RAM", allow_module_level=True)

from playwright.sync_api import sync_playwright  # noqa: E402

from src.web import account_links as links  # noqa: E402
from tests import _home_overview_site as site  # noqa: E402
from tests._retry_once import launch_chromium  # noqa: E402

ACC = "Facebook_DQ-214"
NOPROF = "Facebook_DQ-217"
MAP_CSV = ("account_name,ad_account_id,page_id,multilogin_instance,folder_id,profile_id,proxy_type,proxy_host,proxy_port,proxy_user,proxy_pass\n"
           f"{ACC},act_1,1,A,f,prof-214,,,,,\n{NOPROF},act_2,2,A,f,,,,,,\n")
BEFORE = {"Colombia": [(ACC, 117.0, None, "", 0), (NOPROF, 5.0, None, "", 0)]}
AFTER = {"Colombia": [(ACC, 0, "restricted", "Ad account not in BM", 0), (NOPROF, 5.0, None, "", 0)]}
OK_STATE = {"available": True, "actions": True, "accounts": {ACC: {"state": "closed", "text": "Closed"}}}


class Server:
    def __init__(self):
        self.state = OK_STATE
        self.posts = []
        self.jobs = []            # ответы на опрос задания по очереди; последний повторяется
        self.post_reply = None    # (код, тело) вместо задания
        self.queue = [{"running": False, "queued": []}]   # ответы /account-refresh/state по очереди
        self.map_body = ""
        self.map_gets = 0

    def pop(self, seq):
        return seq.pop(0) if len(seq) > 1 else seq[0]


@pytest.fixture
def rig(monkeypatch, tmp_path):
    from src.web import app as web
    from src.web.app import templates

    csv = tmp_path / "map.csv"
    csv.write_text(MAP_CSV, encoding="utf-8")
    monkeypatch.setattr(links, "get_local_config_dir", lambda: str(tmp_path))
    monkeypatch.setattr(links, "get_ad_account_map_path", lambda: str(csv))
    monkeypatch.setattr("src.web.app.get_ad_account_map_path", lambda: str(csv))
    srv = Server()
    first = site.build_amap(web, links, monkeypatch, BEFORE)
    page = site.page_html(first)
    page = page.replace("</body>", "<script>window.aclConfirm = function () { return Promise.resolve(true); };</script>"
                                   "<script src='/static/warm_sessions.js'></script></body>")
    after = site.build_amap(web, links, monkeypatch, AFTER)
    srv.map_body = templates.env.get_template("_account_map.html").render(
        amap=after, compare=None, compare_fragment_note="", mlx={})

    def handle(route):
        req = route.request
        path = req.url.split("acl.test", 1)[1]

        def send(body, status=200, ctype="application/json"):
            route.fulfill(status=status, content_type=ctype, body=body if isinstance(body, str) else json.dumps(body))

        if path == "/":
            return send(page, ctype="text/html")
        if path.startswith("/static/warm_sessions.js"):
            return send((ROOT / "src/web/static/warm_sessions.js").read_text(encoding="utf-8"), ctype="application/javascript")
        if path.startswith("/warm/state"):
            return send(srv.state)
        if path == "/warm/open":
            srv.posts.append(json.loads(req.post_data or "{}"))
            if srv.post_reply:
                return send(srv.post_reply[1], srv.post_reply[0])
            return send({"job_id": "j1", "state": "running"}, 202)
        if path.startswith("/warm/job/"):
            return send(srv.pop(srv.jobs))
        if path.startswith("/account-refresh/state"):
            return send(srv.pop(srv.queue))
        if path.startswith("/account-map"):
            srv.map_gets += 1
            return send(srv.map_body, ctype="text/html")
        return send({}, 404)

    with sync_playwright() as pw:
        browser = launch_chromium(pw)

        def make(width=1280):
            pg = browser.new_context(viewport={"width": width, "height": 900}).new_page()
            pg.errors = []
            pg.on("pageerror", lambda e: pg.errors.append(str(e)))
            pg.route("http://acl.test/**", handle)
            pg.goto("http://acl.test/")
            return pg

        yield srv, make
        browser.close()


def state_text(pg):
    return pg.inner_text(f'tr.aov-row:has(td.amap-list[data-account="{ACC}"]) td:first-child')


def test_only_accounts_with_a_profile_get_the_button_in_both_views(rig):
    srv, make = rig
    pg = make()
    pg.wait_for_selector(f'[data-aov-open="{ACC}"]:visible')
    assert pg.locator("[data-aov-open]").count() == 1 and pg.locator("[data-aov-open]:visible").count() == 1
    assert srv.posts == []                                    # само по себе ничего не открывается
    pg.click("[data-aov-view=compact]")
    pg.click(f'.aov-tile[data-name="{NOPROF}"]')
    assert pg.locator("#aov-pop [data-aov-open]").count() == 0
    pg.click(f'.aov-tile[data-name="{ACC}"]')
    pg.wait_for_selector(f'#aov-pop [data-aov-open="{ACC}"]:visible')
    assert not pg.errors


def test_button_stays_hidden_without_the_api(rig):
    srv, make = rig
    srv.state = {"available": False, "code": "rpa_backend", "reason": "Needs ACL_FB_BACKEND=api", "accounts": {}}
    pg = make()
    pg.wait_for_timeout(400)
    assert pg.locator("[data-aov-open]:visible").count() == 0


def test_open_shows_progress_ignores_second_click_and_repaints_the_row(rig):
    srv, make = rig
    srv.jobs = [{"state": "running", "results": {ACC: {"state": "waiting"}}},
                {"state": "done", "results": {ACC: {"state": "ok", "ok": True, "status_read": "queued"}}}]
    srv.queue = [{"running": True, "queued": [{"account": ACC, "trigger": "open"}]}, {"running": False, "queued": []}]
    pg = make()
    assert "Issue" not in state_text(pg)
    btn = pg.locator(f'[data-aov-open="{ACC}"]')
    btn.wait_for(state="visible")
    btn.click()
    assert btn.inner_text() == "Opening…" and btn.is_disabled()
    pg.evaluate("document.querySelector('[data-aov-open]').click()")      # второй щелчок: ничего
    assert srv.posts == [{"accounts": [ACC], "confirm_reopen": False}]
    pg.wait_for_function("() => document.querySelector('.aov-open-msg').textContent.includes('Status updated')", timeout=20000)
    assert srv.posts == [{"accounts": [ACC], "confirm_reopen": False}]
    assert "Issue" in state_text(pg)                          # строка перечитана и перерисована без перезагрузки
    assert srv.map_gets == 1
    pg.click("[data-aov-view=compact]")
    assert pg.get_attribute(f'.aov-tile[data-name="{ACC}"]', "data-state") == "issue"
    assert btn.inner_text() == "Open profile" and btn.is_enabled()
    assert not pg.errors


@pytest.mark.parametrize("reason", [
    "Not enough free memory to open another profile. Close one and try again.",
    "This profile is busy with another action.",
    "This account is on hold after a checkpoint.",
    "This profile was closed a moment ago. Try again in 3 minutes.",
])
def test_refusal_reason_is_shown(rig, reason):
    srv, make = rig
    srv.jobs = [{"state": "done", "results": {ACC: {"state": "failed", "ok": False, "code": "refused", "message": reason}}}]
    pg = make()
    pg.locator(f'[data-aov-open="{ACC}"]').click()
    pg.wait_for_function("(r) => document.querySelector('.aov-open-msg').textContent.includes(r)", arg=reason, timeout=10000)
    assert pg.locator(".aov-open-msg.warm-bad").count() == 1
    assert pg.locator(f'[data-aov-open="{ACC}"]').is_enabled()
    assert srv.map_gets == 0                                  # отказ ничего не перерисовывает


def test_start_refusal_shows_server_error(rig):
    srv, make = rig
    srv.post_reply = (409, {"error": "Profiles are already being opened; wait for it to finish."})
    pg = make()
    pg.locator(f'[data-aov-open="{ACC}"]').click()
    pg.wait_for_function("() => document.querySelector('.aov-open-msg').textContent.includes('already being opened')", timeout=10000)


def test_hand_opened_window_needs_confirmation_then_reopens(rig):
    srv, make = rig
    srv.jobs = [{"state": "done", "results": {ACC: {"state": "failed", "ok": False, "code": "open_by_hand",
                                                    "message": "Profile is open in Multilogin by hand."}}},
                {"state": "done", "results": {ACC: {"state": "ok", "ok": True, "status_read": "skipped"}}}]
    pg = make()
    pg.locator(f'[data-aov-open="{ACC}"]').click()
    pg.wait_for_selector(".aov-open-reopen")
    assert srv.posts == [{"accounts": [ACC], "confirm_reopen": False}]
    pg.click(".aov-open-reopen")
    pg.wait_for_function("() => document.querySelector('.aov-open-msg').textContent.includes('Opened')", timeout=10000)
    assert srv.posts[1] == {"accounts": [ACC], "confirm_reopen": True}


def test_compact_popup_button_opens_too(rig):
    srv, make = rig
    srv.jobs = [{"state": "done", "results": {ACC: {"state": "failed", "ok": False, "code": "busy", "message": "Profile is busy."}}}]
    pg = make()
    pg.click("[data-aov-view=compact]")
    pg.click(f'.aov-tile[data-name="{ACC}"]')
    pg.locator(f'#aov-pop [data-aov-open="{ACC}"]').click()
    pg.wait_for_function("() => document.querySelector('#aov-pop .aov-open-msg').textContent.includes('busy')", timeout=10000)
    assert srv.posts == [{"accounts": [ACC], "confirm_reopen": False}]
