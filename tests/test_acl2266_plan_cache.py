"""ACL-2266: кэш вердиктов Check plan на сервере — ключ, что кладём, срок, вытеснение, сброс."""
from __future__ import annotations

import copy
import os

import pytest

from src.web import plan_cache as pc
from tests.test_acl1761_api_create import ACT, _draft


def _row(name="MX Leads 1", **over):
    row = {"campaign_name": name, "success": True, "error": None, "dry_run": True,
           "steps_completed": [pc.PASSED_STEP], "warnings": ["w"], "targeting": [],
           "audit": [{"rule": "account_active", "status": "ok", "detail": "", "ads": []}]}
    row.update(over)
    return row


def _put(row=None, name="MX Leads 1", digest="d1", acct="Acc", country="MX", act=ACT):
    return pc.put(acct, country, act, digest, name, _row(name) if row is None else row)


# ───────────────────────── digest ─────────────────────────

def test_digest_is_stable_for_repeated_builds_of_the_same_draft(tmp_path):
    d = _draft(tmp_path)
    assert pc.draft_digest(d) == pc.draft_digest(copy.deepcopy(d)) == pc.draft_digest(d)


@pytest.mark.parametrize("edit", [
    lambda d: setattr(d, "link", "https://example.invalid/other"),
    lambda d: setattr(d, "pixel_id", "999"),
    lambda d: setattr(d, "page_id", "222"),
    lambda d: setattr(d, "language", "Portuguese"),
    lambda d: setattr(d.ad_sets[0], "targeting_label", "Forex"),
    lambda d: setattr(d.ad_sets[0], "daily_budget", 25.0),
    lambda d: setattr(d.ad_sets[0].ads[0], "ad_body", "Edited library text"),
    lambda d: setattr(d.ad_sets[0].ads[1], "ad_title", "Edited headline"),
    lambda d: d.warnings.append("new warning"),
])
def test_digest_changes_with_any_enriched_draft_field(tmp_path, edit):
    d = _draft(tmp_path)
    before = pc.draft_digest(d)
    edit(d)
    assert pc.draft_digest(d) != before


def test_digest_changes_when_a_creative_file_is_replaced_under_the_same_name(tmp_path):
    d = _draft(tmp_path)
    before = pc.draft_digest(d)
    path = d.ad_sets[0].ads[0].creative_path
    with open(path, "wb") as fh:
        fh.write(b"a different, longer video body")
    assert pc.draft_digest(d) != before
    stat = os.stat(path)
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
    after_mtime = pc.draft_digest(d)
    os.remove(path)
    assert pc.draft_digest(d) not in (before, after_mtime), "a missing file must not look like the old one"


def test_digest_ignores_dict_key_order():
    assert pc.draft_digest({"a": 1, "b": 2}) == pc.draft_digest({"b": 2, "a": 1})


def test_key_changes_with_act_country_account_and_day():
    base = pc.key_for("Acc", "MX", "act_1", "d")
    assert pc.key_for("acc", "mx", "ACT_1", "d") == base          # регистр не важен
    assert pc.key_for("Acc", "MX", "act_2", "d") != base
    assert pc.key_for("Acc", "BR", "act_1", "d") != base
    assert pc.key_for("Other", "MX", "act_1", "d") != base
    assert pc.key_for("Acc", "MX", "act_1", "d", today="2000-01-01") != base
    assert pc.key_for("Acc", "MX", "act_1", "d2") != base


def test_the_key_carries_the_schema_version(monkeypatch):
    a = pc.key_for("Acc", "MX", "act_1", "d")
    monkeypatch.setattr(pc, "SCHEMA", pc.SCHEMA + 1)
    assert pc.key_for("Acc", "MX", "act_1", "d") != a


def test_unique_names():
    class D:
        def __init__(self, n):
            self.name = n

    assert pc.unique_names([D("a"), D("b")])
    assert not pc.unique_names([D("a"), D("a")])
    assert not pc.unique_names([D("")]) and not pc.unique_names([])


# ───────────────────────── что кладём ─────────────────────────

def test_a_clean_row_is_stored_and_served_with_cached_and_checked_at():
    assert _put() == ""
    hit = pc.get("Acc", "MX", ACT, "d1")
    assert hit["cached"] is True and hit["checked_at"] and hit["campaign_name"] == "MX Leads 1"
    assert hit["warnings"] == ["w"], "предупреждения — часть вердикта"
    assert pc.get("Acc", "MX", ACT, "other") is None


