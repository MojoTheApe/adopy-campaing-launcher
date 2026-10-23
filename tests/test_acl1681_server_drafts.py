# -*- coding: utf-8 -*-
"""ACL-1681: черновики билдера переживают крах Chrome — копия на машине дашборда.

Серверная часть (src/web/builder_drafts.py) проверяется напрямую, во временной
папке ACL_CONFIG_DIR. Браузерная — настоящим ящиком в офлайн-Chromium, где
запросы /api/builder-state отвечает настоящий модуль builder_drafts: «сервер» один
на все контексты, а контексты — это разные профили/адреса Chrome.
"""
from __future__ import annotations

import asyncio
import importlib
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

from src.web import app as web  # noqa: E402
from src.web import builder_drafts as bd  # noqa: E402


@pytest.fixture(autouse=True)
def _local_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("ACL_CONFIG_DIR", str(tmp_path))
    return tmp_path


class _Req:
    def __init__(self, body):
        self._body = body

    async def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def _post(body):
    r = asyncio.run(web.api_builder_state_set(_Req(body)))
    return r.status_code, json.loads(r.body)


def _get():
    return json.loads(web.api_builder_state_get().body)["docs"]


# ── сервер ───────────────────────────────────────────────────────────────

def test_a_saved_draft_comes_back_and_survives_a_dashboard_restart():
    assert _get() == {}
    status, out = _post({"name": "keep", "saved_at": 1000, "data": {"v": 2, "forms": {"SA|Demo": {"x": 1}}}})
    assert status == 200 and out["accepted"] is True
    # Перезапуск дашборда: модуль заново, память пуста, файл на месте.
    importlib.reload(bd)
    assert bd.load()["keep"] == {"saved_at": 1000, "data": {"v": 2, "forms": {"SA|Demo": {"x": 1}}}}


def test_the_newer_copy_wins_and_the_loser_is_kept_as_a_backup():
    _post({"name": "keep", "saved_at": 2000, "data": {"forms": {"a": 2}}})
    status, out = _post({"name": "keep", "saved_at": 1000, "data": {"forms": {"a": 1}}})
    assert status == 200 and out["accepted"] is False
    assert out["doc"]["data"] == {"forms": {"a": 2}}, "старая запись затёрла новую"
    assert _get()["keep"]["saved_at"] == 2000
    older = bd.load_older("keep")
    assert older and older["data"] == {"forms": {"a": 1}}, "проигравшая копия потеряна"


def test_a_concurrent_writer_displaces_a_copy_it_never_saw_and_that_copy_is_kept():
    _post({"name": "keep", "saved_at": 1000, "data": {"forms": {"a": "from chrome"}}})
    # Второй профиль не видел серверную копию (base пуст), но пишет позже.
    _, out = _post({"name": "keep", "saved_at": 3000, "base": None, "data": {"forms": {"a": "from edge"}}})
    assert out["accepted"] is True
    assert bd.load_older("keep")["data"] == {"forms": {"a": "from chrome"}}
    # А тот, кто серверную копию видел (base совпал), прежнюю версию не плодит.
    Path(bd._older_path("keep")).unlink()
    _post({"name": "keep", "saved_at": 4000, "base": 3000, "data": {"forms": {"a": "edited"}}})
    assert bd.load_older("keep") is None


def test_a_corrupt_draft_file_is_kept_as_a_backup_and_does_not_break_the_page(_local_dir):
    bd.path().parent.mkdir(parents=True, exist_ok=True)
    bd.path().write_text("{not json", encoding="utf-8")
    assert _get() == {}, "испорченный файл не должен ронять страницу"
    status, out = _post({"name": "jobs", "saved_at": 5, "data": {"j1": {}}})
    assert status == 200 and out["accepted"] is True
    backups = list(_local_dir.glob("builder_drafts.json.corrupt-*"))
    assert len(backups) == 1 and backups[0].read_text(encoding="utf-8") == "{not json"
    assert _get()["jobs"]["data"] == {"j1": {}}


def test_a_file_of_the_wrong_shape_is_also_kept_not_overwritten(_local_dir):
    bd.path().parent.mkdir(parents=True, exist_ok=True)
    bd.path().write_text('["not", "a", "doc"]', encoding="utf-8")
    _post({"name": "ui", "saved_at": 1, "data": {"open": True}})
    assert len(list(_local_dir.glob("builder_drafts.json.corrupt-*"))) == 1


