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


def _mark_deleted(meta, ids):
    """Strict: `status` DELETED is returned for these ids only when `status` is among the requested fields."""
    base = meta._get

    def get(path, params):
        out = base(path, params)
        ident = path.strip("/")
        if ident in ids and isinstance(out, dict) and "error" not in out:
            out = dict(out)
            out.pop("status", None)
            if "status" in str(params.get("fields") or "").split(","):
                out["status"] = "DELETED"
        elif isinstance(out, dict) and isinstance(out.get("data"), list):
            rows = []
            for r in out["data"]:
                r = dict(r)
                r.pop("status", None)
                if r.get("id") in ids and "status" in str(params.get("fields") or "").split(","):
                    r["status"] = "DELETED"
                rows.append(r)
            out = dict(out, data=rows)
        return out

    meta._get = get


def test_receipt_pointing_at_a_deleted_creative_gets_a_new_creative(tmp_path, monkeypatch):
    _images(monkeypatch)
    meta = StrictMeta()
    draft, old = _earlier(tmp_path, meta)
    _mark_deleted(meta, {old["id"]})
    second, _ = _run(meta, draft)
    assert not second.error, second.error
    assert cr.GONE_WARNING in second.warnings
    assert len(adcreative_posts(meta)) == 2 and old["id"] not in second.creative_ids


def test_the_status_field_is_requested_on_creative_reads():
    assert "status" in cr._cr.FIELDS.split(",")


def test_the_log_line_has_code_subcode_and_message_without_raw_ids(tmp_path, monkeypatch, caplog):
    _images(monkeypatch)
    meta = GoneMeta(_MISSING)
    draft, old = _earlier(tmp_path, meta)
    meta.gone_id = old["id"]
    with caplog.at_level("WARNING", logger=cr.log.name):
        second, _ = _run(meta, draft)
    assert not second.error, second.error
    lines = [r.getMessage() for r in caplog.records if "stale creative receipt" in r.getMessage()]
    assert len(lines) == 1
    line = lines[0]
    assert "code=100" in line and "subcode=33" in line and "not found or not accessible" in line
    assert "does not exist" in line
    assert f"creative {old['id']} " not in line and "1234567890" not in line   # the id is masked (…last 4)


def test_a_matching_non_deleted_creative_is_reused_when_the_receipt_one_is_gone(tmp_path, monkeypatch):
    _images(monkeypatch)
    meta = GoneMeta(_MISSING)
    draft, old = _earlier(tmp_path, meta)
    twin_id = meta._store("creative", {k: v for k, v in old.items() if not k.startswith("_") and k != "id"})
    meta.gone_id = old["id"]
    del meta.objects[old["id"]]                            # a really missing creative is not in the listing either
    second, _ = _run(meta, draft)
    assert not second.error, second.error
    assert cr.GONE_WARNING_REUSED in second.warnings
    assert len(adcreative_posts(meta)) == 1                # no new creative was written
    assert second.creative_ids == [twin_id]


def test_a_deleted_twin_is_never_reused(tmp_path, monkeypatch):
    _images(monkeypatch)
    meta = GoneMeta(_MISSING)
    draft, old = _earlier(tmp_path, meta)
    twin_id = meta._store("creative", {k: v for k, v in old.items() if not k.startswith("_") and k != "id"})
    meta.gone_id = old["id"]
    del meta.objects[old["id"]]
    _mark_deleted(meta, {twin_id})
    second, _ = _run(meta, draft)
    assert not second.error, second.error
    assert cr.GONE_WARNING in second.warnings
    assert len(adcreative_posts(meta)) == 2
    assert twin_id not in second.creative_ids and old["id"] not in second.creative_ids


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
    assert not gone(row={"effective_status": "DELETED"})
