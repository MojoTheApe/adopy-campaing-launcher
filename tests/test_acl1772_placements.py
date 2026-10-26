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


@pytest.mark.parametrize('raw', ['{}', 'false', '[]', '0', 'null', 'broken'])
def test_rpa_refuses_every_explicit_placement_value_through_parser(monkeypatch, raw):
    from src.web import app as web, publish_api
    from tests.test_acl1768_ad_sets import _fields
    monkeypatch.setattr(publish_api, 'api_enabled', lambda: False)
    slots = web._parse_build_upload_form(_fields(camp_0_placements=raw))[2]
    response = web._api_only_fields_refusal(slots)
    assert response is not None and response.status_code == 409
    assert b'Placements' in response.body


@pytest.mark.parametrize('value', [None, ''])
def test_rpa_legacy_missing_or_blank_placement_control_keeps_defaults(monkeypatch, value):
    from src.web import app as web, publish_api
    from tests.test_acl1768_ad_sets import _fields
    monkeypatch.setattr(publish_api, 'api_enabled', lambda: False)
    fields = _fields()
    if value is not None:
        fields['camp_0_placements'] = value
    slots = web._parse_build_upload_form(fields)[2]
    assert all('placements' not in slot for slot in slots)
    assert web._api_only_fields_refusal(slots) is None


@pytest.mark.parametrize('automatic,state,ok', [
    (True, 'ENABLED', True), (True, 'DISABLED', False),
    (False, 'DISABLED', True), (False, 'ENABLED', False),
    (True, 'UNKNOWN', False), (False, 'UNKNOWN', False),
    (True, '', False), (False, '', False),
    (True, {'malformed': True}, False),
])
def test_campaign_state_matches_explicit_mode_and_actual_adset_readback(tmp_path, automatic, state, ok):
    from src.web import publish_api
    draft = _draft(tmp_path, ads=1)
    for adset in draft.ad_sets:
        adset.placements = {'mode': 'advantage_plus'} if automatic else manual(facebook=['feed'])
    meta = FakeMeta()
    meta.mutate_on_read[('campaign', 'advantage_state_info')] = {
        'advantage_state': 'DISABLED', 'advantage_placement_state': state, 'advantage_audience_state': 'DISABLED'}
    out, _ = _run(meta, draft)
    assert out.readback['ok'] is ok, out.mismatches
    assert publish_api.result_dict(out)['publishable'] is ok
    if not ok:
        assert any(x['field'] == 'advantage_placement_state' for x in out.mismatches)
    else:
        assert all(x['ok'] for x in out.readback['checks'] if x['field'] in {'publisher_platforms', 'facebook_positions', 'instagram_positions'})


@pytest.mark.parametrize('mixed', [False, True])
def test_correct_campaign_flag_does_not_hide_wrong_adset_targeting(tmp_path, mixed):
    draft = _draft(tmp_path, ads=1)
    for adset in draft.ad_sets:
        adset.placements = {'mode': 'advantage_plus'}
    meta = FakeMeta()
    if mixed:
        draft.ad_sets[0].placements = manual(facebook=['feed'])
    meta.mutate_on_read[('campaign', 'advantage_state_info')] = {'advantage_placement_state': 'DISABLED' if mixed else 'ENABLED'}
    meta.mutate_on_read[('adset', 'targeting.facebook_positions')] = ['feed']
    out, _ = _run(meta, draft)
    assert not out.readback['ok']
    assert any(x['field'] == 'facebook_positions' for x in out.mismatches)


@pytest.mark.parametrize('state,ok', [('DISABLED', True), ('ENABLED', False), ('UNKNOWN', False)])
def test_mixed_modes_use_disabled_campaign_aggregate_and_exact_adsets(tmp_path, state, ok):
    draft = _draft(tmp_path, ads=1)
    draft.ad_sets[0].placements = manual(facebook=['feed'])
    draft.ad_sets[1].placements = {'mode': 'advantage_plus'}
    meta = FakeMeta()
    meta.mutate_on_read[('campaign', 'advantage_state_info')] = {'advantage_placement_state': state}
    out, _ = _run(meta, draft)
    assert out.readback['ok'] is ok, out.mismatches
    assert draft.ad_sets[0].placements == manual(facebook=['feed'])
    assert draft.ad_sets[1].placements == {'mode': 'advantage_plus'}



def test_auto_state_missing_remains_unverified_warning(tmp_path):
    draft = _draft(tmp_path, ads=1)
    for adset in draft.ad_sets:
        adset.placements = {'mode': 'advantage_plus'}
    out, _ = _run(FakeMeta(), draft)
    assert not out.readback['diagnostics']['placement_state_verified']
    assert any('not verified' in w and 'advantage_placement_state' in w for w in out.warnings)


def test_auto_reused_campaign_is_checked_without_duplicate_writes(tmp_path):
    draft = _draft(tmp_path, ads=1)
    for adset in draft.ad_sets:
        adset.placements = {'mode': 'advantage_plus'}
    meta = FakeMeta()
    meta.mutate_on_read[('campaign', 'advantage_state_info')] = {'advantage_placement_state': 'ENABLED'}
    first, _ = _run(meta, draft)
    before = len(meta.posts)
    second, _ = _run(meta, draft)
    assert first.readback['ok'] and second.readback['ok'], (first.mismatches, second.error)
    assert len(meta.posts) == before


def test_existing_auto_campaign_with_wrong_state_is_not_reused_or_verified(tmp_path):
    draft = _draft(tmp_path, ads=1)
    for adset in draft.ad_sets:
        adset.placements = {'mode': 'advantage_plus'}
    meta = FakeMeta()
    meta.mutate_on_read[('campaign', 'advantage_state_info')] = {'advantage_placement_state': 'ENABLED'}
    first, _ = _run(meta, draft)
    assert first.readback['ok']
    before = len(meta.posts)
    meta.mutate_on_read[('campaign', 'advantage_state_info')] = {'advantage_placement_state': 'DISABLED'}
    second, _ = _run(meta, draft)
    # ACL-1902 сохраняет существующую кампанию и создаёт новое имя при несовпадении;
    # здесь проверяем отказ reuse и итоговой верификации, не меняя эту политику.
    assert second.campaign_id != first.campaign_id
    assert len(meta.posts) > before
    assert not second.readback['ok']
    assert any(x['field'] == 'advantage_placement_state' for x in second.mismatches)
