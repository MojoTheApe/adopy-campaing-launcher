"""Issue #1003: проверка плана склеивается и не закрывает профиль между проверками.

Сервер: одинаковые одновременные `check_plan` одного кабинета идут одним чтением.
Профиль, поднятый проверкой, остаётся тёплым (`PLAN_KEEP_WARM_S`) — его забирает
следующая проверка или Publish. Страница: `cbPreflightRun` не шлёт второй запрос
по форме, пока первый идёт (проверяется настоящим JS шаблона в Node).
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.analysis_agent import account_spend as _as  # noqa: E402
from src.web import publish_api  # noqa: E402


# ───────────────────────── сервер: склейка ─────────────────────────

def _slow_once(monkeypatch):
    started, release, calls = threading.Event(), threading.Event(), []

    def once(slots, *, account_label, country):
        calls.append((account_label, len(slots)))
        started.set()
        release.wait(5)
        return {"account": account_label, "results": [{"n": len(calls)}], "dry_run": True, "error": ""}

    monkeypatch.setattr(publish_api, "_check_plan_once", once)
    monkeypatch.setattr(publish_api, "_record", lambda *a, **k: None)
    return started, release, calls


def _run_two(slots_a, slots_b, started, release, account_b="Acc"):
    out = {}

    def go(name, slots, account):
        out[name] = publish_api.check_plan(slots, account_label=account, country="MX")

    a = threading.Thread(target=go, args=("a", slots_a, "Acc"))
    a.start()
    assert started.wait(5)
    b = threading.Thread(target=go, args=("b", slots_b, account_b))
    b.start()
    b.join(0.3)                      # второй не должен уйти в чтение
    release.set()
    a.join(5)
    b.join(5)
    return out


def test_concurrent_identical_checks_read_once(monkeypatch):
    started, release, calls = _slow_once(monkeypatch)
    out = _run_two([{"name": "C1"}], [{"name": "C1"}], started, release)
    assert len(calls) == 1
    assert out["a"] == out["b"] and out["a"]["results"] == [{"n": 1}]
    assert publish_api._PLAN_FLIGHTS == {}


def test_different_slots_or_accounts_are_not_merged(monkeypatch):
    started, release, calls = _slow_once(monkeypatch)
    release.set()
    _run_two([{"name": "C1"}], [{"name": "C2"}], started, release)
    assert len(calls) == 2
    calls.clear()
    started.clear()
    _run_two([{"name": "C1"}], [{"name": "C1"}], started, release, account_b="Other")
    assert len(calls) == 2


def test_a_finished_check_is_not_reused(monkeypatch):
    started, release, calls = _slow_once(monkeypatch)
    release.set()
    publish_api.check_plan([{"name": "C1"}], account_label="Acc", country="MX")
    publish_api.check_plan([{"name": "C1"}], account_label="Acc", country="MX")
    assert len(calls) == 2


# ───────────────────────── профиль остаётся тёплым ─────────────────────────

def test_a_successful_check_asks_to_keep_the_profile_warm(monkeypatch):
    from contextlib import contextmanager

    from src.analysis_agent import account_spend_api as _api
    from src.fb_api import graph as g
    from tests.test_acl1761_api_create import ACT, FakeMeta

    holder = []

    @contextmanager
    def session(account):
        client = g.GraphClient(FakeMeta(), allow_writes=False, sleep=lambda s: None,
                               min_interval_s=0.0, max_calls=24)
        sess = SimpleNamespace(client=client, act=ACT, account=account, notes={}, keep_next=False)
        holder.append(sess)
        yield sess

    monkeypatch.setattr(_api, "_session", session)
    monkeypatch.setattr(publish_api, "preflight_account", lambda client, act: SimpleNamespace())
    assert publish_api._plan_in_read_session("Acc", []) == []
    assert holder[0].keep_next is True
    assert holder[0].keep_grace_s == publish_api.PLAN_KEEP_WARM_S


def test_finish_started_keeps_the_profile_for_the_given_grace(monkeypatch):
    closed, timers = [], []

    class FakeTimer:
        def __init__(self, interval, fn, args=()):
            timers.append(interval)
            self.daemon = False

        def start(self):
            pass

    monkeypatch.setattr(_as.threading, "Timer", FakeTimer)
    monkeypatch.setattr(_as, "_close_started", lambda o: closed.append(o))
    opened = _as._ReadOpen(account="Acc", started=True, profile_id="p1")
    other = _as._ReadOpen(account="Acc", started=True, profile_id="p2")
    third = _as._ReadOpen(account="Acc", started=True, profile_id="p3")
    try:
        _as._finish_started(opened, keep_for_next=True, grace_s=120.0)
        assert closed == [] and timers == [120.0]
        assert _as._PENDING_CLOSE["p1"] is opened
        _as._finish_started(other, keep_for_next=True)          # штатная пауза
        assert timers[-1] == _as.CLOSE_GRACE_S
        _as._finish_started(third)                              # не просили держать
        assert closed == [third]
    finally:
        for pid in ("p1", "p2", "p3"):
            _as._PENDING_CLOSE.pop(pid, None)


def test_the_next_check_takes_the_warm_profile_over():
    prev = _as._ReadOpen(account="Acc", started=True, slot_id="s1", profile_id="p9", entry="E")
    _as._PENDING_CLOSE["p9"] = prev
    try:
        nxt = _as._ReadOpen(account="Acc")
        _as._take_over_pending("p9", nxt)
        assert nxt.started and nxt.slot_id == "s1" and "p9" not in _as._PENDING_CLOSE
    finally:
        _as._PENDING_CLOSE.pop("p9", None)


# ───────────────────────── страница: один запрос на форму ─────────────────────────


_NODE_BODY = r"""
const fetched = [];
let release;
globalThis.fetch = (url, init) => { fetched.push(url); return new Promise((r) => { release = () => r({ json: async () => ({ account: 'A', results: [] }) }); }); };
function cbPfName(s) { return s.n; }
function cbPlanFormData(form, list) { return {}; }
function cbPfRefresh() {}
function cbPfStore(form, data, revs) { for (const k in revs) form._fresh[k] = revs[k]; }
function cbPfFresh(s) { return s.form._fresh[s.n] === s.rev; }
globalThis.window = globalThis;
window.cbPfRev = (s) => s.rev;
const form = { _fresh: {}, querySelectorAll: () => slots };
const mk = (n, rev) => ({ n: n, rev: rev, form: form });
const slots = [mk('a', 1), mk('b', 1)];
(async () => {
  const out = {};
  const p1 = window.cbPreflightRun(form, slots);            // Check plan
  const p2 = window.cbPreflightRun(form, [slots[0]]);       // «Check» в карточке
  const p3 = window.cbPreflightRun(form, null);             // Publish all
  out.sameRun = (p2 === p1) && (p3 === p1);
  out.fetchedWhileRunning = fetched.length;
  const p4 = window.cbPreflightRun(form, [mk('a', 2)]);     // правка во время проверки
  out.editedWaits = (p4 !== p1);
  release();
  await p1;
  await new Promise((r) => setTimeout(r, 5));
  out.afterFirst = fetched.length;                          // p4 проверяет заново только своё
  release();
  await p4;
  out.total = fetched.length;
  out.flightCleared = !form._pfFlight;
  process.stdout.write(JSON.stringify(out));
})();
"""


def _template_js(start: str, end: str) -> str:
    text = (ROOT / "src" / "web" / "templates" / "report.html").read_text(encoding="utf-8")
    i = text.index(start)
    return text[i:text.index(end, i)]


def test_preflight_run_shares_the_request_in_flight_in_node():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    js = _template_js("window.cbPreflightRun = function (form, slots) {", "/* Свежи ли серверные вердикты")
    script = "const fetchedInit = 0;\n" + _NODE_BODY.split("(async () => {")[0] + js + "\n(async () => {" \
        + _NODE_BODY.split("(async () => {", 1)[1]
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
        fh.write(script)
        path = fh.name
    try:
        done = subprocess.run([node, path], capture_output=True, text=True, encoding="utf-8", timeout=60)
    finally:
        Path(path).unlink(missing_ok=True)
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    assert out["sameRun"] is True and out["fetchedWhileRunning"] == 1
    assert out["editedWaits"] is True
    assert out["afterFirst"] == 2 and out["total"] == 2        # одна проверка + одна по правке
    assert out["flightCleared"] is True


def test_publish_all_tells_it_starts_by_itself():
    text = (ROOT / "src" / "web" / "templates" / "report.html").read_text(encoding="utf-8")
    assert "the build starts by itself if the check passes" in text
