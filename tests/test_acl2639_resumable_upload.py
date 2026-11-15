"""ACL-2639: large ad videos go up with the upload protocol of Ads Manager itself (the "file API") from the profile tab.

The documented `video_ads` protocol is refused for the Ads Manager token ((#10) Permission Denied), so the creator uses
what Ads Manager sends (recorded live 15.11): advideos start (waterfall_id, one whole-file partition) -> the bytes to the
page's resumable service (`rupload-...up`, GET offset / POST segments, session cookies, not Graph) -> advideos transfer by
the file handle -> advideos finish with the ACL tag as the title. A drop is resumed from the service's offset, never from
zero, never as a second video; the chunked path stays only as the fallback.
Everything here is fake: no browser, no Meta.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_api import creator as cr  # noqa: E402
from src.fb_api import write_policy as wp  # noqa: E402
from tests.test_acl1761_api_create import ACT  # noqa: E402
from tests.test_acl1891_video_chunks import MB, _big_draft, _run, _sha  # noqa: E402
from tests.test_acl2629_worker_upload import ServedMeta, ServingTab, _node, worker_small  # noqa: E402,F401

WF_RE = r"[0-9a-f]{32}"
HANDLE = "1:YWJj:application/octet-stream:{vid}:e:1800000000:sig_-1"


@pytest.fixture
def resumable(worker_small, monkeypatch):
    """The worker path with small numbers and the resumable protocol on, 1 MB segments."""
    monkeypatch.delenv("ACL_VIDEO_RESUMABLE_UPLOAD", raising=False)
    monkeypatch.setattr(wp, "RUPLOAD_SEGMENT_MAX", MB)


class ResumableMeta(ServedMeta):
    """Meta with the file-API advideos phases, the resumable service (GET offset / POST segments) and the status read."""

    def __init__(self, *, start="ok", faults=(), keep_partial=True, finish_faults=(), service=True,
                 handle_style="h", transfer="ok", transfer_faults=(), get_faults=(), **kw):
        super().__init__(meta_chunk=MB, **kw)
        self.service = service                          # the page knows the resumable service and its session token
        self.finish_faults = list(finish_faults)        # per finish: "", "lost" (finished, no answer), ...
        self.hide_published = False                     # status shows the video not yet published
        self.start_mode = start
        self.faults = list(faults)       # per service POST: "", "drop" (half kept, no answer), "lost" (all kept, no
        #                                  answer), "nothing" (no byte arrives, no answer), "refused" (HTTP 400),
        #                                  "http500"
        self.keep_partial = keep_partial
        self.handle_style = handle_style                # "h" | "shape" | "none"
        self.transfer_mode = transfer                   # "ok" | "partial" | "refused"
        self.transfer_faults = list(transfer_faults)    # per transfer: "lost" (answer lost)
        self.get_faults = list(get_faults)              # per offset GET: "lost" | "bad"
        self.va: dict[str, dict] = {}
        self.by_wf: dict[str, str] = {}
        self.va_posts: list[tuple[str, dict]] = []
        self.rup: list[tuple[str, str, dict]] = []      # (method, path, headers)

    # -- Graph
    def write(self, path, params, files=None, filename="", **kw):
        if not (path.endswith("/advideos") and "upload_phase" in params
                and any(k in params for k in wp.FILE_API_MARKERS)):
            return super().write(path, params, files, filename, **kw)
        wp.check(ACT, "POST", path, params, files)
        phase = params["upload_phase"]
        self.va_posts.append((phase, dict(params)))
        if phase == "start":
            if self.start_mode == "refused":
                return {"status": 400, "body": {"error": {"code": 100, "type": "OAuthException",
                                                          "message": "(#100) Unsupported post request"}}}
            if self.start_mode == "denied":
                return {"status": 403, "body": {"error": {"code": 10, "type": "OAuthException",
                                                          "message": "(#10) Permission Denied"}}}
            if self.start_mode == "lost":
                return {"error": "network"}
            short = self._new("video", title=None, _polls=0)
            vid = "7700" + short.zfill(6)                       # a Meta-like id (5+ digits)
            obj = self.objects.pop(short)
            obj["id"] = vid
            self.objects[vid] = obj
            sid = str(910000 + len(self.va))
            self.va[vid] = {"sid": sid, "wf": params["waterfall_id"], "size": int(params["file_size"]), "received": 0,
                            "sha": hashlib.sha256(), "done": False, "transferred": False, "title": None}
            self.by_wf[params["waterfall_id"]] = vid
            body = {"id": vid, "video_id": vid, "start_offset": "0", "end_offset": params["file_size"],
                    "upload_session_id": sid}
            if self.start_mode == "no_session":
                body.pop("upload_session_id")
            return {"status": 200, "body": body}
        sess = self.va[params["video_id"]]
        assert params["upload_session_id"] == sess["sid"]
        if phase == "transfer":
            fault = self.transfer_faults.pop(0) if self.transfer_faults else ""
            assert params["fbuploader_video_file_chunk"] == HANDLE.format(vid=params["video_id"])
            assert sess["received"] == sess["size"], "transfer before all bytes arrived"
            if self.transfer_mode == "refused":
                return {"status": 400, "body": {"error": {"code": 100, "message": "(#100) bad handle"}}}
            sess["transferred"] = True
            if fault == "lost":
                return {"error": "network"}
            end = str(sess["size"] - 1) if self.transfer_mode == "partial" else str(sess["size"])
            return {"status": 200, "body": {"start_offset": end, "end_offset": end}}
        assert phase == "finish"
        assert sess["transferred"], "finish before the transfer"
        sess["done"] = True
        sess["title"] = params["title"]
        self.objects[params["video_id"]]["title"] = params["title"]
        fault = self.finish_faults.pop(0) if self.finish_faults else ""
        if fault == "lost":
            return {"error": "network"}
        if fault == "nosuccess":
            return {"status": 200, "body": {"success": False}}
        return {"status": 200, "body": {"success": True}}

    def _video_row(self, obj, params):
        row = super()._video_row(obj, params)
        sess = self.va.get(obj["id"])
        if sess is not None and "status" in row and (not sess["done"] or self.hide_published):
            row["status"]["video_status"] = "uploading"
        return row

    # -- the transport of the resumable service
    def upload_service_ready(self):
        return (True, "") if self.service else (False, "no upload service address or session token on this page")

    def rupload(self, method, path, headers=None, chunk=None, **kw):
        wp.check_rupload(method, path, headers=headers,
                         files={wp.RUPLOAD_BODY_FIELD: chunk} if chunk is not None else None)
        self.rup.append((method, path, dict(headers or {})))
        sess = self.va[self.by_wf[path.split("/")[-1].split("-")[0]]]
        if method == "GET":
            fault = self.get_faults.pop(0) if self.get_faults else ""
            if fault == "lost":
                return {"error": "network"}
            if fault == "bad":
                return {"status": 200, "body": {"offset": "x"}}
            return {"status": 200, "body": {"dc": "fra3", "offset": str(sess["received"])}}
        self.served_sizes.append(len(chunk))
        off = int(headers["offset"])
        if off != sess["received"]:
            return {"status": 400, "body": {"error": "wrong offset"}}
        fault = self.faults.pop(0) if self.faults else ""
        if fault == "nothing":
            return {"error": "network"}
        if fault == "refused":
            return {"status": 400, "body": {}}
        if fault == "http500":
            return {"status": 500, "body": {}}
        n = len(chunk) // 2 if fault == "drop" else len(chunk)
        data = chunk.read(0, n)
        sess["received"] += n
        sess["sha"].update(data)
        if fault in ("drop", "lost"):
            return {"error": "network"}
        if not self.keep_partial and sess["received"] < sess["size"]:
            sess["received"] = 0
        body = {}
        if sess["received"] == sess["size"]:
            h = HANDLE.format(vid=self.by_wf[path.split("/")[-1].split("-")[0]])
            body = {"h": h} if self.handle_style == "h" else (
                {"result": {"file": h}, "ok": True} if self.handle_style == "shape" else {"ok": True})
        return {"status": 200, "body": body}


def _phases(meta):
    return [p for p, _ in meta.va_posts]


def _posts(meta):
    return [(m, p, h) for m, p, h in meta.rup if m == "POST"]


def _offsets(meta):
    return [int(h["offset"]) for _, _, h in _posts(meta)]


def test_the_original_goes_up_in_three_graph_calls_and_meta_gets_the_same_bytes(resumable, tmp_path):
    d, f = _big_draft(tmp_path)                                   # 3.5 MB, 1 MB segments here (512 MB live)
    meta = ResumableMeta()
    out, client = _run(meta, d)
    assert out.error == "", out.error
    (vid, sess), = meta.va.items()
    assert sess["done"] and sess["received"] == f.stat().st_size and sess["sha"].hexdigest() == _sha(f)
    size = f.stat().st_size
    assert _phases(meta) == ["start", "transfer", "finish"]            # constant, however many segments
    start, transfer, finish = (p for _, p in meta.va_posts)
    assert start == {"upload_phase": "start", "file_size": str(size), "file_extension": "mp4",
                     "waterfall_id": sess["wf"], "supports_chunking": "true", "supports_file_api": "true",
                     "partition_start_offset": "0", "partition_end_offset": str(size)}
    assert transfer == {"upload_phase": "transfer", "upload_session_id": sess["sid"], "video_id": vid,
                        "start_offset": "0", "end_offset": str(size), "partition_start_offset": "0",
                        "partition_end_offset": str(size), "fbuploader_video_file_chunk": HANDLE.format(vid=vid)}
    assert finish == {"upload_phase": "finish", "upload_session_id": sess["sid"], "video_id": vid,
                      "title": meta.objects[vid]["title"]}
    assert meta.objects[vid]["title"].startswith("acl-")                  # the tag: found by a later search
    assert not any(k.startswith("composer") for k in start)
    # the service: one partition path, the bytes in segments with the offset the service holds
    assert {p for _, p, _ in meta.rup} == {f"/fb_video/{sess['wf']}-0-{size}"}
    assert _offsets(meta) == [0, MB, 2 * MB, 3 * MB] and meta.served_sizes == [MB, MB, MB, 500_000]
    assert all(h == {"offset": str(o), "X-Entity-Length": str(size), "X-Entity-Type": "application/octet-stream"}
               for (_, _, h), o in zip(_posts(meta), _offsets(meta)))
    assert not [1 for p, _ in meta.phases if p in ("start", "transfer", "finish")]     # no chunked advideos session
    assert not any("chunks instead" in w for w in out.warnings)
    assert any("resumable upload finished" in s for s in out.steps)
    assert not [s for s in out.steps if "rupload" in s.lower()]


def test_a_dropped_post_resumes_from_the_offset_the_service_reports(resumable, tmp_path):
    d, f = _big_draft(tmp_path)
    meta = ResumableMeta(faults=["", "drop"])                     # the 2nd POST breaks after half a segment
    out, _ = _run(meta, d)
    assert out.error == "", out.error
    sess = next(iter(meta.va.values()))
    assert sess["done"] and sess["sha"].hexdigest() == _sha(f)    # every byte once, in order
    assert _offsets(meta)[:3] == [0, MB, MB + MB // 2]            # resumed at the service's offset, not from zero
    assert _phases(meta).count("start") == 1 and len(meta.va) == 1  # never a second video
    assert _phases(meta) == ["start", "transfer", "finish"]
    assert any("resuming the upload" in s for s in out.steps)
    assert [m for m, _, _ in meta.rup].count("GET") == 1


def test_a_lost_answer_is_settled_by_the_offset_read_without_resending(resumable, tmp_path):
    d, f = _big_draft(tmp_path)
    meta = ResumableMeta(faults=["lost"])
    out, _ = _run(meta, d)
    assert out.error == "", out.error
    assert _offsets(meta) == [0, MB, 2 * MB, 3 * MB]             # the first segment was not sent twice
    assert next(iter(meta.va.values()))["sha"].hexdigest() == _sha(f)


@pytest.mark.parametrize("fault", ["refused", "http500"])
def test_a_4xx_or_5xx_is_a_refusal_not_progress(resumable, tmp_path, fault):
    d, f = _big_draft(tmp_path)
    meta = ResumableMeta(faults=["", fault])
    out, _ = _run(meta, d)
    assert out.error == "", out.error
    sess = next(iter(meta.va.values()))
    assert sess["done"] and sess["sha"].hexdigest() == _sha(f)
    assert _offsets(meta)[:3] == [0, MB, MB]                      # the refused segment is sent again
    assert any("the upload service refused (HTTP" in s for s in out.steps)


def test_resumes_are_bounded(resumable, tmp_path):
    d, f = _big_draft(tmp_path)
    meta = ResumableMeta(faults=["", "drop"] + ["refused"] * 10)
    out, _ = _run(meta, d, max_calls=400)
    assert out.stop_kind == "video_error" and "resumes" in out.error
    assert "finish" not in _phases(meta) and "transfer" not in _phases(meta)
    assert len(_posts(meta)) == 2 + cr.RESUMABLE_MAX_RESUMES


def test_a_refused_start_falls_back_to_chunks_with_the_exact_reason(resumable, tmp_path):
    d, f = _big_draft(tmp_path)
    meta = ResumableMeta(start="denied")
    out, _ = _run(meta, d)
    assert out.error == "", out.error
    warn = [w for w in out.warnings if "resumable upload was not used" in w]
    assert len(warn) == 1 and "Permission Denied" in warn[0]
    assert meta.rup == []
    assert [p for p, _ in meta.phases if p == "finish"] == ["finish"]          # the chunked session finished
    assert next(iter(meta.sessions.values()))["sha"].hexdigest() == _sha(f)


def test_a_lost_start_answer_falls_back_to_chunks(resumable, tmp_path):
    d, f = _big_draft(tmp_path)
    meta = ResumableMeta(start="lost")
    out, _ = _run(meta, d)
    assert out.error == "", out.error
    assert any("the start answer was lost" in w for w in out.warnings) and meta.rup == []


def test_a_start_without_a_session_is_not_used(resumable, tmp_path):
    d, f = _big_draft(tmp_path)
    meta = ResumableMeta(start="no_session")
    out, _ = _run(meta, d)
    assert out.error == "" and meta.rup == []
    assert any("no usable video id and upload session" in w for w in out.warnings)


def test_a_page_without_the_upload_service_falls_back_before_any_start(resumable, tmp_path):
    d, f = _big_draft(tmp_path)
    meta = ResumableMeta(service=False)
    out, _ = _run(meta, d)
    assert out.error == "", out.error
    assert meta.va_posts == [] and meta.rup == []                  # not even a start was sent
    assert any("upload service address not available on this page" in w for w in out.warnings)
    assert next(iter(meta.sessions.values()))["done"]


def test_an_address_that_takes_no_byte_falls_back_without_finishing(resumable, tmp_path):
    d, f = _big_draft(tmp_path)
    meta = ResumableMeta(faults=["nothing", "nothing"])            # e.g. CORS / proxy / session refused
    out, _ = _run(meta, d)
    assert out.error == "", out.error
    assert "transfer" not in _phases(meta) and "finish" not in _phases(meta)     # the session is never used
    assert any("no byte reached Meta" in w for w in out.warnings)
    assert next(iter(meta.sessions.values()))["done"]


def test_the_calls_of_a_failed_attempt_are_added_to_the_run_budget(resumable, tmp_path):
    d, f = _big_draft(tmp_path)
    meta = ResumableMeta(faults=["nothing", "nothing"])
    out, client = _run(meta, d, max_calls=200)
    assert _phases(meta) == ["start"]
    assert client.max_calls == 200 + 1                                        # only the start was a Graph call


def test_an_unreadable_offset_before_any_byte_falls_back(resumable, tmp_path):
    d, f = _big_draft(tmp_path)
    meta = ResumableMeta(faults=["nothing"], get_faults=["bad"])
    out, _ = _run(meta, d)
    assert out.error == "" and "transfer" not in _phases(meta)
    assert any("did not tell its offset" in w for w in out.warnings)


def test_meta_keeping_none_of_the_segments_stops_instead_of_restarting(resumable, tmp_path):
    d, f = _big_draft(tmp_path)
    meta = ResumableMeta(keep_partial=False)
    out, _ = _run(meta, d, max_calls=400)
    # the service lost what it had confirmed: stop, never start again from zero
    assert out.stop_kind == "video_error" and "fewer bytes than it had confirmed" in out.error
    assert "finish" not in _phases(meta) and _phases(meta).count("start") == 1


# ───────────────────────── the file handle ─────────────────────────

@pytest.mark.parametrize("style", ["h", "shape"])
def test_the_handle_is_taken_from_h_or_from_any_value_of_the_handle_shape(resumable, tmp_path, style):
    d, f = _big_draft(tmp_path)
    meta = ResumableMeta(handle_style=style)
    out, _ = _run(meta, d)
    assert out.error == "", out.error
    assert _phases(meta) == ["start", "transfer", "finish"]


def test_an_answer_without_a_handle_is_refused_before_the_finish(resumable, tmp_path):
    d, f = _big_draft(tmp_path)
    meta = ResumableMeta(handle_style="none")
    out, _ = _run(meta, d)
    assert out.error == "", out.error                                          # the chunked path took over
    assert "transfer" not in _phases(meta) and "finish" not in _phases(meta)
    assert any("took the bytes but gave no file handle" in w for w in out.warnings)
    assert [m for m, _, _ in meta.rup].count("GET") == 1                       # one offset read asked
    assert next(iter(meta.sessions.values()))["done"]


def test_the_handle_parser_is_strict():
    ok = "1:YWJj:application/octet-stream:123:e:1800000000:sig_-1"
    assert wp.file_handle({"h": ok}) == ok
    assert wp.file_handle({"x": {"y": ok}}) == ok and wp.file_handle({"a": 1, "b": ok}) == ok
    for body in ({}, {"h": ""}, {"h": 5}, {"h": "not a handle"}, {"h": ok + " x"}, {"h": "1:a:b:c:d:e:f"},
                 {"h": "x" * 2000}, {"x": {"y": {"z": ok}}}, None, [], "1:abc"):
        assert wp.file_handle(body) == ""


# ───────────────────────── the transfer by handle ─────────────────────────

def test_a_transfer_that_does_not_take_all_the_bytes_is_refused_before_the_finish(resumable, tmp_path):
    d, f = _big_draft(tmp_path)
    meta = ResumableMeta(transfer="partial")
    out, _ = _run(meta, d)
    assert out.error == "", out.error
    assert "finish" not in _phases(meta) and any("did not take all the bytes" in w for w in out.warnings)


def test_a_refused_transfer_falls_back_without_a_finish(resumable, tmp_path):
    d, f = _big_draft(tmp_path)
    meta = ResumableMeta(transfer="refused")
    out, _ = _run(meta, d)
    assert out.error == "" and "finish" not in _phases(meta)
    assert any("Meta refused the transfer" in w for w in out.warnings)


def test_a_lost_transfer_answer_is_asked_once_more(resumable, tmp_path):
    d, f = _big_draft(tmp_path)
    meta = ResumableMeta(transfer_faults=["lost"])
    out, _ = _run(meta, d)
    assert out.error == "", out.error
    assert _phases(meta) == ["start", "transfer", "transfer", "finish"]
    assert any("the answer to the transfer was lost" in s for s in out.steps)


def test_a_lost_finish_answer_is_sent_once_more(resumable, tmp_path):
    d, f = _big_draft(tmp_path)
    meta = ResumableMeta(finish_faults=["lost"])
    out, _ = _run(meta, d)
    assert out.error == "", out.error
    assert _phases(meta).count("finish") == 2
    assert any("sending the finish once more" in s for s in out.steps)


def test_a_finish_without_success_stops(resumable, tmp_path):
    d, f = _big_draft(tmp_path)
    meta = ResumableMeta(finish_faults=["nosuccess"])
    out, _ = _run(meta, d)
    assert out.stop_kind == "video_error" and "did not finish" in out.error


# ───────────────────────── budget ─────────────────────────

def test_the_graph_calls_are_constant_and_the_service_is_not_counted(resumable, tmp_path):
    d, f = _big_draft(tmp_path)
    meta = ResumableMeta()
    out, client = _run(meta, d)
    assert out.error == ""
    assert len(meta.va_posts) == 3 and len(_posts(meta)) == 4                    # 3 Graph writes, 4 segments
    assert cr.resumable_calls(f.stat().st_size, MB) == cr.resumable_calls(2420 * MB, 512 * MB) == 4
    assert cr.resumable_calls(2420 * MB) < cr.upload_calls(2420 * MB, True)      # inside the run budget


def test_the_hard_cap_stops_before_the_transfer(resumable, tmp_path, monkeypatch):
    d, f = _big_draft(tmp_path)
    monkeypatch.setattr(cr, "upload_calls", lambda size, worker=None: 1)
    monkeypatch.setattr(cr, "resumable_calls", lambda size, segment=None: 2)
    monkeypatch.setattr(cr, "CHUNK_RETRY_SLACK_MIN", 0)
    monkeypatch.setattr(cr, "CHUNK_RETRY_SLACK_FRACTION", 0)
    meta = ResumableMeta()
    out, _ = _run(meta, d, max_calls=400)
    assert out.stop_kind == "video_error" and "more Meta calls than planned" in out.error
    assert _phases(meta) == ["start"]


def test_the_switch_turns_the_resumable_path_off(resumable, tmp_path, monkeypatch):
    monkeypatch.setenv("ACL_VIDEO_RESUMABLE_UPLOAD", "0")
    d, f = _big_draft(tmp_path)
    meta = ResumableMeta()
    out, _ = _run(meta, d)
    assert out.error == "" and meta.va_posts == [] and meta.rup == []


def test_one_file_in_several_ads_goes_up_once(resumable, tmp_path):
    from dataclasses import replace
    d, f = _big_draft(tmp_path)
    ads = d.ad_sets[0].ads
    ads.append(replace(ads[0], creative_id="1001", copy_name="Text1", ad_title="Head 1", ad_body="Body 1"))
    meta = ResumableMeta()
    out, _ = _run(meta, d)
    assert out.error == "", out.error
    assert _phases(meta).count("start") == 1 and len(meta.va) == 1
    assert any("video already in the ad account" in s for s in out.steps)


def test_publish_again_reuses_the_titled_video(resumable, tmp_path):
    d, f = _big_draft(tmp_path)
    meta = ResumableMeta()
    first, _ = _run(meta, d)
    assert first.error == ""
    again, _ = _run(meta, d)
    assert again.error == "" and _phases(meta).count("start") == 1


# ───────────────────────── journal: a stopped upload is continued ─────────────────────────

def _journal(tmp_path):
    from src.fb_api import operation_journal as oj
    now = [1_000_000.0]
    return oj, now, oj.OperationJournal(tmp_path / "ops", clock=lambda: now[0])


def _stages(tmp_path):
    return [st for p in (tmp_path / "ops").rglob("*.json")
            for st in (json.loads(p.read_text(encoding="utf-8")).get("stages") or {}).values()]


def _stopped_after_start(tmp_path):
    from tests.test_acl1856_operation_journal import _go
    oj, now, journal = _journal(tmp_path)
    d, f = _big_draft(tmp_path)
    meta = ResumableMeta(faults=["", "drop"] + ["refused"] * 10)       # stops with 1.5 MB at the service
    first, _ = _go(meta, d, journal, request_key="r1#t")
    assert first.stop_kind == "video_error" and "finish" not in _phases(meta)
    (old, _sess), = meta.va.items()
    meta.faults = []
    now[0] += oj.BUSY_S + 60                                                  # "Publish again" a few minutes later
    return _go, oj, journal, d, f, meta, old


def test_a_run_stopped_after_the_start_is_continued_not_repeated(resumable, tmp_path):
    _go, oj, journal, d, f, meta, old = _stopped_after_start(tmp_path)
    sess = meta.va[old]
    held = sess["received"]
    assert 0 < held < f.stat().st_size
    st, = [s for s in _stages(tmp_path) if s.get("pending_id") == old]
    assert set(st["pending"]) >= {"upload_session_id", "waterfall_id", "size", "md5"}
    again, _ = _go(meta, d, journal, request_key="r1#t")
    assert again.error == "", again.error
    assert _phases(meta).count("start") == 1 and len(meta.va) == 1           # the same video, not a second one
    assert [m for m, _, _ in meta.rup][-5:][0] == "GET"                       # asked the service where it stands
    assert _offsets(meta)[-3:][0] == held                                     # continued at the service's offset
    assert sess["done"] and sess["sha"].hexdigest() == _sha(f)
    assert any("continuing the upload of an earlier run" in s for s in again.steps)


def test_a_pending_upload_without_its_waterfall_is_not_continued(resumable, tmp_path):
    _go, oj, journal, d, f, meta, old = _stopped_after_start(tmp_path)
    for p in (tmp_path / "ops").rglob("*.json"):
        data = json.loads(p.read_text(encoding="utf-8"))
        for st in (data.get("stages") or {}).values():
            if st.get("pending_id"):
                st["pending"].pop("waterfall_id", None)
        p.write_text(json.dumps(data), encoding="utf-8")
    again, _ = _go(meta, d, journal, request_key="r1#t")
    assert again.error == "", again.error
    assert _phases(meta).count("start") == 2                                  # forgotten: a new upload


def test_a_video_finished_by_an_earlier_run_is_adopted_without_new_writes(resumable, tmp_path):
    from tests.test_acl1856_operation_journal import _go
    oj, now, journal = _journal(tmp_path)
    d, f = _big_draft(tmp_path)
    meta = ResumableMeta(finish_faults=["lost", "lost"])
    meta.hide_published = True                                              # run 1: finish result stays unknown
    first, _ = _go(meta, d, journal, request_key="r1#t")
    assert first.stop_kind == "unknown"
    meta.hide_published = False
    before = list(meta.va_posts), len(meta.rup)
    now[0] += oj.BUSY_S + 60
    again, _ = _go(meta, d, journal, request_key="r1#t")
    assert again.error == "", again.error
    assert _phases(meta).count("start") == 1 and (list(meta.va_posts), len(meta.rup)) == before
    # the finish carried the title, so the search by the tag already finds it (the journal route is the second line)
    assert any("video already in the ad account" in s or "uploaded by an earlier run is used" in s for s in again.steps)


def test_a_pending_video_that_is_gone_starts_a_new_upload(resumable, tmp_path):
    _go, oj, journal, d, f, meta, old = _stopped_after_start(tmp_path)
    del meta.objects[old]                                                    # deleted at Meta meanwhile
    again, _ = _go(meta, d, journal, request_key="r1#t")
    assert again.error == "", again.error
    assert _phases(meta).count("start") == 2
    assert any("is not available" in s for s in again.steps)


def test_a_pending_upload_the_service_no_longer_holds_starts_a_new_one(resumable, tmp_path):
    _go, oj, journal, d, f, meta, old = _stopped_after_start(tmp_path)
    meta.get_faults = ["lost"]
    again, _ = _go(meta, d, journal, request_key="r1#t")
    assert again.error == "", again.error
    assert _phases(meta).count("start") == 2
    assert any("cannot be continued; starting a new one" in s for s in again.steps)


def test_a_transient_status_error_keeps_the_pending_upload_and_stops(resumable, tmp_path):
    _go, oj, journal, d, f, meta, old = _stopped_after_start(tmp_path)
    real = meta._get
    meta._get = lambda path, params: ({"error": {"code": 1, "message": "An unknown error occurred"}}
                                      if path == f"/{old}" else real(path, params))
    again, _ = _go(meta, d, journal, request_key="r1#t")
    assert again.error and _phases(meta).count("start") == 1                 # no second video
    assert any(st.get("pending_id") == old for st in _stages(tmp_path))       # kept for the next run


def test_a_status_error_100_33_starts_fresh(resumable, tmp_path):
    _go, oj, journal, d, f, meta, old = _stopped_after_start(tmp_path)
    real = meta._get
    meta._get = lambda path, params: ({"error": {"code": 100, "error_subcode": 33, "message": "Unsupported get request."}}
                                      if path == f"/{old}" else real(path, params))
    again, _ = _go(meta, d, journal, request_key="r1#t")
    assert again.error == "", again.error
    assert _phases(meta).count("start") == 2


def test_a_fallback_forgets_the_pending_upload(resumable, tmp_path):
    from tests.test_acl1856_operation_journal import _go
    oj, now, journal = _journal(tmp_path)
    d, f = _big_draft(tmp_path)
    meta = ResumableMeta(faults=["nothing", "nothing"])
    out, _ = _go(meta, d, journal, request_key="r1#t")
    assert out.error == "", out.error
    stages = _stages(tmp_path)
    assert stages and not any(st.get("pending_id") for st in stages)


def test_release_optional_stop_lifts_only_not_sent():
    from src.fb_api import graph as g
    c = g.GraphClient(object(), allow_writes=True, sleep=lambda s: None)
    for exc in (g.AuthRequired("a"), g.Checkpoint("c"), g.RateLimited("r"), g.PolicyBlocked("p"),
                g.LoggedOut("l"), g.CallBudget("b"), g.PageFailed("the page refused the write (fake)")):
        c.stopped = exc
        assert c.release_optional_stop(exc) is False and c.stopped is exc
    other = g.NotSent("older")
    c.stopped = other
    assert c.release_optional_stop(g.NotSent("newer")) is False and c.stopped is other   # never an older stop
    assert c.optional_write_ok() is False
    assert c.release_optional_stop(other) is True and c.stopped is None
    assert c.optional_write_ok() is True


@pytest.mark.parametrize("code,sub,msg,gone", [
    (100, 33, "Unsupported get request.", True),
    (100, None, "Object with ID '7700000001' does not exist", True),
    (100, None, "(#100) Tried accessing nonexisting field (status) on node type (AdVideo)", False),  # live object
    (None, None, "Object does not exist", False),                                  # not a Graph 100: stays open
    (1, None, "An unknown error occurred", False),
    (100, None, "(#100) Missing permission", False),
])
def test_only_a_missing_object_counts_as_gone(code, sub, msg, gone):
    from src.fb_api.graph import GraphError
    assert cr._object_gone(GraphError(msg, code=code, subcode=sub)) is gone


# ───────────────────────── policy (Python) ─────────────────────────

ADV = f"/{ACT}/advideos"
WF = "a" * 32
TITLE = "acl-tag1-0123456789ab"
H = HANDLE.format(vid="1234567")
SZ = str(4 * MB)


def _start(**kw):
    p = {"upload_phase": "start", "file_size": SZ, "file_extension": "mp4", "waterfall_id": WF,
         "supports_chunking": "true", "supports_file_api": "true", "partition_start_offset": "0",
         "partition_end_offset": SZ}
    p.update(kw)
    return p


def _transfer(**kw):
    p = {"upload_phase": "transfer", "upload_session_id": "900001", "video_id": "1234567", "start_offset": "0",
         "end_offset": SZ, "partition_start_offset": "0", "partition_end_offset": SZ, "fbuploader_video_file_chunk": H}
    p.update(kw)
    return p


def _finish(**kw):
    p = {"upload_phase": "finish", "upload_session_id": "900001", "video_id": "1234567", "title": TITLE}
    p.update(kw)
    return p


def test_policy_allows_exactly_the_three_file_api_phases():
    for p in (_start(), _transfer(), _finish()):
        assert wp.check(ACT, "POST", ADV, p) == "advideos"


def _without(p, key):
    q = dict(p)
    del q[key]
    return q


@pytest.mark.parametrize("params", [
    _start(composer_session_id="x"),                                      # a composer field
    _start(source="ads_interfaces"),                                      # not sent: minimal
    _without(_start(), "supports_file_api"),                              # (then it is a chunked start with extra keys)
    _start(waterfall_id="A" * 32), _start(waterfall_id="a" * 31),
    _start(file_extension="exe"), _start(supports_chunking="false"), _start(supports_file_api="1"),
    _start(partition_start_offset="5"), _start(partition_end_offset="5"), _start(file_size="0", partition_end_offset="0"),
    _start(file_size=str(5 * 1024 ** 3), partition_end_offset=str(5 * 1024 ** 3)),
    _start(file_size=4 * MB),                                             # a number, not text
    _transfer(fbuploader_video_file_chunk="not a handle"), _transfer(fbuploader_video_file_chunk=""),
    _transfer(start_offset="1"), _transfer(partition_start_offset="1"), _transfer(partition_end_offset="7"),
    _transfer(end_offset="0", partition_end_offset="0"), _transfer(video_id="../me"), _transfer(upload_session_id="x"),
    _without(_transfer(), "video_id"), _transfer(waterfall_id=WF), _transfer(offset="0"),
    _finish(title="Campaign"), _finish(name=TITLE), _without(_finish(), "title"),
    _finish(video_id="abc"),
])
def test_policy_refuses_everything_else_in_the_file_api(params):
    with pytest.raises(wp.WriteRefused):
        wp.check(ACT, "POST", ADV, params)


def test_a_file_api_phase_takes_no_file_and_the_old_edge_and_naming_are_gone():
    for p in (_start(), _transfer(), _finish()):
        with pytest.raises(wp.WriteRefused):
            wp.check(ACT, "POST", ADV, p, {"video_file_chunk": b"x"})
    with pytest.raises(wp.WriteRefused):
        wp.check(ACT, "POST", f"/{ACT}/video_ads", {"upload_phase": "start"})        # the documented edge is gone
    with pytest.raises(wp.WriteRefused):
        wp.check(ACT, "POST", "/7770001", {"name": TITLE, "title": TITLE})           # no object write of a creation
    assert "video_ads" not in wp.EDGE_KEYS and not hasattr(wp, "VIDEO_ADS_KEYS")


def test_the_chunked_phases_still_work_next_to_it():
    assert wp.check(ACT, "POST", ADV, {"upload_phase": "start", "file_size": "5", "waterfall_id": WF}) == "advideos"
    assert wp.check(ACT, "POST", ADV, {"upload_phase": "finish", "upload_session_id": "900001", "title": TITLE}) == "advideos"


# ───────────────────────── policy: the resumable service ─────────────────────────

def _chunk(off=0, n=MB):
    class Src:
        def read_range(self, o, k):
            return b"x" * k
    return wp.ServedChunk(Src(), off, n)


def _hdr(off=0, total=4 * MB, **kw):
    h = {"offset": str(off), "X-Entity-Length": str(total), "X-Entity-Type": "application/octet-stream"}
    h.update(kw)
    return h


PATH = f"/fb_video/{WF}-0-{4 * MB}"
BODY = {wp.RUPLOAD_BODY_FIELD: None}


def test_the_service_accepts_exactly_a_get_and_a_post_of_its_own_path():
    wp.check_rupload("GET", PATH)
    wp.check_rupload("GET", PATH, query={"fb_dtsg_ag": "x", "__a": "1"})
    wp.check_rupload("POST", PATH, headers=_hdr(), files={wp.RUPLOAD_BODY_FIELD: _chunk()})
    wp.check_rupload("POST", PATH, headers=_hdr(MB), files={wp.RUPLOAD_BODY_FIELD: _chunk(MB)})


@pytest.mark.parametrize("kw", [
    dict(method="DELETE"), dict(method="PUT"), dict(method="get"),
    dict(path="/fb_video/xyz-0-5"), dict(path=f"/fb_video/{WF}-1-{4 * MB}"), dict(path=f"/other/{WF}-0-{4 * MB}"),
    dict(path=f"/fb_video/{WF}-0-{4 * MB}/x"), dict(path=f"/fb_video/{WF.upper()}-0-5"),
    dict(query={"access_token": "x"}), dict(query={"fb_dtsg_ag": "x", "extra": "1"}),
    dict(headers={"offset": "0"}),                                                  # a GET takes no headers
    dict(files={wp.RUPLOAD_BODY_FIELD: _chunk()}),                                  # nor a body
])
def test_the_service_refuses_everything_else_on_a_get(kw):
    args = dict(method="GET", path=PATH)
    args.update(kw)
    with pytest.raises(wp.WriteRefused):
        wp.check_rupload(args.pop("method"), args.pop("path"), **args)


@pytest.mark.parametrize("headers,files", [
    (_hdr(Authorization="OAuth x"), "chunk"),                                      # an extra header
    (_without(_hdr(), "X-Entity-Type"), "chunk"),                                  # a missing one
    (_hdr(**{"X-Entity-Type": "text/plain"}), "chunk"),
    (_hdr(**{"X-Entity-Length": "5"}), "chunk"),                                   # not the size in the path
    (_hdr(off=4 * MB), "chunk"),                                                   # offset at the end
    (_hdr(off=1), "chunk"),                                                        # body not at offset
    (_hdr(), "bytes"),                                                             # never staged bytes
    (_hdr(), "big"),                                                               # over the segment cap
    (_hdr(), "other"),                                                             # another field
    (_hdr(), None),                                                                # no body
])
def test_the_service_post_refuses_everything_else(headers, files):
    f = {"chunk": {wp.RUPLOAD_BODY_FIELD: _chunk()}, "bytes": {wp.RUPLOAD_BODY_FIELD: b"x" * 10},
         "big": {wp.RUPLOAD_BODY_FIELD: _chunk(0, wp.RUPLOAD_SEGMENT_MAX + 1)}, "other": {"video_file_chunk": _chunk()},
         None: None}[files]
    with pytest.raises(wp.WriteRefused):
        wp.check_rupload("POST", PATH, headers=headers, files=f)


def test_the_service_address_is_the_pages_own_and_strictly_validated():
    for name in ("rupload-fra3-1.up", "rupload-ber1-1.up", "rupload-a.up"):
        assert wp.rupload_service_ok(name, "facebook.com")
    for name, dom in (("rupload-fra3-1.up", "facebook.com.evil.example"), ("rupload-fra3-1.up", "evil.com"),
                      ("rupload-fra3-1.up", "Facebook.com"), ("evil.example", "facebook.com"), ("rupload.up", "facebook.com"),
                      ("rupload-fra3-1.up.evil", "facebook.com"), ("rupload-FRA.up", "facebook.com"),
                      ("rupload-" + "a" * 31 + ".up", "facebook.com"), ("rupload-a/b.up", "facebook.com"),
                      ("rupload-fra3-1.up:8080", "facebook.com"), (None, "facebook.com"), ("rupload-a.up", None)):
        assert not wp.rupload_service_ok(name, dom), (name, dom)
    tok = "D" * 62
    assert wp.rupload_url("rupload-fra3-1.up", "facebook.com", PATH, tok) == \
        f"https://rupload-fra3-1.up.facebook.com{PATH}?fb_dtsg_ag={tok}&__a=1"
    with pytest.raises(ValueError):
        wp.rupload_url("rupload-fra3-1.up", "evil.com", PATH, tok)


def test_the_page_policy_carries_the_new_rules_and_no_old_ones():
    pol = wp.policy_for_page(ACT)
    assert "video_ads" not in pol and "video_ads" not in pol["edges"]
    r = pol["rupload"]
    assert r["domain"] == "facebook.com" and r["methods"] == ["GET", "POST"] and r["query_keys"] == ["fb_dtsg_ag", "__a"]
    assert r["headers"] == ["offset", "X-Entity-Length", "X-Entity-Type"] and r["segment_max"] == wp.RUPLOAD_SEGMENT_MAX
    assert set(pol["file_api"]["keys"]) == {"start", "transfer", "finish"}


# ───────────────────────── InPageGraph: the service calls ─────────────────────────

class ServiceTab(ServingTab):
    def __init__(self, ready=True, **kw):
        super().__init__(**kw)
        self.ready = ready

    def evaluate_in(self, ctx, expr, timeout=60.0):
        if ".uploadService()" in expr:
            self.exprs.append(expr)
            return {"ok": self.ready}
        return super().evaluate_in(ctx, expr, timeout)


def _service_graph(tab):
    from tests.test_acl2629_worker_upload import _graph
    return _graph(tab)


def test_the_transport_asks_the_page_whether_it_knows_the_service():
    assert _service_graph(ServiceTab(ready=True)).upload_service_ready() == (True, "")
    ok, why = _service_graph(ServiceTab(ready=False)).upload_service_ready()
    assert ok is False and "no upload service address" in why


def test_a_service_get_is_one_helper_call_with_no_address_or_token():
    tab = ServiceTab(call_reply={"status": 200, "body": {"offset": "0"}})
    got = _service_graph(tab).rupload("GET", PATH)
    assert got == {"status": 200, "body": {"offset": "0"}}
    call, = [e for e in tab.exprs if e.startswith("globalThis[") and ".call(" in e]
    spec = json.loads(call.split(".call(", 1)[1].rsplit(")", 1)[0])
    assert spec["kind"] == "rupload" and spec["method"] == "GET" and spec["path"] == PATH
    assert not ({"url", "host", "query", "params", "credentials", "headers"} & set(spec))


def test_a_service_post_is_a_served_job_with_only_the_three_headers():
    tab = ServiceTab(job_reply={"status": 200, "body": {"h": H}})
    got = _service_graph(tab).rupload("POST", PATH, headers=_hdr(), chunk=_chunk())
    assert got["body"] == {"h": H}
    spec = tab.specs[-1]
    assert spec["kind"] == "rupload" and spec["method"] == "POST" and spec["path"] == PATH
    assert spec["headers"] == _hdr() and spec["files"] == {wp.RUPLOAD_BODY_FIELD: "served"}
    assert set(spec["source"]) == {"token", "pieces"} and spec["expect"] == {"bytes": MB}
    assert not ({"url", "host", "query", "params", "credentials"} & set(spec))


def test_the_transport_refuses_a_bad_service_request_before_the_page_sees_it():
    tab = ServiceTab()
    t = _service_graph(tab)
    with pytest.raises(wp.WriteRefused):
        t.rupload("POST", PATH, headers=_hdr(Authorization="x"), chunk=_chunk())
    with pytest.raises(wp.WriteRefused):
        t.rupload("DELETE", PATH)
    with pytest.raises(wp.WriteRefused):
        t.rupload("GET", "/fb_video/../x")
    assert tab.specs == [] and not [e for e in tab.exprs if ".call(" in e]


def test_the_client_maps_the_service_answers():
    from src.fb_api import graph as g

    class T:
        def __init__(self, reply):
            self.reply = reply

        def rupload(self, method, path, headers=None, chunk=None, **kw):
            return self.reply

    def client(reply):
        return g.GraphClient(T(reply), allow_writes=True, sleep=lambda s: None, min_interval_s=0.0)

    assert client({"status": 200, "body": {"offset": "5"}}).rupload("GET", PATH) == {"status": 200, "body": {"offset": "5"}}
    c = client({"status": 500, "body": {}})
    assert c.rupload("GET", PATH) == {"status": 500, "body": {}} and c.calls == 0          # not a Graph call
    for err in ("network", "network_timeout", "call_unknown"):
        with pytest.raises(g.RuploadLost):
            client({"error": err}).rupload("GET", PATH)
    c = client({"error": "write_refused", "why": "upload_service"})
    with pytest.raises(g.NotSent):
        c.rupload("GET", PATH)
    assert c.stopped is not None
    c = client({"error": "no_file", "why": "x"})
    with pytest.raises(g.PageFailed) as exc:
        c.rupload("GET", PATH)
    assert c.resume_after_unsent(exc.value)
    with pytest.raises(g.RuploadLost):
        client({"body": {}}).rupload("GET", PATH)                                            # no status: not success


# ───────────────────────── the helper in the page (node) ─────────────────────────

def test_the_helper_sends_the_session_cookies_and_the_token_in_the_query_only_to_the_pages_service():
    out = _node({"name": "rupload", "piece": 400_000})
    assert out["service"] == {"ok": True}
    assert out["get"]["body"] == {"dc": "fra3", "offset": "0"}
    assert out["ok"]["ok"] is True and out["ok"]["body"]["h"] == HANDLE.format(vid="7777777")
    log = out["log"]["rupload"]
    assert [x["method"] for x in log] == ["GET", "POST"]                                    # the refusals sent nothing
    for x in log:
        assert x["url"] == "https://rupload-fra3-1.up.facebook.com/fb_video/" + "a" * 32 + "-0-2500000"
        assert x["cred"] == "include" and x["redirect"] == "error" and x["dtsg_in_url"] is True
        assert x["query_keys"] == ["fb_dtsg_ag", "__a"] and x["token_in_url"] is False
    post = log[1]
    assert post["headers"] == ["X-Entity-Length", "X-Entity-Type", "offset"]                  # no Authorization header
    assert "auth" not in post and post["form"] is False
    assert (post["offset"], post["length"], post["type"]) == ("0", "2500000", "application/octet-stream")
    from tests.test_acl2629_worker_upload import _SOURCE
    assert post["md5"] == hashlib.md5(_SOURCE).hexdigest() and out["rup"]["md5"] == post["md5"]


def test_the_helper_never_returns_the_session_token():
    out = _node({"name": "rupload", "piece": 400_000})
    assert out["ok"]["body"]["echo"] == "<hidden>"                                           # masked in the answer
    assert "D" * 20 + "Tg_AG" not in json.dumps({k: v for k, v in out.items() if k not in ("log",)})


def test_the_helper_refuses_every_malformed_service_request():
    out = _node({"name": "rupload", "piece": 400_000})
    assert len(out["log"]["rupload"]) == 2                                                    # nothing else was sent
    for k, r in out["refused"].items():
        assert r.get("error") in ("write_refused", "read_only"), (k, r)
    assert set(out["refused"]) >= {"path", "partition", "method_put", "header_extra", "header_missing", "header_type",
                                   "length", "offset_end", "past_end", "staged", "wrong_field", "extra_key_url",
                                   "extra_key_host", "extra_key_query", "no_job", "get_headers", "get_path"}


def test_the_helper_checks_the_graph_phases_like_python_and_never_names_a_video():
    out = _node({"name": "rupload", "piece": 400_000})
    assert out["start"]["ok"] and out["transfer"]["body"]["end_offset"] == "2500000" and out["finish"]["body"] == {"success": True}
    for k, r in out["graph_refused"].items():
        assert r["error"] == "write_refused", k
    assert out["graph_untouched"] is True
    assert out["rename"] == {"error": "write_refused", "why": "path"}                         # a video is titled by its finish
    assert out["video_ads"] == {"error": "write_refused", "why": "edge"}                      # the documented edge is gone
    sent = [(x["phase"], x["token_in_body"]) for x in out["log"]["graph"]]
    assert sent == [("start", True), ("transfer", True), ("finish", True)]


def test_the_helper_gives_up_on_a_service_it_does_not_trust():
    for kw in ({"service_domain": "evil.com"}, {"service_name": "evil.up"}, {"service_name": "rupload-x.up.evil"},
               {"no_service": True}):
        out = _node(dict({"name": "rupload"}, **kw))
        assert out["service"] == {"ok": False}, kw
        assert out["get"] == {"error": "write_refused", "why": "upload_service"}, kw
        assert out["log"]["rupload"] == [], kw


def test_the_upload_values_come_with_the_token_by_the_hand_over_and_never_leave_the_helper():
    out = _node({"name": "handover"})
    assert out["before"] == {"ok": False} and out["listen"] == "listening" and out["status"] == "received"
    assert out["after"] == {"ok": True} and out["state"]["has_token"] is True
    assert out["leak"] is False and "async_get_token" not in json.dumps(out["state"])


# ───────────────────────── a lost result of an in-page job ─────────────────────────

def test_an_unknown_in_page_result_aborts_the_request_first():
    from src.fb_api import token_capture as tc
    from tests.test_acl2629_worker_upload import _graph

    class Tab(ServingTab):
        def evaluate_in(self, ctx, expr, timeout=60.0):
            if ".poll(" in expr:
                raise tc.StaleContextError("gone")
            return super().evaluate_in(ctx, expr, timeout)

    tab = Tab()
    t = _graph(tab)
    got = t._write_file_job(7, {"method": "POST"}, None, None)
    assert got == {"error": "call_unknown"}
    assert any(".abort(" in e for e in tab.exprs)
