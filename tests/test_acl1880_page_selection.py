from types import SimpleNamespace
import pytest
from src.fb_api.creator import AccountFacts, resolve_page, PlanError


def test_automatic_uses_first_of_multiple_pages():
    plan = SimpleNamespace(page_id='', page_source='')
    resolve_page(plan, AccountFacts('act_1234567890', 'USD', ['123', '456'], []))
    assert plan.page_id == '123'


def test_empty_pages_offer_manual_website_completion():
    plan = SimpleNamespace(page_id='', page_source='', ad_sets=[])
    warnings = resolve_page(plan, AccountFacts('act_1234567890', 'USD', [], []))
    assert plan.page_id == '' and plan.page_source == 'manual completion'
    assert 'ads will not be created' in warnings[0]

@pytest.mark.parametrize('chosen', ['123', '456'])
def test_explicit_page_preserved(chosen):
    plan = SimpleNamespace(page_id=chosen, page_source='draft')
    resolve_page(plan, AccountFacts('act_1234567890', 'USD', ['123', '456'], []))
    assert plan.page_id == chosen


def test_foreign_page_refused():
    with pytest.raises(PlanError, match='not among'):
        resolve_page(SimpleNamespace(page_id='789', page_source='draft'),
                     AccountFacts('act_1234567890', 'USD', ['123'], []))


def test_builder_auto_does_not_inherit_account_default(tmp_path):
    from src.fb_ads_rpa.build_draft import slots_to_campaign_drafts
    from src.fb_ads_rpa.enrich_draft import enrich_campaign_drafts
    mapping = tmp_path / 'map.csv'
    mapping.write_text('account_name,ad_account_id,page_id\nDemo,act_1234567890,999\n')
    for selected in ['', '456']:
        d = slots_to_campaign_drafts([{'name': 'C', 'budget': '50', 'page_id': selected}],
                                     account_label='Demo', country='MX')[0]
        enrich_campaign_drafts([d], account_label='Demo', upload_fb_path=str(tmp_path / 'none.xlsx'),
                               ad_account_map_path=str(mapping), creatives_root=str(tmp_path), preview_mode=True)
        assert (d.page_id or '') == selected
    assert '999' in mapping.read_text(), 'campaign choice must not write the account map'


def test_page_discovery_preserves_order_names_and_rejects_truncation():
    from src.web.account_pages import pages_from_client
    client = SimpleNamespace(get_all=lambda *a, **k: [
        {'id': '456', 'name': 'First'}, {'id': '123', 'name': 'Second'}, {'id': '456', 'name': 'Duplicate'}], truncated=[])
    assert pages_from_client(client, 'act_1234567890') == [
        {'id': '456', 'name': 'First', 'source': 'ad account'}, {'id': '123', 'name': 'Second', 'source': 'ad account'}]
    client.truncated = ['/act_1234567890/promote_pages']
    with pytest.raises(ValueError, match='incomplete'):
        pages_from_client(client, 'act_1234567890')


def test_failed_read_differs_from_empty_and_retry_succeeds(monkeypatch, tmp_path):
    from src.web import account_pages as p
    from src.analysis_agent import account_spend as accounts, account_spend_api as api
    monkeypatch.setattr(accounts, 'serialized_browser', lambda **kw: lambda fn: fn)
    monkeypatch.setattr(api, '_failure', lambda account, exc: ('Session unavailable', 'failed'))
    def failed(account):
        raise RuntimeError('failed')
    monkeypatch.setattr(p, '_cache_path', lambda: tmp_path / 'account_pages_cache.json')
    monkeypatch.setattr(p, '_read_in_session', failed)
    assert p.read_pages('Demo')['state'] == 'failed'
    monkeypatch.setattr(p, '_read_in_session', lambda account: {'ok': True, 'state': 'empty', 'pages': []})
    assert p.read_pages('Demo', force=True)['state'] == 'empty'
    monkeypatch.setattr(p, '_read_in_session', lambda account: {'ok': True, 'state': 'ready', 'pages': [{'id': '123', 'name': 'Page'}]})
    retried = p.read_pages('Demo', force=True)
    assert retried['ok'] and retried['state'] == 'ready' and 'cached_at' not in retried


def test_form_parser_carries_campaign_specific_selection():
    from src.web.app import _parse_build_upload_form
    _, _, slots = _parse_build_upload_form({'account_label': 'Demo', 'country': 'MX', 'camp_count': '2',
        'camp_0_name': 'First', 'camp_1_name': 'Second', 'camp_0_page_id': '123', 'camp_1_page_id': ''})
    assert [s['page_id'] for s in slots] == ['123', '']