def test_the_write_is_atomic_no_temp_files_are_left(_local_dir):
    for i in range(5):
        _post({"name": "keep", "saved_at": 100 + i, "data": {"forms": {"n": i}}})
    names = sorted(p.name for p in _local_dir.iterdir())
    assert "builder_drafts.json" in names
    assert not [n for n in names if n.endswith(".tmp")], names
    json.loads(bd.path().read_text(encoding="utf-8"))


@pytest.mark.parametrize("body", [
    {"name": "nope", "saved_at": 1, "data": {}},
    {"name": "keep", "saved_at": "x", "data": {}},
    {"name": "keep", "saved_at": True, "data": {}},
    {"name": "keep", "saved_at": 1, "data": []},
    {"name": "keep", "saved_at": 1, "data": {"blob": "x" * (bd.MAX_DOC_BYTES + 1)}},
])
def test_bad_documents_are_refused_and_nothing_is_written(body, _local_dir):
    status, out = _post(body)
    assert status == 400 and out["ok"] is False
    assert not bd.path().exists()


def test_bad_json_is_a_400():
    r = asyncio.run(web.api_builder_state_set(_Req(ValueError("boom"))))
    assert r.status_code == 400
    r = asyncio.run(web.api_builder_state_set(_Req([1])))
    assert r.status_code == 400


# ── браузер ──────────────────────────────────────────────────────────────

pw_sync = pytest.importorskip("playwright.sync_api")

from test_builder_per_account import _render  # noqa: E402
from test_drawer_review_browser import add  # noqa: E402


@pytest.fixture(scope="module")
def browser():
    with pw_sync.sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=os.environ.get("ACL_BROWSER_EXECUTABLE") or None)
        yield b
        b.close()


class Profile:
    """Один «профиль Chrome» на одном «адресе»: своё хранилище, общий сервер."""

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


def _asset(page):
    return page.evaluate("() => { const a = document.querySelector('.cb-slot [name$=\"_asset\"]'); return a ? a.value : null; }")


def _draft_on_server(text):
    doc = bd.load().get("keep")
    return bool(doc) and text in json.dumps(doc["data"])


def _build(page, text="Alpha"):
    add(page)
    page.evaluate("t => { const a = document.querySelector('.cb-slot [name$=\"_asset\"]'); a.value = t;"
                  " a.dispatchEvent(new Event('input', {bubbles: true})); }", text)


def test_a_draft_survives_a_fresh_browser_profile(make):
    """Chrome упал, профиль/хранилище пусты — карточка возвращается с сервера."""
    one = make()
    p = one.open()
    _build(p)
    _until(lambda: _draft_on_server("Alpha"), p)

    two = make()   # новый контекст = пустой localStorage
    q = two.open()
    q.wait_for_function("document.querySelectorAll('.cb-slot').length === 1", polling=100)
    assert _asset(q) == "Alpha"
    assert q.evaluate("localStorage.getItem('acl.builder.v2')").count("Alpha") == 1, "кэш не обновился"


def test_a_draft_follows_the_user_between_127_0_0_1_and_localhost(make):
    one = make("http://127.0.0.1:8000")
    p = one.open()
    _build(p, "Beta")
    _until(lambda: _draft_on_server("Beta"), p)
    two = make("http://localhost:8000")
    q = two.open()
    q.wait_for_function("document.querySelectorAll('.cb-slot').length === 1", polling=100)
    assert _asset(q) == "Beta"


def test_a_draft_survives_clearing_local_storage_and_a_dashboard_restart(make):
    one = make()
    p = one.open()
    _build(p, "Gamma")
    _until(lambda: _draft_on_server("Gamma"), p)
    importlib.reload(bd)               # «перезапуск дашборда»: в памяти пусто, файл на диске
    # Страница F5 пишет в localStorage своё текущее состояние, поэтому чистим
    # хранилище перед самой загрузкой — как после краха.
    one.ctx.add_init_script("if (!sessionStorage.getItem('cleared')) { localStorage.clear(); sessionStorage.setItem('cleared', '1'); }")
    p.reload()
    p.wait_for_function("window.cbSrv && cbSrv.ready()", polling=100)
    p.wait_for_function("document.querySelectorAll('.cb-slot').length === 1", polling=100)
    assert _asset(p) == "Gamma"


def test_the_drawer_open_state_is_restored_from_the_server(make):
    one = make()
    p = one.open()
    p.evaluate("cbDrawer.open(); cbDrawer.expand()")
    _until(lambda: bd.load().get("ui", {}).get("data") == {"open": True, "expanded": True}, p)
    two = make()
    q = two.open()
    q.wait_for_function("cbDrawer.isOpen() && cbDrawer.isExpanded()", polling=100)


