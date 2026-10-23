"""ACL-1897 keeps Page identity readback; all Graph operations use FakeMeta."""
from tests.test_acl1761_api_create import FakeMeta, _draft, _run


def test_creative_page_changed_by_meta_is_reported_as_a_readback_mismatch(tmp_path):
    meta = FakeMeta()
    meta.mutate_ads["creative.object_story_spec.page_id"] = "999"
    draft = _draft(tmp_path, ads=1)
    out, _ = _run(meta, draft)
    mismatches = [m for m in out.mismatches if m["field"] == "page"]
    assert len(mismatches) == 1
    assert mismatches[0]["expected"] == draft.page_id
    assert mismatches[0]["actual"] == "999"
