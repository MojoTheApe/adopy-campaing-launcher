"""ACL-2244: `_prepare` checks the remaining call budget once more, right before the first write."""
from __future__ import annotations

from src.fb_api import creator as cr
from tests.test_acl1761_api_create import FakeMeta
from tests.test_acl1768_ad_sets import _creator, _draft


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
