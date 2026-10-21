"""ACL-1050: сверка ловит кампанию, которую Meta перевела на Advantage+ площадки.

Живой тестовый кабинет, 20-21.10: кампания из Ads Manager пришла как ADVANTAGE_PLUS_LEADS,
advantage_placement_state ENABLED, без publisher_platforms и с value rules
(Audience Network получает бюджет). Кампания билдера через API на том же
кабинете - DISABLED с ручным списком. Здесь Meta - `FakeMeta`.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_api import creator as cr  # noqa: E402
from tests.test_acl1761_api_create import FakeMeta, _draft, _run  # noqa: E402

OFF = {"advantage_state": "DISABLED", "advantage_budget_state": "ENABLED",
       "advantage_audience_state": "DISABLED", "advantage_placement_state": "DISABLED"}


def _meta(**state):
    meta = FakeMeta()
    meta.mutate_on_read[("campaign", "advantage_state_info")] = {**OFF, **state}
    return meta


def _bad(out):
    return {(m["object"], m["field"]): m for m in out.mismatches}


def test_the_readback_asks_meta_for_the_advantage_state_and_value_rules():
    assert "advantage_state_info" in cr.CAMPAIGN_FIELDS.split(",")
    assert "value_rules_applied" in cr.ADSET_FIELDS.split(",")


def test_builder_campaign_with_manual_placements_passes(tmp_path):
    out, _ = _run(_meta(), _draft(tmp_path, ads=1))
    assert out.error == "" and out.readback["ok"] is True
    ok = {c["field"] for c in out.readback["checks"] if c["ok"]}
    assert {"advantage_campaign", "advantage_placements", "value_rules"} <= ok


def test_advantage_placements_turned_on_by_meta_is_a_mismatch(tmp_path):
    out, _ = _run(_meta(advantage_placement_state="ENABLED"), _draft(tmp_path, ads=1))
    m = _bad(out)[("campaign", "advantage_placements")]
    assert (m["expected"], m["actual"]) == ("DISABLED", "ENABLED")
    assert cr._describe_diffs([m]) == ["Advantage+ placements (builder: DISABLED, Meta: ENABLED)"]


def test_advantage_plus_leads_campaign_is_a_mismatch(tmp_path):
    out, _ = _run(_meta(advantage_state="ADVANTAGE_PLUS_LEADS"), _draft(tmp_path, ads=1))
    m = _bad(out)[("campaign", "advantage_campaign")]
    assert m["actual"] == "ADVANTAGE_PLUS_LEADS" and out.readback["ok"] is False


def test_no_advantage_state_in_the_answer_skips_only_that_check(tmp_path):
    out, _ = _run(FakeMeta(), _draft(tmp_path, ads=1))
    fields = {c["field"] for c in out.readback["checks"]}
    assert out.readback["ok"] is True
    assert not {"advantage_campaign", "advantage_placements"} & fields
    assert {"publisher_platforms", "facebook_positions", "instagram_positions"} <= fields


def test_positions_missing_on_the_ad_set_is_a_mismatch(tmp_path):
    meta = _meta()
    for key in ("publisher_platforms", "facebook_positions", "instagram_positions"):
        meta.mutate_on_read[("adset", f"targeting.{key}")] = None
    out, _ = _run(meta, _draft(tmp_path, ads=1))
    obj = next(m["object"] for m in out.mismatches if m["field"] == "publisher_platforms")
    assert {"publisher_platforms", "facebook_positions", "instagram_positions"} <= {
        m["field"] for m in out.mismatches if m["object"] == obj}
    assert cr._describe_diffs([m for m in out.mismatches if m["object"] == obj]) == [
        "placements (Advantage+ vs manual list)"]


def test_value_rules_attached_by_meta_is_a_mismatch(tmp_path):
    meta = _meta()
    meta.mutate_on_read[("adset", "value_rules_applied")] = True
    out, _ = _run(meta, _draft(tmp_path, ads=1))
    m = next(m for m in out.mismatches if m["field"] == "value_rules")
    assert cr._describe_diffs([m]) == ["placement value rules (builder: off, Meta: on)"]


def test_an_earlier_advantage_plus_campaign_is_not_reused(tmp_path):
    meta = FakeMeta()
    out, _ = _run(meta, _draft(tmp_path, ads=1))
    assert out.error == "", out.error
    camp = next(o for o in meta.objects.values() if o["_kind"] == "campaign")
    camp["advantage_state_info"] = {**OFF, "advantage_state": "ADVANTAGE_PLUS_LEADS",
                                    "advantage_placement_state": "ENABLED"}
    out2, _ = _run(meta, _draft(tmp_path, ads=2))
    assert out2.error == "" and out2.published_as == "MX Leads 1_2"
    assert any("Advantage+ campaign (builder: DISABLED, Meta: ADVANTAGE_PLUS_LEADS)" in w
               and "Advantage+ placements (builder: DISABLED, Meta: ENABLED)" in w for w in out2.warnings)
