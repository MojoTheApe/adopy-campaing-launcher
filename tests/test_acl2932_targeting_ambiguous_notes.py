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


# --- cross-stage: the decision is made after ALL search stages, not at the first tail hit ---

def test_a_tail_in_adinterest_and_a_corporate_name_from_a_later_stage_is_ambiguous(tmp_path):
    store = _store(tmp_path)
    c = FakeSearch(interests={"Apple": [{"id": "1", "name": "Apple (fruit)"}]},
                   typed={("adworkemployer", "Apple"): [{"id": "2", "name": "Apple Inc."}]})
    r = cr.TargetingResolver(c, store)
    assert r.interests([["Apple"]]) == []
    note = r.targeting_notes()[0]
    assert note.startswith("Removed 'Apple': several possible matches (")
    assert "Apple (fruit)" in note and "Apple Inc." in note
    assert store.entries() == [] and r.terms._warm_hit("Apple") is None


def test_two_tails_from_different_stages_are_ambiguous(tmp_path):
    store = _store(tmp_path)
    c = FakeSearch(interests={"Apple": [{"id": "1", "name": "Apple (fruit)"}]},
                   typed={("adworkemployer", "Apple"): [{"id": "3", "name": "Apple (company)"}]})
    r = cr.TargetingResolver(c, store)
    assert r.interests([["Apple"]]) == []
    assert r.targeting_notes()[0].startswith("Removed 'Apple': several possible matches (")
    assert store.entries() == []


def test_a_lone_tail_with_other_stages_empty_still_resolves(tmp_path):
    c = FakeSearch(interests={"Apple": [{"id": "1", "name": "Apple (fruit)"}]})
    r = cr.TargetingResolver(c, _store(tmp_path))
    assert r.interests([["Apple"]]) == [{"interests": [{"id": "1", "name": "Apple (fruit)"}]}]


# --- run()-level: several campaigns sharing one account resolver ---

def test_run_shows_each_campaign_only_its_own_notes(tmp_path):
    from tests.test_acl1761_api_create import ACT, _draft
    from tests.test_acl1771_targeting_resolver import TargetMeta
    from src.fb_api import graph as g

    meta = TargetMeta(interests={"Apple": [{"id": "1", "name": "Apple (fruit)"}, {"id": "2", "name": "Apple Inc."}],
                                 "Investing": [{"id": "7", "name": "Investing.com"}]})
    client = g.GraphClient(meta, allow_writes=True, sleep=lambda s: None, min_interval_s=0.0, max_calls=600)
    facts = cr.preflight_account(client, ACT)
    creator = cr.CampaignCreator(client, facts, cr.TargetingResolver(client, _store(tmp_path)),
                                 sleep=lambda s: None)
    outs = []
    for i, label in enumerate(("Apple, Investing", "Zzzz", "Apple")):
        d = _draft(tmp_path / f"c{i}" if (tmp_path / f"c{i}").mkdir() is None else tmp_path, ads=1)
        d.name = d.ad_sets[0].name = f"MX Leads {i + 1}"
        d.ad_sets[0].targeting_label = label
        outs.append(creator.run(cr.plan_from_draft(d)))
    a, b, c = ([w for w in o.warnings if w.startswith(("Removed", "Used"))] for o in outs)
    assert any("Apple" in w and "several possible matches" in w for w in a) and any("Investing.com" in w for w in a)
    assert any("Zzzz" in w for w in b) and not any("Apple" in w or "Investing" in w for w in b)
    assert any("Apple" in w for w in c) and not any("Investing" in w or "Zzzz" in w for w in c)


# --- the decision does not depend on the MAX_CANDIDATES cut ---

def _many(names, start=10):
    return [{"id": str(start + i), "name": n} for i, n in enumerate(names)]


FILLER = ["Apples", "Apple Arcade", "Apple Books", "Apple Fitness", "Apple Music", "Apple TV", "Apple Watch",
          "Apple Pay", "Apple Maps"]