@pytest.mark.parametrize('available,selected,expected,error', [
    # ACL-1897: Automatic resolves the first usable Page, just like the API.
    ({'ok': True, 'act': 'act_1234567890', 'pages': [{'id': '123', 'name': 'First', 'source': 'ad account'}]}, '', '123', False),
    ({'ok': True, 'act': 'act_1234567890', 'pages': []}, '', None, False),
    ({'ok': True, 'act': 'act_1234567890', 'pages': [{'id': '123', 'name': 'First', 'source': 'ad account'}, {'id': '456', 'name': 'Second', 'source': 'ad account'}]}, '456', '456', False),
    ({'ok': True, 'act': 'act_1234567890', 'pages': []}, '456', None, True),
    ({'ok': False, 'message': 'Could not load pages'}, '456', None, True),
    ({'ok': True, 'act': 'act_999', 'pages': [{'id': '456', 'name': 'Wrong account'}]}, '456', None, True),
])
def test_rpa_receives_resolved_page_or_stops_before_browser(monkeypatch, available, selected, expected, error):
    from src.fb_ads_rpa import rpa_publisher as rpa
    from src.fb_ads_rpa.draft import CampaignDraft
    from src.web import account_pages
    draft = CampaignDraft('C', 'MX', 'Demo', page_id=selected or None, page_provided=True, ad_account_id='act_1234567890')
    monkeypatch.setattr(rpa, 'slots_to_campaign_drafts', lambda *a, **k: [draft])
    monkeypatch.setattr(rpa, 'enrich_campaign_drafts', lambda drafts, **k: drafts)
    monkeypatch.setattr(account_pages, 'read_pages', lambda account, **k: available)
    publisher = rpa.RpaPublisher()
    result = publisher.build_drafts([], account_label='Demo', country='MX')[0]
    assert bool(result.errors) is error
    if not error and expected:
        assert result.page_id == expected
        assert result.page_name_unique and result.page_name
    if not error and not expected:
        assert not result.page_id


def test_conflicting_page_choices_cannot_be_silently_grouped():
    from src.fb_ads_rpa.build_draft import slots_to_campaign_drafts
    drafts = slots_to_campaign_drafts([{'name':'Same', 'budget':'50', 'page_id':'123'},
                                       {'name':'Same', 'budget':'50', 'page_id':''}], account_label='Demo',country='MX')
    assert any('different Facebook Page choices' in e for e in drafts[0].errors)


def test_confirmed_empty_automatic_rpa_keeps_existing_identity(monkeypatch):
    """A fresh confirmed-empty list keeps the existing Website RPA behavior."""
    from src.fb_ads_rpa import rpa_publisher as rpa, page_identity
    from src.fb_ads_rpa.ads_manager_flow import page_identity_kwargs, explicit_page_choice
    from src.fb_ads_rpa.build_draft import slots_to_campaign_drafts
    from src.fb_ads_rpa.draft import CampaignDraft
    from src.web import account_pages
    drafts = slots_to_campaign_drafts([{'name': 'C', 'budget': '50', 'page_id': ''}], account_label='Demo', country='MX')
    assert drafts[0].page_provided and not drafts[0].page_id
    calls = []
    monkeypatch.setattr(account_pages, 'read_pages', lambda *a, **k: calls.append((a, k)) or {
        'ok': True, 'act': 'act_1234567890', 'pages': []})
    monkeypatch.setattr(page_identity, 'ensure_page_identity', lambda *a, **k: calls.append('identity'))
    monkeypatch.setattr(rpa, 'slots_to_campaign_drafts', lambda *a, **k: drafts)
    monkeypatch.setattr(rpa, 'enrich_campaign_drafts', lambda d, **k: d)
    drafts[0].ad_account_id = 'act_1234567890'
    out = rpa.RpaPublisher().build_drafts([], account_label='Demo', country='MX')
    assert calls == [(('Demo',), {'force': True})] and not out[0].errors and page_identity_kwargs(out[0]) == {}
    assert not explicit_page_choice(out[0])
    chosen = CampaignDraft('C', 'MX', 'Demo', page_id='123', page_provided=True, page_name='P', page_name_unique=True)
    assert page_identity_kwargs(chosen) == {'page_id': '123', 'page_name': 'P', 'page_name_unique': True}


def test_render_time_read_is_cache_only_and_never_opens_a_profile(monkeypatch, tmp_path):
    from src.web import account_pages as p
    monkeypatch.setattr(p, '_cache_path', lambda: tmp_path / 'account_pages_cache.json')
    def boom(account):
        raise AssertionError('profile must not be opened')
    monkeypatch.setattr(p, '_read_in_session', boom)
    miss = p.cached_only('Demo')
    assert miss['state'] == 'not_loaded' and miss['ok'] and miss['pages'] == []
    p.remember_pages('Demo', {'ok': True, 'state': 'ready', 'pages': [{'id': '1', 'name': 'A'}]})
    assert p.cached_only('Demo')['state'] == 'ready'


def test_account_pages_endpoint_cached_flag(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from src.web import account_pages as p, app as webapp
    monkeypatch.setattr(p, '_cache_path', lambda: tmp_path / 'account_pages_cache.json')
    monkeypatch.setattr(p, '_read_in_session', lambda a: (_ for _ in ()).throw(AssertionError('opened')))
    r = TestClient(webapp.app, base_url='http://127.0.0.1:9090').post('/api/account-pages', headers={'Origin': 'http://127.0.0.1:9090'}, data={'account_label': 'Demo', 'cached': '1'})
    assert r.status_code == 200 and r.json()['state'] == 'not_loaded'
