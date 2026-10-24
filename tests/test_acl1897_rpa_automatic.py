"""Automatic Page parity at the actual RPA preparation boundary; no live sessions."""
from types import SimpleNamespace

import pytest

from src.fb_api.creator import AccountFacts, resolve_page
from src.fb_ads_rpa import rpa_publisher as rpa
from src.fb_ads_rpa.ads_manager_flow import page_identity_kwargs
from src.fb_ads_rpa.draft import CampaignDraft
from src.web import account_pages


ACT = "act_1234567890"


def _build(monkeypatch, draft, available, *, preview=False):
    reads = []
    monkeypatch.setattr(rpa, "slots_to_campaign_drafts", lambda *a, **k: [draft])
    monkeypatch.setattr(rpa, "enrich_campaign_drafts", lambda drafts, **k: drafts)
    monkeypatch.setattr(account_pages, "read_pages", lambda account, **k:
                        reads.append((account, k)) or available)
    out = rpa.RpaPublisher().build_drafts([], account_label="Demo", country="MX", preview_mode=preview)
    return out[0], reads


@pytest.mark.parametrize("ids,selected", [(["123"], ""), (["123", "456"], ""), (["456", "123"], ""),
                                           (["123", "456"], "456")])
def test_api_and_rpa_resolve_the_same_page_before_identity(monkeypatch, ids, selected):
    rows = [{"id": pid, "name": f"Page {pid}", "source": "ad account"} for pid in ids]
    plan = SimpleNamespace(page_id=selected, page_source="draft" if selected else "automatic", ad_sets=[])
    resolve_page(plan, AccountFacts(ACT, "USD", ids, []))
    draft = CampaignDraft("C", "MX", "Demo", page_id=selected or None,
                          page_provided=True, ad_account_id=ACT)
    draft.page_source = "draft" if selected else "automatic"
    out, reads = _build(monkeypatch, draft, {"ok": True, "act": ACT, "pages": rows})
    assert not out.errors
    assert out.page_id == plan.page_id
    assert page_identity_kwargs(out) == {"page_id": plan.page_id,
        "page_name": f"Page {plan.page_id}", "page_name_unique": True}
    assert reads == [("Demo", {"force": True})]


def test_a_valid_saved_map_does_not_override_builder_automatic_in_either_backend(monkeypatch, tmp_path):
    from src.fb_ads_rpa.enrich_draft import enrich_campaign_drafts
    from src.fb_ads_rpa.draft import AdDraft, AdSetDraft
    from src.fb_api.creator import plan_from_draft
    mapping = tmp_path / "map.csv"
    original = "account_name,ad_account_id,page_id\nDemo,act_1234567890,456\n"
    mapping.write_text(original)
    creative = tmp_path / "creative.png"
    creative.write_bytes(b"offline creative")
    draft = CampaignDraft("C", "MX", "Demo", page_provided=True, link="https://example.com/",
        ad_sets=[AdSetDraft("Set", daily_budget=50, ads=[AdDraft(creative_filename=creative.name,
            creative_path=str(creative), ad_title="Title", ad_body="Body")])])
    enrich_campaign_drafts([draft], account_label="Demo", upload_fb_path=str(tmp_path / "none.xlsx"),
                          ad_account_map_path=str(mapping), creatives_root=str(tmp_path), preview_mode=True)
    assert not draft.page_id and draft.page_source == "automatic" and not draft.errors
    plan = plan_from_draft(draft)
    resolve_page(plan, AccountFacts(ACT, "USD", ["123", "456"], []))
    out, _ = _build(monkeypatch, draft, {"ok": True, "act": ACT, "pages": [
        {"id": "123", "name": "First", "source": "ad account"},
        {"id": "456", "name": "Saved", "source": "ad account"}]})
    assert out.page_id == plan.page_id == "123" and mapping.read_text() == original


@pytest.mark.parametrize("available,reason", [
    ({"ok": False, "message": "Page discovery refused"}, "Page discovery refused"),
    ({"ok": True, "act": "act_1234567890123456", "pages": []}, "account changed"),
    ({"ok": True, "act": ACT, "pages": [{"id": "123", "name": "P"}]}, "source is unknown"),
])
def test_automatic_read_failure_is_not_an_empty_success_or_a_created_campaign(monkeypatch, available, reason):
    from src.fb_ads_rpa.ads_manager_flow import AdsManagerFlow
    draft = CampaignDraft("C", "MX", "Demo", page_provided=True, ad_account_id=ACT)
    out, _ = _build(monkeypatch, draft, available)
    assert any(reason in e for e in out.errors)
    # The actual flow's creation boundary refuses even before it needs a page.
    flow = AdsManagerFlow.__new__(AdsManagerFlow)
    monkeypatch.setattr(flow, "_goto_campaigns", lambda *a: pytest.fail("a campaign must not start"))
    result = flow.publish_campaign(out)
    assert not result.success and reason in result.error


@pytest.mark.parametrize("selected", ["", "777"])
def test_profile_page_requires_a_fresh_account_bound_proof(monkeypatch, selected):
    calls = []
    monkeypatch.setattr(account_pages, "validate_profile_page", lambda account, **kw: calls.append((account, kw)) or "")
    rows = [{"id": "777", "name": "Profile Page", "source": "profile"}]
    draft = CampaignDraft("C", "MX", "Demo", page_id=selected or None, page_provided=True,
                          ad_account_id=ACT, link="https://example.com/")
    out, _ = _build(monkeypatch, draft, {"ok": True, "act": ACT, "pages": rows})
    assert not out.errors and out.page_id == "777" and out.page_origin == "profile"
    assert calls == [("Demo", {"act": ACT, "page_id": "777", "page_name": "Profile Page",
                               "link": "https://example.com/"})]
    assert page_identity_kwargs(out)["page_id"] == "777"


