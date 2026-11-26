"""ACL-2939: отказ Meta в необязательном поиске (ACL-1920) виден только на кампании, которая его получила."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_api import creator as cr  # noqa: E402
from src.fb_api import graph as g  # noqa: E402
from tests.test_acl1761_api_create import ACT, _draft  # noqa: E402
from tests.test_acl1771_targeting_resolver import _store  # noqa: E402
from tests.test_acl1920_optional_refusals import RefusingMeta, Refusing  # noqa: E402

REFUSED = "adworkposition"
INTERESTS = {"Forex": [{"id": "5", "name": "Forex"}]}


@pytest.fixture(autouse=True)
def _temp_config(tmp_path, monkeypatch):
    monkeypatch.setenv("ACL_CONFIG_DIR", str(tmp_path / "cfg"))


def _refusal(notes):
    return [w for w in notes if f"({REFUSED})" in w]


def test_resolver_second_campaign_without_the_refused_search_has_no_note(tmp_path):
    r = cr.TargetingResolver(Refusing(refuse=[REFUSED], interests=INTERESTS), _store(tmp_path))
    r.interests([["Qwerty"]])                       # goes through every stage, hits the refusal
    assert _refusal(r.targeting_warnings())
    r.reset_targeting_notes()
    r.interests([["Forex"]])                        # exact in adinterest, no further stages
    assert _refusal(r.targeting_warnings()) == []


def test_resolver_later_campaign_reusing_the_term_shows_the_note(tmp_path):
    r = cr.TargetingResolver(Refusing(refuse=[REFUSED], interests=INTERESTS), _store(tmp_path))
    r.interests([["Qwerty"]])
    r.reset_targeting_notes()
    r.interests([["Forex"]])
    r.reset_targeting_notes()
    r.interests([["Qwerty"]])                       # term result comes from the job cache
    assert _refusal(r.targeting_warnings())


def test_without_reset_the_warnings_stay_job_wide(tmp_path):
    r = cr.TargetingResolver(Refusing(refuse=[REFUSED], interests=INTERESTS), _store(tmp_path))
    r.interests([["Qwerty"]])
    r.interests([["Forex"]])
    assert len(_refusal(r.targeting_warnings())) == 1


def test_run_shows_the_refusal_only_on_the_campaigns_that_hit_it(tmp_path):
    meta = RefusingMeta(refuse=(REFUSED,), interests=INTERESTS)
    client = g.GraphClient(meta, allow_writes=True, sleep=lambda s: None, min_interval_s=0.0, max_calls=900)
    facts = cr.preflight_account(client, ACT)
    creator = cr.CampaignCreator(client, facts, cr.TargetingResolver(client, _store(tmp_path)),
                                 sleep=lambda s: None)
    outs = []
    for i, label in enumerate(("Qwerty", "Forex", "Qwerty")):
        sub = tmp_path / f"c{i}"
        sub.mkdir()
        d = _draft(sub, ads=1)
        d.name = d.ad_sets[0].name = f"MX Leads {i + 1}"
        d.ad_sets[0].targeting_label = label
        outs.append(creator.run(cr.plan_from_draft(d)))
    first, second, third = (_refusal(o.warnings) for o in outs)
    assert first, "the campaign that hit the refusal shows it"
    assert second == [], "the campaign that did not hit it shows nothing"
    assert third, "a later campaign reusing the refused lookup shows it again"


# --- ACL-2939 fix round 1: adinterestvalid, "not re-checked" line, behaviors list (run()-level) ---

def _run_campaigns(tmp_path, meta, labels, store=None):
    client = g.GraphClient(meta, allow_writes=True, sleep=lambda s: None, min_interval_s=0.0, max_calls=900)
    facts = cr.preflight_account(client, ACT)
    creator = cr.CampaignCreator(client, facts, cr.TargetingResolver(client, store or _store(tmp_path)),
                                 sleep=lambda s: None)
    outs = []
    for i, label in enumerate(labels):
        sub = tmp_path / f"r{i}"
        sub.mkdir()
        d = _draft(sub, ads=1)
        d.name = d.ad_sets[0].name = f"MX Leads {i + 1}"
        d.ad_sets[0].targeting_label = label
        outs.append(creator.run(cr.plan_from_draft(d)))
    return outs


def _mentions(out, needle):
    return [w for w in out.warnings if needle in w]


def test_run_adinterestvalid_refusal_replays_on_a_reused_term(tmp_path):
    meta = RefusingMeta(refuse=("adinterestvalid",), interests=INTERESTS)
    a, b, c = _run_campaigns(tmp_path, meta, ("Qwerty", "Forex", "Qwerty"))
    assert _mentions(a, "(adinterestvalid)")
    assert not _mentions(b, "(adinterestvalid)")
    assert _mentions(c, "(adinterestvalid)")


def test_run_adinterestvalid_refusal_shows_on_a_new_term_after_the_refusal(tmp_path):
    meta = RefusingMeta(refuse=("adinterestvalid",), interests=INTERESTS)
    a, b = _run_campaigns(tmp_path, meta, ("Qwerty", "Asdfgh"))
    assert _mentions(a, "(adinterestvalid)") and _mentions(b, "(adinterestvalid)")


def test_run_not_rechecked_line_replays_on_a_reused_confirmed_term(tmp_path):
    store = _store(tmp_path)
    store.put("Mytopic", id="77", type="interests", name="Mytopic")
    meta = RefusingMeta(refuse=("adinterestvalid",), interests=INTERESTS)
    a, b, c = _run_campaigns(tmp_path, meta, ("Mytopic", "Forex", "Mytopic"), store=store)
    line = "were not re-checked"
    assert _mentions(a, line) and not _mentions(b, line) and _mentions(c, line)


def test_run_refused_behaviors_list_shows_on_a_later_campaign_using_the_cached_list(tmp_path):
    meta = RefusingMeta(refuse=("adTargetingCategory",), interests=INTERESTS)
    a, b, c = _run_campaigns(tmp_path, meta, ("Qwerty", "Forex", "Asdfgh"))
    assert _mentions(a, "(adTargetingCategory)")
    assert not _mentions(b, "(adTargetingCategory)")
    assert _mentions(c, "(adTargetingCategory)")
