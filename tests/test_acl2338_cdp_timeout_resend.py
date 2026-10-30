"""ACL-2338: an in-page write that hits a CDP timeout is reconciled like a network timeout.

Fakes only: no Meta, no Multilogin, no real waits.
"""
import pytest

from src.fb_ads_rpa import run_telemetry as rt
from src.fb_api import graph as g
from tests.test_acl1761_api_create import ACT, FakeMeta, _draft, _run

CDP = {"error": "call_unknown", "reason": "cdp_timeout"}


class CdpMeta(FakeMeta):
    """`cdp_lost`: created, the in-page call never returned. `cdp_unsent`: not created, same timeout."""

    def write(self, path, params, files=None, filename="", **kw):
        edge = path.rsplit("/", 1)[1]
        mode = self.fail_next.get(edge)
        if mode in ("cdp_lost", "cdp_unsent"):
            self.fail_next[edge] = "lost" if mode == "cdp_lost" else "unsent"
            super().write(path, params, files=files, filename=filename, **kw)
            return dict(CDP)
        return super().write(path, params, files=files, filename=filename, **kw)


def _edges(meta, edge):
    return [p for p, _ in meta.posts if p.endswith("/" + edge)]


def test_cdp_timeout_with_both_readbacks_empty_resends_exactly_once(tmp_path):
    meta = CdpMeta()
    meta.fail_next["ads"] = "cdp_unsent"
    out, _ = _run(meta, _draft(tmp_path, ads=1), reconcile_wait_s=0)
    assert not out.error, out.error
    assert len(_edges(meta, "ads")) == 2
    assert len([o for o in meta.objects.values() if o["_kind"] == "ad"]) == 1


def test_cdp_timeout_where_the_ad_exists_is_not_resent(tmp_path):
    meta = CdpMeta()
    meta.fail_next["ads"] = "cdp_lost"
    out, _ = _run(meta, _draft(tmp_path, ads=1), reconcile_wait_s=0)
    assert not out.error, out.error
    assert len(_edges(meta, "ads")) == 1


def test_ad_appearing_only_for_the_second_readback_is_not_resent(tmp_path):
    meta = CdpMeta()
    meta.fail_next["ads"] = "cdp_lost"
    hidden = {"n": 0}
    real_get = meta.request

    def request(method, path, params=None, **kw):
        reply = real_get(method, path, params, **kw)
        if path.endswith("/ads") and hidden["n"] < 1:
            hidden["n"] += 1          # the first readback of the ad list sees nothing yet
            return {"status": 200, "body": {"data": []}}
        return reply

    meta.request = request
    out, _ = _run(meta, _draft(tmp_path, ads=1), reconcile_wait_s=0)
    assert not out.error, out.error
    assert len(_edges(meta, "ads")) == 1


@pytest.mark.parametrize("video", [False, True])
def test_upload_with_a_cdp_timeout_is_never_resent(tmp_path, video):
    meta = CdpMeta()
    edge = "advideos" if video else "adimages"
    meta.fail_next[edge] = "cdp_unsent"
    out, _ = _run(meta, _draft(tmp_path, ads=1, video=video), reconcile_wait_s=0)
    assert out.error and out.stop_kind == "unknown"
    assert len(_edges(meta, edge)) == 1


@pytest.mark.parametrize("reason", ["context_stale", "page_closed"])
def test_stale_context_and_closed_page_name_the_reason_but_are_not_resent(tmp_path, reason):
    class Gone(FakeMeta):
        def write(self, path, params, files=None, filename="", **kw):
            if path.endswith("/ads"):
                super().write(path, params, files=files, filename=filename, **kw)
                return {"error": "call_unknown", "reason": reason}
            return super().write(path, params, files=files, filename=filename, **kw)

    meta = Gone()
    client = g.GraphClient(meta, allow_writes=True, sleep=lambda s: None, min_interval_s=0.0, max_calls=200)
    from src.fb_api import creator as cr
    cr.preflight_account(client, ACT)
    out, _ = _run(meta, _draft(tmp_path, ads=1), reconcile_wait_s=0)
    # the ad exists (lost reply): reconciled by the first readback, exactly one POST, never a resend
    assert not out.error, out.error
    assert len(_edges(meta, "ads")) == 1


def test_write_unknown_carries_the_in_page_reason(tmp_path):
    class Gone(FakeMeta):
        def write(self, path, params, files=None, filename="", **kw):
            return {"error": "call_unknown", "reason": "context_stale"}

    client = g.GraphClient(Gone(), allow_writes=True, sleep=lambda s: None, min_interval_s=0.0, max_calls=200)
    client.write_act = ACT
    with pytest.raises(g.WriteUnknown) as caught:
        client.post(f"/{ACT}/ads", {"name": "x"})
    assert caught.value.reason == "context_stale"


def test_telemetry_row_carries_reason_waits_and_both_readbacks(tmp_path, monkeypatch):
    rows = []
    monkeypatch.setattr(rt, "record", lambda step, ok=None, **kw: rows.append((step, kw.get("diag") or {})))
    meta = CdpMeta()
    meta.fail_next["ads"] = "cdp_unsent"
    out, _ = _run(meta, _draft(tmp_path, ads=1), reconcile_wait_s=0)
    assert not out.error, out.error
    row = [d for s, d in rows if s == "api_create_step" and d.get("reason") == "cdp_timeout"]
    assert len(row) == 1
    assert row[0]["outcome"] == "resend" and row[0]["readback_1"] is False and row[0]["readback_2"] is False
    assert {"waited_s", "cpu_pct", "page_count"} <= set(row[0])