@pytest.mark.parametrize("proof", ["Meta refused this Page for the ad account", None])
def test_refused_or_unconfirmed_profile_page_cannot_start_the_flow(monkeypatch, proof):
    from src.fb_ads_rpa.ads_manager_flow import AdsManagerFlow
    monkeypatch.setattr(account_pages, "validate_profile_page", lambda *a, **kw: proof)
    draft = CampaignDraft("C", "MX", "Demo", page_provided=True, ad_account_id=ACT)
    out, _ = _build(monkeypatch, draft, {"ok": True, "act": ACT,
        "pages": [{"id": "777", "name": "Profile Page", "source": "profile"}]})
    assert out.errors and not out.page_id
    flow = AdsManagerFlow.__new__(AdsManagerFlow)
    result = flow.publish_campaign(out)
    assert not result.success and not result.campaign_id


def test_account_page_wins_over_later_profile_page_without_extra_validation(monkeypatch):
    monkeypatch.setattr(account_pages, "validate_profile_page", lambda *a, **kw: pytest.fail("account Page does not need fallback proof"))
    draft = CampaignDraft("C", "MX", "Demo", page_provided=True, ad_account_id=ACT)
    out, _ = _build(monkeypatch, draft, {"ok": True, "act": ACT, "pages": [
        {"id": "123", "name": "Account Page", "source": "ad account"},
        {"id": "777", "name": "Profile Page", "source": "profile"}]})
    assert not out.errors and out.page_id == "123" and out.page_origin == "ad account"


@pytest.mark.parametrize("preview,provided", [(True, True), (False, False)])
def test_preview_and_legacy_non_builder_drafts_do_not_start_discovery(monkeypatch, preview, provided):
    draft = CampaignDraft("C", "MX", "Demo", page_id="456" if not provided else None,
                          page_provided=provided, ad_account_id=ACT)
    out, reads = _build(monkeypatch, draft, {}, preview=preview)
    assert not reads and not out.errors
    assert out.page_id == ("456" if not provided else None)


def test_automatic_identity_reaches_recorded_flow_and_other_engines_refuse(monkeypatch):
    from src.fb_ads_rpa import config, ads_manager_flow as flow
    from src.fb_ads_rpa.draft import AdDraft, AdSetDraft
    from tests.test_second_primary_text_reaches_the_robot import _publish
    draft = CampaignDraft("C", "MX", "Demo", page_provided=True, ad_account_id=ACT,
                          ad_sets=[AdSetDraft("Set", ads=[AdDraft()])])
    out, _ = _build(monkeypatch, draft, {"ok": True, "act": ACT, "pages": [
        {"id": "123", "name": "Account Page", "source": "ad account"}]})
    result, seen = _publish(monkeypatch, out)
    assert seen["page_id"] == "123" and seen["page_name"] == "Account Page" and seen["page_name_unique"]
    job = result.to_job_dict()
    assert (job["page_id"], job["page_name"], job["page_origin"]) == ("123", "Account Page", "ad account")
    monkeypatch.setattr(config, "get_use_recorded_flow", lambda: False)
    monkeypatch.setattr(flow, "run_recorded_campaign_flow", lambda *a, **kw: pytest.fail("unsupported engine must not create"))
    refused = flow.AdsManagerFlow.__new__(flow.AdsManagerFlow).publish_campaign(out)
    assert not refused.success and refused.blocking_failures == ["page_identity"]


def test_duplicate_page_names_never_become_a_name_only_identity_proof(monkeypatch):
    draft = CampaignDraft("C", "MX", "Demo", page_provided=True, ad_account_id=ACT)
    out, _ = _build(monkeypatch, draft, {"ok": True, "act": ACT, "pages": [
        {"id": "123", "name": "Same name", "source": "ad account"},
        {"id": "456", "name": "Same name", "source": "ad account"}]})
    assert not out.errors and out.page_id == "123"
    assert page_identity_kwargs(out) == {"page_id": "123", "page_name": "Same name", "page_name_unique": False}


def test_profile_proof_is_shared_only_inside_one_build_for_the_same_page_and_link(monkeypatch):
    calls = []
    monkeypatch.setattr(account_pages, "read_pages", lambda *a, **kw: {"ok": True, "act": ACT,
        "pages": [{"id": "777", "name": "Profile Page", "source": "profile"}]})
    monkeypatch.setattr(account_pages, "validate_profile_page", lambda account, **kw: calls.append(kw) or "")
    monkeypatch.setattr(rpa, "enrich_campaign_drafts", lambda drafts, **kw: drafts)

    def drafts():
        return [CampaignDraft(name, "MX", "Demo", page_provided=True, ad_account_id=ACT, link=link)
                for name, link in [("First", "https://example.com/"), ("Second", "https://example.com/"),
                                   ("Third", "https://example.org/")]]

    monkeypatch.setattr(rpa, "slots_to_campaign_drafts", lambda *a, **kw: drafts())
    publisher = rpa.RpaPublisher()
    assert all(not d.errors for d in publisher.build_drafts([], account_label="Demo", country="MX"))
    assert [c["link"] for c in calls] == ["https://example.com/", "https://example.org/"]
    assert all(not d.errors for d in publisher.build_drafts([], account_label="Demo", country="MX"))
    assert len(calls) == 4, "another operation must obtain fresh proof"
