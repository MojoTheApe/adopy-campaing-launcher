"""ACL-1772: выбранные площадки каждого набора доходят до API и readback."""
import pytest

from src.fb_api import creator as cr
from tests.test_acl1768_ad_sets import _draft, _run, FakeMeta


def manual(facebook=(), instagram=()):
    return {"mode": "manual", "facebook_positions": list(facebook), "instagram_positions": list(instagram)}


def test_manual_placements_are_independent_per_adset_and_read_back(tmp_path):
    draft = _draft(tmp_path, ads=1)
    draft.ad_sets[0].placements = manual(facebook=['feed'])
    draft.ad_sets[1].placements = manual(instagram=['stream'])
    plan = cr.plan_from_draft(draft)
    assert plan.ad_sets[0].placements == manual(facebook=['feed'])
    meta = FakeMeta()
    out, _ = _run(meta, draft)
    assert not out.error and out.readback['ok'], out.mismatches
    sets = [row for row in meta.objects.values() if row['_kind'] == 'adset']
    first, second = [row['targeting'] for row in sets]
    assert first['publisher_platforms'] == ['facebook']
    assert first['facebook_positions'] == ['feed']
    assert 'instagram_positions' not in first
    assert second['publisher_platforms'] == ['instagram']
    assert second['instagram_positions'] == ['stream']
    assert 'facebook_positions' not in second


def test_advantage_placements_omit_manual_restrictions(tmp_path):
    draft = _draft(tmp_path, ads=1)
    for adset in draft.ad_sets:
        adset.placements = {'mode': 'advantage_plus'}
    meta = FakeMeta()
    out, _ = _run(meta, draft)
    assert not out.error and out.readback['ok'], out.mismatches
    for row in meta.objects.values():
        if row['_kind'] == 'adset':
            assert not {'publisher_platforms', 'facebook_positions', 'instagram_positions'} & row['targeting'].keys()


@pytest.mark.parametrize('selection', [manual(), manual(facebook=['unknown']), {'mode': 'unknown'}, 'malformed'])
def test_invalid_placements_refuse_before_writes(tmp_path, selection):
    draft = _draft(tmp_path, ads=1)
    draft.ad_sets[0].placements = selection
    with pytest.raises(cr.PlanError, match='[Pp]lacement'):
        cr.plan_from_draft(draft)


def test_feed_only_needs_no_vertical_and_desktop_is_allowed(tmp_path):
    draft = _draft(tmp_path, ads=1)
    for adset in draft.ad_sets:
        adset.placements = manual(facebook=['feed'])
        adset.device = 'desktop'
    plan = cr.plan_from_draft(draft)
    assert not cr.plan_has_instagram_placements(plan)
    assert not any('vertical' in warning.lower() for warning in plan.warnings)
    out, _ = _run(FakeMeta(), draft)
    assert not out.error and out.readback['ok'], out.mismatches


def test_stories_without_vertical_warns_instead_of_blocking(tmp_path):
    draft = _draft(tmp_path, ads=1)
    for adset in draft.ad_sets:
        adset.placements = manual(facebook=['story'])
    plan = cr.plan_from_draft(draft)
    assert any('vertical' in warning.lower() for warning in plan.warnings)
    assert not cr.plan_has_instagram_placements(plan)


def test_old_drafts_keep_existing_fixed_placements(tmp_path):
    meta = FakeMeta()
    out, _ = _run(meta, _draft(tmp_path, ads=1))
    assert not out.error and out.readback['ok']
    for row in meta.objects.values():
        if row['_kind'] == 'adset':
            assert row['targeting']['publisher_platforms'] == list(cr.PUBLISHER_PLATFORMS)
            assert row['targeting']['facebook_positions'] == list(cr.FACEBOOK_POSITIONS)
            assert row['targeting']['instagram_positions'] == list(cr.INSTAGRAM_POSITIONS)


def test_form_slots_and_draft_preserve_independent_choices():
    import json
    from src.web import app as web
    from src.fb_ads_rpa.build_draft import slots_to_campaign_drafts
    from tests.test_acl1768_ad_sets import _multi_fields
    fields = _multi_fields(camp_0_placements=json.dumps(manual(facebook=['feed'])),
                           camp_0_adset_1_placements=json.dumps({'mode': 'advantage_plus'}))
    country, label, slots = web._parse_build_upload_form(fields)
    assert [slot['placements']['mode'] for slot in slots] == ['manual', 'manual', 'advantage_plus']
    [draft] = slots_to_campaign_drafts(slots, account_label=label, country=country)
    assert draft.ad_sets[0].placements == manual(facebook=['feed'])
    assert draft.ad_sets[1].to_preview_dict()['placements'] == {'mode': 'advantage_plus'}


