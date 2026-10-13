from types import SimpleNamespace
import pytest
from src.fb_api.creator import AccountFacts, resolve_page, PlanError


def test_automatic_uses_first_of_multiple_pages():
    plan = SimpleNamespace(page_id='', page_source='')
    resolve_page(plan, AccountFacts('act_1234567890', 'USD', ['123', '456'], []))
    assert plan.page_id == '123'


def test_empty_pages_has_actionable_error():
    with pytest.raises(PlanError, match='No Facebook Pages.*Refresh'):
        resolve_page(SimpleNamespace(page_id='', page_source=''),
                     AccountFacts('act_1234567890', 'USD', [], []))

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
        {'id': '456', 'name': 'First'}, {'id': '123', 'name': 'Second'}]
    client.truncated = ['/act_1234567890/promote_pages']
    with pytest.raises(ValueError, match='incomplete'):
        pages_from_client(client, 'act_1234567890')


def test_failed_read_differs_from_empty_and_retry_succeeds(monkeypatch):
    from src.web import account_pages as p
    from src.analysis_agent import account_spend as accounts, account_spend_api as api
    monkeypatch.setattr(accounts, 'serialized_browser', lambda **kw: lambda fn: fn)
    monkeypatch.setattr(api, '_failure', lambda account, exc: ('Session unavailable', 'failed'))
    def failed(account):
        raise RuntimeError('failed')
    monkeypatch.setattr(p, '_read_in_session', failed)
    assert p.read_pages('Demo')['state'] == 'failed'
    monkeypatch.setattr(p, '_read_in_session', lambda account: {'ok': True, 'state': 'empty', 'pages': []})
    assert p.read_pages('Demo')['state'] == 'empty'
    monkeypatch.setattr(p, '_read_in_session', lambda account: {'ok': True, 'state': 'ready', 'pages': [{'id': '123', 'name': 'Page'}]})
    assert p.read_pages('Demo')['ok']


def test_form_parser_carries_campaign_specific_selection():
    from src.web.app import _parse_build_upload_form
    _, _, slots = _parse_build_upload_form({'account_label': 'Demo', 'country': 'MX', 'camp_count': '2',
        'camp_0_name': 'First', 'camp_1_name': 'Second', 'camp_0_page_id': '123', 'camp_1_page_id': ''})
    assert [s['page_id'] for s in slots] == ['123', '']


@pytest.mark.parametrize('available,selected,expected,error', [
    ({'ok': True, 'act': 'act_1234567890', 'pages': [{'id': '123', 'name': 'First'}, {'id': '456', 'name': 'Second'}]}, '', '123', False),
    ({'ok': True, 'act': 'act_1234567890', 'pages': [{'id': '123', 'name': 'First'}, {'id': '456', 'name': 'Second'}]}, '456', '456', False),
    ({'ok': True, 'act': 'act_1234567890', 'pages': []}, '', None, True),
    ({'ok': False, 'message': 'Could not load pages'}, '', None, True),
    ({'ok': True, 'act': 'act_999', 'pages': [{'id': '123', 'name': 'Wrong account'}]}, '', None, True),
])
def test_rpa_receives_resolved_page_or_stops_before_browser(monkeypatch, available, selected, expected, error):
    from src.fb_ads_rpa import rpa_publisher as rpa
    from src.fb_ads_rpa.draft import CampaignDraft
    from src.web import account_pages
    draft = CampaignDraft('C', 'MX', 'Demo', page_id=selected or None, page_provided=True, ad_account_id='act_1234567890')
    monkeypatch.setattr(rpa, 'slots_to_campaign_drafts', lambda *a, **k: [draft])
    monkeypatch.setattr(rpa, 'enrich_campaign_drafts', lambda drafts, **k: drafts)
    monkeypatch.setattr(account_pages, 'read_pages', lambda account: available)
    publisher = rpa.RpaPublisher()
    result = publisher.build_drafts([], account_label='Demo', country='MX')[0]
    assert bool(result.errors) is error
    if not error:
        assert result.page_id == expected
        assert result.page_name_unique and result.page_name


def test_conflicting_page_choices_cannot_be_silently_grouped():
    from src.fb_ads_rpa.build_draft import slots_to_campaign_drafts
    drafts = slots_to_campaign_drafts([{'name':'Same', 'budget':'50', 'page_id':'123'},
                                       {'name':'Same', 'budget':'50', 'page_id':''}], account_label='Demo',country='MX')
    assert any('different Facebook Page choices' in e for e in drafts[0].errors)
