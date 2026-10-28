from pathlib import Path

import pytest

from src.fb_api import creator as cr
from tests.test_acl1761_api_create import FakeMeta, _run
from tests.test_acl1774_texts_vertical import _ad_with, _creative, png


@pytest.mark.parametrize("alias", [False, True])
def test_same_portrait_file_is_one_asset_with_a_visible_warning(tmp_path, alias):
    draft = _ad_with(tmp_path, main=(1080, 1920), vertical=None)
    ad = draft.ad_sets[0].ads[0]
    ad.vertical_filename = Path(ad.creative_path).name
    if alias:
        (tmp_path / "alias").mkdir()
        ad.vertical_path = str(tmp_path / "alias") + "/../" + ad.vertical_filename
    else:
        ad.vertical_path = ad.creative_path
    plan = cr.plan_from_draft(draft)
    assert plan.ad_sets[0].ads[0].vertical_path == ""
    assert any("same file" in text.lower() for text in plan.warnings)
    meta = FakeMeta()
    out, _ = _run(meta, draft)
    assert out.error == "" and out.readback["ok"], out.mismatches
    assert any("same file" in text.lower() for text in out.warnings)
    assert len([path for path, _ in meta.posts if path.endswith("/adimages")]) == 1
    assert "asset_feed_spec" not in _creative(meta)


def test_same_file_does_not_turn_multiple_texts_into_a_media_pair(tmp_path):
    draft = _ad_with(tmp_path, vertical=None, texts=1)
    ad = draft.ad_sets[0].ads[0]
    ad.vertical_path, ad.vertical_filename = ad.creative_path, Path(ad.creative_path).name
    plan = cr.plan_from_draft(draft)
    assert plan.ad_sets[0].ads[0].multi_text and not plan.ad_sets[0].ads[0].vertical_path
    meta = FakeMeta()
    out, _ = _run(meta, draft)
    assert out.error == "" and out.readback["ok"], out.mismatches
    feed = _creative(meta)["asset_feed_spec"]
    assert len(feed["images"]) == 1 and "asset_customization_rules" not in feed


def test_distinct_paths_with_the_same_basename_stay_a_real_pair(tmp_path):
    draft = _ad_with(tmp_path, vertical=None)
    ad = draft.ad_sets[0].ads[0]
    other = tmp_path / "other" / Path(ad.creative_path).name
    other.parent.mkdir()
    other.write_bytes(png(1080, 1920, "distinct"))
    ad.vertical_path, ad.vertical_filename = str(other), other.name
    plan = cr.plan_from_draft(draft)
    assert plan.ad_sets[0].ads[0].vertical_path == str(other)
    assert not any("same file" in text.lower() for text in plan.warnings)
    meta = FakeMeta()
    out, _ = _run(meta, draft)
    assert out.error == "" and out.readback["ok"], out.mismatches
    assert len(_creative(meta)["asset_feed_spec"]["images"]) == 2


def test_single_portrait_main_stays_on_the_existing_single_asset_path(tmp_path):
    meta = FakeMeta()
    out, _ = _run(meta, _ad_with(tmp_path, main=(1080, 1920), vertical=None))
    assert out.error == "" and out.readback["ok"], out.mismatches
    assert "asset_feed_spec" not in _creative(meta)
