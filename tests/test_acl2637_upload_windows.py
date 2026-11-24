"""ACL-2637 (live run 2026-11-14, G87, 2420 MB): Meta's upload windows, the inline clean tab, closing that tab.

1. Meta decides every chunked-upload window ("We send you new offsets for each chunk"); the first one was 1 MB.
   A small first window no longer stops the upload: a few windows are tried, the rest is projected from the windows
   seen (growth included), and the upload stops only when that projection exceeds the calls planned for the file.
   The planned calls stay a hard cap.
2. The clean upload tab (light document) runs no app: its helper uploads inline on purpose - no worker, no wait,
   no "worker not used" warning, no auto-attach.
3. A dedicated worker cannot be closed with Target.closeTarget; a page tab's close is checked and a failed close
   is said.
Everything here is fake: no browser, no Meta.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_api import cdp  # noqa: E402
from src.fb_api import creator as cr  # noqa: E402
from src.fb_api import light_tab as lt  # noqa: E402
from src.fb_api import upload_server as us  # noqa: E402
from tests.test_acl1891_video_chunks import MB, _big_draft, _run, _sha  # noqa: E402
from tests.test_acl2629_worker_upload import (  # noqa: E402
    FakeCdpTab, ServedMeta, ServingTab, _graph, _node, worker_small)  # noqa: F401 - fixture


# ───────────────────────── the projection ─────────────────────────

def test_steady_windows_are_projected_as_they_are():
    assert cr.project_upload_calls(10 * MB, [MB, MB, MB], 64 * MB) == 10
    assert cr.project_upload_calls(0, [MB], 64 * MB) == 0


def test_growing_windows_are_projected_with_their_growth_up_to_the_cap():
    # 1, 2, 4 MB seen: 8, 16, 32, 64, 64 ... -> 2420 MB in far fewer calls than at 4 MB
    n = cr.project_upload_calls(2420 * MB, [MB, 2 * MB, 4 * MB], 64 * MB)
    assert n == 4 + -(-(2420 - 4 - 8 - 16 - 32) // 64) == 41
    # growth is never assumed above doubling
    assert cr.project_upload_calls(100 * MB, [MB, 10 * MB], 64 * MB) == cr.project_upload_calls(
        100 * MB, [5 * MB, 10 * MB], 64 * MB)


def test_shrinking_windows_are_projected_with_the_last_one():
    assert cr.project_upload_calls(8 * MB, [4 * MB, MB], 64 * MB) == 8


#: ACL-2639: a chunked upload may use the run's remaining budget minus a reserve; a tight run budget makes the
#: planned calls the cap again.
BUDGET_TIGHT = 60


def test_the_plan_reserves_the_window_ramp_on_the_worker_path_only(monkeypatch):
    # ACL-2639: the chunked upload is the primary plan (the file API is off by default) at Meta's real 5 MB windows
    monkeypatch.delenv("ACL_VIDEO_RESUMABLE_UPLOAD", raising=False)
    assert cr.upload_calls(2420 * MB, True) == 484 + cr.CALLS_UPLOAD_WINDOW_RAMP + cr.CALLS_UPLOAD_FIXED
    monkeypatch.setenv("ACL_VIDEO_RESUMABLE_UPLOAD", "1")                          # switched on: the file API is the plan
    assert cr.upload_calls(2420 * MB, True) == cr.resumable_calls()
    assert cr.upload_calls(2420 * MB, True, chunked=True) == 484 + cr.CALLS_UPLOAD_WINDOW_RAMP + cr.CALLS_UPLOAD_FIXED
    big = cr.single_request_max(False) + 1
    assert cr.upload_calls(big, False) == -(-big // cr.VIDEO_CHUNK_STAGED_MAX) + cr.CALLS_UPLOAD_FIXED   # unchanged


# ───────────────────────── the creator on growing / small windows ─────────────────────────

class GrowingMeta(ServedMeta):
    """Meta whose window starts small and doubles on every answer (up to `top`)."""

    def __init__(self, *, first, top, growth=2.0, **kw):
        super().__init__(meta_chunk=first, **kw)
        self.first, self.top, self.growth = first, top, growth
        self.answers = 0

    def _range(self, sess):
        w = int(min(self.top, self.first * (self.growth ** self.answers)))
        self.answers += 1
        got = sess["received"]
        return {"start_offset": str(got), "end_offset": str(min(got + w, sess["size"]))}


def _upload_posts(meta):
    return [p for p, _ in meta.phases]


def test_a_small_first_window_that_grows_uploads_the_whole_file(worker_small, tmp_path):
    d, f = _big_draft(tmp_path, size=12 * MB)
    meta = GrowingMeta(first=64 * 1024, top=MB)
    out, _ = _run(meta, d, max_calls=400)
    assert out.error == ""
    sess = next(iter(meta.sessions.values()))
    assert sess["done"] and sess["sha"].hexdigest() == _sha(f)
    assert meta.served_sizes[:4] == [64 * 1024, 128 * 1024, 256 * 1024, 512 * 1024] and max(meta.served_sizes) == MB
    # within the calls planned for the file (a hard cap): start + transfers + finish
    planned = cr.upload_calls(12 * MB, True)
    assert len(_upload_posts(meta)) <= planned + max(cr.CHUNK_RETRY_SLACK_MIN,
                                                     -(-planned * cr.CHUNK_RETRY_SLACK_FRACTION // 1))
    # telemetry for the next live run: how the windows moved
    assert any("Meta upload window 0.1 MB at 0 MB" in s for s in out.steps)


def test_windows_that_grow_too_slowly_stop_once_the_projection_exceeds_the_cap(worker_small, tmp_path):
    d, f = _big_draft(tmp_path, size=12 * MB)
    meta = GrowingMeta(first=64 * 1024, top=MB, growth=1.05)
    out, _ = _run(meta, d, max_calls=80)
    assert out.stop_kind == "video_error" and "small pieces" in out.error
    assert "ACL_VIDEO_WORKER_UPLOAD" not in out.error
    assert not any(p == "finish" for p, _ in meta.phases)
    assert len(meta.served_sizes) == cr.UPLOAD_WINDOW_PROBE_TRANSFERS
    assert any("upload windows offered by Meta" in s for s in out.steps)


def test_the_planned_calls_are_a_hard_cap_even_while_windows_are_tried(worker_small, tmp_path, monkeypatch):
    # with no room beyond start + one transfer + finish the probe does not get its three tries
    monkeypatch.setattr(cr, "UPLOAD_WINDOW_PROBE_TRANSFERS", 100)
    d, f = _big_draft(tmp_path)
    meta = ServedMeta(meta_chunk=50_000)
    out, _ = _run(meta, d, max_calls=BUDGET_TIGHT)
    assert out.stop_kind == "video_error"
    planned = cr.upload_calls(f.stat().st_size, True, chunked=True)
    allowance = planned + max(cr.CHUNK_RETRY_SLACK_MIN, -(-planned * cr.CHUNK_RETRY_SLACK_FRACTION // 1))
    assert len(_upload_posts(meta)) <= allowance and not any(p == "finish" for p, _ in meta.phases)


# ───────────────────────── the clean tab: inline on purpose ─────────────────────────

def test_the_helper_runs_inline_on_purpose_without_starting_a_worker():
    out = _node({"name": "probe", "inline_only": True})
    assert out["probe"]["ok"] is True and out["probe"]["mode"] == "inline" and out["probe"]["intended"] is True
    assert out["probe"]["worker_why"] == "" and out["log"]["workers"] == 0 and out["graph_calls"] == 0


def test_a_light_tab_probes_inline_only_with_no_auto_attach_and_no_warning():
    tab = ServingTab()
    tab.light = SimpleNamespace(document_intact=lambda **kw: "")
    tab.probe_reply = {"ok": True, "bytes": 4096, "mode": "inline", "intended": True, "worker_why": ""}
    t = _graph(tab)
    ok, why = t.probe_worker()
    assert (ok, why) == (True, "") and t.upload_mode == "inline" and t.upload_mode_note == ""
    assert tab.specs[0].get("inline_only") is True
    assert not [1 for m, _ in tab.cdp_calls if m == "Target.setAutoAttach" and _.get("autoAttach")]


def test_an_ordinary_tab_still_tries_the_worker_first():
    tab = ServingTab()
    _graph(tab).probe_worker()
    assert "inline_only" not in tab.specs[0]


def test_arm_without_workers_turns_no_auto_attach_on():
    tab = FakeCdpTab()
    us.UploadServer(tab).arm(attach_workers=False)
    assert tab.of("Target.setAutoAttach") == []


def test_the_clean_tab_inline_upload_is_a_step_not_a_warning(worker_small, tmp_path):
    class CleanInline(ServedMeta):
        upload_context = "clean_tab"
        upload_mode = "inline"

        def close_upload_context(self):
            return True

    d, f = _big_draft(tmp_path)
    meta = CleanInline(meta_chunk=MB)
    out, _ = _run(meta, d)
    assert out.error == "" and not any("worker" in w.lower() for w in out.warnings)
    assert any("the helper of a separate tab without a service worker" in s for s in out.steps)


# ───────────────────────── closing the clean tab ─────────────────────────

class _BrowserCdp:
    def __init__(self, *, closes=True):
        self.closes = closes
        self.targets = [{"targetId": "T1", "type": "page", "url": "https://adsmanager.facebook.com/__acl_work"},
                        {"targetId": "T0", "type": "page", "url": "https://adsmanager.facebook.com/adsmanager"}]
        self.calls = []

    def call(self, method, params=None, *, session_id=None, timeout=0):
        self.calls.append(method)
        if method == "Target.getTargets":
            return {"targetInfos": list(self.targets)}
        if method == "Target.closeTarget" and self.closes:
            self.targets = [t for t in self.targets if t["targetId"] != (params or {}).get("targetId")]
        return {}

    def close(self):
        pass


def _tab(cdp_conn):
    tab = object.__new__(cdp.PageTab)
    tab.closed = False
    tab.target_id = "T1"
    tab.session_id = "S1"
    tab._cdp = cdp_conn
    tab._cdp_url = ""
    tab.CLOSE_CHECK_S = 0.2
    return tab


def test_a_page_tab_close_is_checked_against_the_browser_list():
    tab = _tab(_BrowserCdp())
    tab.close(verify=True)
    assert tab.close_ok is True


def test_a_page_tab_that_stays_listed_is_reported_not_closed():
    tab = _tab(_BrowserCdp(closes=False))
    tab.close(verify=True)
    assert tab.close_ok is False
    plain = _tab(_BrowserCdp(closes=False))
    plain.close()                                    # an ordinary close is not checked (no wait)
    assert plain.close_ok is None
    assert lt.settle_and_close(_tab(_BrowserCdp(closes=False))) is False


def test_a_clean_tab_that_could_not_be_closed_is_said_to_the_buyer():
    creator = object.__new__(cr.CampaignCreator)
    creator.progress = lambda text: None
    transport = SimpleNamespace(upload_context="clean_tab", close_upload_context=lambda: False)
    creator.client = SimpleNamespace(transport=transport)
    out = cr.Outcome(campaign_name="x")
    creator._close_upload_context(out)
    assert any("may still be open" in w for w in out.warnings)
    assert any("could not be confirmed closed" in s for s in out.steps)
    # nothing opened -> nothing said
    out2 = cr.Outcome(campaign_name="x")
    creator.client = SimpleNamespace(transport=SimpleNamespace(upload_context="", close_upload_context=lambda: True))
    creator._close_upload_context(out2)
    assert out2.warnings == []


def test_the_http_check_accepts_the_last_tab_left_on_about_blank(monkeypatch):
    tab = _tab(_BrowserCdp())
    tab._cdp_url = "127.0.0.1:9222"
    lists = {"only": [{"id": "T1", "type": "page", "url": "about:blank"}],
             "two": [{"id": "T1", "type": "page", "url": "about:blank"}, {"id": "T0", "type": "page", "url": "x"}]}
    which = ["only"]
    monkeypatch.setattr(cdp.PageTab, "_http", staticmethod(lambda host, port, path, **kw: lists[which[0]]))
    assert tab._gone_over_http() is True
    which[0] = "two"
    assert tab._gone_over_http() is False


class RefusingMeta(ServedMeta):
    """Every transfer is refused with the offset Meta wants and a 1 KB window: a window learned on the error path."""

    def write(self, path, params, files=None, filename="", **kw):
        if params.get("upload_phase") == "transfer":
            self.phases.append(("transfer", int(params["start_offset"])))
            return {"status": 400, "body": {"error": {"code": 100, "type": "OAuthException", "message": "(#100) x",
                    "error_data": {"start_offset": params["start_offset"],
                                   "end_offset": str(int(params["start_offset"]) + 1024)}}}}
        return super().write(path, params, files, filename, **kw)


def test_a_window_learned_from_a_refusal_is_checked_too(worker_small, tmp_path, monkeypatch):
    monkeypatch.setattr(cr, "UPLOAD_WINDOW_PROBE_TRANSFERS", 0)      # judge every window at once
    d, f = _big_draft(tmp_path)
    meta = RefusingMeta(meta_chunk=MB)
    out, _ = _run(meta, d, max_calls=400)
    assert out.stop_kind == "video_error" and "small pieces" in out.error
    assert sum(1 for p, _ in meta.phases if p == "transfer") == 1    # stopped on the refusal's 1 KB window
