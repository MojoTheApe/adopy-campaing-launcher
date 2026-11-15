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
                 handle_style="h", transfer="ok", transfer_faults=(), get_faults=(), get_returns_handle=False, refuse_calls=0, refuse_style="page", host_kind_reply="", **kw):
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
        self.refuse_calls = refuse_calls                # the first N service requests are refused before sending
        self.refuse_style = refuse_style                # "page" (helper) | "policy" (Python)
        self.host_kind_reply = host_kind_reply          # the host kind the helper reports ("generic" | "regional")
        self.host_kinds_asked: list[str | None] = []
        self.lag_titles = False                         # a list read (no status field) does not show titles yet
        self.get_returns_handle = get_returns_handle    # a GET of the finished upload also returns the file handle
        self.refreshes = 0
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
        if fault == "denied":
            return {"status": 400, "body": {"error": {"code": 100, "message": "(#100) Invalid parameter"}}}
        if fault == "already":
            return {"status": 400, "body": {"error": {"code": 100, "message": "(#100) Video already finished"}}}
        return {"status": 200, "body": {"success": True}}

    def _video_row(self, obj, params):
        row = super()._video_row(obj, params)
        sess = self.va.get(obj["id"])
        if sess is not None and "status" in row and (not sess["done"] or self.hide_published):
            row["status"]["video_status"] = "uploading"
        if self.lag_titles and "status" not in row:
            row.pop("title", None)
        return row

    # -- the transport of the resumable service
    def upload_service_ready(self):
        return (True, "") if self.service else (False, "upload service address not available on this page")

    def refresh_upload_service(self):
        self.refreshes += 1
        return True

    def rupload(self, method, path, headers=None, chunk=None, **kw):
        if self.refuse_calls:
            self.refuse_calls -= 1
            if self.refuse_style == "policy":
                raise ValueError("write refused: not allowed here")
            return {"error": "write_refused", "why": "upload_service"}
        wp.check_rupload(method, path, headers=headers,
                         files={wp.RUPLOAD_BODY_FIELD: chunk} if chunk is not None else None)
        self.rup.append((method, path, dict(headers or {})))
        self.host_kinds_asked.append(kw.get("host_kind"))
        sess = self.va[self.by_wf[path.split("/")[-1].split("-")[0]]]
        reply = self._rupload_reply(method, path, headers, chunk, sess)
        if self.host_kind_reply and isinstance(reply, dict) and "status" in reply:
            reply = dict(reply, host_kind=self.host_kind_reply)
        return reply

    def _rupload_reply(self, method, path, headers, chunk, sess):
        if method == "GET":
            fault = self.get_faults.pop(0) if self.get_faults else ""
            if fault == "lost":
                return {"error": "network"}
            if fault == "bad":
                return {"status": 200, "body": {"offset": "x"}}
            body = {"dc": "fra3", "offset": str(sess["received"])}
            if self.get_returns_handle and sess["received"] == sess["size"]:
                body["h"] = HANDLE.format(vid=self.by_wf[path.split("/")[-1].split("-")[0]])
            return {"status": 200, "body": body}
        self.served_sizes.append(len(chunk))
        off = int(headers["offset"])
        if off != sess["received"]:
            return {"status": 400, "body": {"error": "wrong offset"}}
        fault = self.faults.pop(0) if self.faults else ""
        if fault == "nothing":
            return {"error": "network"}
        if fault == "retired":
            return {"error": "retired"}
        if fault == "page_refused":
            return {"error": "write_refused", "why": "upload_service"}
        if fault == "kept400":                       # a short POST: all bytes kept, yet answered 4xx
            sess["received"] += len(chunk)
            sess["sha"].update(chunk.read(0, len(chunk)))
            return {"status": 400, "body": {}}
        if fault == "discard200":                    # a short POST: answered 200, nothing kept
            return {"status": 200, "body": {}}
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
    assert [m for m, _, _ in meta.rup].count("GET") == 2                # the partial-piece check + the resume read


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
    assert len(_posts(meta)) == 2 + cr.RESUMABLE_MAX_RESUMES + 1        # progress is free, the 6th refusal without progress stops


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


def test_a_service_that_keeps_no_partial_piece_falls_back_after_the_first_segment(resumable, tmp_path):
    d, f = _big_draft(tmp_path)
    meta = ResumableMeta(keep_partial=False)
    out, _ = _run(meta, d, max_calls=400)
    assert out.error == "", out.error
    assert "transfer" not in _phases(meta) and "finish" not in _phases(meta)       # nothing finished
    assert any("did not keep a partial piece" in w for w in out.warnings)
    assert len(_posts(meta)) == 1                                                    # not a byte more was sent
    assert next(iter(meta.sessions.values()))["done"]


def test_a_short_post_answered_4xx_but_kept_is_continued_normally(resumable, tmp_path):
    d, f = _big_draft(tmp_path)
    meta = ResumableMeta(faults=["kept400"])
    out, _ = _run(meta, d)
    assert out.error == "", out.error
    sess = next(iter(meta.va.values()))
    assert sess["done"] and sess["sha"].hexdigest() == _sha(f)
    assert _offsets(meta) == [0, MB, 2 * MB, 3 * MB] and not any("resumable" in w for w in out.warnings)
    assert any("answered HTTP 400 but kept the bytes" in s for s in out.steps)
    assert not any("resuming the upload" in s for s in out.steps)                    # not a resume


