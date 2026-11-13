"""ACL-2589: a creative remembered in the receipt that no longer exists in Meta is a stale receipt, not a stop."""
import pytest

from src.fb_api import creator as cr
from tests.test_acl1761_api_create import _draft, _run
from tests.test_acl2284_stale_media import StrictMeta, _earlier, _images, adcreative_posts, creatives


class GoneMeta(StrictMeta):
    """Reading the OLD creative answers like Meta does for a deleted object; other reads are normal."""

    def __init__(self, error, *a, **k):
        super().__init__(*a, **k)
        self.error = error
        self.gone_id = None
        self.gone_reads = 0

    def _get(self, path, params):
        if self.gone_id and path.strip("/") == self.gone_id and "fields" in params:
            self.gone_reads += 1
            return self.error
        return super()._get(path, params)


_MISSING = {"error": {"code": 100, "error_subcode": 33, "type": "GraphMethodException",
                      "message": "Unsupported get request. Object with ID '1234567890123456' does not exist "
                                 "for act_1234567890"}}


def test_receipt_pointing_at_a_missing_creative_gets_a_new_creative(tmp_path, monkeypatch):
    _images(monkeypatch)
    meta = GoneMeta(_MISSING)
    draft, old = _earlier(tmp_path, meta)
    meta.gone_id = old["id"]
    second, _ = _run(meta, draft)
    assert not second.error, second.error
    assert cr.GONE_WARNING in second.warnings
    assert len(adcreative_posts(meta)) == 2 and len(second.ad_ids) == 1
    assert old["id"] not in second.creative_ids and len(second.creative_ids) == 1
    assert meta.gone_reads == 1


def test_receipt_pointing_at_a_deleted_creative_gets_a_new_creative(tmp_path, monkeypatch):
    _images(monkeypatch)
    meta = StrictMeta()
    draft, old = _earlier(tmp_path, meta)
    base = meta._get

    def get(path, params):
        if path.strip("/") == old["id"] and "fields" in params:
            return {"id": old["id"], "status": "DELETED"}
        return base(path, params)

    meta._get = get
    second, _ = _run(meta, draft)
    assert not second.error, second.error
    assert cr.GONE_WARNING in second.warnings
    assert len(adcreative_posts(meta)) == 2 and old["id"] not in second.creative_ids


@pytest.mark.parametrize("error", [
    {"error": {"code": 2, "is_transient": True, "type": "OAuthException", "message": "(#2) Service temporarily unavailable"}},
    {"error": {"code": 100, "type": "OAuthException", "message": "(#100) Invalid parameter"}},
    {"error": {"code": 1, "type": "OAuthException", "message": "An unknown error occurred"}},
])
def test_other_read_errors_keep_todays_handling(tmp_path, monkeypatch, error):
    _images(monkeypatch)
    meta = GoneMeta(error)
    draft, old = _earlier(tmp_path, meta)
    meta.gone_id = old["id"]
    second, _ = _run(meta, draft)
    assert second.error                                   # still stops, as before
    assert cr.GONE_WARNING not in second.warnings
    assert len(adcreative_posts(meta)) == 1 and not second.ad_ids   # nothing new was written


def test_creative_gone_only_for_100_33():
    gone = cr.CampaignCreator._creative_gone
    assert gone(cr.GraphError("x", code=100, subcode=33))
    assert not gone(cr.GraphError("x", code=100, subcode=None))
    assert not gone(cr.GraphError("x", code=4, subcode=33))
    assert not gone(RuntimeError("timeout"))
    assert gone(row={"status": "DELETED"}) and not gone(row={"status": "ACTIVE"})
