"""ACL-2932: нет тихого выбора между разными смыслами; заметки у каждой кампании свои; формулировка ambiguous."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_api import creator as cr  # noqa: E402
from src.fb_api import targeting_resolver as tr  # noqa: E402
from tests.test_acl1771_targeting_resolver import FakeSearch, _store  # noqa: E402

APPLE = {"Apple": [{"id": "1", "name": "Apple (fruit)"}, {"id": "2", "name": "Apple Inc."}]}


def test_apple_with_two_meanings_is_removed_as_ambiguous_and_not_remembered(tmp_path):
    store = _store(tmp_path)
    r = cr.TargetingResolver(FakeSearch(interests=APPLE), store)
    assert r.interests([["Apple"]]) == []
    notes = r.targeting_notes()
    assert notes[0].startswith("Removed 'Apple': several possible matches (")
    assert "Apple (fruit)" in notes[0] and "Apple Inc." in notes[0]
    assert "no match" not in notes[0]
    assert store.get("Apple") is None and store.entries() == []


def test_a_single_category_tail_alone_still_resolves(tmp_path):
    c = FakeSearch(interests={"Apple": [{"id": "1", "name": "Apple (fruit)"}]})
    r = cr.TargetingResolver(c, _store(tmp_path))
    assert r.interests([["Apple"]]) == [{"interests": [{"id": "1", "name": "Apple (fruit)"}]}]


def test_two_category_tails_stay_unresolved_for_a_human(tmp_path):
    c = FakeSearch(interests={"Apple": [{"id": "1", "name": "Apple (fruit)"}, {"id": "3", "name": "Apple (company)"}]})
    r = cr.TargetingResolver(c, _store(tmp_path))
    assert r.interests([["Apple"]]) == []
    assert r.targeting_notes()[0].startswith("Removed 'Apple': several possible matches (")


def test_no_match_wording_only_when_nothing_passed(tmp_path):
    c = FakeSearch(interests={"Apple": [{"id": "9", "name": "Apple pie recipes"}]})
    r = cr.TargetingResolver(c, _store(tmp_path))
    assert r.interests([["Apple"]]) == []
    assert r.targeting_notes()[0].startswith("Removed 'Apple': Meta has no match on this account")


def test_each_campaign_shows_only_its_own_notes(tmp_path):
    c = FakeSearch(interests={**APPLE, "Zzzz": []})
    r = cr.TargetingResolver(c, _store(tmp_path))
    r.interests([["Apple"]])
    first = r.targeting_notes()
    assert any("Apple" in n for n in first)
    r.reset_targeting_notes()                       # начало следующей кампании
    r.interests([["Zzzz"]])
    second = r.targeting_notes()
    assert second and not any("Apple" in n for n in second)
    assert any("Zzzz" in n for n in second)


def test_a_term_reused_by_the_next_campaign_shows_its_note_again_for_that_campaign(tmp_path):
    r = cr.TargetingResolver(FakeSearch(interests=APPLE), _store(tmp_path))
    r.interests([["Apple"]])
    r.reset_targeting_notes()
    assert r.targeting_notes() == []
    r.interests([["Apple"]])
    assert r.targeting_notes()[0].startswith("Removed 'Apple': several possible matches")


def test_outcome_warnings_carry_only_the_current_campaign_notes(tmp_path):
    r = cr.TargetingResolver(FakeSearch(interests={**APPLE}), _store(tmp_path))
    holder = type("H", (), {"resolver": r})()
    note = cr.__dict__[next(n for n, v in cr.__dict__.items()
                            if isinstance(v, type) and "_note_targeting_warnings" in v.__dict__)]
    out1, out2 = cr.Outcome(campaign_name="A"), cr.Outcome(campaign_name="B")
    r.interests([["Apple"]])
    note._note_targeting_warnings(holder, out1)
    r.reset_targeting_notes()
    r.interests([["Zzzz"]])
    note._note_targeting_warnings(holder, out2)
    assert any("Apple" in w for w in out1.warnings)
    assert not any("Apple" in w for w in out2.warnings) and any("Zzzz" in w for w in out2.warnings)


@pytest.mark.parametrize("term,name", [("Investing", "Investing.com"), ("Bloomberg", "Bloomberg L.P."),
                                       ("E*TRADE", "E-Trade")])
def test_acl2925_positives_still_resolve(tmp_path, term, name):
    c = FakeSearch(interests={term: [{"id": "7", "name": name}]})
    r = cr.TargetingResolver(c, _store(tmp_path))
    assert r.interests([[term]]) == [{"interests": [{"id": "7", "name": name}]}]