def test_a_short_post_answered_2xx_but_discarded_falls_back(resumable, tmp_path):
    d, f = _big_draft(tmp_path)
    meta = ResumableMeta(faults=["discard200"])
    out, _ = _run(meta, d)
    assert out.error == "" and "transfer" not in _phases(meta)
    assert any("did not keep a partial piece" in w for w in out.warnings)


def test_resumes_that_make_progress_are_free(resumable, tmp_path):
    d, f = _big_draft(tmp_path)
    meta = ResumableMeta(faults=["", "drop"] * 1 + ["drop"] * 7)                     # 8 broken answers, each with progress
    out, _ = _run(meta, d, max_calls=400)
    assert out.error == "", out.error
    sess = next(iter(meta.va.values()))
    assert sess["done"] and sess["sha"].hexdigest() == _sha(f) and len(meta.va) == 1


def test_two_4xx_answers_in_a_row_read_the_service_address_again_once(resumable, tmp_path):
    d, f = _big_draft(tmp_path)
    meta = ResumableMeta(faults=["", "refused", "refused", "refused", "refused", "refused", "refused"])
    out, _ = _run(meta, d, max_calls=400)
    assert out.stop_kind == "video_error"
    assert meta.refreshes == 1


# ───────────────────────── a refusal before sending ─────────────────────────

@pytest.mark.parametrize("style", ["page", "policy"])
def test_a_refusal_before_sending_while_nothing_moved_falls_back(resumable, tmp_path, style):
    d, f = _big_draft(tmp_path)
    meta = ResumableMeta(refuse_calls=1, refuse_style=style)
    out, client = _run(meta, d)
    assert out.error == "", out.error
    assert any("the page refused the upload service request" in w for w in out.warnings)
    assert "transfer" not in _phases(meta) and client.stopped is None
    assert next(iter(meta.sessions.values()))["done"]


def test_a_refusal_before_sending_after_bytes_moved_is_a_stop_with_the_reason(resumable, tmp_path):
    d, f = _big_draft(tmp_path)
    meta = ResumableMeta(faults=["", "page_refused"])
    out, _ = _run(meta, d)
    assert out.stop_kind == "video_error" and "the page refused an upload service request" in out.error
    assert "transfer" not in _phases(meta)


# ───────────────────────── a lost answer to the last POST ─────────────────────────

def test_a_lost_answer_to_the_last_post_takes_the_handle_from_the_offset_read(resumable, tmp_path):
    d, f = _big_draft(tmp_path)
    meta = ResumableMeta(faults=["", "", "", "lost"], get_returns_handle=True)
    out, _ = _run(meta, d)
    assert out.error == "", out.error
    assert _phases(meta) == ["start", "transfer", "finish"] and len(_posts(meta)) == 4       # the last POST not resent
    assert next(iter(meta.va.values()))["sha"].hexdigest() == _sha(f)


def test_a_lost_answer_to_the_last_post_without_a_handle_anywhere_falls_back(resumable, tmp_path):
    d, f = _big_draft(tmp_path)
    meta = ResumableMeta(faults=["", "", "", "lost"])
    out, _ = _run(meta, d)
    assert out.error == "", out.error
    assert "transfer" not in _phases(meta) and len(_posts(meta)) == 4
    assert any("every byte but no file handle" in w for w in out.warnings)


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
    assert [m for m, _, _ in meta.rup].count("GET") == 2                       # the partial-piece check + one offset read
    assert any("without a file handle (ok:bool)" in s for s in out.steps)      # key names and value types only
    assert next(iter(meta.sessions.values()))["done"]


def test_the_handle_parser_is_strict():
    ok = "1:YWJj:application/octet-stream:123:e:1800000000:sig_-1"
    assert wp.file_handle({"h": ok}) == ok
    assert wp.file_handle({"x": {"y": ok}}) == ok and wp.file_handle({"a": 1, "b": ok}) == ok
    for body in ({}, {"h": ""}, {"h": 5}, {"h": "not a handle"}, {"h": ok + " x"}, {"h": "1:a:b:c:d:e:f"},
                 {"h": "x" * 2000}, {"x": {"y": {"z": ok}}}, None, [], "1:abc"):
        assert wp.file_handle(body) == ""
    longer = "1:YWJj:application/octet-stream:123:e:1800000000:sig+/=-1:more_2.3"        # a longer tail is allowed
    assert wp.file_handle({"h": longer}) == longer
    assert wp.file_handle({"h": ok + ":a:b:c:d:e:f:g"}) == ""                          # but not an endless one


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


def test_a_transfer_lost_twice_falls_back_before_any_finish(resumable, tmp_path):
    d, f = _big_draft(tmp_path)
    meta = ResumableMeta(transfer_faults=["lost", "lost"])
    out, _ = _run(meta, d)
    assert out.error == "", out.error
    assert _phases(meta) == ["start", "transfer", "transfer"]
    assert any("the transfer answer was lost twice" in w for w in out.warnings)


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
    assert out.stop_kind == "unknown" and "Publish again later" in out.error and "may be finished" in out.error


