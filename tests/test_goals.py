"""Goals per campaign manager: stored, edited, deleted.

They used to be a JavaScript literal in report.html, so adding a country meant
editing HTML and every machine carried the same numbers regardless of whose they
were. These tests hold what changed and, more importantly, what must not: a
machine that has been showing these numbers for months must not open the new
screen empty.
"""
from __future__ import annotations

import pytest

from src.web import goals


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(goals, "get_local_config_dir", lambda: str(tmp_path))
    monkeypatch.delenv("ACL_TEAM_LEAD", raising=False)
    yield


def test_a_machine_that_never_edited_anything_sees_what_it_always_saw():
    assert goals.for_cm("Evgeni Sedin") == goals.DEFAULT_GOALS
    assert not goals.goals_path().exists(), "reading must not create a file"


def test_each_manager_keeps_their_own():
    goals.upsert("Evgeni", {"country": "Peru", "code": "PE", "market": "Latam", "ftd": 10})

    assert [g["country"] for g in goals.for_cm("Evgeni")].count("Peru") == 1
    assert goals.for_cm("Yotam") == goals.DEFAULT_GOALS, "untouched, still on defaults"


def test_editing_a_country_replaces_it_rather_than_adding_a_second():
    goals.upsert("Evgeni", {"country": "Kuwait", "code": "KW", "market": "GCC", "ftd": 25})

    kuwait = [g for g in goals.for_cm("Evgeni") if g["country"] == "Kuwait"]
    assert len(kuwait) == 1
    assert kuwait[0]["ftd"] == 25


def test_deleting_a_country_stops_the_target_and_nothing_else():
    goals.delete("Evgeni", "Mexico")

    countries = [g["country"] for g in goals.for_cm("Evgeni")]
    assert "Mexico" not in countries
    assert "Chile" in countries


def test_the_same_country_twice_is_refused_rather_than_stored():
    with pytest.raises(ValueError, match="listed twice"):
        goals.save("Evgeni", [
            {"country": "Chile", "ftd": 10},
            {"country": "chile", "ftd": 20},
        ])


def test_a_goal_needs_a_country_and_numbers_that_are_numbers():
    with pytest.raises(ValueError, match="country is required"):
        goals.upsert("Evgeni", {"country": "  ", "ftd": 5})
    with pytest.raises(ValueError, match="must be a number"):
        goals.upsert("Evgeni", {"country": "Peru", "ftd": "many"})
    with pytest.raises(ValueError, match="cannot be negative"):
        goals.upsert("Evgeni", {"country": "Peru", "ftd": -3})


def test_team_lead_is_an_agreement_and_says_so(monkeypatch):
    """The dashboard has no sign-in; the flag unlocks a UI, not a door."""
    assert goals.is_team_lead() is False
    monkeypatch.setenv("ACL_TEAM_LEAD", "1")
    assert goals.is_team_lead() is True

    module = (goals.__file__)
    text = open(module, encoding="utf-8").read()
    assert "an agreement, not a mechanism" in text
