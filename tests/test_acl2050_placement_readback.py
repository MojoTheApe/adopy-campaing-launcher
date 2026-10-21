"""ACL-2050: после создания проверяем, что Meta оставила ручные площадки (новый макет Advantage+).

Meta здесь - `FakeMeta`; «ответ Meta» подменяем через mutate_on_read. Только чтение, записей не добавилось.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.web import publish_api  # noqa: E402
from tests.test_acl1761_api_create import FakeMeta, _draft, _run  # noqa: E402

KEPT = {"advantage_state": "DISABLED", "advantage_placement_state": "DISABLED",
        "advantage_audience_state": "DISABLED"}


def _go(tmp_path, mutate=None):  # ключи - кортежи (вид объекта, путь)
    meta = FakeMeta()
    meta.mutate_on_read.update(mutate or {})
    out, _ = _run(meta, _draft(tmp_path, ads=1))
    return out, meta


def _fields(out):
    return {(m["object"].split(" ")[0], m["field"]) for m in out.mismatches}


def test_placements_kept_passes(tmp_path):
    out, _ = _go(tmp_path, {("campaign", "advantage_state_info"): KEPT})
    assert out.readback["ok"] is True, out.mismatches
    assert out.readback["diagnostics"]["advantage_placement_state"] == "DISABLED"
    assert publish_api.result_dict(out)["publishable"] is True


def test_placement_state_enabled_stops(tmp_path):
    info = dict(KEPT, advantage_state="ADVANTAGE_PLUS_LEADS", advantage_placement_state="ENABLED")
    out, _ = _go(tmp_path, {("campaign", "advantage_state_info"): info})
    assert ("campaign", "advantage_placement_state") in _fields(out)
    res = publish_api.result_dict(out)
    assert res["publishable"] is False
    assert any("advantage_placement_state" in t and "ENABLED" in t for t in res["blocking_failures"])


def test_positions_missing_stops(tmp_path):
    out, _ = _go(tmp_path, {("adset", "targeting.publisher_platforms"): [],
                              ("adset", "targeting.facebook_positions"): [],
                              ("adset", "targeting.instagram_positions"): []})
    fields = _fields(out)
    assert ("ad", "publisher_platforms") in fields and ("ad", "placements_summary") in fields
    summary = next(m for m in out.mismatches if m["field"] == "placements_summary")
    assert summary["actual"] == "automatic (Advantage+ placements)"
    assert publish_api.result_dict(out)["publishable"] is False


def test_positions_differ_stops(tmp_path):
    out, _ = _go(tmp_path, {("adset", "targeting.facebook_positions"): ["feed"]})
    assert ("ad", "facebook_positions") in _fields(out)
    assert publish_api.result_dict(out)["publishable"] is False


def test_value_rules_and_audience_flag_are_reported_not_blocking(tmp_path):
    info = dict(KEPT, advantage_audience_state="ENABLED")
    out, _ = _go(tmp_path, {("campaign", "advantage_state_info"): info, ("adset", "value_rules_applied"): True})
    assert out.readback["ok"] is True, out.mismatches
    assert out.readback["diagnostics"]["adsets_with_value_rules"] == 1
    assert any("advantage_audience_state" in w for w in out.warnings)
    assert any("value rules" in w for w in out.warnings)


def test_readback_asks_for_the_fields_and_writes_nothing_new(tmp_path):
    out, meta = _go(tmp_path)
    asked = str([p[1].get("fields") for p in meta.gets])
    assert "advantage_state_info" in asked and "value_rules_applied" in asked
    assert [p.rsplit("/", 1)[1] for p, _ in meta.posts][:2] == ["campaigns", "adsets"]