class WideSearch(FakeSearch):
    def get_all(self, path, params=None, *, max_pages=5):
        # no 3-row page cap: a real adinterest search returns up to 50 rows
        rows = self.interests.get(params["q"], []) if params["type"] == "adinterest" else None
        if rows is None:
            return super().get_all(path, params, max_pages=max_pages)
        self.calls += 1
        self.log.append((params["type"], 1, dict(params)))
        return rows


def test_fruit_with_many_higher_ranked_interests_and_a_later_employer_is_ambiguous(tmp_path):
    store = _store(tmp_path)
    interests = [{"id": "1", "name": "Apple (fruit)"}] + _many(FILLER)
    c = WideSearch(interests={"Apple": interests}, typed={("adworkemployer", "Apple"): [{"id": "2", "name": "Apple Inc."}]})
    r = cr.TargetingResolver(c, store)
    assert r.interests([["Apple"]]) == []
    note = r.targeting_notes()[0]
    assert note.startswith("Removed 'Apple': several possible matches (")
    assert "Apple (fruit)" in note and "Apple Inc." in note
    assert store.entries() == []


def test_mirror_corporate_name_among_many_interests_and_fruit_later_is_ambiguous(tmp_path):
    store = _store(tmp_path)
    interests = [{"id": "2", "name": "Apple Inc."}] + _many(FILLER)
    c = WideSearch(interests={"Apple": interests},
                   typed={("adworkemployer", "Apple"): [{"id": "1", "name": "Apple (fruit)"}]})
    r = cr.TargetingResolver(c, store)
    assert r.interests([["Apple"]]) == []
    note = r.targeting_notes()[0]
    assert "several possible matches" in note and "Apple (fruit)" in note and "Apple Inc." in note
    assert store.entries() == []


# --- ACL-2938: warm entries made under the old first-stage rule are re-checked ---

def _warm_old(facts, term, name, **extra):
    """Запись прежнего выпуска: без метки правила."""
    facts.put_term(facts.term_key("term", tr.norm(term)),
                   {"picks": [{"id": "6003000000001", "type": "interests", "name": name}], **extra})


def _facts(tmp_path):
    from src.fb_api import facts_cache as fc

    return fc.FactsCache(path=tmp_path / "facts.json")


def test_old_warm_tail_pick_is_rechecked_and_removed_as_ambiguous(tmp_path):
    facts, store = _facts(tmp_path), _store(tmp_path)
    _warm_old(facts, "Apple", "Apple (fruit)")
    meta = FakeSearch(valid={"Apple (fruit)": ("Apple (fruit)", "6003000000001")}, interests=APPLE)
    r = tr.TermResolver(meta, store, facts=facts)
    r.prefetch(["Apple"])
    res = r.resolve("Apple")
    assert r.settle(res) == []
    assert "several possible matches" in " ".join(r.notes)
    assert store.entries() == []
    assert facts.get_term(facts.term_key("term", "apple")) is None
    assert "adinterest" in meta.types()      # поиск был, запись не использована


def test_old_warm_entry_that_still_passes_is_used_without_lookup(tmp_path):
    facts, store = _facts(tmp_path), _store(tmp_path)
    _warm_old(facts, "Yoga", "Yoga")
    meta = FakeSearch(valid={"Yoga": ("Yoga", "6003000000001")})
    r = tr.TermResolver(meta, store, facts=facts)
    r.prefetch(["Yoga"])
    res = r.resolve("Yoga")
    assert res["state"] == "exact" and res["picks"][0]["id"] == "6003000000001"
    assert r.facts_stats["warm_hits"] == 1
    assert "adinterest" not in meta.types()


def test_current_rule_warm_entry_with_tail_is_kept(tmp_path):
    facts, store = _facts(tmp_path), _store(tmp_path)
    _warm_old(facts, "Apple", "Apple (fruit)", rule=tr.WARM_RULE_VERSION)
    meta = FakeSearch(valid={"Apple (fruit)": ("Apple (fruit)", "6003000000001")})
    r = tr.TermResolver(meta, store, facts=facts)
    r.prefetch(["Apple"])
    assert r.resolve("Apple")["state"] == "exact"
    assert r.facts_stats["warm_hits"] == 1
