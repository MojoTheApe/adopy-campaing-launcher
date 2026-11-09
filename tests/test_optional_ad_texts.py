"""Основной текст и заголовок необязательны: Check plan не блокирует, креатив уходит без пустых полей."""
from types import SimpleNamespace

import pytest

from src.fb_api import preflight_rules as pf
from src.fb_api.creator import AdPlan, CampaignCreator

PLAN = SimpleNamespace(page_id="1", link="https://example.com/?a=1")


def _ad(kind, **kw):
    base = dict(index=1, name="n", kind=kind, path="f", title="", body="", description="")
    base.update(kw)
    return AdPlan(**base)


@pytest.mark.parametrize("kind,data_key,body_key,title_key",
                         [("image", "link_data", "message", "name"), ("video", "video_data", "message", "title")])
def test_empty_texts_are_not_sent(kind, data_key, body_key, title_key):
    media = {"image_hash": "h", "video_id": "v", "thumb": "t"}
    data = CampaignCreator.story_spec(PLAN, _ad(kind), media)[data_key]
    assert body_key not in data and title_key not in data
    full = CampaignCreator.story_spec(PLAN, _ad(kind, title="H", body="B"), media)[data_key]
    assert full[body_key] == "B" and full[title_key] == "H"


def test_missing_texts_only_warn():
    ads = [{"name": "a", "file": "x.jpg", "body": "", "headline": "", "cta": "LEARN_MORE"}]
    got = {v["rule"]: v for v in pf.evaluate_client({
        "destination": "website", "objective": "leads", "link": "https://e.com/?a=1", "country": "US",
        "budget": 10, "age_min": 18, "age_max": 65, "ads": ads})}
    assert got["primary_text"]["status"] == pf.WARNING and got["headline"]["status"] == pf.WARNING
    assert not any(v["status"] == pf.ERROR for v in got.values())


def test_text_free_ad_gives_no_error():
    ads = [{"name": "a", "file": "x.jpg", "cta": "LEARN_MORE"}]
    got = {v["rule"]: v for v in pf.evaluate_client({
        "destination": "website", "objective": "leads", "link": "https://e.com/?a=1", "country": "US",
        "budget": 10, "age_min": 18, "age_max": 65, "ads": ads})}
    assert got["feed_texts"]["status"] == pf.OK
    assert not any(v["status"] == pf.ERROR for v in got.values())


def test_policy_accepts_a_feed_without_bodies_when_headline_present():
    """ACL-2474: Meta приняла ленту без основного текста, но с заголовком (живой прогон 08.11, job 893dd606)."""
    from src.fb_api import write_policy as wp
    creative = {"object_story_spec": {"page_id": "1"}}
    for spec in ({"titles": [{"text": "H"}], "images": [{"hash": "a" * 32}]},):
        spec.update({"call_to_action_types": ["LEARN_MORE"], "ad_formats": ["SINGLE_IMAGE"]})
        wp._check_asset_feed(spec, creative)


@pytest.mark.parametrize("bodies", [[], [{"text": " "}]])
def test_policy_refuses_present_but_empty_bodies(bodies):
    from src.fb_api import write_policy as wp
    spec = {"bodies": bodies, "titles": [{"text": "H"}], "images": [{"hash": "h"}]}
    with pytest.raises(wp.WriteRefused, match="bodies"):
        wp._check_asset_feed(spec, {"object_story_spec": {"page_id": "1"}})


@pytest.mark.parametrize("spec", [
    {"bodies": [{"text": "B"}], "titles": [], "images": [{"hash": "h"}]},
    {"bodies": [{"text": "B"}], "images": [{"hash": "h"}]},
    {"images": [{"hash": "h"}]},
])
def test_policy_still_refuses_a_feed_without_titles(spec):
    """Не проверено вживую (ACL-2474): основной текст без заголовка и лента без текстов остаются отказом."""
    from src.fb_api import write_policy as wp
    with pytest.raises(wp.WriteRefused, match="titles"):
        wp._check_asset_feed(spec, {"object_story_spec": {"page_id": "1"}})
