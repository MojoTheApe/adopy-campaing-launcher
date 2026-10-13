"""End-to-end boundaries for the workbook-free builder integration."""
import asyncio
import csv
import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.datastructures import FormData

from src.web import app as web, account_links
from src.fb_ads_rpa import browser, enrich_draft as en
from src.fb_ads_rpa.build_draft import slots_to_campaign_drafts
from src.meta_campaign_agent.ad_account_map import load_ad_account_map
from tests.profile_lease_fakes import fake_profile


@pytest.fixture
def machine(tmp_path, monkeypatch):
    path = tmp_path / "map.csv"
    monkeypatch.setenv("META_AD_ACCOUNT_MAP_PATH", str(path))
    monkeypatch.setenv("ACL_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(account_links, "get_local_config_dir", lambda: str(tmp_path))
    monkeypatch.setattr(web, "get_upload_fb_excel_path", lambda: str(tmp_path / "missing.xlsx"))
    monkeypatch.setattr(web, "_get_upload_ref_cached", lambda: {})
    rows = [dict(account_name="Demo", ad_account_id="act_demo", profile_id="demo-profile",
                 folder_id="demo-folder", country="SA", tracking_link="https://example.test/saved")]
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    return path


def request(data):
    return NS(json=AsyncMock(return_value=data))


def form_draft(machine, **values):
    form = FormData(dict(country="Saudi Arabia", account_label="Demo", camp_count="1",
                         camp_0_budget="10", camp_0_ad_count="1", camp_0_ad_0_creative_file="demo.jpg",
                         **values))
    country, account, slots = web._parse_build_upload_form(form)
    drafts = slots_to_campaign_drafts(slots, account_label=account, country=country)
    en.enrich_campaign_drafts(drafts, account_label=account,
                             upload_fb_path=str(machine.parent / "missing.xlsx"),
                             ad_account_map_path=str(machine), creatives_root=str(machine.parent), preview_mode=True)
    return drafts[0]


def test_explicit_empty_language_survives_the_actual_form_pipeline(machine):
    draft = form_draft(machine, camp_0_language="")
    assert draft.language == ""
    assert any("language not set" in w for w in draft.warnings)
    from src.fb_ads_rpa.preview import preview_campaign_drafts
    assert preview_campaign_drafts([draft])[0]["Language"] == "—"


def test_local_link_is_loaded_without_any_workbook(machine):
    assert web._build_account_preflight(["Demo"])["Demo"]["link"] == "https://example.test/saved"
    assert form_draft(machine).link == "https://example.test/saved"


def test_save_uses_the_machine_even_when_excel_is_unavailable(machine, monkeypatch):
    monkeypatch.setattr(web, "_write_link_to_excel", lambda *a: pytest.fail("Excel must not be written"))
    response = asyncio.run(web.account_link_patch(request({"account_name": "Demo", "link": "https://example.test/new"})))
    assert response.status_code == 200
    assert load_ad_account_map(machine)["Demo"].tracking_link == "https://example.test/new"


def test_clearing_a_saved_link_does_not_resurrect_workbook_copy(machine, monkeypatch):
    ref = {"accounts": {"Demo": {"link": "https://example.test/old"}}}
    assert web._save_link_to_machine_map("Demo", "")
    monkeypatch.setattr(web, "_get_upload_ref_cached", lambda: ref)
    assert web._build_account_preflight(["Demo"])["Demo"]["link"] == ""


def test_connect_without_account_id_survives_reload_and_keeps_its_profile(machine):
    response = asyncio.run(web.account_map_patch(request({"account_name": "New Demo", "profile_id": "new-profile",
                                                         "folder_id": "new-folder", "country": "MX"})))
    assert response.status_code == 200
    entry = load_ad_account_map(machine).get("New Demo")
    assert entry is not None and entry.profile_id == "new-profile" and entry.ad_account_id == ""
    pf = web._build_account_preflight(["New Demo"])["New Demo"]
    assert pf["missing_map_fields"] == ["ad_account_id"]


@pytest.fixture
def real_provider(tmp_path, monkeypatch):
    from src.analysis_agent import account_spend as sp
    from src.meta_campaign_agent import multilogin_launcher as launcher, multilogin_config
    events = []
    state = {"folder": None, "locked": False}
    monkeypatch.setattr(sp, "_mlx_profile_dir", lambda *a: state["folder"])
    monkeypatch.setattr(browser, "_find_mlx_profile_dir", lambda *a: state["folder"])
    monkeypatch.setattr(browser.MultiloginDirectCDPProvider, "_try_attach_existing", lambda *a, **kw: None)
    monkeypatch.setattr(browser.MultiloginDirectCDPProvider, "_profile_process_running", lambda *a: False)
    monkeypatch.setattr(multilogin_config, "get_multilogin_instance", lambda value: NS(key=value))

    class Launcher:
        def __init__(self, config):
            self.config = config
        def start_profile(self, folder, profile, **kw):
            events.append(("start", self.config.key, folder, profile))
            if state["locked"]:
                raise launcher.MultiloginLauncherError("profile_already_running")
            return NS(port=9222, ws_endpoint="")
        def stop_profile(self, *a, **kw):
            events.append(("stop",))

    monkeypatch.setattr(launcher, "MultiloginLauncher", Launcher)
    monkeypatch.setattr(browser, "_connect_playwright", lambda **kw: NS(close=lambda **k: events.append(("detach",))))
    monkeypatch.setattr(browser, "_kill_chrome_for_user_data_dir", lambda *a, **kw: events.append(("kill",)))
    monkeypatch.setattr(browser.time, "sleep", lambda *a: None)
    return sp, state, events


def test_open_new_profile_reaches_the_real_provider_without_a_local_folder(real_provider):
    sp, _, events = real_provider
    ok, message = sp.open_profile_for_automation(fake_profile("New Demo"), "new-folder", "B", "New Demo")
    assert ok, message
    assert events == [("start", "B", "new-folder", fake_profile("New Demo")), ("detach",)]


def test_api_lock_conflict_never_stops_or_restarts_a_profile(real_provider, tmp_path):
    sp, state, events = real_provider
    state.update(folder=tmp_path, locked=True)
    ok, message = sp.open_profile_for_automation(fake_profile("New Demo"), "new-folder", "B", "New Demo")
    assert not ok and "profile" in message.lower()
    assert events == [("start", "B", "new-folder", fake_profile("New Demo"))]


def test_linked_country_without_statistics_gets_a_renderable_builder(machine):
    account_links.link("Demo", "country", "SA")
    report = {"countries": []}
    assert web._add_linked_accounts_to_builder(report) == 1
    assert report["countries"][0]["open_recs"] == {"Demo": []}
    from tests.test_web_build_upload import _minimal_report, _render_html
    html = _render_html({**_minimal_report(), **report})
    assert 'data-acc-label="Demo"' in html


def test_render_does_not_mutate_the_cached_report_or_keep_removed_links(machine, monkeypatch):
    from tests.test_web_build_upload import _minimal_report
    cached = _minimal_report()
    cached["countries"][0]["open_recs"] = {}
    account_links.link("Demo", "country", "Mexico")
    monkeypatch.setattr(web, "get_cached_report", lambda: (cached, None, []))
    monkeypatch.setattr(web, "_load_ref_options", lambda: {})
    monkeypatch.setattr(web, "_load_draft_status", lambda: {})
    monkeypatch.setattr(web, "_build_account_map_view", lambda *a: {})
    monkeypatch.setattr(web, "_build_goals_view", lambda *a: {})
    monkeypatch.setattr(web, "_installed_release", lambda: {})
    first = web._render_report(MagicMock())
    assert "Demo" in first.context["report"]["countries"][0]["open_recs"]
    assert cached["countries"][0]["open_recs"] == {}
    account_links.unlink("Demo", "country", "Mexico")
    second = web._render_report(MagicMock())
    assert "Demo" not in second.context["report"]["countries"][0]["open_recs"]


def test_saving_exact_account_does_not_patch_a_similarly_named_row(machine):
    with machine.open(newline="") as f:
        reader = csv.DictReader(f)
        rows, fields = list(reader), reader.fieldnames
    rows.insert(0, {**rows[0], "account_name": "Demo longer"})
    with machine.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    assert web._save_link_to_machine_map("Demo", "https://example.test/exact")
    assert load_ad_account_map(machine)["Demo"].tracking_link == "https://example.test/exact"
    assert load_ad_account_map(machine)["Demo longer"].tracking_link == "https://example.test/saved"


@pytest.mark.parametrize("has_report_row", [True, False])
def test_workbook_membership_survives_case_changes_and_empty_periods(machine, monkeypatch, has_report_row):
    from tests.test_dashboard_review import _frame
    account_links.link("Demo", "country", "CO")
    report = {"countries": [{"country": "Colombia", "account_rows":
               [{"account_label": "DEMO", "is_ready": False}] if has_report_row else []}]}
    monkeypatch.setattr(web, "_account_status", lambda *a: {})
    view = web._build_account_map_view(report, _frame("Demo", "Demo"), "2026-09-09", "2026-09-09")
    row = next(c for c in view["countries"] if c["key"] == "CO")["rows"][0]
    assert row["in_workbook"] is True


def test_explicit_clear_and_legacy_omission_have_distinct_link_semantics(machine):
    assert form_draft(machine, tracking_link="").link == ""
    assert form_draft(machine).link == "https://example.test/saved"


def test_clearing_legacy_link_column_is_not_undone_by_the_alias(machine):
    machine.write_text(machine.read_text().replace("tracking_link", "link"))
    assert web._save_link_to_machine_map("Demo", "")
    assert load_ad_account_map(machine)["Demo"].tracking_link is None


def test_ambiguous_partial_account_name_does_not_choose_a_profile():
    from src.meta_campaign_agent.ad_account_map import AccountMapEntry, resolve_account_entry
    entries = {"Demo one": AccountMapEntry("act_one"), "Demo two": AccountMapEntry("act_two")}
    assert resolve_account_entry(entries, "Demo") is None
    assert resolve_account_entry(entries, "Demo one").ad_account_id == "act_one"


def test_partially_configured_map_preserves_unconfigured_legacy_fields():
    from src.meta_campaign_agent.ad_account_map import AccountMapEntry
    from src.upload_agent.build_upload_excel import get_account_info
    ref = {"accounts": {"Demo": {"link": "https://example.test/legacy", "domain": "example.test", "pixel": "old"}}}
    info = get_account_info(ref, "Demo", account_map={"Demo": AccountMapEntry("act_demo", pixel_id="local")})
    assert info == {"link": "https://example.test/legacy", "domain": "example.test", "pixel": "local"}
