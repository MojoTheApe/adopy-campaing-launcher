"""ACL-2639: large ad videos go up with Meta's documented resumable ("non-chunking") protocol from the profile tab.

Marketing API guide "FB Video Ads" (https://developers.facebook.com/docs/marketing-api/guides/videoads/fbvideoads/):
start on act_X/video_ads -> POST the bytes to rupload.facebook.com/video-ads-upload/<v>/<id> with `offset` and
`file_size` -> status (`bytes_transferred`) -> finish. The bytes are served from the file piece by piece. A drop is
resumed from Meta's offset, never from zero, never as a second video; the chunked path stays only as the fallback.
Everything here is fake: no browser, no Meta.
"""
from __future__ import annotations

import hashlib
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

VA = f"/{ACT}/video_ads"


@pytest.fixture
def resumable(worker_small, monkeypatch):
    """The worker path with small numbers and the resumable protocol on, 1 MB segments."""
    monkeypatch.delenv("ACL_VIDEO_RESUMABLE_UPLOAD", raising=False)
    monkeypatch.setattr(wp, "VIDEO_ADS_SEGMENT_MAX", MB)


class ResumableMeta(ServedMeta):
    """Meta with the video_ads start/finish, the rupload POST (phase 'transfer' of our policy) and the status read."""

    def __init__(self, *, start="ok", faults=(), keep_partial=True, status_zero=False, finish_faults=(), **kw):
        super().__init__(meta_chunk=MB, **kw)
        self.status_zero = status_zero
        self.finish_faults = list(finish_faults)      # per finish: "", "lost" (finished, no answer)
        self.renames: list[tuple[str, dict]] = []
        self.start_mode = start
        self.faults = list(faults)      # per rupload POST: "", "drop" (half kept, no answer), "lost" (all kept,
        #                                 no answer), "nothing" (no byte arrives, no answer), "refused" (Graph error)
        self.keep_partial = keep_partial
        self.va: dict[str, dict] = {}
        self.va_posts: list[tuple[str, dict]] = []

    def write(self, path, params, files=None, filename="", **kw):
        if path.strip("/").isdigit():
            wp.check(ACT, "POST", path, params, files)          # the naming of a video (ACL-2639)
            self.renames.append((path.strip("/"), dict(params)))
            obj = self.objects[path.strip("/")]
            obj["title"], obj["name"] = params["title"], params["name"]
            return {"status": 200, "body": {"success": True}}
        if not path.endswith("/video_ads"):
            return super().write(path, params, files, filename, **kw)
        wp.check(ACT, "POST", path, params, files)
        phase = params["upload_phase"]
        self.va_posts.append((phase, dict(params)))
        if phase == "start":
            if self.start_mode == "refused":
                return {"status": 400, "body": {"error": {"code": 100, "type": "OAuthException",
                                                          "message": "(#100) Unsupported post request"}}}
            short = self._new("video", title=None, _polls=0)
            vid = "7700" + short.zfill(6)                       # a Meta-like id (5+ digits)
            obj = self.objects.pop(short)
            obj["id"] = vid
            self.objects[vid] = obj
            self.va[vid] = {"received": 0, "sha": hashlib.sha256(), "done": False, "size": None}
            url = f"https://rupload.facebook.com/video-ads-upload/v22.0/{vid}"
            if self.start_mode == "bad_url":
                url = f"https://evil.example/video-ads-upload/v22.0/{vid}"
            return {"status": 200, "body": {"video_id": vid, "upload_url": url}}
        sess = self.va[params["video_id"]]
        if phase == "transfer":
            chunk = files[wp.VIDEO_ADS_FIELD]
            assert isinstance(chunk, wp.ServedChunk)
            self.served_sizes.append(len(chunk))
            off = int(params["offset"])
            sess["size"] = int(params["file_size"])
            if off != sess["received"]:
                return {"status": 400, "body": {"error": {"code": 100, "message": "(#100) bad offset"}}}
            fault = self.faults.pop(0) if self.faults else ""
            if fault == "nothing":
                return {"error": "network"}
            if fault == "refused":
                return {"status": 400, "body": {"error": {"code": 6000, "message": "upload failed"}}}
            if fault == "debug":
                # how rupload answers a refusal: a 4xx JSON without a Graph `error` (no byte kept)
                return {"status": 400, "body": {"debug_info": {"type": "OAuthException", "message": "header"}}}
            n = len(chunk) // 2 if fault == "drop" else len(chunk)
            data = chunk.read(0, n)
            sess["received"] += n
            sess["sha"].update(data)
            if fault in ("drop", "lost"):
                return {"error": "network"}
            if not self.keep_partial and sess["received"] < sess["size"]:
                sess["received"] = 0
            return {"status": 200, "body": {"success": True}}
        assert phase == "finish"
        assert sess["received"] == sess["size"], "finish before all bytes arrived"
        sess["done"] = True
        if self.finish_faults and self.finish_faults.pop(0) == "lost":
            return {"error": "network"}
        return {"status": 200, "body": {"success": True}}

    def _video_row(self, obj, params):
        row = super()._video_row(obj, params)
        sess = self.va.get(obj["id"])
        if sess is not None and "status" in row:
            row["status"]["uploading_phase"] = {
                "status": "complete" if sess["size"] and sess["received"] == sess["size"] else "in_progress",
                "bytes_transferred": 0 if self.status_zero else sess["received"]}
        return row


