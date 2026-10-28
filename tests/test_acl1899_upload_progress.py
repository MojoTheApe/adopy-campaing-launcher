"""ACL-1899: загрузка файла без общего срока, процент на панели, Stop и сверка в несколько чтений.

Живой Meta и профиль не нужны: вкладка и Meta — поддельные. Помощник страницы
проверяется в Node с поддельным XMLHttpRequest.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_api import creator as cr  # noqa: E402
from src.fb_api import graph as g  # noqa: E402
from src.fb_api import token_capture as tc  # noqa: E402
from src.fb_api import write_policy as wp  # noqa: E402
from src.fb_api.cdp import CdpError, CdpTimeout  # noqa: E402
from tests.test_acl1761_api_create import ACT, TOKEN, FakeMeta, _camp, _draft  # noqa: E402


class JobTab:
    """Вкладка: startCall -> job, poll отдаёт `running` заданное число раз, потом итог."""

    def __init__(self, running_polls=3, result=None, fail_poll_at=None, start_reply=None, call_exc=None,
                 timeout_polls=()):
        self.timeout_polls = set(timeout_polls)
        self.running_polls = running_polls
        self.result = result if result is not None else {"status": 200, "body": {"id": "77"}}
        self.fail_poll_at = fail_poll_at
        self.start_reply = start_reply
        self.call_exc = call_exc
        self.polls = 0
        self.staged = 0
        self.timeouts: list[tuple[str, float]] = []
        self.names: list[str] = []

    def create_isolated_world(self, name):
        return 7

    def evaluate_in(self, ctx, expr, timeout=60.0):
        if expr.lstrip().startswith("(() =>"):
            return None
        if ".state()" in expr:
            return {"kind": "ads_manager", "has_token": True}
        if ".stage(" in expr:
            import base64

            _sid, chunk = json.loads("[" + expr.split(".stage(", 1)[1].rsplit(")", 1)[0] + "]")
            self.staged += len(base64.b64decode(chunk))
            return {"ok": True, "bytes": self.staged}
        for name in ("startCall", "poll", "abort", "call"):
            if f".{name}(" in expr:
                self.names.append(name)
                self.timeouts.append((name, timeout))
        if ".startCall(" in expr:
            return self.start_reply if self.start_reply is not None else {"job": "j1"}
        if ".poll(" in expr:
            self.polls += 1
            if self.polls in self.timeout_polls:
                raise CdpTimeout("poll slow")
            if self.fail_poll_at is not None and self.polls == self.fail_poll_at:
                raise CdpError("target closed")
            if self.polls <= self.running_polls:
                return {"state": "running"}
            return {"state": "done", "result": self.result}
        if ".abort(" in expr:
            return {"ok": True}
        if ".call(" in expr:
            if self.call_exc:
                raise self.call_exc
            return self.result
        raise AssertionError(expr[:60])


def _transport(tab):
    slept: list[float] = []
    now = [0.0]

    def sleep(s):
        slept.append(s)
        now[0] += s
    t = tc.InPageGraph(tab, allow_writes=True, write_act=ACT, sleep=sleep, clock=lambda: now[0])
    return t, slept


def _wr(t, **kw):
    return t.write(f"/{ACT}/advideos", {"name": "n", "title": "n"}, files={"source": b"0123456789"},
                   filename="v.mp4", **kw)


# ───────────────────────── Python: транспорт ─────────────────────────

def test_a_file_write_has_no_overall_deadline():
    # 500 опросов по секунде = 500 с, вчетверо дольше прежних 120 с; запись ждёт ответа.
    tab = JobTab(running_polls=500)
    t, slept = _transport(tab)
    got = _wr(t)
    assert got == {"status": 200, "body": {"id": "77"}}
    assert sum(slept) >= 500 and tab.names.count("startCall") == 1
    assert "call" not in tab.names
    # каждый отдельный локальный вызов - с коротким сроком
    assert all(tmo <= 20 for _n, tmo in tab.timeouts), tab.timeouts


def test_elapsed_seconds_are_forwarded_for_each_poll():
    seen = []
    _wr(_transport(JobTab(running_polls=3))[0], progress=seen.append)
    assert seen == [0.0, 1.0, 2.0]


def test_a_failing_progress_callback_does_not_break_the_write():
    def boom(_s):
        raise RuntimeError("ui")
    assert _wr(_transport(JobTab(running_polls=2))[0], progress=boom)["body"] == {"id": "77"}


def test_lost_page_while_polling_is_call_unknown_and_not_resent():
    tab = JobTab(running_polls=10, fail_poll_at=3)
    got = _wr(_transport(tab)[0])
    assert got == {"error": "call_unknown"} and tab.names.count("startCall") == 1


def test_a_few_poll_timeouts_in_a_row_are_tolerated_without_resending():
    tab = JobTab(running_polls=5, timeout_polls={2, 3, 4})
    got = _wr(_transport(tab)[0])
    assert got == {"status": 200, "body": {"id": "77"}} and tab.names.count("startCall") == 1


def test_four_poll_timeouts_in_a_row_end_the_wait_as_unknown():
    tab = JobTab(running_polls=50, timeout_polls={2, 3, 4, 5})
    got = _wr(_transport(tab)[0])
    assert got == {"error": "call_unknown"} and tab.names.count("startCall") == 1


def test_a_success_between_poll_timeouts_resets_the_count():
    tab = JobTab(running_polls=20, timeout_polls={2, 3, 4, 6, 7, 8, 10, 11, 12})
    assert _wr(_transport(tab)[0])["body"] == {"id": "77"}


def test_a_stale_world_or_page_error_ends_the_wait_at_once():
    for exc in (tc.StaleContextError("gone"), tc.PageError("boom"), CdpError("closed")):
        tab = JobTab(running_polls=50)
        orig = tab.evaluate_in

        def ev(ctx, expr, timeout=60.0, _o=orig, _e=exc, _t=tab):
            if ".poll(" in expr and _t.polls >= 1:
                raise _e
            return _o(ctx, expr, timeout=timeout)
        tab.evaluate_in = ev
        assert _wr(_transport(tab)[0]) == {"error": "call_unknown"}
        assert tab.names.count("startCall") == 1


def test_a_refused_start_is_reported_as_not_sent():
    tab = JobTab(start_reply={"error": "write_refused", "why": "file_hash"})
    assert _wr(_transport(tab)[0]) == {"error": "write_refused", "why": "file_hash"}
    assert "poll" not in tab.names


def test_cancel_aborts_the_job_and_returns_unknown():
    tab = JobTab(running_polls=100)
    state = {"n": 0}

    def cancelled():
        state["n"] += 1
        return state["n"] > 3
    got = _wr(_transport(tab)[0], cancelled=cancelled)
    assert got == {"error": "call_unknown", "why": "cancelled"}
    assert tab.names.count("abort") == 1 and tab.names.count("startCall") == 1


def test_writes_without_files_keep_the_short_timeout():
    tab = JobTab()
    t, _slept = _transport(tab)
    t.write(f"/{ACT}/campaigns", _camp())
    assert ("call", 120.0) in tab.timeouts and "startCall" not in tab.names
    slow = JobTab(call_exc=CdpTimeout("slow"))
    t2, _ = _transport(slow)
    assert t2.write(f"/{ACT}/campaigns", _camp(), timeout=1.0) == {"error": "call_unknown"}
    assert ("call", 1.0) in slow.timeouts


def test_the_python_policy_still_runs_before_a_file_job_starts():
    tab = JobTab()
    t, _s = _transport(tab)
    with pytest.raises(wp.WriteRefused):
        t.write(f"/{ACT}/campaigns", _camp(status="ACTIVE"), files={"source": b"x"})
    assert "startCall" not in tab.names


# ───────────────────────── GraphClient / создатель ─────────────────────────

def test_the_client_passes_progress_and_cancel_only_for_files():
    class T:
        def __init__(self):
            self.kw = []

        def write(self, path, params, files=None, filename="", **kw):
            self.kw.append(kw)
            return {"status": 200, "body": {"id": "1"}}

        def state(self):
            return {"has_token": True}
    tr = T()
    c = g.GraphClient(tr, allow_writes=True, min_interval_s=0, sleep=lambda s: None)
    c.post("/p", {}, progress=lambda e: None, cancelled=lambda: False)
    c.post("/p", {}, files={"source": b"x"}, progress=lambda e: None, cancelled=lambda: False)
    assert tr.kw[0] == {} and set(tr.kw[1]) == {"progress", "cancelled"}


def _creator(meta, **kw):
    now = [0.0]
    slept: list[float] = []

    def sleep(s):
        slept.append(s)
        now[0] += s
    client = g.GraphClient(meta, allow_writes=True, sleep=sleep, min_interval_s=0.0, max_calls=200)
    facts = cr.preflight_account(client, ACT)
    creator = cr.CampaignCreator(client, facts, cr.TargetingResolver(client), sleep=sleep,
                                 clock=lambda: now[0], **kw)
    return creator, client, slept


def test_progress_text_is_elapsed_time_throttled_and_in_english():
    texts: list[str] = []
    now = [0.0]
    creator = cr.CampaignCreator(None, None, None, clock=lambda: now[0], live_progress=texts.append)
    show = creator._upload_progress(1, "video", 180 * 1024 * 1024)
    show(80.0)
    now[0] += 0.4
    show(80.4)                                  # слишком рано - не показываем
    now[0] += 1.0
    show(45.0)
    now[0] += 1.0
    show(3725.0)
    assert texts == ["ad 1: uploading video (180 MB), 1m 20s",
                     "ad 1: uploading video (180 MB), 45s",
                     "ad 1: uploading video (180 MB), 1h 02m"]
    assert "%" not in "".join(texts)


class LateMeta(FakeMeta):
    """Видео создано, ответ потерян; список `advideos` покажет его лишь с N-го чтения после записи."""

    def __init__(self, visible_on_read, **kw):
        super().__init__(**kw)
        self.visible_on_read = visible_on_read
        self.reads_after = 0
        self.written = False
        self.progress_seen = None
        self.cancel_seen = None
        self.fail_next["advideos"] = "lost"

    def write(self, path, params, files=None, filename="", **kw):
        if path.endswith("/advideos"):
            self.written = True
            self.progress_seen = kw.get("progress")
            self.cancel_seen = kw.get("cancelled")
            self.on_video_write()
        return super().write(path, params, files=files, filename=filename, **kw)

    def on_video_write(self):
        pass

    def request(self, method, path, params=None):
        if self.written and path.endswith("/advideos"):
            self.reads_after += 1
            if self.reads_after < self.visible_on_read:
                return {"data": []}
        return super().request(method, path, params)


def test_a_late_video_is_found_by_the_repeated_readback(tmp_path):
    meta = LateMeta(visible_on_read=4)
    creator, client, slept = _creator(meta)
    out = creator.run(cr.plan_from_draft(_draft(tmp_path, video=True, ads=1)))
    assert out.error == "", out.error
    assert meta.reads_after == 4 and client.unreconciled is None
    assert slept[:4] == [5.0, 10.0, 20.0, 30.0]              # паузы растут
    assert len(out.ad_ids) == 1
    assert len([p for p, _ in meta.posts if p.endswith("/advideos")]) == 1      # не загружали дважды


def test_the_readback_is_bounded_and_counted_in_the_call_budget(tmp_path):
    meta = LateMeta(visible_on_read=999)
    creator, client, slept = _creator(meta)
    out = creator.run(cr.plan_from_draft(_draft(tmp_path, video=True, ads=1)))
    assert out.stop_kind == "unknown"
    assert meta.reads_after == len(cr.UPLOAD_RECONCILE_FACTORS) == 6
    assert sum(w for w in slept if w >= 5) <= 130            # ≈ 2 минуты, не больше
    assert client.calls >= 6                                  # чтения идут через клиента и считаются


def test_other_writes_keep_one_readback(tmp_path):
    meta = FakeMeta()
    meta.fail_next["adsets"] = "unsent"
    creator, client, slept = _creator(meta)
    out = creator.run(cr.plan_from_draft(_draft(tmp_path, ads=1)))
    assert out.stop_kind == "unknown" and slept.count(5.0) == 1


def test_stop_during_the_upload_reads_back_twice_and_stops(tmp_path):
    meta = LateMeta(visible_on_read=999)
    meta.fail_next["advideos"] = "unsent"
    stop = {"on": False}
    meta.on_video_write = lambda: stop.__setitem__("on", True)    # Stop нажат, пока шла загрузка
    creator, client, slept = _creator(meta, cancelled=lambda: stop["on"])
    out = creator.run(cr.plan_from_draft(_draft(tmp_path, video=True, ads=1)))
    assert out.stop_kind == "cancelled"
    assert meta.reads_after == 2 and slept[:2] == [5.0, 20.0] and 30.0 not in slept
    assert "wait a few minutes before publishing again" in out.error
    assert len([p for p, _ in meta.posts if p.endswith("/advideos")]) == 1


def test_stop_found_video_during_the_stop_readback_is_recorded(tmp_path):
    meta = LateMeta(visible_on_read=2)
    stop = {"on": False}
    meta.on_video_write = lambda: stop.__setitem__("on", True)
    creator, client, _slept = _creator(meta, cancelled=lambda: stop["on"])
    out = creator.run(cr.plan_from_draft(_draft(tmp_path, video=True, ads=1)))
    assert out.stop_kind == "cancelled" and meta.reads_after == 2 and client.unreconciled is None


def test_stop_during_the_readback_stops_at_once_and_keeps_the_marker(tmp_path):
    meta = LateMeta(visible_on_read=999)
    state = {"reconciling": False}
    creator, client, slept = _creator(meta, cancelled=lambda: state["reconciling"] and len(slept) >= 2)
    orig_find_reads = []

    def watch(method, path, params=None, _o=meta.request):
        if meta.written and path.endswith("/advideos"):
            orig_find_reads.append(1)
        return _o(method, path, params)
    meta.request = watch
    state["reconciling"] = True
    out = creator.run(cr.plan_from_draft(_draft(tmp_path, video=True, ads=1)))
    assert out.stop_kind == "cancelled"
    assert len(orig_find_reads) == 1 and client.unreconciled is not None      # после Stop не читаем
    assert "wait a few minutes before publishing again" in out.error


def test_repeated_readbacks_read_fewer_pages_than_the_first_lookup(tmp_path):
    meta = LateMeta(visible_on_read=999)
    creator, client, _slept = _creator(meta)
    pages = []
    orig = client.get_all

    def spy(path, params=None, *, max_pages=5, _o=orig):
        if path.endswith("/advideos"):
            pages.append(max_pages)
        return _o(path, params, max_pages=max_pages)
    client.get_all = spy
    creator.run(cr.plan_from_draft(_draft(tmp_path, video=True, ads=1)))
    # поиск до загрузки - 10 страниц, первое чтение сверки - 10, остальные пять - по 3
    assert pages == [10, 10, 3, 3, 3, 3, 3]


def test_several_videos_run_out_of_budget_during_readback_and_stop_safely(tmp_path):
    meta = LateMeta(visible_on_read=999)
    creator, client, _slept = _creator(meta)
    meta.on_video_write = lambda: setattr(client, "max_calls", client.calls + 3)
    draft = _draft(tmp_path, video=True, ads=2)
    first = draft.ad_sets[0].ads[0]
    clip = tmp_path / "1000_clip.mp4"
    clip.write_bytes(b"creative-0")
    first.creative_type, first.creative_filename, first.creative_path = "Video", clip.name, str(clip)
    out = creator.run(cr.plan_from_draft(draft))
    assert out.stop_kind == "unknown"
    assert "readback failed" in out.error and "reconcile the existing object" in out.error
    assert "publish again" not in out.error
    assert client.unreconciled is not None


def test_the_creator_hands_progress_and_cancel_to_file_writes(tmp_path):
    meta = LateMeta(visible_on_read=1)
    creator, _client, _slept = _creator(meta)
    creator.run(cr.plan_from_draft(_draft(tmp_path, video=True, ads=1)))
    assert callable(meta.progress_seen) and callable(meta.cancel_seen)


def test_publish_api_wires_live_progress_to_the_job_panel():
    src = (ROOT / "src" / "web" / "publish_api.py").read_text(encoding="utf-8")
    assert 'live_progress=lambda text: self.progress(f"{name}: {text}")' in src


# ───────────────────────── помощник страницы (Node) ─────────────────────────

_PRELUDE = r"""
class FakeXhr {
  constructor() {
    // Любой слушатель на `.upload` включает CORS-preflight - его Graph не пропускает с куками.
    const bad = (name) => { FakeXhr.upload_listeners.push(name); };
    this.upload = new Proxy({}, { set(t, k, v) { bad('set:' + String(k)); return true; },
                                  get(t, k) { return k === 'addEventListener' ? () => bad('addEventListener') : undefined; } });
  }
  set withCredentials(v) { this._wc = v; FakeXhr.last_cred = v; }
  open(m, u) { this.method = m; this.url = u; this.responseURL = u; }
  send(body) {
    FakeXhr.sent.push({ url: this.url, keys: [...body.keys()] });
    if (MODE === 'redirect_headers') {
      setTimeout(() => { this.readyState = 2; this.responseURL = 'https://evil.example.com/x';
                         this.onreadystatechange(); }, 5);
      return;
    }
    if (MODE === 'redirect_load') {
      setTimeout(() => { this.status = 200; this.responseText = '{"id":"9"}';
                         this.responseURL = 'https://evil.example.com/x'; this.onload(); }, 5);
      return;
    }
    if (MODE === 'error') { setTimeout(() => this.onerror(), 5); return; }
    if (MODE === 'ok') {
      setTimeout(() => { this.status = 200; this.responseText = '{"id":"9"}'; this.onload(); }, 60);
    }
    // MODE === 'hang': ни ответа, ни ошибки - у помощника нет собственного срока
  }
  abort() { FakeXhr.aborts = (FakeXhr.aborts || 0) + 1; setTimeout(() => this.onabort(), 1); }
}
FakeXhr.sent = [];
FakeXhr.upload_listeners = [];
globalThis.XMLHttpRequest = FakeXhr;
const fakeFetch = async () => { throw new Error('fetch must not be used for file jobs'); };
const loc = { href: 'https://adsmanager.facebook.com/adsmanager/manage/campaigns?act=1',
              hostname: 'adsmanager.facebook.com', pathname: '/adsmanager/manage/campaigns' };
