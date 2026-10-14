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

    def __init__(self, running_polls=3, result=None, fail_poll_at=None, start_reply=None, call_exc=None):
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
            if self.fail_poll_at is not None and self.polls == self.fail_poll_at:
                raise CdpError("target closed")
            if self.polls <= self.running_polls:
                return {"state": "running", "sent": self.polls * 10, "total": 100}
            return {"state": "done", "sent": 100, "total": 100, "result": self.result}
        if ".abort(" in expr:
            return {"ok": True}
        if ".call(" in expr:
            if self.call_exc:
                raise self.call_exc
            return self.result
        raise AssertionError(expr[:60])


def _transport(tab):
    slept: list[float] = []
    t = tc.InPageGraph(tab, allow_writes=True, write_act=ACT, sleep=slept.append)
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


def test_progress_is_forwarded_for_each_poll():
    seen = []
    _wr(_transport(JobTab(running_polls=3))[0], progress=lambda s, tot: seen.append((s, tot)))
    assert seen == [(10, 100), (20, 100), (30, 100)]


def test_a_failing_progress_callback_does_not_break_the_write():
    def boom(_s, _t):
        raise RuntimeError("ui")
    assert _wr(_transport(JobTab(running_polls=2))[0], progress=boom)["body"] == {"id": "77"}


def test_lost_page_while_polling_is_call_unknown_and_not_resent():
    tab = JobTab(running_polls=10, fail_poll_at=3)
    got = _wr(_transport(tab)[0])
    assert got == {"error": "call_unknown"} and tab.names.count("startCall") == 1


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
    c.post("/p", {}, progress=lambda a, b: None, cancelled=lambda: False)
    c.post("/p", {}, files={"source": b"x"}, progress=lambda a, b: None, cancelled=lambda: False)
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


def test_progress_text_is_throttled_and_in_english():
    texts: list[str] = []
    now = [0.0]
    creator = cr.CampaignCreator(None, None, None, clock=lambda: now[0], live_progress=texts.append)
    show = creator._upload_progress(1, "video", 200 * 1024 * 1024)
    total = 205_000_000
    show(total * 45 // 100, total)
    now[0] += 0.4
    show(total * 50 // 100, total)             # слишком рано - не показываем
    now[0] += 1.0
    show(total * 90 // 100, total)
    now[0] += 1.0
    show(total, total)                          # 100% не обещаем до ответа Meta
    assert texts[0].startswith("ad 1: uploading video 45% (")
    assert texts[0].endswith(" of 200 MB)") and len(texts) == 3
    assert "90%" in texts[1] and "99%" in texts[2]


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


def test_stop_during_the_upload_reads_back_once_and_stops(tmp_path):
    meta = LateMeta(visible_on_read=999)
    meta.fail_next["advideos"] = "unsent"
    stop = {"on": False}
    meta.on_video_write = lambda: stop.__setitem__("on", True)    # Stop нажат, пока шла загрузка
    creator, client, slept = _creator(meta, cancelled=lambda: stop["on"])
    out = creator.run(cr.plan_from_draft(_draft(tmp_path, video=True, ads=1)))
    assert out.stop_kind == "cancelled"
    assert meta.reads_after == 1 and 30.0 not in slept
    assert len([p for p, _ in meta.posts if p.endswith("/advideos")]) == 1


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
  constructor() { this.upload = {}; }
  open(m, u) { this.method = m; this.url = u; this.responseURL = u; }
  send(body) {
    FakeXhr.sent.push({ url: this.url, keys: [...body.keys()] });
    if (MODE === 'error') { setTimeout(() => this.onerror(), 5); return; }
    if (MODE === 'ok') {
      setTimeout(() => { this.upload.onprogress({ lengthComputable: true, loaded: 5, total: 10 }); }, 5);
      setTimeout(() => { this.status = 200; this.responseText = '{"id":"9"}'; this.onload(); }, 60);
    }
    // MODE === 'hang': ни ответа, ни ошибки - у помощника нет собственного срока
  }
  abort() { setTimeout(() => this.onabort(), 1); }
}
FakeXhr.sent = [];
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
  for (let i = 0; i < 100 && !(h.poll(out.start.job).sent > 0); i++) await sleep(5);
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
    assert out["mid"]["state"] == "running" and out["mid"]["sent"] == 5 and out["mid"]["total"] == 10
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