# ───────────────────────── budget ─────────────────────────

def test_the_graph_calls_are_constant_and_the_service_is_not_counted(resumable, tmp_path):
    d, f = _big_draft(tmp_path)
    meta = ResumableMeta()
    out, client = _run(meta, d)
    assert out.error == ""
    assert len(meta.va_posts) == 3 and len(_posts(meta)) == 4                    # 3 Graph writes, 4 segments
    assert cr.resumable_calls() == 4
    assert cr.resumable_calls() < cr.upload_calls(2420 * MB, True, chunked=True)      # inside the run budget


def test_the_hard_cap_stops_before_the_transfer(resumable, tmp_path, monkeypatch):
    d, f = _big_draft(tmp_path)
    monkeypatch.setattr(cr, "RESUMABLE_FIXED_CALLS", -2)            # the file-API cap is RESUMABLE_FIXED_CALLS + 4 = 2
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
    assert _offsets(meta)[-2] == held                                     # continued at the service's offset
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


def test_a_transferred_but_unfinished_video_is_only_finished_with_its_title(resumable, tmp_path):
    from tests.test_acl1856_operation_journal import _go
    oj, now, journal = _journal(tmp_path)
    d, f = _big_draft(tmp_path)
    meta = ResumableMeta(finish_faults=["lost", "lost"])
    meta.hide_published = True
    first, _ = _go(meta, d, journal, request_key="r1#t")
    assert first.stop_kind == "unknown"
    st, = [s for s in _stages(tmp_path) if s.get("pending_id")]
    assert st["pending"]["transferred"] is True
    (vid, _sess), = meta.va.items()
    meta.objects[vid]["title"] = None                  # the finish never took: a search by the tag finds nothing
    meta.hide_published = False
    now[0] += oj.BUSY_S + 60
    again, _ = _go(meta, d, journal, request_key="r1#t")
    assert again.error == "", again.error
    assert _phases(meta) == ["start", "transfer", "finish", "finish", "finish"]       # ONE finish, no new transfer
    assert meta.objects[vid]["title"].startswith("acl-") and len(_posts(meta)) == 4
    assert any("the video uploaded by an earlier run is used" in s for s in again.steps)


def test_an_already_finished_answer_to_the_repeated_finish_is_success(resumable, tmp_path):
    from tests.test_acl1856_operation_journal import _go
    oj, now, journal = _journal(tmp_path)
    d, f = _big_draft(tmp_path)
    meta = ResumableMeta(finish_faults=["lost", "lost", "already"])
    meta.hide_published = True
    _go(meta, d, journal, request_key="r1#t")
    (vid, _s), = meta.va.items()
    meta.objects[vid]["title"] = None
    meta.hide_published = False
    now[0] += oj.BUSY_S + 60
    again, _ = _go(meta, d, journal, request_key="r1#t")
    assert again.error == "", again.error
    assert _phases(meta).count("start") == 1 and _phases(meta).count("finish") == 3


def test_processing_or_ready_alone_is_not_trusted_for_an_adopted_video(resumable, tmp_path):
    _go, oj, journal, d, f, meta, old = _stopped_after_start(tmp_path)
    sess = meta.va[old]
    sess["received"], sess["transferred"] = sess["size"], True       # as if it was transferred, the journal never knew
    sess["done"] = True                                              # and the status already says processing/ready
    again, _ = _go(meta, d, journal, request_key="r1#t")
    assert again.error == "", again.error
    assert _phases(meta).count("start") == 1 and _phases(meta).count("finish") == 1   # the finish with the title was sent
    assert meta.objects[old]["title"].startswith("acl-")


def test_a_pending_upload_with_every_byte_in_and_the_handle_from_the_service_goes_to_the_transfer(resumable, tmp_path):
    _go, oj, journal, d, f, meta, old = _stopped_after_start(tmp_path)
    sess = meta.va[old]
    sess["received"] = sess["size"]                      # the rest of the bytes arrived, the handle answer was lost
    meta.get_returns_handle = True
    posts = len(_posts(meta))
    again, _ = _go(meta, d, journal, request_key="r1#t")
    assert again.error == "", again.error
    assert len(_posts(meta)) == posts and _phases(meta) == ["start", "transfer", "finish"]   # no new POST, no new start
    assert any("all bytes are in" in s for s in again.steps)


def test_a_published_pending_video_whose_finish_is_unclear_stops_as_unknown(resumable, tmp_path):
    _go, oj, journal, d, f, meta, old = _stopped_after_start(tmp_path)
    sess = meta.va[old]
    sess["received"], sess["transferred"], sess["done"] = sess["size"], True, True
    meta.finish_faults = ["nosuccess"]
    again, _ = _go(meta, d, journal, request_key="r1#t")
    assert again.stop_kind == "unknown" and "Publish again later" in again.error
    assert _phases(meta).count("start") == 1
    assert any(st.get("pending_id") == old for st in _stages(tmp_path))      # kept for the next run