def _phases(meta):
    return [p for p, _ in meta.va_posts]


def test_the_original_goes_up_in_a_few_calls_and_meta_gets_the_same_bytes(resumable, tmp_path):
    d, f = _big_draft(tmp_path)                                   # 3.5 MB, 1 MB segments here (512 MB live)
    meta = ResumableMeta()
    out, client = _run(meta, d)
    assert out.error == "", out.error
    vid, sess = next(iter(meta.va.items()))
    assert sess["done"] and sess["received"] == f.stat().st_size and sess["sha"].hexdigest() == _sha(f)
    assert _phases(meta) == ["start", "transfer", "transfer", "transfer", "transfer", "finish"]
    offsets = [int(p["offset"]) for ph, p in meta.va_posts if ph == "transfer"]
    assert offsets == [0, MB, 2 * MB, 3 * MB] and meta.served_sizes == [MB, MB, MB, 500_000]
    assert not [1 for p, _ in meta.phases if p in ("start", "transfer", "finish")]     # no chunked advideos session
    assert not any("chunks instead" in w for w in out.warnings)
    assert any("resumable upload finished" in s for s in out.steps)


def test_a_dropped_post_resumes_from_the_offset_meta_reports(resumable, tmp_path):
    d, f = _big_draft(tmp_path)
    meta = ResumableMeta(faults=["", "drop"])                     # the 2nd POST breaks after half a segment
    out, _ = _run(meta, d)
    assert out.error == "", out.error
    sess = next(iter(meta.va.values()))
    assert sess["done"] and sess["sha"].hexdigest() == _sha(f)    # every byte once, in order
    offsets = [int(p["offset"]) for ph, p in meta.va_posts if ph == "transfer"]
    assert offsets[:3] == [0, MB, MB + MB // 2]                   # resumed at Meta's offset, not from zero
    assert _phases(meta).count("start") == 1 and len(meta.va) == 1  # never a second video
    assert any("resuming the upload" in s for s in out.steps)


def test_a_lost_answer_is_settled_by_the_status_read_without_resending(resumable, tmp_path):
    d, f = _big_draft(tmp_path)
    meta = ResumableMeta(faults=["lost"])
    out, _ = _run(meta, d)
    assert out.error == "", out.error
    offsets = [int(p["offset"]) for ph, p in meta.va_posts if ph == "transfer"]
    assert offsets == [0, MB, 2 * MB, 3 * MB]                    # the first segment was not sent twice
    assert next(iter(meta.va.values()))["sha"].hexdigest() == _sha(f)


def test_resumes_are_bounded(resumable, tmp_path, monkeypatch):
    monkeypatch.setattr(cr, "upload_calls", lambda size, worker=None: 100)   # the call cap is not what stops here
    d, f = _big_draft(tmp_path)
    meta = ResumableMeta(faults=["", "drop"] + ["refused"] * 10)
    out, _ = _run(meta, d, max_calls=400)
    assert out.stop_kind == "video_error" and "resumes" in out.error
    assert "finish" not in _phases(meta)
    assert sum(1 for p in _phases(meta) if p == "transfer") == 2 + cr.RESUMABLE_MAX_RESUMES


def test_a_refused_start_falls_back_to_chunks_with_the_exact_reason(resumable, tmp_path):
    d, f = _big_draft(tmp_path)
    meta = ResumableMeta(start="refused")
    out, _ = _run(meta, d)
    assert out.error == "", out.error
    warn = [w for w in out.warnings if "resumable upload was not used" in w]
    assert len(warn) == 1 and "Unsupported post request" in warn[0]
    assert [p for p, _ in meta.phases if p == "finish"] == ["finish"]          # the chunked session finished
    assert next(iter(meta.sessions.values()))["sha"].hexdigest() == _sha(f)


def test_a_start_with_a_foreign_upload_address_is_not_used(resumable, tmp_path):
    d, f = _big_draft(tmp_path)
    meta = ResumableMeta(start="bad_url")
    out, _ = _run(meta, d)
    assert out.error == "" and "transfer" not in _phases(meta)
    assert any("no usable video id and upload address" in w for w in out.warnings)


def test_an_address_that_takes_no_byte_falls_back_without_finishing(resumable, tmp_path):
    d, f = _big_draft(tmp_path)
    meta = ResumableMeta(faults=["nothing", "nothing"])            # e.g. CORS / proxy refuses rupload
    out, _ = _run(meta, d)
    assert out.error == "", out.error
    assert "finish" not in _phases(meta)                           # the resumable session is never used
    assert any("no byte reached Meta" in w for w in out.warnings)
    assert next(iter(meta.sessions.values()))["done"]


def test_meta_keeping_none_of_the_segments_stops_instead_of_restarting(resumable, tmp_path):
    d, f = _big_draft(tmp_path)
    meta = ResumableMeta(keep_partial=False)
    out, _ = _run(meta, d, max_calls=400)
    # Meta lost what it had confirmed: stop, never start again from zero
    assert out.stop_kind == "video_error" and "fewer bytes than it had confirmed" in out.error
    assert "finish" not in _phases(meta) and _phases(meta).count("start") == 1


def test_the_calls_stay_within_the_plan_and_single_digits_for_a_large_file(resumable, tmp_path):
    d, f = _big_draft(tmp_path)
    meta = ResumableMeta()
    out, client = _run(meta, d)
    assert out.error == ""
    size = f.stat().st_size
    assert len(meta.va_posts) + 1 <= cr.resumable_calls(size, MB)               # + the one status read
    live = 512 * MB
    assert cr.resumable_calls(2420 * MB, live) == 5 + cr.RESUMABLE_FIXED_CALLS == 8   # 2.4 GB live: 8 calls
    assert cr.resumable_calls(2420 * MB, live) < cr.upload_calls(2420 * MB, True)    # inside the run budget


def test_the_hard_cap_stops_before_the_next_post(resumable, tmp_path, monkeypatch):
    d, f = _big_draft(tmp_path)
    monkeypatch.setattr(cr, "upload_calls", lambda size, worker=None: 1)
    monkeypatch.setattr(cr, "resumable_calls", lambda size, segment=None: 4)
    meta = ResumableMeta()
    out, _ = _run(meta, d, max_calls=400)
    assert out.stop_kind == "video_error" and "more Meta calls than planned" in out.error
    assert len(meta.va_posts) <= 4 and "finish" not in _phases(meta)


def test_the_switch_turns_the_resumable_path_off(resumable, tmp_path, monkeypatch):
    monkeypatch.setenv("ACL_VIDEO_RESUMABLE_UPLOAD", "0")
    d, f = _big_draft(tmp_path)
    meta = ResumableMeta()
    out, _ = _run(meta, d)
    assert out.error == "" and meta.va_posts == []


# ───────────────────────── policy ─────────────────────────

def _chunk(off=0, n=MB):
    class Src:
        def read_range(self, o, k):
            return b"x" * k
    return wp.ServedChunk(Src(), off, n)


def _transfer(**kw):
    p = {"upload_phase": "transfer", "video_id": "1234567", "offset": "0", "file_size": str(4 * MB),
         "upload_version": "v22.0"}
    p.update(kw)
    return p


def test_policy_allows_exactly_the_three_phases():
    assert wp.check(ACT, "POST", VA, {"upload_phase": "start"}) == "video_ads"
    assert wp.check(ACT, "POST", VA, _transfer(), {wp.VIDEO_ADS_FIELD: _chunk()}) == "video_ads"
    assert wp.check(ACT, "POST", VA, {"upload_phase": "finish", "video_id": "1234567"}) == "video_ads"


@pytest.mark.parametrize("params,files", [
    ({}, None),                                                           # no phase
    ({"upload_phase": "start", "file_size": "1"}, None),                  # extra key
    ({"upload_phase": "finish"}, None),                                   # missing video_id
    ({"upload_phase": "finish", "video_id": "1234567", "title": "x"}, None),
    (_transfer(upload_version="v22.0/../x"), "chunk"),
    (_transfer(video_id="../me"), "chunk"),
    (_transfer(offset=str(4 * MB)), "chunk"),                             # offset at the end
    (_transfer(offset="1"), "chunk"),                                     # body not at offset
    (_transfer(), "bytes"),                                               # never staged bytes
    (_transfer(), "big"),                                                 # over the segment cap
    (_transfer(), None),                                                  # no body
    ({"upload_phase": "start"}, "chunk"),                                 # start takes no file
])
def test_policy_refuses_everything_else(params, files):
    f = {"chunk": {wp.VIDEO_ADS_FIELD: _chunk()}, "bytes": {wp.VIDEO_ADS_FIELD: b"x" * 10},
         "big": {wp.VIDEO_ADS_FIELD: _chunk(0, wp.VIDEO_ADS_SEGMENT_MAX + 1)}, None: None}[files]
    if files == "big":
        params = _transfer(file_size=str(wp.VIDEO_ADS_SEGMENT_MAX + 10))
    with pytest.raises(wp.WriteRefused):
        wp.check(ACT, "POST", VA, params, f)


def test_only_the_documented_upload_address_of_this_video_is_accepted():
    assert wp.rupload_version("https://rupload.facebook.com/video-ads-upload/v22.0/1234567", "1234567") == "v22.0"
    for url in ("https://rupload.facebook.com/video-ads-upload/v22.0/1234568",
                "http://rupload.facebook.com/video-ads-upload/v22.0/1234567",
                "https://rupload.facebook.com.evil/video-ads-upload/v22.0/1234567",
                "https://rupload.facebook.com/video-ads-upload/v22.0/1234567?x=1",
                "https://rupload-ber1-1.up.facebook.com/fb_video/1234567", None):
        assert wp.rupload_version(url, "1234567") == ""


# ───────────────────────── the helper in the page (node) ─────────────────────────

def test_the_helper_posts_the_raw_bytes_to_rupload_with_the_documented_headers():
    out = _node({"name": "rupload", "piece": 400_000})
    assert out["ok"]["ok"] is True and out["ok"]["body"] == {"success": True}
    post = out["log"]["rupload"][0]
    assert post["url"] == "https://rupload.facebook.com/video-ads-upload/v22.0/7770001"
    assert post["auth"] == "OAuth TOKEN" and post["offset"] == "0" and post["file_size"] == str(post["bytes"])
    assert post["form"] is False and post["token_in_url"] is False                       # raw body, header token
    assert post["cred"] == "omit" and post["redirect"] == "error"
    from tests.test_acl2629_worker_upload import _SOURCE
    assert post["md5"] == hashlib.md5(_SOURCE).hexdigest()
    assert len(out["log"]["rupload"]) == 1                                               # the refusals sent nothing
    for k, r in out["refused"].items():
        assert r["error"] == "write_refused", k
    # the start / finish phases are ordinary Graph calls (token in the body, Graph host)
    assert out["start"]["ok"] is True
    starts = [x for x in out["log"]["graph"] if x["phase"] == "start"]
    assert starts and starts[0]["url"].endswith(f"/{ACT}/video_ads") and starts[0]["token_in_body"]


def test_a_status_with_no_bytes_after_all_posts_stops_instead_of_restarting(resumable, tmp_path):
    d, f = _big_draft(tmp_path)
    meta = ResumableMeta(status_zero=True)
    out, _ = _run(meta, d, max_calls=400)
    assert out.stop_kind == "video_error" and "kept none" in out.error
    assert "finish" not in _phases(meta) and [int(p["offset"]) for ph, p in meta.va_posts if ph == "transfer"][0] == 0
    assert sum(1 for ph in _phases(meta) if ph == "transfer") == 4                     # no second pass from zero


# ───────────────────────── review B1/B2/N1/N2 ─────────────────────────

def _two_ads_one_file(tmp_path):
    from dataclasses import replace
    d, f = _big_draft(tmp_path)
    ads = d.ad_sets[0].ads
    ads.append(replace(ads[0], creative_id="1001", copy_name="Text1", ad_title="Head 1", ad_body="Body 1"))
    return d, f


def test_the_finished_video_gets_the_acl_tag_name(resumable, tmp_path):
    d, f = _big_draft(tmp_path)
    meta = ResumableMeta()
    out, _ = _run(meta, d)
    assert out.error == ""
    (vid, sess), = meta.va.items()
    assert meta.renames == [(vid, {"name": meta.objects[vid]["title"], "title": meta.objects[vid]["title"]})]
    assert meta.objects[vid]["title"].startswith("acl-")
    # named only after the finish (an unfinished video is never found by the tag)
    assert _phases(meta)[-1] == "finish"


def test_one_file_in_several_ads_goes_up_once(resumable, tmp_path):
    d, f = _two_ads_one_file(tmp_path)
    meta = ResumableMeta()
    out, _ = _run(meta, d)
    assert out.error == "", out.error
    assert _phases(meta).count("start") == 1 and len(meta.va) == 1
    assert any("video already in the ad account" in s for s in out.steps)


def test_publish_again_reuses_the_named_video(resumable, tmp_path):
    d, f = _big_draft(tmp_path)
    meta = ResumableMeta()
    first, _ = _run(meta, d)
    assert first.error == ""
    again, _ = _run(meta, d)
    assert again.error == "" and _phases(meta).count("start") == 1


def test_a_run_stopped_after_the_start_is_continued_not_repeated(resumable, tmp_path, monkeypatch):
    from src.fb_api import operation_journal as oj
    from tests.test_acl1856_operation_journal import _go
    monkeypatch.setattr(cr, "upload_calls", lambda size, worker=None: 100)
    d, f = _big_draft(tmp_path)
    now = [1_000_000.0]
    journal = oj.OperationJournal(tmp_path / "ops", clock=lambda: now[0])
    meta = ResumableMeta(faults=["", "drop"] + ["refused"] * 10)       # stops with 1.5 MB at Meta
    first, _ = _go(meta, d, journal, request_key="r1#t")
    assert first.stop_kind == "video_error" and "finish" not in _phases(meta)
    (vid, sess), = meta.va.items()
    held = sess["received"]
    assert 0 < held < f.stat().st_size
    meta.faults = []
    now[0] += oj.BUSY_S + 60                                                  # "Publish again" a few minutes later
    again, _ = _go(meta, d, journal, request_key="r1#t")
    assert again.error == "", again.error
    assert _phases(meta).count("start") == 1 and len(meta.va) == 1           # the same video, not a second one
    later = [int(p["offset"]) for ph, p in meta.va_posts if ph == "transfer"][-3:]
    assert later[0] == held                                                   # continued at Meta's offset
    assert sess["done"] and sess["sha"].hexdigest() == _sha(f)
    assert any("continuing the upload of an earlier run" in s for s in again.steps)


def test_a_4xx_without_a_graph_error_is_a_refusal_not_progress(resumable, tmp_path):
    d, f = _big_draft(tmp_path)
    meta = ResumableMeta(faults=["", "debug"])
    out, _ = _run(meta, d)
    assert out.error == "", out.error
    sess = next(iter(meta.va.values()))
    assert sess["done"] and sess["sha"].hexdigest() == _sha(f)
    offsets = [int(p["offset"]) for ph, p in meta.va_posts if ph == "transfer"]
    assert offsets[:3] == [0, MB, MB]                                         # the refused segment is sent again
    assert any("did not confirm the bytes (debug_info)" in s for s in out.steps)


def test_a_header_token_refused_on_the_first_post_falls_back_to_chunks(resumable, tmp_path):
    d, f = _big_draft(tmp_path)
    meta = ResumableMeta(faults=["debug", "debug"])
    out, client = _run(meta, d)
    assert out.error == "", out.error
    assert "finish" not in _phases(meta)
    assert any("no byte reached Meta" in w and "debug_info" in w for w in out.warnings)
    assert next(iter(meta.sessions.values()))["done"]                        # the chunked session finished


def test_the_calls_of_a_failed_attempt_are_added_to_the_run_budget(resumable, tmp_path):
    d, f = _big_draft(tmp_path)
    meta = ResumableMeta(faults=["debug", "debug"])
    out, client = _run(meta, d, max_calls=200)
    assert _phases(meta) == ["start", "transfer", "transfer"]
    assert client.max_calls == 200 + 5                                        # start + 2 POSTs + 2 status reads


def test_a_lost_finish_answer_is_sent_once_more(resumable, tmp_path):
    d, f = _big_draft(tmp_path)
    meta = ResumableMeta(finish_faults=["lost"])
    out, _ = _run(meta, d)
    assert out.error == "", out.error
    assert _phases(meta).count("finish") == 2
    assert any("sending the finish once more" in s for s in out.steps)


def test_the_helper_names_only_a_video_it_finished():
    out = _node({"name": "rupload", "piece": 400_000})
    assert out["rename_before"]["error"] == "write_refused"
    assert out["finish"]["ok"] is True and out["rename_after"]["ok"] is True
    for k in ("rename_other", "rename_bad", "rename_extra"):
        assert out[k]["error"] == "write_refused", k
    assert out["proto"] == {"error": "write_refused", "why": "edge"}          # N4: no prototype key is an edge


def test_python_policy_names_a_video_only_with_its_tag():
    t = "acl-tag1-0123456789ab"
    assert wp.check(ACT, "POST", "/7770001", {"name": t, "title": t}) == wp.VIDEO_TITLE_EDGE
    for params in ({"name": t}, {"name": t, "title": t + "x"}, {"name": "Campaign", "title": "Campaign"},
                   {"name": t, "title": t, "status": "ACTIVE"}):
        with pytest.raises(wp.WriteRefused):
            wp.check(ACT, "POST", "/7770001", params)


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