def test_publish_jobs_memory_is_restored_from_the_server(make):
    now = int(time.time() * 1000)
    bd.save("jobs", now, {"job-1": {"account": "Demo", "country": "Saudi Arabia", "names": ["C1"],
                                    "at": now, "lost": False, "track": {}}})
    q = make().open()
    q.wait_for_function("(localStorage.getItem('acl.builder.jobs') || '').indexOf('job-1') !== -1", polling=100)


def test_a_newer_server_copy_replaces_an_older_local_one_and_the_local_is_kept(make):
    one = make()
    p = one.open()
    _build(p, "Old local")
    _until(lambda: _draft_on_server("Old local"), p)
    state = bd.load()["keep"]
    newer = json.loads(json.dumps(state["data"]).replace("Old local", "Newer server"))
    bd.save("keep", state["saved_at"] + 60_000, newer)
    p.reload()
    p.wait_for_function("window.cbSrv && cbSrv.ready()", polling=100)
    p.wait_for_function("document.querySelectorAll('.cb-slot').length === 1", polling=100)
    p.wait_for_function("document.querySelector('.cb-slot [name$=\"_asset\"]').value === 'Newer server'")
    older = p.evaluate("localStorage.getItem('acl.builder.v2.older')")
    assert older and "Old local" in older, "проигравшая локальная копия потеряна"


def test_a_newer_local_copy_is_pushed_and_the_old_server_copy_is_backed_up(make):
    one = make()
    p = one.open()
    _build(p, "First")
    _until(lambda: _draft_on_server("First"), p)
    two = make()
    q = two.open()
    q.wait_for_function("document.querySelectorAll('.cb-slot').length === 1", polling=100)
    # Второй профиль правит — серверная копия обновляется им.
    q.evaluate("() => { const a = document.querySelector('.cb-slot [name$=\"_asset\"]'); a.value = 'Second';"
               " a.dispatchEvent(new Event('input', {bubbles: true})); }")
    _until(lambda: _draft_on_server("Second"), q)
    # Первый профиль, не видевший правки, пишет позже: его запись принимается,
    # а то, что он перетёр и не видел, остаётся в backup.
    p.evaluate("() => { const a = document.querySelector('.cb-slot [name$=\"_asset\"]'); a.value = 'Third';"
               " a.dispatchEvent(new Event('input', {bubbles: true})); }")
    _until(lambda: _draft_on_server("Third") or bd.load_older("keep") is not None, p)
    # Что бы ни победило, "Second" не пропал молча.
    live = json.dumps(bd.load()["keep"]["data"])
    older = bd.load_older("keep")
    assert "Second" in live or (older and "Second" in json.dumps(older["data"]))


def test_without_the_server_the_builder_works_on_local_storage_alone(browser):
    html = _render({"Demo": []})
    ctx = browser.new_context()
    p = ctx.new_page()
    errors: list[str] = []
    p.on("pageerror", lambda e: errors.append(str(e)))

    def route(r):
        if r.request.resource_type == "document":
            r.fulfill(content_type="text/html", body=html)
        elif urlparse(r.request.url).path == "/api/builder-state":
            r.abort()
        else:
            r.fulfill(json={"files": [], "texts": [], "rows": [], "profiles": [], "waiting": False})

    p.route("**/*", route)
    p.goto("http://acl.test/")
    _build(p, "Offline")
    p.evaluate("cbKeep.save()")
    assert "Offline" in p.evaluate("localStorage.getItem('acl.builder.v2')")
    p.reload()
    p.wait_for_function("document.querySelectorAll('.cb-slot').length === 1", polling=100)
    assert _asset(p) == "Offline"
    assert not errors, errors
    ctx.close()