def test_a_pending_video_with_our_title_is_adopted_without_a_write(resumable, tmp_path):
    from tests.test_acl1856_operation_journal import _go
    oj, now, journal = _journal(tmp_path)
    d, f = _big_draft(tmp_path)
    meta = ResumableMeta(finish_faults=["lost", "lost"])
    meta.hide_published = True
    first, _ = _go(meta, d, journal, request_key="r1#t")
    assert first.stop_kind == "unknown"
    meta.hide_published, meta.lag_titles = False, True      # the list does not show the title yet; the status read does
    before = list(meta.va_posts)
    now[0] += oj.BUSY_S + 60
    again, _ = _go(meta, d, journal, request_key="r1#t")
    assert again.error == "", again.error
    assert list(meta.va_posts) == before                     # not a single write: adopted by its title
    assert any("the video uploaded by an earlier run is used" in s for s in again.steps)


def test_a_refused_repeated_finish_without_the_title_forgets_the_entry_and_starts_again(resumable, tmp_path):
    from tests.test_acl1856_operation_journal import _go
    oj, now, journal = _journal(tmp_path)
    d, f = _big_draft(tmp_path)
    meta = ResumableMeta(finish_faults=["lost", "lost", "denied"])
    meta.hide_published = True
    _go(meta, d, journal, request_key="r1#t")
    (vid, _s), = meta.va.items()
    meta.objects[vid]["title"] = None
    meta.hide_published = False
    now[0] += oj.BUSY_S + 60
    again, _ = _go(meta, d, journal, request_key="r1#t")
    assert again.error == "", again.error
    assert _phases(meta).count("start") == 2
    assert any("Meta refused to finish the upload of an earlier run" in s for s in again.steps)


def test_a_retired_helper_is_a_stop_even_before_any_byte_moved(resumable, tmp_path):
    d, f = _big_draft(tmp_path)
    meta = ResumableMeta(faults=["retired"])
    out, _ = _run(meta, d)
    assert out.stop_kind == "video_error" and "retired" in out.error
    assert "transfer" not in _phases(meta) and not any("chunks instead" in w for w in out.warnings)


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
        if ".uploadParts()" in expr:
            self.exprs.append(expr)
            return {"service": bool(self.ready), "token": bool(self.ready)}
        return super().evaluate_in(ctx, expr, timeout)


def _service_graph(tab):
    from tests.test_acl2629_worker_upload import _graph
    return _graph(tab)


def test_the_transport_asks_the_page_whether_it_knows_the_service():
    assert _service_graph(ServiceTab(ready=True)).upload_service_ready() == (True, "")
    ok, why = _service_graph(ServiceTab(ready=False)).upload_service_ready()
    assert ok is False and "session token for the upload not available" in why


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
    # an untrusted regional service is ignored: the generic host is used instead, never the foreign one
    for kw in ({"service_domain": "evil.com"}, {"service_name": "evil.up"}, {"service_name": "rupload-x.up.evil"}):
        out = _node(dict({"name": "rupload"}, **kw))
        assert out["service"] == {"ok": True}, kw
        assert out["get"]["ok"] is True, kw
        urls = [x["url"] for x in out["log"]["rupload"]]
        assert urls and all(u.startswith("https://rupload.facebook.com/fb_video/") for u in urls), (kw, urls)
    # no session token at all: nothing can be sent
    out = _node({"name": "rupload", "no_service": True})
    assert out["service"] == {"ok": False}
    assert out["get"] == {"error": "write_refused", "why": "upload_service"} and out["log"]["rupload"] == []


def test_a_token_only_helper_uses_the_generic_host():
    out = _node({"name": "rupload", "token_only": True, "piece": 400_000})
    assert out["service"] == {"ok": True}
    assert out["get"]["ok"] is True
    log = out["log"]["rupload"]
    assert [x["method"] for x in log] == ["GET", "POST"]
    assert all(x["url"] == "https://rupload.facebook.com/fb_video/" + "a" * 32 + "-0-2500000" for x in log)
    assert all(x["cred"] == "include" and x["dtsg_in_url"] is True and x["query_keys"] == ["fb_dtsg_ag", "__a"] for x in log)


def test_the_policy_accepts_exactly_the_generic_and_the_regional_hosts():
    for host in ("rupload.facebook.com", "rupload-fra3-1.up.facebook.com", "rupload-ber1-1.up.facebook.com"):
        assert wp.rupload_host_ok(host), host
    for host in ("rupload.facebook.com.evil", "rupload.up.facebook.com", "x.facebook.com", "facebook.com",
                 "rupload-fra3-1.up.facebook.com.evil", "evil.rupload.facebook.com", "rupload.facebook.com:8080",
                 "RUPLOAD.facebook.com", "rupload-.up.facebook.com ", "", None, "rupload-A.up.facebook.com"):
        assert not wp.rupload_host_ok(host), host
    assert wp.policy_for_page(ACT)["rupload"]["generic_host"] == "rupload.facebook.com"


