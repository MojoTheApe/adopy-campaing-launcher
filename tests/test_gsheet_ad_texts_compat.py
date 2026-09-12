"""Configured ad-copy sources survive migration to the local-library bootstrap."""
from types import SimpleNamespace as NS

import pytest

from src.analysis_agent import gsheet_ad_texts as gs, gsheet_targeting as targeting


@pytest.fixture
def sources(monkeypatch):
    for name in ("GSHEET_ADCOPY_ID", "GSHEET_TARGETING_SPREADSHEET_ID", "GSHEET_TARGETS_ID",
                 "GSHEET_AD_TEXTS_TAB", "GSHEET_ADCOPY_SHEET"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(gs, "_cache", gs._Cache())
    monkeypatch.setattr(gs, "last_error", "")
    calls, state = [], {"fail": False}

    def get(**kwargs):
        calls.append(kwargs)

        def execute():
            if state["fail"]:
                raise RuntimeError("unavailable")
            if "range" in kwargs:
                return {"values": [[kwargs["spreadsheetId"], kwargs["range"], "Body"]]}
            return {"sheets": [{"properties": {"title": tab}} for tab in ("ad texts", "Legacy", "New")]}

        return NS(execute=execute)

    svc = NS(spreadsheets=lambda: NS(get=get, values=lambda: NS(get=get)))
    monkeypatch.setattr(targeting, "_sheets_services", lambda: [("test", svc)])
    return calls, state, svc


def test_legacy_sheet_settings_and_changed_cache_key(monkeypatch, sources):
    calls, _, _ = sources
    monkeypatch.setenv("GSHEET_TARGETS_ID", "legacy-id")
    monkeypatch.setenv("GSHEET_ADCOPY_SHEET", "Legacy")
    assert gs.load_ad_text_rows() == [["legacy-id", "'Legacy'!A2:F", "Body"]]
    assert gs.load_ad_text_rows()[0][0] == "legacy-id"
    assert len(calls) == 2  # metadata + values; second call was cached
    monkeypatch.setenv("GSHEET_ADCOPY_ID", "copy-id")
    monkeypatch.setenv("GSHEET_TARGETING_SPREADSHEET_ID", "targeting-id")
    monkeypatch.setenv("GSHEET_AD_TEXTS_TAB", "New")
    assert gs.load_ad_text_rows() == [["copy-id", "'New'!A2:F", "Body"]]
    assert len(calls) == 4
    monkeypatch.delenv("GSHEET_ADCOPY_ID")
    assert gs.load_ad_text_rows()[0][0] == "targeting-id"


def test_failed_force_read_does_not_return_old_cache_as_success(sources):
    calls, state, _ = sources
    assert gs.load_ad_text_rows()
    state["fail"] = True
    assert gs.load_ad_text_rows(force=True) == []
    assert gs.load_ad_text_rows() == []
    assert len(calls) == 4 and "unavailable" in gs.last_error
    state["fail"] = False
    assert gs.load_ad_text_rows() and gs.last_error == ""


def test_permission_error_tries_the_next_google_service(monkeypatch, sources):
    _, _, working = sources

    def denied(**kwargs):
        raise PermissionError("403")

    broken = NS(spreadsheets=lambda: NS(get=denied))
    monkeypatch.setattr(targeting, "_sheets_services", lambda: [("service account", broken), ("OAuth", working)])
    assert gs.load_ad_text_rows() and gs.last_error == ""
