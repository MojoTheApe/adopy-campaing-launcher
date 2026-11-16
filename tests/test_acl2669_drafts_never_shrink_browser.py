# -*- coding: utf-8 -*-
"""ACL-2669: черновики билдера никогда не уменьшаются сами.

Браузерная часть: клиентский cbKeep.save() переносит записи форм, которых нет
на странице, и не знает срока годности. Фикстуры те же, что в
test_acl1681_server_drafts: «сервер» — настоящий builder_drafts, один на все
контексты. Сюда же добавляются тесты следующих задач ACL-2669.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.web import builder_drafts as bd  # noqa: E402

pw_sync = pytest.importorskip("playwright.sync_api")

from test_builder_per_account import _render  # noqa: E402
from test_drawer_review_browser import add  # noqa: E402

DAY = 24 * 60 * 60 * 1000
FORM_KEY = "saudi-arabia|Demo"


@pytest.fixture(autouse=True)
def _local_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("ACL_CONFIG_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture(scope="module")
def browser():
    with pw_sync.sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=os.environ.get("ACL_BROWSER_EXECUTABLE") or None)
        yield b
        b.close()


class Profile:
    """Один «профиль Chrome»: своё хранилище, общий сервер builder_drafts."""

    def __init__(self, browser, origin, html):
        self.origin = origin
        self.ctx = browser.new_context()
        self.errors: list[str] = []
        self.html = html
        self.posts: list[dict] = []
        self.page = self.ctx.new_page()
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.page.route("**/*", self._route)

    def _route(self, r):
        path = urlparse(r.request.url).path
        if r.request.resource_type == "document":
            r.fulfill(content_type="text/html", body=self.html)
        elif r.request.resource_type == "script":
            r.fulfill(content_type="application/javascript", body="")
        elif path == "/api/builder-state" and r.request.method == "GET":
            r.fulfill(json={"ok": True, "docs": bd.load()})
        elif path == "/api/builder-state":
            body = json.loads(r.request.post_data)
            self.posts.append(body)
            accepted, doc = bd.save(body["name"], body["saved_at"], body["data"], body.get("base"))
            r.fulfill(json={"ok": True, "accepted": accepted, "doc": doc})
        else:
            r.fulfill(json={"files": [], "texts": [], "rows": [], "profiles": [], "waiting": False})

    def open(self):
        self.page.goto(self.origin + "/")
        self.page.wait_for_function("window.cbSrv && cbSrv.ready()", polling=100)
        return self.page

    def close(self):
        self.ctx.close()


@pytest.fixture
def make(browser):
    html = _render({"Demo": [], "Other": []})
    made: list[Profile] = []

    def factory(origin="http://127.0.0.1:8000"):
        prof = Profile(browser, origin, html)
        made.append(prof)
        return prof

    yield factory
    for prof in made:
        assert not prof.errors, prof.errors
        prof.close()


def _until(cond, page, timeout_s=10.0):
    deadline = time.monotonic() + timeout_s
    while not cond() and time.monotonic() < deadline:
        page.wait_for_timeout(50)
    assert cond(), "ждали-ждали — не дождались"


def campaign(asset, cid=None):
    """Минимальная валидная запись кампании в документе keep."""
    c = {"fields": {"asset": asset}, "shown": {}, "ads": 1}
    if cid:
        c["id"] = cid
    return c


def seed_keep(forms, touched=None, removed=None):
    """Положить документ keep на «сервер» до открытия страницы."""
    now = int(time.time() * 1000)
    doc = {"v": 4, "forms": {}}
    for key, assets in forms.items():
        doc["forms"][key] = {"touched": touched or now, "tracking": "",
                             "campaigns": [campaign(a) for a in assets]}
    if removed:
        doc["removed"] = removed
    bd.save("keep", now, doc)


def server_keep():
    return bd.load()["keep"]["data"]


def edit_first_asset(page, text):
    page.evaluate("t => { const a = document.querySelector('.cb-slot [name$=\"_asset\"]'); a.value = t;"
                  " a.dispatchEvent(new Event('input', {bubbles: true})); }", text)


def test_campaigns_of_an_account_not_on_the_page_survive_an_edit(make):
    seed_keep({"Peru|UL869": ["P1", "P2", "P3"], "Chile|UL869": ["C1", "C2"]})
    prof = make()
    p = prof.open()
    add(p)
    edit_first_asset(p, "Edited")
    _until(lambda: "Edited" in json.dumps(server_keep()), p)
    # клиент сам не шлёт укороченный документ — не полагаемся на серверное слияние
    sent = [post["data"] for post in prof.posts if post["name"] == "keep" and "Edited" in json.dumps(post["data"])]
    assert sent, "правка не ушла на сервер"
    for data in sent:
        assert len(data["forms"]["Peru|UL869"]["campaigns"]) == 3
        assert len(data["forms"]["Chile|UL869"]["campaigns"]) == 2
    assert len(server_keep()["forms"]["Peru|UL869"]["campaigns"]) == 3


def test_a_fifteen_day_old_draft_survives_reload(make):
    old = int(time.time() * 1000) - 15 * DAY
    seed_keep({FORM_KEY: ["Ancient"]}, touched=old)
    p = make().open()
    p.wait_for_function("document.querySelectorAll('.cb-slot').length === 1", polling=100)
    assert p.evaluate("document.querySelector('.cb-slot [name$=\"_asset\"]').value") == "Ancient"
    p.reload()
    p.wait_for_function("window.cbSrv && cbSrv.ready()", polling=100)
    p.wait_for_function("document.querySelectorAll('.cb-slot').length === 1", polling=100)
    # после сохранения запись всё ещё держит свою кампанию
    p.evaluate("cbKeep.save()")
    _until(lambda: "Ancient" in json.dumps(server_keep()["forms"].get(FORM_KEY, {})), p)
    assert len(server_keep()["forms"][FORM_KEY]["campaigns"]) == 1


def keep_slots(p):
    return p.evaluate("document.querySelectorAll('.cb-slot').length")


def test_form_with_zero_slots_and_no_tombstone_keeps_its_entry(make):
    seed_keep({FORM_KEY: ["Keep me"]})
    prof = make()
    p = prof.open()
    p.wait_for_function("document.querySelectorAll('.cb-slot').length === 1", polling=100)
    # карточку убрали не человеком (не через cbRemoveSlot): надгробия нет
    p.evaluate("document.querySelector('.cb-slot').remove(); cbKeep.save()")
    assert keep_slots(p) == 0
    p.wait_for_timeout(800)
    assert p.evaluate("Object.keys(cbKeep.all())") == [FORM_KEY]
    assert server_keep()["forms"][FORM_KEY]["campaigns"][0]["fields"]["asset"] == "Keep me"
    assert FORM_KEY not in (server_keep().get("removed") or {})


def test_removing_the_last_campaign_stays_removed_after_reload(make):
    seed_keep({FORM_KEY: ["Gone"]})
    prof = make()
    p = prof.open()
    p.wait_for_function("document.querySelectorAll('.cb-slot').length === 1", polling=100)
    p.evaluate("cbRemoveSlot(document.querySelector('.cb-remove-btn'))")
    _until(lambda: FORM_KEY not in server_keep()["forms"], p)
    assert FORM_KEY in server_keep()["removed"]
    p.reload()
    p.wait_for_function("window.cbSrv && cbSrv.ready()", polling=100)
    p.wait_for_timeout(500)
    assert keep_slots(p) == 0
    assert FORM_KEY not in server_keep()["forms"]


def test_removing_a_campaign_is_tombstoned_and_an_older_copy_cannot_bring_it_back(make):
    seed_keep({FORM_KEY: ["A", "B"]})
    old_doc = json.loads(json.dumps(server_keep()))
    prof = make()
    p = prof.open()
    p.wait_for_function("document.querySelectorAll('.cb-slot').length === 2", polling=100)
    p.evaluate("cbKeep.save()")
    _until(lambda: all(c.get("id") for c in server_keep()["forms"][FORM_KEY]["campaigns"]), p)
    full = json.loads(json.dumps(server_keep()))
    gone = full["forms"][FORM_KEY]["campaigns"][1]
    p.evaluate("cbRemoveSlot(document.querySelectorAll('.cb-remove-btn')[1])")
    _until(lambda: len(server_keep()["forms"][FORM_KEY]["campaigns"]) == 1, p)
    assert f"{FORM_KEY}#{gone['id']}" in server_keep()["removed"]
    # старая копия с обоими кампаниями приходит позже и новее по saved_at
    full["forms"][FORM_KEY]["touched"] = old_doc["forms"][FORM_KEY]["touched"]
    full.pop("removed", None)
    bd.save("keep", int(time.time() * 1000) + 5000, full)
    ids = [c["id"] for c in server_keep()["forms"][FORM_KEY]["campaigns"]]
    assert gone["id"] not in ids and len(ids) == 1


def test_two_tabs_older_tab_save_does_not_drop_newer_campaigns(make):
    seed_keep({FORM_KEY: ["One"]})
    a = make().open()
    a.wait_for_function("document.querySelectorAll('.cb-slot').length === 1", polling=100)
    b_prof = make("http://127.0.0.1:8001")
    b = b_prof.open()
    b.wait_for_function("document.querySelectorAll('.cb-slot').length === 1", polling=100)
    add(b)
    _until(lambda: len(server_keep()["forms"][FORM_KEY]["campaigns"]) == 2, b)
    # старая вкладка правит своё: сервер не должен потерять вторую кампанию
    edit_first_asset(a, "One edited")
    _until(lambda: "One edited" in json.dumps(server_keep()), a)
    assert len(server_keep()["forms"][FORM_KEY]["campaigns"]) == 2


def test_duplicated_campaign_gets_its_own_id_and_ids_survive_reload(make):
    seed_keep({FORM_KEY: ["Orig"]})
    prof = make()
    p = prof.open()
    p.wait_for_function("document.querySelectorAll('.cb-slot').length === 1", polling=100)
    p.evaluate("cbDuplicateCampaign(document.querySelector('.cb-dup-slot-btn'))")
    _until(lambda: len(server_keep()["forms"][FORM_KEY]["campaigns"]) == 2, p)
    ids = [c["id"] for c in server_keep()["forms"][FORM_KEY]["campaigns"]]
    assert len(set(ids)) == 2
    p.reload()
    p.wait_for_function("window.cbSrv && cbSrv.ready()", polling=100)
    p.wait_for_function("document.querySelectorAll('.cb-slot').length === 2", polling=100)
    assert p.evaluate("[...document.querySelectorAll('.cb-slot')].map(s => s.dataset.cbdId)") == ids
    touched = server_keep()["forms"][FORM_KEY]["touched"]
    p.evaluate("cbKeep.save()")
    p.wait_for_timeout(300)
    assert server_keep()["forms"][FORM_KEY]["touched"] == touched


def test_moving_the_last_campaign_to_another_form_tombstones_only_the_source(make):
    other = "saudi-arabia|Other"
    seed_keep({FORM_KEY: ["Mover"]})
    prof = make()
    p = prof.open()
    p.wait_for_function("document.querySelectorAll('.cb-slot').length === 1", polling=100)
    p.evaluate("""() => {
      const forms = document.querySelectorAll('form.open-account-form');
      const copy = cbDuplicateSlot(forms[0].querySelector('.cb-slot'), forms[1]);
      cbRemoveSlot(forms[0].querySelector('.cb-remove-btn'));
    }""")
    _until(lambda: FORM_KEY not in server_keep()["forms"] and other in server_keep()["forms"], p)
    assert FORM_KEY in server_keep()["removed"]
    assert other not in server_keep()["removed"]
    p.reload()
    p.wait_for_function("window.cbSrv && cbSrv.ready()", polling=100)
    p.wait_for_function("document.querySelectorAll('.cb-slot').length === 1", polling=100)
    assert p.evaluate("document.querySelectorAll('form.open-account-form')[1].querySelectorAll('.cb-slot').length") == 1
    assert len(server_keep()["forms"][other]["campaigns"]) == 1
    assert FORM_KEY not in server_keep()["forms"]