def test_the_session_token_is_masked_raw_and_url_encoded_in_error_and_html_bodies():
    out = _node({"name": "rupload", "echo": True, "colon_token": True, "piece": 400_000})
    blob = json.dumps({k: v for k, v in out.items() if k != "log"})
    assert out["get"]["body"]["error"]["message"].count("<hidden>") >= 2
    assert out["post"]["status"] == 502 and "non_json" in out["post"]["body"] and "<hidden>" in out["post"]["body"]["non_json"]
    for leak in ("DDDDDDDDDDDD", "%3ATg%3AAG%3A", ":Tg:AG:", "Tg%3AAG"):
        assert leak not in blob, leak


def test_the_upload_values_come_with_the_token_by_the_hand_over_and_never_leave_the_helper():
    out = _node({"name": "handover"})
    assert out["upload_only"] if False else True
    assert out["listen2"] == "listening" and out["sent2"] == "acked" and out["status2"] == "received"
    assert out["refresh"] == {"ok": True} and out["listen3"] == "no_token"          # upload-only never gives a token
    assert out["has4"] is True and out["listen4"] == "listening" and out["status4"] == "rejected"   # another token
    assert out["after4"] == {"ok": False}                                            # ... took nothing
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


# ───────────────────────── the chunked fallback uses the run's budget, not the 64 MB plan ─────────────────────────

class LiveWindows(ResumableMeta):
    """Meta as in every live run so far: a 1 MB first window, then a steady 5 MB."""

    def _range(self, sess):
        got = sess["received"]
        return {"start_offset": str(got), "end_offset": str(min(got + (MB if got == 0 else 5 * MB), sess["size"]))}


def test_a_70_mb_video_whose_file_api_is_refused_completes_in_chunks_within_the_run_budget(tmp_path, monkeypatch):
    monkeypatch.delenv("ACL_VIDEO_WORKER_UPLOAD", raising=False)
    monkeypatch.delenv("ACL_VIDEO_RESUMABLE_UPLOAD", raising=False)
    d, f = _big_draft(tmp_path, size=70 * MB)
    meta = LiveWindows(start="denied")
    out, client = _run(meta, d, max_calls=200)
    assert out.error == "", out.error
    assert any("resumable upload was not used" in w for w in out.warnings)
    sess = next(iter(meta.sessions.values()))
    assert sess["done"] and sess["sha"].hexdigest() == _sha(f)
    assert 14 <= len(meta.served_sizes) <= 16 and client.calls <= client.max_calls        # 1 MB + 5 MB windows


def test_a_huge_video_with_small_windows_stops_with_the_clear_message_on_a_tight_budget(worker_small, tmp_path, monkeypatch):
    # (scaled down: 3.5 MB file, 5 KB windows ~ a 2.4 GB file with 5 MB windows and a 340-call run budget)
    monkeypatch.setenv("ACL_VIDEO_RESUMABLE_UPLOAD", "0")
    d, f = _big_draft(tmp_path)
    meta = ServedMeta(meta_chunk=5_000)
    out, client = _run(meta, d, max_calls=340)
    assert out.stop_kind == "video_error" and "small pieces" in out.error and "smaller video file" in out.error
    assert not any(p == "finish" for p, _ in meta.phases)
    assert len(meta.served_sizes) == cr.UPLOAD_WINDOW_PROBE_TRANSFERS and client.calls < client.max_calls
    assert cr.upload_calls(2420 * MB, True, chunked=True) > 340 - cr.estimate_create_calls(cr.plan_from_draft(d))


def test_the_plan_with_the_flag_off_assumes_5_mb_windows_and_with_it_on_the_file_api(monkeypatch):
    monkeypatch.delenv("ACL_VIDEO_WORKER_UPLOAD", raising=False)
    monkeypatch.delenv("ACL_VIDEO_RESUMABLE_UPLOAD", raising=False)
    assert cr.upload_calls(70 * MB, True) == cr.resumable_calls()
    monkeypatch.setenv("ACL_VIDEO_RESUMABLE_UPLOAD", "0")
    assert cr.upload_calls(70 * MB, True) == 14 + 6 + cr.CALLS_UPLOAD_FIXED


# ───────────────────────── the fallback never strands a campaign ─────────────────────────

def _two_ads_video_first(tmp_path):
    from dataclasses import replace
    d, f = _big_draft(tmp_path)
    ads = d.ad_sets[0].ads
    ads.append(replace(ads[0], creative_id="1002", copy_name="Text2", ad_title="Head 2", ad_body="Body 2"))
    return d, f


def test_a_refused_fast_upload_on_a_tight_budget_stops_before_the_first_chunk(resumable, tmp_path):
    d, f = _two_ads_video_first(tmp_path)
    plan = cr.plan_from_draft(d)
    budget = cr.estimate_create_calls(plan) + cr.resumable_calls() + 12          # the file-API plan fits, the chunks do not
    meta = ResumableMeta(start="denied")
    out, client = _run(meta, d, max_calls=budget)
    assert out.stop_kind == "video_error"
    assert "the fast upload was refused" in out.error and "this run can spare" in out.error
    assert "Nothing was uploaded" in out.error and meta.sessions == {} and meta.rup == []      # not even a chunked start
    assert client.calls <= client.max_calls


