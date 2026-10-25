"""Behavioral regressions found while integrating the September feature branches."""
import asyncio
import json
from types import SimpleNamespace as NS

import pytest

from scripts import acl_board as board
from src.analysis_agent import ad_text_registry as ar, account_spend as spend
from src.web import app as web, ad_copy as copy_source
from tests.profile_lease_fakes import fake_profile


@pytest.fixture
def library(tmp_path, monkeypatch):
    monkeypatch.setattr(ar.paths, "get_local_config_dir", lambda: str(tmp_path))
    return tmp_path, []


def test_rpa_uses_the_same_local_text_as_the_builder(library, monkeypatch):
    from src.fb_ads_rpa import enrich_draft as enrich
    from src.fb_ads_rpa.build_draft import slots_to_campaign_drafts
    ar.add("Saved", "Local headline", "Local body", "Local description")
    path, _ = library
    entry = NS(ad_account_id="act_demo", profile_id="demo-profile", page_id="demo-page")
    monkeypatch.setattr(enrich, "load_ad_account_map", lambda p: {"Demo": entry})
    monkeypatch.setattr(enrich, "resolve_account_entry", lambda m, label: entry)
    drafts = slots_to_campaign_drafts(
        [{"name": "Demo", "budget": "10", "creative_type": "Image", "creative_file": "demo.jpg", "copy": "Saved"}],
        account_label="Demo", country="Mexico")
    enrich.enrich_campaign_drafts(drafts, account_label="Demo", upload_fb_path=str(path / "missing.xlsx"),
                         ad_account_map_path=str(path / "missing.csv"), creatives_root=str(path), preview_mode=True)
    ad = drafts[0].ad_sets[0].ads[0]
    assert (ad.ad_title, ad.ad_body, ad.ad_link_description) == (
        "Local headline", "Local body", "Local description")


def test_deleting_the_last_text_leaves_the_library_empty(library):
    ar.add("Saved", "Old headline", "Old body", language="ES")
    ar.remove("Saved")
    assert web._ad_copy_rows() == []
    with pytest.raises(ar.AdTextError):
        copy_source.lookup_ad_copy_by_title("Saved")


def test_a_corrupt_library_is_reported_without_substituting_old_copy(library):
    path, _ = library
    file = path / "ad_text_registry.json"
    file.write_text("{invalid", encoding="utf-8")
    response = asyncio.run(web.api_ad_copy())
    assert response.status_code == 503
    assert json.loads(response.body)["error"]
    assert file.read_text() == "{invalid"


def test_unknown_text_cannot_match_a_different_ad_by_substring(library):
    ar.add("Saved longer", "Different", "Other copy")
    with pytest.raises(ar.AdTextError):
        copy_source.lookup_ad_copy_by_title("Saved")


def test_master_updates_the_board_to_merged_without_claiming_rollout():
    source = "## 1 · Area\n### Work\n- [x] `acl-001` Ready #review\n"
    text, changed = board.apply_commit(source, "abc1234", "closes acl-001", branch="master")
    assert changed == ["acl-001"]
    assert board.parse(text).by_id("acl-001").shown_status == "merged"


def test_feature_followup_does_not_downgrade_a_merged_task():
    source = "## 1 · Area\n### Work\n- [x] `acl-001` Ready #merged\n"
    assert board.apply_commit(source, "abc1234", "closes acl-001", branch="feature/followup") == (source, [])


def test_parent_is_merged_when_all_children_are_in_master():
    source = ("## 1 · Area\n### Work\n- [x] `acl-001` Parent #review\n"
              "  - [x] `acl-001a` First #merged\n  - [x] `acl-001b` Last #review\n")
    text, _ = board.apply_commit(source, "abc1234", "closes acl-001b", branch="master")
    assert board.parse(text).by_id("acl-001").shown_status == "merged"


def test_branch_link_escapes_the_url_attribute():
    task = board.Task("acl-001", "Demo", status="review", explicit=True)
    html = board._where(task, {"acl-001": {"branch": 'feature/a"onmouseover="alert(1)', "sha": "abc1234"}})
    assert 'href="https://github.com/MojoTheApe/adopy-campaing-launcher/tree/feature/a"' not in html
    assert "%22" in html


def test_unknown_child_branch_is_not_inferred_from_only_one_sibling():
    parent = board.Task("acl-001", "Parent", status="review", explicit=True,
                        subs=[board.Task("acl-001a", "A"), board.Task("acl-001b", "B")])
    assert "branch not recorded" in board._where(parent, {
        "acl-001a": {"branch": "feature/one", "sha": "abc1234"}})