@pytest.mark.parametrize("over,reason", [
    ({"error": "Name taken", "success": False}, "error"),
    ({"error": "boom"}, "error"),
    ({"success": False}, "error"),
    ({"steps_completed": []}, "not_passed"),
    ({"audit": []}, "audit_empty"),
    ({"audit": None}, "audit_empty"),
    ({"audit": [{"rule": "x", "status": "ok"}, {"rule": "y", "status": "error"}]}, "audit_error"),
    ({"targeting": [{"term": "t", "state": "pending"}]}, "pending_terms"),
    ({"targeting": [{"term": "t", "state": "unresolved"}]}, "pending_terms"),
    ({"targeting": [{"term": "t", "state": "unknown"}]}, "pending_terms"),
    ({"campaign_name": "Another"}, "name_mismatch"),
])
def test_a_row_without_positive_evidence_is_rejected_with_a_reason(over, reason):
    assert pc.put("Acc", "MX", ACT, "d1", "MX Leads 1", _row(**over)) == reason
    assert pc.get("Acc", "MX", ACT, "d1") is None
    assert pc.counters()["rejected"] == {reason: 1}


def test_resolved_terms_do_not_block_caching():
    assert _put(_row(targeting=[{"term": "t", "state": "ok"}, {"term": "u", "state": "confirmed"}])) == ""


def test_rows_are_deep_copied_in_and_out():
    row = _row()
    assert _put(row) == ""
    row["warnings"].append("mutated after put")
    first = pc.get("Acc", "MX", ACT, "d1")
    first["warnings"].append("mutated after get")
    assert pc.get("Acc", "MX", ACT, "d1")["warnings"] == ["w"]


# ───────────────────────── срок, выключатель, потолок ─────────────────────────

def test_ttl_default_is_60_minutes_and_clamped(monkeypatch):
    monkeypatch.delenv(pc.TTL_ENV, raising=False)
    assert pc.ttl_s() == 3600.0
    monkeypatch.setenv(pc.TTL_ENV, "999999")
    assert pc.ttl_s() == 24 * 3600.0
    monkeypatch.setenv(pc.TTL_ENV, "garbage")
    assert pc.ttl_s() == 3600.0
    monkeypatch.setenv(pc.TTL_ENV, "-5")
    assert pc.ttl_s() == 0.0


def test_a_row_expires_after_the_ttl(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(pc.time, "monotonic", lambda: clock[0])
    monkeypatch.setenv(pc.TTL_ENV, "10")
    _put()
    clock[0] += 599
    assert pc.get("Acc", "MX", ACT, "d1") is not None
    clock[0] += 2
    assert pc.get("Acc", "MX", ACT, "d1") is None
    assert pc.peek("Acc", "MX", ACT, "d1") is None


def test_zero_ttl_switches_the_cache_off(monkeypatch):
    monkeypatch.setenv(pc.TTL_ENV, "0")
    assert _put() == "off" and not pc.enabled()
    assert pc.get("Acc", "MX", ACT, "d1") is None and pc.peek("Acc", "MX", ACT, "d1") is None


def test_the_store_is_bounded_and_drops_the_oldest(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(pc.time, "monotonic", lambda: clock[0])
    for i in range(pc.MAX_ENTRIES + 5):
        clock[0] += 1
        _put(digest=f"d{i}")
    assert len(pc._store) == pc.MAX_ENTRIES
    assert pc.peek("Acc", "MX", ACT, "d0") is None and pc.peek("Acc", "MX", ACT, "d4") is None
    assert pc.peek("Acc", "MX", ACT, f"d{pc.MAX_ENTRIES + 4}") is not None


def test_peek_has_no_side_effects_on_the_counters():
    _put()
    before = copy.deepcopy(pc.counters())
    assert pc.peek("Acc", "MX", ACT, "d1") is not None and pc.peek("Acc", "MX", ACT, "nope") is None
    assert pc.counters() == before
    pc.get("Acc", "MX", ACT, "d1")
    pc.get("Acc", "MX", ACT, "nope")
    c = pc.counters()
    assert c["hits"] == 1 and c["misses"] == 1 and c["puts"] == 1


# ───────────────────────── сброс ─────────────────────────

def test_invalidate_account_drops_only_that_account_and_counts_the_trigger():
    _put(acct="Acc", digest="a")
    _put(acct="Acc", digest="b", country="BR")
    _put(acct="Other", digest="c")
    assert pc.invalidate_account("acc", "publish_job") == 2
    assert pc.peek("Acc", "MX", ACT, "a") is None and pc.peek("Acc", "BR", ACT, "b") is None
    assert pc.peek("Other", "MX", ACT, "c") is not None
    assert pc.counters()["invalidated"] == {"publish_job": 1}


def test_invalidate_all_empties_the_store():
    _put(acct="Acc", digest="a")
    _put(acct="Other", digest="c")
    assert pc.invalidate_all("targeting_confirm") == 2
    assert pc.peek("Acc", "MX", ACT, "a") is None and pc.peek("Other", "MX", ACT, "c") is None