def test_a_refused_fast_upload_with_room_completes_every_object(resumable, tmp_path):
    d, f = _two_ads_video_first(tmp_path)
    meta = ResumableMeta(start="denied")
    out, client = _run(meta, d, max_calls=300)
    assert out.error == "", out.error
    assert len(out.created["ads"]) == 2 and client.calls <= client.max_calls
    assert next(iter(meta.sessions.values()))["done"]


def test_check_plan_warns_when_the_fallback_would_not_fit(resumable, tmp_path, monkeypatch):
    monkeypatch.setattr(cr, "VIDEO_SINGLE_MAX_BYTES", MB)                 # Check plan reads the file as a large one
    d, f = _big_draft(tmp_path)
    plan = cr.plan_from_draft(d)
    from src.fb_api import graph as g
    meta = ResumableMeta()
    client = g.GraphClient(meta, allow_writes=False, sleep=lambda s: None, min_interval_s=0.0,
                           max_calls=cr.estimate_create_calls(plan) + cr.resumable_calls() + 5)
    creator = cr.CampaignCreator(client, cr.preflight_account(client, ACT), cr.TargetingResolver(client),
                                 sleep=lambda s: None)
    out = cr.Outcome(campaign_name=plan.name)
    creator._prepare(plan, writing=False, out=out)
    warns = [w for w in out.warnings if w.startswith(("Large video", "If the fast upload"))]
    assert any("the fast upload takes about" in w and "chunks of" not in w for w in warns), warns
    assert any(w.startswith("If the fast upload is refused") and "this run can give" in w for w in warns), warns


def _two_videos(tmp_path):
    from dataclasses import replace
    d, f = _big_draft(tmp_path)
    ad = d.ad_sets[0].ads[0]
    f2 = Path(str(ad.creative_path) + ".second.mp4")
    f2.write_bytes(b"second-video-" * 300_000)                       # a different large file (3.9 MB)
    d.ad_sets[0].ads.append(replace(ad, creative_id="1002", creative_filename=f2.name, creative_path=str(f2),
                                    copy_name="Text2", ad_title="Head 2", ad_body="Body 2"))
    return d, f


@pytest.mark.parametrize("extra", [8, 14, 20, 40, 400])
def test_two_large_videos_on_a_tight_budget_never_strand_later_objects(resumable, tmp_path, extra):
    d, f = _two_videos(tmp_path)
    plan = cr.plan_from_draft(d)
    budget = cr.estimate_create_calls(plan) + 2 * cr.resumable_calls() + extra
    meta = ResumableMeta(start="denied")                              # the fast upload is refused for every video
    out, client = _run(meta, d, max_calls=budget)
    if out.error:
        # stopped BEFORE the first chunk of the first video: nothing was uploaded, no object of a later ad is missing
        assert "this run can spare" in out.error or "plan" in str(out.stop_kind), out.error
        assert meta.sessions == {} and len(out.created.get("ads", [])) == 0
    else:
        assert len(out.created["ads"]) == 2 and len(out.created["videos"]) == 2
    assert client.calls <= client.max_calls and client.stopped is None or out.error


def test_the_reserve_counts_the_videos_still_to_come_and_not_the_upload_writes(resumable, tmp_path):
    d, f = _two_videos(tmp_path)
    plan = cr.plan_from_draft(d)
    # just enough for ONE chunked upload but not for the second video's fast upload on top of the campaign
    fb = cr.upload_calls(f.stat().st_size, True, chunked=True)
    meta = ResumableMeta(start="denied")
    out, client = _run(meta, d, max_calls=cr.estimate_create_calls(plan) + cr.resumable_calls() + fb + 14)
    # the first video fits (chunked), the second would eat what the campaign still needs: a clean stop BEFORE its chunks
    assert out.stop_kind == "video_error" and "this run can spare" in out.error
    assert len(meta.sessions) == 1 and next(iter(meta.sessions.values()))["done"]
    assert client.calls < client.max_calls and "Nothing was uploaded" in out.error


# ───────────────────────── the page's module registry (main world) ─────────────────────────

TOKEN62 = "Q" * 20 + ":" + "z" * 41


class MainWorldTab(ServingTab):
    """A page whose inline scripts show neither value; the main world's module registry has them."""

    def __init__(self, service="rupload-ber1-1.up|facebook.com", token=TOKEN62, loaded_after=0, **kw):
        super().__init__(**kw)
        self.have = {"service": False, "token": False}
        self.service, self.token = service, token
        self.loaded_after = loaded_after               # the module shows up only after this many reads
        self.main_calls: list[str] = []
        self.set_args: list[list] = []

    def evaluate_main(self, expr, timeout=10.0):
        self.main_calls.append(expr)
        if "AdsVideoUploadConfig" in expr:
            if len([c for c in self.main_calls if "AdsVideoUploadConfig" in c]) <= self.loaded_after:
                return ""
            return self.service
        if "DTSGInitData" in expr:
            return self.token
        return None

    def evaluate_in(self, ctx, expr, timeout=60.0):
        if ".uploadParts()" in expr:
            self.exprs.append(expr)
            return dict(self.have)
        if ".setUploadService(" in expr:
            self.exprs.append(expr)
            args = json.loads("[" + expr.split(".setUploadService(", 1)[1].rsplit(")", 1)[0] + "]")
            self.set_args.append(args)
            name, dom, tok = args
            if name and dom:
                self.have["service"] = True
            if tok:
                self.have["token"] = True
            return dict(self.have)
        return super().evaluate_in(ctx, expr, timeout)


