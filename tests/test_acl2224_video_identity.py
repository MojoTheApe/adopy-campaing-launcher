"""ACL-2224: cloned creative video IDs still need verified file and placement identity."""
from __future__ import annotations

import copy
from pathlib import Path

import pytest

from src.fb_api import creator as cr
from tests.test_acl1761_api_create import FakeMeta, _draft, _run
from tests.test_acl1819_preflight import _mp4


class ClonedVideoMeta(FakeMeta):
    """Replay the captured shape: each ad returns its own video IDs, retaining titles."""
    def __init__(self, fault=""):
        super().__init__()
        self.next_id = 100000
        self.fault = fault
        self.clones = {}

    def _get(self, path, params):
        reply = super()._get(path, params)
        if not path.endswith("/ads"):
            return reply
        for ad in reply.get("data", []):
            creative = ad.get("creative", {})
            rows = creative.get("asset_feed_spec", {}).get("videos", [])
            simple = creative.get("object_story_spec", {}).get("video_data")
            if simple:
                rows = [simple]
            sources = [self.objects[str(row["video_id"])] for row in rows]
            for index, row in enumerate(rows):
                source = str(row["video_id"])
                key = (ad["id"], source)
                if key not in self.clones:
                    title = sources[index].get("title")
                    if self.fault == "wrong_content":
                        title = "different-file"
                    elif self.fault == "missing_title":
                        title = None
                    elif self.fault == "swapped" and len(sources) == 2:
                        title = sources[1-index].get("title")
                    self.clones[key] = self._new("video", title=title, _polls=0)
                row["video_id"] = self.clones[key]
            if self.fault == "missing_binding" and rows:
                rows[0].pop("video_id")
            if self.fault == "wrong_placement":
                creative["asset_feed_spec"]["asset_customization_rules"][0]["customization_spec"] = {}
        return reply


def video_draft(tmp_path, *, pair=True, ads=1):
    draft = _draft(tmp_path, video=True, ads=ads)
    # All ads use the same main file, as in the original multi-ad failure.
    main = tmp_path / "main.mp4"
    main.write_bytes(_mp4(15, 1080, 1080) + b"main")
    vertical = tmp_path / "story.mp4"
    vertical.write_bytes(_mp4(15, 1080, 1920) + b"vertical")
    for ad in draft.ad_sets[0].ads:
        ad.creative_type = "Video"
        ad.creative_path, ad.creative_filename = str(main), main.name
        if pair:
            ad.vertical_path, ad.vertical_filename = str(vertical), vertical.name
    return draft


@pytest.mark.parametrize("pair", [True, False])
def test_verified_clones_pass_first_creation_and_reuse_without_duplicates(tmp_path, pair):
    meta = ClonedVideoMeta()
    draft = video_draft(tmp_path, pair=pair, ads=2)
    out, client = _run(meta, draft)
    assert not out.error and out.readback["ok"], out.error or out.mismatches
    assert len(out.created["ads"]) == 2
    identities = out.readback["diagnostics"]["video_identity"]
    assert len(identities) == (4 if pair else 2)
    assert all(i["uploaded_id"] != i["creative_video_id"] and i["content_verified"] for i in identities)
    assert all(i["verified_by"] == "content_title" for i in identities)
    assert next(o for o in meta.objects.values() if o["_kind"] == "campaign")["status"] == "PAUSED"
    posts = copy.deepcopy(meta.posts)
    second, _ = _run(meta, draft)
    assert not second.error and second.readback["ok"], second.error or second.mismatches
    assert not second.created and meta.posts == posts


@pytest.mark.parametrize("pair", [True, False])
@pytest.mark.parametrize("fault", ["wrong_content", "missing_title", "missing_binding"])
def test_changed_ids_without_file_evidence_remain_blocked(tmp_path, pair, fault):
    out, _ = _run(ClonedVideoMeta(fault), video_draft(tmp_path, pair=pair))
    assert out.error or not out.readback["ok"]
    if not out.error:
        assert any("file" in m["field"] or "identity" in m["field"] or m["field"] == "video_id" for m in out.mismatches)


@pytest.mark.parametrize("fault", ["swapped", "wrong_placement"])
def test_correct_titles_do_not_hide_wrong_slot_or_placement(tmp_path, fault):
    out, _ = _run(ClonedVideoMeta(fault), video_draft(tmp_path))
    assert not out.readback["ok"] and out.mismatches
    if fault == "swapped":
        assert {"main_file_content", "stories_reels_file_content"} <= {m["field"] for m in out.mismatches}
    else:
        assert any(m["field"].startswith("placements_") for m in out.mismatches)


def test_unreadable_clone_title_stops_instead_of_accepting_id(tmp_path):
    meta = ClonedVideoMeta()
    original = meta._get
    def get(path, params):
        if path[1:] in meta.clones.values() and params.get("fields") == "title":
            return {"error": {"code": 100, "message": "title not readable"}}
        return original(path, params)
    meta._get = get
    out, _ = _run(meta, video_draft(tmp_path))
    assert out.error and out.stop_kind == "graph_error"


@pytest.mark.parametrize("fault", ["", "wrong_content", "missing_title"])
def test_sanitized_captured_identity_pattern(tmp_path, fault):
    import json
    capture = json.loads((Path(__file__).parent / "fixtures/ads_manager/fb_api_video_identity_2026_10_26.json").read_text())
    owner = object.__new__(cr.CampaignCreator)
    rows = capture["slots"]
    files = {r["creative_video_id"]: r["file_bytes"].encode() for r in rows}
    def get(path, params):
        title = cr.video_title(capture["tag"], files[path[1:]])
        return {"title": "wrong" if fault == "wrong_content" else None if fault == "missing_title" else title}
    from types import SimpleNamespace
    owner.client = SimpleNamespace(get=get)
    checks, identities = [], []
    def check(obj, field, expected, actual):
        checks.append({"field": field, "ok": expected == actual})
    for row in rows:
        owner._video_file_checks(check, "synthetic-ad", row["slot"], row["slot"] + "_content",
                                 row["uploaded_id"], row["creative_video_id"], capture["tag"],
                                 files[row["creative_video_id"]], identities)
    assert all(c["ok"] for c in checks) == (not fault)
    assert all(i["content_verified"] for i in identities) == (not fault)


def test_changed_id_without_local_file_keeps_strict_id_comparison():
    from types import SimpleNamespace
    owner = object.__new__(cr.CampaignCreator)
    def no_read(*args):
        raise AssertionError("no fingerprint can be derived without local media")
    owner.client = SimpleNamespace(get=no_read)
    checks = []
    owner._video_file_checks(lambda obj, field, expected, actual: checks.append((field, expected == actual)),
                             "ad", "video_id", "video_file", "710001", "720001", "test", b"")
    assert checks == [("video_id", False)]
