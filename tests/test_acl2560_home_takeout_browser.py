"""ACL-2560: Home в настоящем браузере — выравнивание кнопок и «Take out of <country>».

Страница — настоящие шаблоны Home, настоящие amapSend/amapUnlink и warm_sessions.js; сервер подменён маршрутами Playwright.
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

REPORT = site.REPORT
DIALOG_JS = (ROOT / "src/web/templates/_acl_dialog.js").read_text(encoding="utf-8")
#: Настоящие функции страницы: отправка в /account-links и «Take out» с подтверждением.
SEND_JS = REPORT[REPORT.index("async function amapSend("): REPORT.index("/* ACL-1620 notes")]
UNLINK_JS = REPORT[REPORT.index("async function amapUnlink("): REPORT.index("/* ACL-142: второе")]

SHORT = "Facebook_DQ-214"
NOPROF = "Facebook_DQ-217"
LONG = "Facebook_Very-Long-Account-Name-For-The-Alignment-Check-0099"
MAP_CSV = ("account_name,ad_account_id,page_id,multilogin_instance,folder_id,profile_id,proxy_type,proxy_host,proxy_port,proxy_user,proxy_pass\n"
           f"{SHORT},act_1,1,A,f,prof-214,,,,,\n{NOPROF},act_2,2,A,f,,,,,,\n{LONG},act_3,3,A,f,prof-long,,,,,\n")
BEFORE = {"Colombia": [(SHORT, 117.0, None, "", 0), (NOPROF, 5.0, None, "", 0), (LONG, 30.0, None, "", 0)]}
AFTER = {"Colombia": [(NOPROF, 5.0, None, "", 0), (LONG, 30.0, None, "", 0)]}
OK_STATE = {"available": True, "actions": True, "accounts": {}}


class Server:
    def __init__(self):
        self.links = []           # тела POST /account-links
        self.link_reply = (200, {"ok": True})
        self.map_body = ""
        self.map_gets = 0


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
    page = page.replace("</body>", f"<script>{DIALOG_JS}</script><script>{SEND_JS}\n{UNLINK_JS}\n"
                        "window.__confirms = []; window.__answer = true;"
                        "window.aclConfirm = function (m, o) { window.__confirms.push(m); return Promise.resolve(window.__answer); };"
                        "</script><script src='/static/warm_sessions.js'></script></body>")
    links.unlink(SHORT, "country", "Colombia")                # сервер после успешного «Take out»: аккаунта в стране больше нет
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
            return send(OK_STATE)
        if path == "/account-links":
            srv.links.append(json.loads(req.post_data or "{}"))
            return send(srv.link_reply[1], srv.link_reply[0])
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
            pg.wait_for_selector(f'[data-aov-open="{SHORT}"]:visible')
            pg.evaluate("window.__keep = 'same page'")
            return pg

        yield srv, make
        browser.close()


def rows_of(pg):
    return pg.locator("tr.aov-row").count()


def test_action_buttons_share_one_left_offset_whatever_the_name_length(rig):
    _srv, make = rig
    for width in (1280, 1000):
        pg = make(width)
        takes = pg.locator("[data-aov-takeout]")
        assert takes.count() == 3
        lefts = [takes.nth(i).bounding_box()["x"] for i in range(3)]
        assert max(lefts) - min(lefts) < 1, lefts
        opens = pg.locator("[data-aov-open]:visible")
        assert opens.count() == 2                             # у аккаунта без профиля Open profile нет
        olefts = [opens.nth(i).bounding_box()["x"] for i in range(2)]
        assert max(olefts) - min(olefts) < 1, olefts
        assert abs(olefts[0] - lefts[0]) < 1                  # обе кнопки стопкой на одном x
        for acc in (SHORT, LONG):                             # Open profile выше, Take out ниже
            o = pg.locator(f'[data-aov-open="{acc}"]').bounding_box()
            t = pg.locator(f'[data-aov-takeout="{acc}"]').bounding_box()
            assert o["y"] + o["height"] <= t["y"] + 1
        assert not pg.errors


def test_every_row_and_the_compact_popup_offer_take_out(rig):
    _srv, make = rig
    pg = make()
    assert pg.locator("tr.aov-row [data-aov-takeout]").count() == rows_of(pg) == 3
    assert pg.locator("tr.aov-row [data-aov-takeout]").first.inner_text() == "Take out of Colombia"
    pg.click("[data-aov-view=compact]")
    for acc in (SHORT, NOPROF):                               # с профилем и без
        pg.click(f'.aov-tile[data-name="{acc}"]')
        assert pg.locator(f'#aov-pop [data-aov-takeout="{acc}"]').inner_text() == "Take out of Colombia"


def test_cancel_does_nothing_and_ok_sends_the_manage_unlink_request(rig):
    srv, make = rig
    pg = make()
    ckey = pg.get_attribute(f'[data-aov-takeout="{SHORT}"]', "data-aov-ckey")
    pg.evaluate("window.__answer = false")
    pg.click(f'[data-aov-takeout="{SHORT}"]')
    pg.wait_for_function("() => window.__confirms.length === 1")
    msg = pg.evaluate("window.__confirms[0]")
    assert msg.startswith(f"Take {SHORT} out of {ckey}?") and "moves to Available accounts" in msg
    pg.wait_for_timeout(200)
    assert srv.links == [] and rows_of(pg) == 3 and srv.map_gets == 0
    pg.evaluate("window.__answer = true")
    pg.click(f'[data-aov-takeout="{SHORT}"]')
    pg.wait_for_function("() => document.querySelectorAll('tr.aov-row').length === 2")
    assert srv.links == [{"action": "unlink", "kind": "country", "value": ckey, "account": SHORT}]


def test_success_repaints_row_tile_and_counters_without_reload(rig):
    srv, make = rig
    pg = make()
    all_count = '.aov-fbar [data-aov-filter="all"] b'
    assert pg.inner_text(all_count) == "3"
    pg.click(f'[data-aov-takeout="{SHORT}"]')
    pg.wait_for_function("() => document.querySelectorAll('tr.aov-row').length === 2")
    assert pg.evaluate("window.__keep") == "same page"        # страница не перезагружалась
    assert srv.map_gets == 1
    assert pg.locator(f'[data-aov-takeout="{SHORT}"]').count() == 0
    assert pg.locator(f'[data-aov-open="{LONG}"]:visible').count() == 1
    assert pg.inner_text(all_count) == "2"
    pg.click("[data-aov-view=compact]")
    assert pg.locator(f'.aov-tile[data-name="{SHORT}"]').count() == 0
    assert pg.locator(".aov-tile").count() == 2
    # то же из поповера плитки (заглушка сервера отдаёт тот же ответ, поповер при этом закрывается)
    pg.click(f'.aov-tile[data-name="{NOPROF}"]')
    pg.click(f'#aov-pop [data-aov-takeout="{NOPROF}"]')
    pg.wait_for_function("() => document.getElementById('aov-pop').hidden")
    assert srv.links[-1]["account"] == NOPROF
    assert not pg.errors


def test_error_is_shown_next_to_the_button_and_the_row_stays(rig):
    srv, make = rig
    srv.link_reply = (400, {"ok": False, "error": "Could not save the account map."})
    pg = make()
    pg.click(f'[data-aov-takeout="{SHORT}"]')
    err = pg.locator(f'[data-aov-takeout="{SHORT}"] + .acl-inline-err')
    err.wait_for()
    assert err.inner_text() == "Could not save the account map."
    assert rows_of(pg) == 3 and srv.map_gets == 0
    assert pg.locator(f'[data-aov-takeout="{SHORT}"]').is_enabled()
    assert not pg.errors