def test_the_main_world_read_is_used_when_the_scan_finds_nothing(caplog):
    import logging
    caplog.set_level(logging.DEBUG)
    tab = MainWorldTab()
    t = _service_graph(tab)
    ok, why = t.upload_service_ready()
    assert (ok, why) == (True, "")
    assert tab.set_args == [["rupload-ber1-1.up", "facebook.com", TOKEN62]]
    # only the two fixed read-only expressions ran in the main world; nothing else of those modules is read
    assert len(tab.main_calls) == 2
    assert not any("access_token" in c or "accessToken" in c for c in tab.main_calls)
    assert TOKEN62 not in caplog.text and TOKEN62 not in why                    # the value is never logged


def test_the_main_world_read_is_retried_once_when_the_module_is_not_loaded_yet():
    tab = MainWorldTab(loaded_after=1)
    t = _service_graph(tab)
    slept: list[float] = []
    t._sleep = slept.append
    assert t.fill_upload_from_page() is True
    assert slept and slept[0] <= 3.0 and len([c for c in tab.main_calls if "AdsVideoUploadConfig" in c]) == 2


def test_a_value_that_is_not_the_pages_own_service_is_never_handed_in():
    for raw in ("evil.up|facebook.com", "rupload-x.up|evil.com", "rupload-x.up|facebook.com|y", "rupload-x.up", "|", "",
                "rupload-x.up.evil|facebook.com"):
        tab = MainWorldTab(service=raw, token="short")
        t = _service_graph(tab)
        t._sleep = lambda s: None
        ok, why = t.upload_service_ready()
        assert ok is False and "session token for the upload not available" in why, raw
        assert tab.set_args == [], raw                                           # nothing was passed to the helper


def test_the_reason_names_the_missing_piece_without_values():
    tab = MainWorldTab(token="")
    t = _service_graph(tab)
    t._sleep = lambda s: None
    ok, why = t.upload_service_ready()
    assert ok is False and why == "session token for the upload not available on this page"
    tab = MainWorldTab(service="")                        # no regional service: the generic host, token present
    t = _service_graph(tab)
    t._sleep = lambda s: None
    assert t.upload_service_ready() == (True, "generic upload host")


def test_the_senders_page_values_are_read_before_a_hand_over():
    from src.fb_api import light_tab

    calls = []

    class Src:
        def fill_upload_from_page(self):
            calls.append("fill")

        def _eval_helper(self, tail, timeout=0):
            calls.append(tail)
            return "acked"

    class Dst:
        def _eval_helper(self, tail, timeout=0):
            return "listening" if ".listen(" in tail else "received"

    light_tab.hand_over(Src(), Dst(), wait_s=0.1)
    assert calls[0] == "fill" and any(".handOver(" in c for c in calls)


def test_the_helper_validates_what_python_hands_in():
    out = _node({"name": "handover"})
    assert out["set_bad"] == {"service": False, "token": False, "generic": False}
    assert out["set_good"] == {"service": True, "token": True, "generic": False}


def test_the_fast_upload_refused_for_a_missing_piece_says_which_piece(resumable, tmp_path):
    d, f = _big_draft(tmp_path)
    meta = ResumableMeta(service=False)
    out, _ = _run(meta, d)
    assert out.error == "" and any("upload service address not available on this page" in s for s in out.steps)


def test_the_call_budget_stop_is_not_reported_as_a_bad_creative_file(resumable, tmp_path):
    from src.fb_api import plain_errors as pe
    d, f = _two_ads_video_first(tmp_path)
    plan = cr.plan_from_draft(d)
    meta = ResumableMeta(start="denied")
    out, _ = _run(meta, d, max_calls=cr.estimate_create_calls(plan) + cr.resumable_calls() + 12)
    assert "this run can spare" in out.error
    failure = out.failure if out.failure is not None else pe.classify(out.error)
    assert "too large for this run's Meta call budget" in failure.short
    assert "creative file" not in failure.short.lower() and "replace the file" not in failure.next_step


# ───────────────────────── light mode end to end (live bug, G87) ─────────────────────────

@pytest.mark.parametrize("variant", [{}, {"token_only": True, "main_world": True}, {"token_only": True}])
def test_the_upload_values_survive_capture_light_and_clean_tab(variant):
    """The capture tab's helper is READ-ONLY (no write policy). It used to ignore the upload values altogether, so
    nothing reached the light tab and the clean upload tab (live: 'address and session token not available')."""
    out = _node(dict({"name": "chain"}, **variant))
    if variant == {"token_only": True}:                       # no main-world read: only the token can be there
        assert out["a_after"] == {"service": False, "token": True, "generic": True} and out["c_ready"] == {"ok": True}
        return
    both = {"service": True, "token": True, "generic": False}
    assert out["a_after"] == both                                                    # the capture helper holds both
    assert out["ab"]["status"] == "received" and out["b"] == both
    assert out["bc"]["status"] == "received" and out["c"] == both
    assert out["c_ready"] == {"ok": True} and out["leak"] is False


