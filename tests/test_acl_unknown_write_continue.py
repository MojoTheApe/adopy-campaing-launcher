"""Owner rule 12.11 "publishing beats blocking": an unknown write result is re-sent after an empty readback,
whatever the reason, and one that stays unknown stops only its own campaign.

Fakes only: no Meta, no Multilogin, no real waits.
"""
import pytest

from src.fb_api import creator as cr
from src.fb_api import graph as g
from tests.test_acl1761_api_create import ACT, FakeMeta, _draft, _run

REASONS = ["cdp_timeout", "context_stale", "page_closed", "network", "network_timeout"]


class UnknownMeta(FakeMeta):
    """`unknown[edge] = (reason, n, created)`: the next `n` POSTs to `edge` get no answer."""

    def __init__(self):
        super().__init__()
        self.unknown: dict[str, list] = {}

    def write(self, path, params, files=None, filename="", **kw):
        edge = path.rsplit("/", 1)[1]
        rule = self.unknown.get(edge)
        if rule and rule[1] > 0:
            rule[1] -= 1
            if rule[2]:                                   # the object was created, only the answer is lost
                super().write(path, params, files=files, filename=filename, **kw)
            else:
                self.posts.append((path, dict(params)))
            if rule[0] in ("network", "network_timeout"):  # the transport names these in `error`, not in `reason`
                return {"error": rule[0]}
            return {"error": "call_unknown", "reason": rule[0]}
        return super().write(path, params, files=files, filename=filename, **kw)


def _edges(meta, edge):
    return [p for p, _ in meta.posts if p.endswith("/" + edge)]


@pytest.mark.parametrize("reason", REASONS)
def test_unknown_ad_write_with_empty_readback_is_resent_and_the_run_continues(tmp_path, reason):
    meta = UnknownMeta()
    meta.unknown["ads"] = [reason, 1, False]
    out, _ = _run(meta, _draft(tmp_path, ads=3), reconcile_wait_s=0)
    assert not out.error, out.error
    assert len(_edges(meta, "ads")) == 4                  # one extra POST: the re-send
    assert len(out.ad_ids) == 3


@pytest.mark.parametrize("reason", ["cdp_timeout", "context_stale", "page_closed"])
@pytest.mark.parametrize("video", [False, True])
def test_unknown_upload_is_reconciled_then_resent(tmp_path, reason, video):
    meta = UnknownMeta()
    edge = "advideos" if video else "adimages"
    meta.unknown[edge] = [reason, 1, False]
    out, _ = _run(meta, _draft(tmp_path, ads=1, video=video), reconcile_wait_s=0)
    assert not out.error, out.error
    assert len(_edges(meta, edge)) == 2


def test_unknown_write_where_the_object_exists_is_not_resent(tmp_path):
    meta = UnknownMeta()
    meta.unknown["ads"] = ["cdp_timeout", 1, True]
    out, _ = _run(meta, _draft(tmp_path, ads=1), reconcile_wait_s=0)
    assert not out.error, out.error
    assert len(_edges(meta, "ads")) == 1


def test_creative_cdp_timeout_resends_the_creative(tmp_path):
    """The 17.11 incident: ad 3's creative stopped the whole run."""
    meta = UnknownMeta()
    meta.unknown["adcreatives"] = ["cdp_timeout", 1, False]
    out, _ = _run(meta, _draft(tmp_path, ads=3), reconcile_wait_s=0)
    assert not out.error, out.error
    assert len(out.creative_ids) == 3


def test_a_write_that_stays_unknown_stops_only_its_own_campaign(tmp_path):
    meta = UnknownMeta()
    meta.unknown["ads"] = ["cdp_timeout", 2, False]       # the write and its re-send both get no answer
    first, client = _run(meta, _draft(tmp_path, ads=1), reconcile_wait_s=0)
    assert first.error and first.stop_kind == "unknown"
    assert len(_edges(meta, "ads")) == 2                  # no third POST
    assert first.run_continues is True
    assert client.unreconciled is None                    # the next campaign may write
    facts = cr.preflight_account(client, ACT)
    nxt = cr.CampaignCreator(client, facts, cr.TargetingResolver(client), sleep=lambda s: None, reconcile_wait_s=0)
    plan = cr.plan_from_draft(_draft(tmp_path, ads=2))
    plan.name = "Another campaign"
    second = nxt.run(plan)
    assert not second.error, second.error
    assert len(second.ad_ids) == 2


def test_a_definite_meta_answer_is_not_resent(tmp_path):
    meta = FakeMeta()
    meta.fail_next["ads"] = "unsent"                      # call_unknown without a reason: Meta said something unclear
    out, _ = _run(meta, _draft(tmp_path, ads=1), reconcile_wait_s=0)
    assert out.error and out.stop_kind == "unknown"
    assert getattr(out, "run_continues", False) is False
    assert len(_edges(meta, "ads")) == 1


def test_publish_session_does_not_block_after_a_run_that_continues():
    from src.web import publish_api
    src = open(publish_api.__file__, encoding="utf-8").read()
    assert "run_continues" in src


def test_a_failed_readback_still_blocks_the_run(tmp_path):
    meta = UnknownMeta()
    meta.unknown["ads"] = ["cdp_timeout", 1, False]
    real = meta.request

    def request(method, path, params=None, **kw):
        if path.endswith("/ads") and _edges(meta, "ads"):
            raise g.PageFailed("tab gone")
        return real(method, path, params, **kw)

    meta.request = request
    out, client = _run(meta, _draft(tmp_path, ads=1), reconcile_wait_s=0)
    assert out.error and out.stop_kind == "unknown"
    assert getattr(out, "run_continues", False) is False
    assert client.unreconciled is not None
