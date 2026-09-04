"""Telemetry is on by default, and every machine is told what it sends.

Leaving the spreadsheet id as a setting meant every fresh install arrived with
telemetry off and nobody knowing it existed. On 2026-09-04 the fleet was two
machines and one had never sent a row — so when a run stalled on that one, the
only way to investigate was to ask its owner for screenshots, for a day.

The id is the same for every campaign manager, exactly like the accounts sheet.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from config import paths

_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def _clean_env(monkeypatch):
    monkeypatch.delenv("GSHEET_TELEMETRY_ID", raising=False)
    yield


def test_a_machine_that_configured_nothing_still_reports(_clean_env, monkeypatch):
    monkeypatch.setattr(paths, "get_env_files", lambda: [])

    paths.load_env()

    assert os.environ["GSHEET_TELEMETRY_ID"] == paths.DEFAULT_TELEMETRY_SHEET_ID


def test_a_machine_that_chose_its_own_sheet_keeps_it(_clean_env, monkeypatch):
    """A deliberate local value always wins — the default only fills a gap."""
    monkeypatch.setattr(paths, "get_env_files", lambda: [])
    monkeypatch.setenv("GSHEET_TELEMETRY_ID", "a-different-sheet")

    paths.load_env()

    assert os.environ["GSHEET_TELEMETRY_ID"] == "a-different-sheet"


def test_the_doctor_says_what_leaves_the_machine():
    """Consent is not a document somebody was shown once.

    Adopy's policy leaves no anonymous transport, so rows are written as the
    person signed in on that machine. They are entitled to read that on their
    own screen, next to the check that says telemetry is on.
    """
    doctor = (_ROOT / "scripts" / "acl_doctor.py").read_text(encoding="utf-8")

    said = doctor[doctor.index("def check_telemetry"):]
    assert "under YOUR Google account" in said
    assert "no ad accounts, creatives, links or ad text are sent" in said
    # And it must sit on the branch that reports telemetry as ON, not in the
    # branch nobody reaches once the default is in place.
    assert said.index("under YOUR Google account") < said.index('"off — "')