def test_the_capture_helper_without_a_write_policy_validates_with_its_own_patterns():
    from src.fb_api import token_capture as tc
    src = tc.helper_source("n", None)
    assert "const WRITE = null;" in src and '"name_re"' in src and "const UPLOAD = {" in src


def test_every_hop_records_which_pieces_it_holds_without_values(monkeypatch):
    from src.fb_api import light_tab
    events = []
    monkeypatch.setattr(light_tab, "_record", events.append)

    class H:
        def __init__(self, parts):
            self.parts = parts

        def _upload_parts(self):
            return self.parts

        def fill_upload_from_page(self):
            return True

        def _eval_helper(self, tail, timeout=0):
            return "acked" if ".handOver(" in tail else ("listening" if ".listen(" in tail else "received")

    light_tab.hand_over(H({"service": True, "token": False}), H({"service": True, "token": True}), wait_s=0.1)
    hops = [(e["hop"], e["service"], e["token"]) for e in events if e["event"] == "fb_api_upload_service"]
    assert hops == [("after_fill", True, False), ("after_handover", True, True)]
    assert [e["generic_host"] for e in events if e["event"] == "fb_api_upload_service"] == [False, False]
    assert all(set(e) <= {"event", "hop", "service", "token", "generic_host", "status", "clean_tab", "upload_only"}
               for e in events)


# ───────────────────────── the host of an upload is pinned ─────────────────────────

def test_the_host_of_an_upload_is_pinned_by_its_path_in_the_helper():
    out = _node({"name": "pin", "token_only": True})
    hosts = [x["url"].split("/fb_video/")[0] for x in out["log"]["rupload"]]
    g, r = "https://rupload.facebook.com", "https://rupload-ber1-1.up.facebook.com"
    # a: generic, a again: STILL generic, b (new): regional, c asked generic: generic, d asked regional: regional,
    # c again asked regional: the pin wins (generic)
    assert hosts == [g, g, r, g, r, g]
    assert out["bad"] == {"error": "write_refused", "why": "host_kind"} and len(hosts) == 6
    assert out["svc"]["service"] is True


def test_the_python_policy_validates_the_host_kind():
    wp.check_rupload("GET", PATH, host_kind="generic")
    wp.check_rupload("GET", PATH, host_kind="regional")
    wp.check_rupload("GET", PATH, host_kind=None)
    for bad in ("evil", "", "Generic", "rupload.facebook.com"):
        with pytest.raises(wp.WriteRefused):
            wp.check_rupload("GET", PATH, host_kind=bad)


def test_the_client_passes_the_host_kind_and_checks_the_reported_host():
    from src.fb_api import graph as g
    seen = {}

    class T:
        reply = {"status": 200, "body": {}, "host_kind": "generic", "endpoint": {"host": "rupload.facebook.com"}}

        def rupload(self, method, path, headers=None, chunk=None, **kw):
            seen.update(kw)
            return self.reply

    t = T()
    c = g.GraphClient(t, allow_writes=True, sleep=lambda s: None, min_interval_s=0.0)
    assert c.rupload("GET", PATH, host_kind="generic")["host_kind"] == "generic" and seen == {"host_kind": "generic"}
    t.reply = {"status": 200, "body": {}, "endpoint": {"host": "rupload-fra3-1.up.facebook.com"}}
    assert "host_kind" not in c.rupload("GET", PATH)
    for host in ("evil.example", "rupload.facebook.com.evil", "rupload.up.facebook.com"):
        t.reply = {"status": 200, "body": {}, "endpoint": {"host": host}}
        c = g.GraphClient(t, allow_writes=True, sleep=lambda s: None, min_interval_s=0.0)
        with pytest.raises(g.PageFailed):
            c.rupload("GET", PATH)
        assert c.stopped is not None


def test_the_journal_keeps_the_host_kind_and_a_continuation_reuses_it(resumable, tmp_path):
    from tests.test_acl1856_operation_journal import _go
    oj, now, journal = _journal(tmp_path)
    d, f = _big_draft(tmp_path)
    meta = ResumableMeta(faults=["", "drop"] + ["refused"] * 10, host_kind_reply="generic")
    first, _ = _go(meta, d, journal, request_key="r1#t")
    assert first.stop_kind == "video_error"
    st, = [s for s in _stages(tmp_path) if s.get("pending_id")]
    assert st["pending"]["host_kind"] == "generic"                                  # the kind, never a host value
    assert "rupload" not in json.dumps(st) and "facebook" not in json.dumps(st)
    assert meta.host_kinds_asked[0] is None and meta.host_kinds_asked[-1] == "generic"   # pinned after the first reply
    meta.faults = []
    meta.host_kinds_asked.clear()
    now[0] += oj.BUSY_S + 60
    again, _ = _go(meta, d, journal, request_key="r1#t")
    assert again.error == "", again.error
    assert meta.host_kinds_asked and set(meta.host_kinds_asked) == {"generic"}       # the continuation asks the same kind