@pytest.mark.parametrize("status", ["DRAFT", "ACTIVE"])
def test_missing_link_is_a_warning_only_for_drafts(library, monkeypatch, status):
    from src.fb_ads_rpa import enrich_draft as enrich
    from src.fb_ads_rpa.draft import CampaignDraft
    path, _ = library
    entry = NS(ad_account_id="act_demo", profile_id="demo-profile", page_id="demo-page")
    monkeypatch.setattr(enrich, "load_ad_account_map", lambda p: {"Demo": entry})
    monkeypatch.setattr(enrich, "resolve_account_entry", lambda m, label: entry)
    draft = CampaignDraft("Demo", "Mexico", "Demo", status=status)
    enrich.enrich_campaign_drafts([draft], account_label="Demo", upload_fb_path=str(path / "absent.xlsx"),
        ad_account_map_path=str(path / "absent.csv"), creatives_root=str(path))
    if status == "DRAFT":
        assert draft.errors == [] and any("link" in w for w in draft.warnings)
    else:
        assert any("Link empty" in e for e in draft.errors)


def test_updated_library_and_saved_tracking_link_in_rendered_dashboard(tmp_path):
    from pathlib import Path
    from tests.test_review_ui_regressions import run_node
    from tests.test_web_build_upload import _minimal_report, _render_html
    path = tmp_path / "report.html"
    path.write_text(_render_html(_minimal_report()), encoding="utf-8")
    run_node([Path(__file__).with_name("september_ui_harness.cjs"), path])


def test_todo_and_readiness_follow_case_insensitive_account_identity(tmp_path, monkeypatch):
    from src.web import account_links
    from tests.test_dashboard_review import _frame
    monkeypatch.setattr(account_links, "get_local_config_dir", lambda: str(tmp_path))
    monkeypatch.setattr(account_links, "get_ad_account_map_path", lambda: str(tmp_path / "missing.csv"))
    monkeypatch.setattr(web, "get_ad_account_map_path", lambda: str(tmp_path / "missing.csv"))
    monkeypatch.setattr(web, "_account_status", lambda label: {})
    account_links.link("Linked_X01", "country", "CO")
    report = {"countries": [{"country": "Colombia", "account_rows": [
        {"account_label": "LINKED_X01", "to_do_display": "Prepare creative", "is_ready": True}]}]}
    view = web._build_account_map_view(report, _frame("Linked_X01", "Linked_X01"), "2026-09-01", "2026-09-02")
    row = view["countries"][0]["rows"][0]
    assert row["to_do"] == "Prepare creative" and row["ready"] is True


def test_process_detection_failure_does_not_start_a_profile(monkeypatch):
    from src.fb_ads_rpa import browser
    from pathlib import Path
    def fail(*args):
        raise RuntimeError("process inventory unavailable")
    provider = NS(attach_existing_only=lambda *a, **kw: None, profile_is_running=fail)
    monkeypatch.setattr(spend, "_entry_for", lambda a: NS(profile_id=fake_profile("Demo"), folder_id="folder"))
    monkeypatch.setattr(spend, "_mlx_profile_dir", lambda *a: Path("LONG"))
    monkeypatch.setattr(browser, "MultiloginDirectCDPProvider", lambda: provider)
    monkeypatch.setattr(browser, "get_browser_provider", lambda name: pytest.fail("Unexpected browser startup"))
    ok, message = spend.open_for_automation("Demo")
    assert not ok and "process inventory unavailable" in message


def test_failed_snapshot_replace_preserves_the_previous_backup(tmp_path, monkeypatch):
    target = board.write_snapshot(tmp_path, "Previous snapshot")
    def fail(*args):
        raise OSError("disk unavailable")
    monkeypatch.setattr(board.os, "replace", fail)
    assert board.write_snapshot(tmp_path, "New snapshot") is None
    assert target.read_text() == "Previous snapshot"
    assert list(target.parent.glob("*.tmp")) == []


@pytest.mark.parametrize("ready,reason", [
    (True, "Open recommendations"),
    (False, "Verify <identity>"),
    (False, ""),
])
def test_row_states_render_with_account_actions_and_matching_drill_width(ready, reason):
    import re
    row = dict(account="Demo", ready=ready, to_do=reason, in_workbook=True, has_data=True, spend=12,
               leads=2, deposits=1, cpl=6, cpa=12, cr=50, status={})
    html = web.templates.env.get_template("_account_map_country.html").render(
        mc={"country": "Mexico", "key": "MX", "rows": [row]},
        amap={"live": {}, "span": "day", "date_from": "2026-09-09", "all_accounts": []},
        mlx={"A": "Demo A", "B": "Demo B"})
    # ACL-1620: колонки Setup больше нет — причина неготовности не выводится.
    assert "amap-setup" not in html and "needs setup" not in html
    assert "Open recommendations" not in html and "<identity>" not in html
    assert len(re.findall(r"<th[ >]", html)) == len(re.findall(r"<td[ >]", html)) == 11
    # `ncols=11` проверялось здесь же: это была ширина книжной строки
    # раскрытия. 12.09 книжная детализация ушла со стрелки — у живого дерева
    # colspan берётся из самой строки.
    assert "amapReadSpend(this)" not in html and "amapUnlink(" in html
    assert "amap-list" in html
