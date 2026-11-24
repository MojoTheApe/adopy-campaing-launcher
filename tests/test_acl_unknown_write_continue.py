"""Owner rule 12.11 "publishing beats blocking": an unknown write result is re-sent after an empty readback,
whatever the reason, and one that stays unknown stops only its own campaign.

Fakes only: no Meta, no Multilogin, no real waits.
"""
import pytest

from src.fb_api import creator as cr
from src.fb_api import graph as g
from tests.test_acl1761_api_create import ACT, FakeMeta, _draft, _run
from tests.test_acl1856_operation_journal import clock, journal  # noqa: F401 (fixtures)

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
            if not rule[0]:                                # the page helper's bare reply: no reason
                return {"error": "call_unknown"}
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


def test_a_reasonless_unknown_is_resent_once_then_stops_only_its_campaign(tmp_path):
    meta = UnknownMeta()
    meta.unknown["ads"] = ["", 2, False]               # bare call_unknown: no reason at all
    out, client = _run(meta, _draft(tmp_path, ads=1), reconcile_wait_s=0)
    assert out.error and out.stop_kind == "unknown" and out.run_continues is True
    assert len(_edges(meta, "ads")) == 2 and client.unreconciled is None


class MetaFiveHundred(UnknownMeta):
    """Meta answers the write with HTTP 500 / code 2 (an unclear answer): the object may or may not exist."""

    def write(self, path, params, files=None, filename="", **kw):
        edge = path.rsplit("/", 1)[1]
        rule = self.unknown.get(edge)
        if rule and rule[1] > 0:
            rule[1] -= 1
            self.posts.append((path, dict(params)))
            return {"status": 500, "body": {"error": {"code": 2, "message": "Service temporarily unavailable"}}}
        return super().write(path, params, files=files, filename=filename, **kw)


def test_meta_5xx_is_resent_once_after_empty_readbacks(tmp_path):
    meta = MetaFiveHundred()
    meta.unknown["ads"] = ["", 1, False]
    out, _ = _run(meta, _draft(tmp_path, ads=2), reconcile_wait_s=0)
    assert not out.error, out.error
    assert len(_edges(meta, "ads")) == 3


def test_meta_5xx_that_repeats_stops_only_its_campaign(tmp_path):
    meta = MetaFiveHundred()
    meta.unknown["ads"] = ["", 2, False]
    out, client = _run(meta, _draft(tmp_path, ads=1), reconcile_wait_s=0)
    assert out.error and out.run_continues is True and client.unreconciled is None
    assert len(_edges(meta, "ads")) == 2


def test_a_definite_meta_error_with_a_real_code_is_not_resent(tmp_path):
    class Refuse(UnknownMeta):
        def write(self, path, params, files=None, filename="", **kw):
            if path.endswith("/ads"):
                self.posts.append((path, dict(params)))
                return {"status": 400, "body": {"error": {"code": 100, "message": "Invalid parameter"}}}
            return super().write(path, params, files=files, filename=filename, **kw)

    meta = Refuse()
    out, _ = _run(meta, _draft(tmp_path, ads=1), reconcile_wait_s=0)
    assert out.error and out.stop_kind == "graph_error"
    assert len(_edges(meta, "ads")) == 1


def test_an_answer_without_an_id_is_resent_once(tmp_path):
    class NoId(UnknownMeta):
        n = 0

        def write(self, path, params, files=None, filename="", **kw):
            if path.endswith("/ads") and self.n == 0:
                self.n += 1
                self.posts.append((path, dict(params)))
                return {"status": 200, "body": {}}
            return super().write(path, params, files=files, filename=filename, **kw)

    meta = NoId()
    out, _ = _run(meta, _draft(tmp_path, ads=1), reconcile_wait_s=0)
    assert not out.error, out.error
    assert len(_edges(meta, "ads")) == 2


@pytest.mark.parametrize("video", [False, True])
def test_real_upload_failure_replies_without_a_reason_are_resent(tmp_path, video):
    """The page helper's real shape: a startCall failure or a stale poll is a bare {"error": "call_unknown"}."""
    meta = UnknownMeta()
    edge = "advideos" if video else "adimages"
    meta.unknown[edge] = ["", 1, False]
    out, _ = _run(meta, _draft(tmp_path, ads=1, video=video), reconcile_wait_s=0)
    assert not out.error, out.error
    assert len(_edges(meta, edge)) == 2


def test_a_stalled_upload_is_resent(tmp_path):
    meta = UnknownMeta()
    meta.unknown["adimages"] = ["stalled", 1, False]
    out, _ = _run(meta, _draft(tmp_path, ads=1), reconcile_wait_s=0)
    assert not out.error, out.error
    assert len(_edges(meta, "adimages")) == 2


# -- criterion 3: the next run adopts what the re-send left ------------------------------------------------
def _journal_go(meta, draft, journal):
    from tests.test_acl1856_operation_journal import _go
    return _go(meta, draft, journal)


def test_next_run_after_an_unresolved_unknown_creates_no_second_ad(tmp_path, journal):
    meta = UnknownMeta()
    meta.unknown["ads"] = ["cdp_timeout", 2, False]
    first, _ = _journal_go(meta, _draft(tmp_path, ads=1), journal)
    assert first.run_continues is True
    again, _ = _journal_go(meta, _draft(tmp_path, ads=1), journal)
    assert again.error == ""
    assert len([o for o in meta.objects.values() if o["_kind"] == "ad"]) == 1


def test_next_run_after_a_duplicate_from_the_resend_adopts_it(tmp_path, journal):
    meta = UnknownMeta()
    meta.unknown["ads"] = ["cdp_timeout", 1, True]    # created, answer lost
    real = meta.request

    def request(method, path, params=None, **kw):     # the first readbacks do not see the ad yet
        if method == "GET" and path.endswith("/ads") and len(_edges(meta, "ads")) < 2:
            return {"status": 200, "body": {"data": []}}
        return real(method, path, params, **kw)

    meta.request = request
    first, _ = _journal_go(meta, _draft(tmp_path, ads=1), journal)
    assert first.error == ""
    assert len([o for o in meta.objects.values() if o["_kind"] == "ad"]) == 2    # a duplicate, accepted
    posts = len(meta.posts)
    again, _ = _journal_go(meta, _draft(tmp_path, ads=1), journal)
    assert again.error == "" and again.created == {} and len(meta.posts) == posts


# -- the run-level block -----------------------------------------------------------------------------------
def test_blocks_run_only_when_the_unknown_stop_did_not_survive_a_resend():
    from types import SimpleNamespace as NS
    from src.web.publish_api import blocks_run

    assert blocks_run(NS(stop_kind="unknown", run_continues=False), NS(unreconciled=None)) is True
    assert blocks_run(NS(stop_kind="unknown", run_continues=True), NS(unreconciled=None)) is False
    assert blocks_run(NS(stop_kind="unknown", run_continues=True), NS(unreconciled="/act/ads")) is True
    assert blocks_run(NS(stop_kind="graph_error", run_continues=False), NS(unreconciled=None)) is False


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
