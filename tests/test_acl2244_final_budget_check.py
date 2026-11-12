"""ACL-2244: `_prepare` checks the remaining call budget once more, right before the first write."""
from __future__ import annotations

from src.fb_api import creator as cr
from tests.test_acl1761_api_create import FakeMeta
from src.fb_api import graph as g
from src.web.publish_api import create_call_budget
from tests.test_acl1761_api_create import ACT
from tests.test_acl1768_ad_sets import _creator, _draft
from tests.test_acl1775_instant_forms import FORM, FormMeta


def _leave(creator, client, plan, leave: int):
    """Make an earlier preparation step spend calls until exactly `leave` remain at the end of prepare."""
    original = creator._check_meta_rules

    def spend(*a, **k):
        original(*a, **k)
        client.calls = client.max_calls - leave
    creator._check_meta_rules = spend


def test_a_budget_one_call_too_small_stops_before_any_write(tmp_path):
    meta = FakeMeta()
    creator, client = _creator(meta)
    plan = cr.plan_from_draft(_draft(tmp_path))
    need = cr.estimate_create_calls(plan)
    _leave(creator, client, plan, need - 1)
    out = creator.run(plan)
    assert out.stop_kind == "plan" and "1 are missing" in out.error and "Nothing was created" in out.error
    assert meta.posts == []


def test_a_sufficient_budget_changes_nothing(tmp_path):
    meta = FakeMeta()
    creator, client = _creator(meta)
    plan = cr.plan_from_draft(_draft(tmp_path))
    _leave(creator, client, plan, cr.estimate_create_calls(plan))
    out = creator.run(plan)
    assert out.error == "", out.error
    assert out.campaign_id


def _sized(meta, plan, *, spend: int = 0):
    """A creator with the budget the buyer's run gets (`create_call_budget`), and `spend` extra prepare reads."""
    budget, refusal = create_call_budget(plan)
    assert refusal == ""
    client = g.GraphClient(meta, allow_writes=True, sleep=lambda s: None, min_interval_s=0.0, max_calls=budget)
    facts = cr.preflight_account(client, ACT)
    creator = cr.CampaignCreator(client, facts, cr.TargetingResolver(client), sleep=lambda s: None)
    original = creator._check_meta_rules

    def read_more(*a, **k):
        original(*a, **k)
        client.calls += spend                        # stands for reads of prepare outside the targeting reserve
    creator._check_meta_rules = read_more
    return creator, client, budget


def test_a_big_plan_without_interests_still_publishes_when_prepare_spends_a_call_or_two(tmp_path):
    for spend in (1, 2):
        meta = FakeMeta()
        d = _draft(tmp_path, sets=6, ads=6)
        for s in d.ad_sets:
            s.targeting_label = ""
        plan = cr.plan_from_draft(d)
        need = cr.estimate_create_calls(plan)
        assert need > 150 and cr.estimate_targeting_reads(plan) == 0
        creator, client, budget = _sized(meta, plan, spend=spend)
        assert budget == need
        out = creator.run(plan)
        assert out.error == "", out.error
        assert out.campaign_id and meta.posts


def test_an_instant_form_plan_with_30_ads_still_publishes(tmp_path):
    meta = FormMeta()
    meta.next_id = 1000                  # the fake's object ids must not run into the Page id 111
    d = _draft(tmp_path, sets=8, ads=4)                     # 32 ads, every ad set an Instant Form one
    for s in d.ad_sets:
        s.destination = "instant_form"
        for ad in s.ads:
            ad.lead_form_id = FORM
    d.link = "https://example.invalid/l"
    plan = cr.plan_from_draft(d)
    assert cr.estimate_create_calls(plan) > 150
    creator, client, _budget = _sized(meta, plan, spend=2)
    out = creator.run(plan)
    assert out.error == "", out.error
    assert out.campaign_id


def test_a_budget_really_too_small_stops_with_the_message_and_no_post(tmp_path):
    meta = FakeMeta()
    d = _draft(tmp_path, sets=6, ads=6)
    plan = cr.plan_from_draft(d)
    creator, client, budget = _sized(meta, plan, spend=60)          # prepare ate far more than was reserved
    out = creator.run(plan)
    assert out.stop_kind == "plan" and "are missing" in out.error and "Nothing was created" in out.error
    assert meta.posts == []