@pytest.mark.parametrize('raw', ['{}', 'null', 'false', '[]', 'broken'])
def test_explicit_invalid_form_selection_is_not_defaulted(raw):
    from src.web import app as web
    from tests.test_acl1768_ad_sets import _fields
    slots = web._parse_build_upload_form(_fields(camp_0_placements=raw))[2]
    assert slots[0]['placements'] is not None
    from types import SimpleNamespace
    errors = []
    cr._plan_placements(SimpleNamespace(placements=slots[0]['placements']), errors, 'A')
    assert errors


def test_rpa_refuses_saved_custom_placements(monkeypatch):
    from src.web import app as web, publish_api
    monkeypatch.setattr(publish_api, 'api_enabled', lambda: False)
    response = web._api_only_fields_refusal([{'placements': manual(facebook=['feed'])}])
    assert response.status_code == 409 and b'Placements' in response.body


def test_instagram_identity_guard_uses_actual_selection(tmp_path):
    d = _draft(tmp_path, ads=1)
    for adset in d.ad_sets:
        adset.placements = manual(facebook=['feed'])
    assert not cr.plan_has_instagram_placements(cr.plan_from_draft(d))
    assert not cr.plan_has_instagram([cr.placement_targeting(manual(facebook=['feed']))])
    d.ad_sets[1].placements = {'mode': 'advantage_plus'}
    assert cr.plan_has_instagram_placements(cr.plan_from_draft(d))
    assert cr.plan_has_instagram([{}])


def test_pair_rules_use_only_selected_positions_and_read_back(tmp_path):
    from tests.test_acl1774_texts_vertical import _ad_with, _run as run_pair
    d = _ad_with(tmp_path)
    d.ad_sets[0].placements = manual(facebook=['feed', 'story'])
    meta = FakeMeta()
    out, _ = run_pair(meta, d)
    assert not out.error and out.readback['ok'], out.mismatches
    creative = next(x for x in meta.objects.values() if x['_kind'] == 'creative')
    rules = creative['asset_feed_spec']['asset_customization_rules']
    assert {tuple(rule['customization_spec']['facebook_positions']) for rule in rules} == {('feed',), ('story',)}
    assert all(rule['customization_spec']['publisher_platforms'] == ['facebook'] for rule in rules)
    assert all('instagram_positions' not in rule['customization_spec'] for rule in rules)


@pytest.mark.parametrize('selection', [manual(facebook=['feed']), manual(facebook=['story']), {'mode': 'advantage_plus'}])
def test_unverified_two_file_mapping_refuses_explicitly(tmp_path, selection):
    from tests.test_acl1774_texts_vertical import _ad_with
    d = _ad_with(tmp_path)
    d.ad_sets[0].placements = selection
    with pytest.raises(cr.PlanError, match='two-file pair|two files'):
        cr.plan_from_draft(d)
    assert d.ad_sets[0].ads[0].vertical_path


def test_readback_reports_changed_placements(tmp_path):
    d = _draft(tmp_path, ads=1)
    for adset in d.ad_sets:
        adset.placements = manual(facebook=['feed'])
    meta = FakeMeta()
    meta.mutate_on_read[('adset', 'targeting')] = {'publisher_platforms': ['facebook', 'instagram'], 'facebook_positions': ['feed'], 'instagram_positions': ['stream']}
    out, _ = _run(meta, d)
    assert not out.readback['ok']
    assert any(x['field'] == 'instagram_positions' for x in out.mismatches)


def test_facebook_only_creates_and_audits_without_any_instagram_account(tmp_path):
    from tests.test_acl1774_texts_vertical import _ad_with
    from tests.test_acl1819_preflight import AuditMeta, _audit, _by
    d = _ad_with(tmp_path, vertical=None)
    d.ad_sets[0].placements = manual(facebook=['feed'])
    from tests.test_acl1761_api_create import _run as run_one
    meta = FakeMeta(instagram='none')
    out, _ = run_one(meta, d)
    assert not out.error and out.readback['ok'], out.mismatches
    creatives = [o for o in meta.objects.values() if o['_kind'] == 'creative']
    assert all('instagram_user_id' not in row['object_story_spec'] for row in creatives)
    audit = _by(_audit(AuditMeta(instagram='none'), d))
    assert audit['instagram_identity']['status'] == 'ok'
    assert audit['media_vertical']['status'] == 'ok'
