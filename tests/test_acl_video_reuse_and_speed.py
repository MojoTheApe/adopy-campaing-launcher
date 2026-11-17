"""Video uploads: reuse by content hash per ad account (any campaign tag) and size / MB/s in the live status + run log."""
from __future__ import annotations

import itertools
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.fb_ads_rpa import run_telemetry as rt  # noqa: E402
from src.fb_api import creator as cr  # noqa: E402
from tests.test_acl1761_api_create import ACT, FakeMeta, _draft, _run  # noqa: E402


def _video_rows(rows):
    return [(ok, kw) for step, ok, kw in rows if (kw.get("diag") or {}).get("metric") == "video_upload"]


def test_video_from_another_campaign_tag_is_reused_by_content_hash(tmp_path, monkeypatch):
    rows: list = []
    monkeypatch.setattr(rt, "record", lambda step, ok=None, **kw: rows.append((step, ok, kw)))
    meta = FakeMeta()
    draft = _draft(tmp_path, video=True, ads=3)
    data = (tmp_path / "1002_clip.mp4").read_bytes()
    # uploaded earlier by ANOTHER campaign: its own tag in the title, same content hash
    meta._new("video", title=f"acl-othercampaign-{cr._md5(data)[:12]}", _polls=0)
    out, _client = _run(meta, draft)
    assert out.error == "", out.error
    assert not any(p.endswith("/advideos") for p, _ in meta.posts), "no second upload of the same content"
    assert [kw["diag"]["mode"] for _ok, kw in _video_rows(rows)] == ["found"]


def test_other_content_is_not_mistaken_for_a_reusable_video(tmp_path):
    meta = FakeMeta()
    meta._new("video", title="acl-othercampaign-000000000000", _polls=0)
    out, _client = _run(meta, _draft(tmp_path, video=True, ads=3))
    assert out.error == ""
    assert any(p.endswith("/advideos") for p, _ in meta.posts)


def test_upload_reports_size_duration_and_throughput(tmp_path, monkeypatch):
    rows: list = []
    monkeypatch.setattr(rt, "record", lambda step, ok=None, **kw: rows.append((step, ok, kw)))
    ticks = itertools.count(0, 10)           # every clock() call is 10 s later
    meta = FakeMeta()
    from src.fb_api import graph as g

    client = g.GraphClient(meta, allow_writes=True, sleep=lambda s: None, min_interval_s=0.0, max_calls=200)
    facts = cr.preflight_account(client, ACT)
    creator = cr.CampaignCreator(client, facts, cr.TargetingResolver(client), sleep=lambda s: None,
                                 clock=lambda: float(next(ticks)))
    out = creator.run(cr.plan_from_draft(_draft(tmp_path, video=True, ads=3)))
    assert out.error == "", out.error
    ok, kw = _video_rows(rows)[0]
    assert kw["diag"]["upload_s"] > 0 and "mb_s" in kw["diag"]
    line = next(s for s in out.steps if "video uploaded" in s)
    assert "MB/s" in line and " MB" in line and "s" in line.split("(")[-1]


def test_found_video_step_names_the_content_match(tmp_path):
    meta = FakeMeta()
    draft = _draft(tmp_path, video=True, ads=3)
    data = (tmp_path / "1002_clip.mp4").read_bytes()
    meta._new("video", title=f"acl-x-{cr._md5(data)[:12]}", _polls=0)
    out, _client = _run(meta, draft)
    assert any("video already in the ad account" in s for s in out.steps)