@pytest.fixture
def autosave(browser):
    """Real cbSrv with deterministic clock, debounce and delayed fetch replies."""
    page = browser.new_page()
    page.route("**/*", lambda route: route.fulfill(content_type="text/html", body="<html></html>"))
    page.goto("http://acl.test/")
    page.evaluate("""() => {
      localStorage.clear();
      const t = window.draftsTest = { now: 0, data: {}, server: null, posts: [], keepalive: [], pending: [], applied: [], timers: new Map(), seq: 0 };
      const copy = x => JSON.parse(JSON.stringify(x));
      Date.now = () => t.now;
      window.setTimeout = fn => { const id = ++t.seq; t.timers.set(id, fn); return id; };
      window.clearTimeout = id => t.timers.delete(id);
      window.fetch = (url, opts) => {
        if (!opts || !opts.method) return Promise.resolve({ json: () => Promise.resolve({ ok: true, docs: {} }) });
        const body = JSON.parse(opts.body);
        t.posts.push(body);
        t.keepalive.push(opts.keepalive);
        const accepted = !t.server || body.saved_at >= t.server.saved_at;
        if (accepted) t.server = { saved_at: body.saved_at, data: copy(body.data) };
        const answer = { ok: true, accepted, doc: copy(t.server) };
        return new Promise(resolve => t.pending.push(() => resolve({ json: () => Promise.resolve(answer) })));
      };
      t.edit = (at, text) => { t.now = at; t.data = { text }; cbSrv.note('keep'); };
      t.tick = () => { const fs = [...t.timers.values()]; t.timers.clear(); fs.forEach(fn => fn()); };
      t.reply = () => t.pending.shift()();
      t.result = () => ({ data: t.data, server: t.server, posts: t.posts,
        applied: t.applied, meta: JSON.parse(localStorage.getItem('acl.builder.sync')) });
    }""")
    template = (ROOT / "src/web/templates/report.html").read_text(encoding="utf-8")
    start = template.index("window.cbSrv = (function () {")
    page.add_script_tag(content=template[start:template.index("</script>", start)])
    page.evaluate("""() => cbSrv.register('keep', {
      snapshot: () => draftsTest.data,
      legacyAt: () => 0,
      apply: doc => { draftsTest.applied.push(doc); draftsTest.data = doc.data; }
    })""")
    page.wait_for_function("cbSrv.ready()")
    yield page
    page.close()


@pytest.mark.parametrize("closing", [False, True])
def test_delayed_save_acknowledgement_keeps_and_serializes_the_latest_edit(autosave, closing):
    page = autosave
    page.evaluate("draftsTest.edit(1000, 'A'); draftsTest.tick()")
    page.evaluate("draftsTest.edit(2000, 'B'); draftsTest.tick()")
    assert page.evaluate("draftsTest.posts.length") == 1, "same-document writes overlapped"
    page.evaluate("draftsTest.edit(3000, 'C')")
    if closing:
        page.evaluate("cbSrv.flush()")
    assert page.evaluate("draftsTest.posts.length") == 1
    page.evaluate("draftsTest.reply()")
    before = page.evaluate("draftsTest.result()")
    assert before["data"] == {"text": "C"}
    assert before["meta"] == {"at": {"keep": 3000}, "base": {"keep": 1000}}
    page.evaluate("draftsTest.tick()")
    sent = page.evaluate("draftsTest.posts")
    assert sent == [
        {"name": "keep", "saved_at": 1000, "base": None, "data": {"text": "A"}},
        {"name": "keep", "saved_at": 3000, "base": 1000, "data": {"text": "C"}},
    ]
    assert page.evaluate("draftsTest.keepalive") == [False, closing]
    page.evaluate("draftsTest.reply()")
    final = page.evaluate("draftsTest.result()")
    assert final["server"] == {"saved_at": 3000, "data": {"text": "C"}}
    assert final["data"] == {"text": "C"} and final["applied"] == []
    assert final["meta"] == {"at": {"keep": 3000}, "base": {"keep": 3000}}
    page.evaluate("cbSrv.flush()")
    assert page.evaluate("draftsTest.posts.length") == 2, "acknowledged draft stayed dirty"


def test_delayed_refusal_preserves_a_newer_local_edit_and_uses_the_server_base(autosave):
    page = autosave
    page.evaluate("draftsTest.server = { saved_at: 2000, data: { text: 'B from another tab' } }")
    page.evaluate("draftsTest.edit(1000, 'A'); draftsTest.tick()")
    page.evaluate("draftsTest.edit(3000, 'C'); draftsTest.tick(); draftsTest.reply()")
    before = page.evaluate("draftsTest.result()")
    assert before["data"] == {"text": "C"}, "an old refusal replaced a later dirty edit"
    assert before["applied"] == []
    assert before["meta"] == {"at": {"keep": 3000}, "base": {"keep": 2000}}
    assert before["posts"][1] == {
        "name": "keep", "saved_at": 3000, "base": 2000, "data": {"text": "C"},
    }
    page.evaluate("draftsTest.reply()")
    final = page.evaluate("draftsTest.result()")
    assert final["server"] == {"saved_at": 3000, "data": {"text": "C"}}
    assert final["meta"] == {"at": {"keep": 3000}, "base": {"keep": 3000}}
    page.evaluate("cbSrv.flush()")
    assert page.evaluate("draftsTest.posts.length") == 2