const doc = { readyState: 'complete', querySelector: () => null, scripts: [],
              documentElement: { innerHTML: '' } };
const perf = { getEntriesByType: (t) => t === 'resource' ? [
  { name: 'https://graph.facebook.com/v22.0/act_1/campaigns?access_token=' + TOKENV }] : [] };
const G = {};
new Function('globalThis','location','document','performance','fetch','URL','URLSearchParams', HELPER)(
  G, loc, doc, perf, fakeFetch, URL, URLSearchParams);
const h = G[NAME];
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const hello = Buffer.from('hello');
const sha = require('crypto').createHash('sha256').update(hello).digest('hex');
const spec = (extra) => Object.assign({ method: 'POST', path: '/' + ACT + '/advideos',
  params: { name: 'v', title: 'v' }, files: { source: 'fx' }, filename: 'a.mp4',
  expect: { bytes: 5, sha256: sha } }, extra || {});
"""

_MAIN_OK = r"""
(async () => {
  const out = {};
  h.stage('fx', hello.toString('base64'));
  out.start = h.startCall(spec());
  await sleep(20);
  out.mid = h.poll(out.start.job);
  await sleep(150);
  out.end = h.poll(out.start.job);
  out.no_job = h.poll('j999');
  out.nofile = h.startCall({ method: 'POST', path: '/' + ACT + '/campaigns', files: {},
    params: { name: 'C', objective: 'OUTCOME_LEADS', status: 'PAUSED', special_ad_categories: [] } });
  h.stage('fy', hello.toString('base64'));
  const bad = h.startCall(spec({ files: { source: 'fy' }, expect: { bytes: 5, sha256: 'ff' } }));
  await sleep(30);
  out.badsha = h.poll(bad.job);
  h.stage('fz', hello.toString('base64'));
  const wrong = h.startCall(spec({ path: '/' + ACT + '/adcreatives', files: { source: 'fz' } }));
  await sleep(30);
  out.wrongedge = h.poll(wrong.job);
  out.aborts = FakeXhr.aborts || 0;
  out.upload_listeners = FakeXhr.upload_listeners;
  out.with_credentials = FakeXhr.last_cred;
  out.sent_count = FakeXhr.sent.length;
  out.sent_keys = FakeXhr.sent[0].keys;
  out.sent_has_token_in_url = FakeXhr.sent[0].url.includes('access_token');
  out.token_in_output = JSON.stringify(out).includes(TOKENV);
  process.stdout.write(JSON.stringify(out));
})();
"""

_MAIN_HANG = r"""
(async () => {
  const out = {};
  h.stage('fx', hello.toString('base64'));
  const st = h.startCall(spec());
  await sleep(300);
  out.long_wait = h.poll(st.job).state;            // ответа нет - задание идёт, срока нет
  out.second = h.startCall(spec());                 // одновременно - только одно задание
  out.abort = h.abort(st.job);
  await sleep(40);
  const end = h.poll(st.job);
  out.after_abort = { state: end.state, error: end.result && end.result.error };
  process.stdout.write(JSON.stringify(out));
})();
"""


def _node(mode, main):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    name = "__acl_graph_1899"
    template = re.sub(r"(?<![A-Za-z])ACT(?![A-Za-z])", lambda _m: json.dumps(ACT), _PRELUDE + main)
    script = ("const MODE = " + json.dumps(mode) + ";\n"
              + template.replace("TOKENV", json.dumps(TOKEN)).replace("NAME", json.dumps(name))
              .replace("HELPER", json.dumps(tc.helper_source(name, wp.policy_for_page(ACT)))))
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
        fh.write(script)
        path = fh.name
    try:
        done = subprocess.run([node, path], capture_output=True, text=True, encoding="utf-8", timeout=60)
    finally:
        Path(path).unlink(missing_ok=True)
    assert done.returncode == 0, done.stderr[-1500:]
    return json.loads(done.stdout)


def test_the_page_helper_reports_progress_and_the_result():
    out = _node("ok", _MAIN_OK)
    assert out["start"]["job"].startswith("j")
    assert out["mid"] == {"state": "running"}                  # байтов нет: прогресс - время на стороне Python
    assert out["upload_listeners"] == []                        # ни одного слушателя на xhr.upload (CORS-preflight)
    assert out["with_credentials"] is True                      # куки как прежде
    assert out["end"]["state"] == "done" and out["end"]["result"]["body"] == {"id": "9"}
    assert out["sent_keys"] == ["name", "title", "access_token", "source"]
    assert out["sent_has_token_in_url"] is False and out["token_in_output"] is False


def test_the_page_helper_keeps_the_write_policy_for_jobs():
    out = _node("ok", _MAIN_OK)
    assert out["nofile"] == {"error": "bad_option"}
    assert out["badsha"]["state"] == "error"
    assert out["badsha"]["result"] == {"error": "write_refused", "why": "file_hash"}
    assert out["wrongedge"]["result"]["error"] == "write_refused"
    assert out["sent_count"] == 1                      # ушёл только разрешённый файл
    assert out["no_job"]["state"] == "error"


def test_the_page_helper_has_no_deadline_and_abort_stops_the_upload():
    out = _node("hang", _MAIN_HANG)
    assert out["long_wait"] == "running"
    assert out["second"] == {"error": "bad_option"}
    assert out["abort"] == {"ok": True}
    assert out["after_abort"] == {"state": "error", "error": "aborted"}


def test_the_page_helper_network_error_ends_the_wait():
    out = _node("error", _MAIN_OK)
    assert out["end"]["state"] == "error" and out["end"]["result"]["error"] == "network"


@pytest.mark.parametrize("mode", ["redirect_headers", "redirect_load"])
def test_a_redirected_upload_is_aborted_and_reported_as_unknown(mode):
    out = _node(mode, _MAIN_OK)
    assert out["end"]["state"] == "error" and out["end"]["result"]["error"] == "redirected"
    assert "body" not in out["end"]["result"]
    if mode == "redirect_headers":
        assert out["aborts"] >= 1                          # запрос оборван сразу
    from src.fb_api.graph import _NOT_SENT
    assert "redirected" not in _NOT_SENT                   # неизвестный результат -> сверка, не "не ушло"
