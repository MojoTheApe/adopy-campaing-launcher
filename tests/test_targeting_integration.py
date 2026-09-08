"""Offline integration of the library, HTTP API, builder and RPA resolution."""
import asyncio
import json
import multiprocessing
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest

from src.analysis_agent import gsheet_targeting as gs, targeting_registry as tr
from src.web import app as web


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(tr, '_store', lambda: tmp_path / 'targets.json')
    monkeypatch.setattr(gs, '_cache', gs._Cache())
    monkeypatch.setattr(gs, 'last_error', '')
    monkeypatch.setattr(gs, '_sheets_services', lambda: [])


def test_bad_registry_is_preserved(tmp_path):
    for content in ('{broken', '[]', '{"bad": {}}'):
        tr._store().write_text(content, encoding='utf-8')
        with pytest.raises(tr.TargetingError):
            tr.add('New', 'Finance')
        assert tr._store().read_text(encoding='utf-8') == content


def _process_add(path, prefix):
    tr._store = lambda: Path(path)
    for i in range(8):
        tr.add(f'{prefix}-{i}', f'Interest {prefix} {i}')


def test_multiple_processes_do_not_lose_writes():
    ctx = multiprocessing.get_context('spawn')
    workers = [ctx.Process(target=_process_add, args=(str(tr._store()), i)) for i in range(3)]
    for w in workers:
        w.start()
    for w in workers:
        w.join(40)
        if w.is_alive():
            w.terminate()
        assert w.exitcode == 0
    assert len(tr.all_targets()) == 24


def test_concurrent_duplicate_check_is_atomic():
    def add(i):
        try:
            tr.add(str(i), 'Investment, Finance')
            return True
        except tr.TargetingError:
            return False
    with ThreadPoolExecutor(8) as pool:
        assert sum(pool.map(add, range(12))) == 1
    assert len(tr.all_targets()) == 1


def test_refresh_keeps_aliases_local_edits_and_removals():
    shared = [gs.TargetingProfile('A', 'Finance'), gs.TargetingProfile('Alias', 'Finance')]
    assert tr.import_targets(shared) == 2
    tr.update('A', 'A local', 'Investing', 'Owners')
    tr.remove('Alias')
    assert tr.import_targets(shared + [gs.TargetingProfile('New', 'Travel')]) == 1
    assert [t.name for t in tr.all_targets()] == ['A local', 'New']
    assert tr.lookup('A local').narrow == 'Owners'
    with pytest.raises(tr.TargetingError):
        tr.resolve_interests('A')
    with pytest.raises(tr.TargetingError):
        tr.resolve_interests('Alias')


def test_refresh_populated_library_and_builder_without_excel(monkeypatch, tmp_path):
    tr.add('Local', 'Finance', 'Owners')
    monkeypatch.setattr(gs, 'load_targeting_profiles', lambda **kw: {'New': gs.TargetingProfile('New', 'Travel')})
    monkeypatch.setattr(web, 'get_upload_fb_excel_path', lambda: str(tmp_path / 'missing.xlsx'))
    response = json.loads(web.api_targeting_profiles().body)
    assert {p['name'] for p in response['profiles']} == {'Local', 'New'}
    targets = {p['code']: p['fb_interest'] for p in web._load_ref_options()['targetings']}
    assert targets == {'Local': 'Finance [AND] Owners', 'New': 'Travel'}
    assert tr.resolve_interests('local') == targets['Local']
    tr.remove('New')
    assert 'New' not in {p['code'] for p in web._load_ref_options()['targetings']}


def test_offline_local_profile_reaches_enriched_draft(monkeypatch, tmp_path):
    from src.fb_ads_rpa import enrich_draft as en
    from src.fb_ads_rpa.draft import CampaignDraft, AdSetDraft
    tr.add('Local', 'Finance', 'Owners')
    monkeypatch.setattr(en, 'load_ad_account_map', lambda p: {})
    monkeypatch.setattr(en, 'resolve_account_entry', lambda *a: SimpleNamespace(ad_account_id='act_test', page_id='page', profile_id='profile'))
    draft = CampaignDraft('Test', 'CL', 'Test', ad_sets=[AdSetDraft('Set', targeting_label='Local')])
    en.enrich_campaign_drafts([draft], account_label='Test', upload_fb_path=str(tmp_path/'none'), ad_account_map_path=str(tmp_path/'map'), creatives_root=str(tmp_path))
    assert draft.ad_sets[0].targeting_label == 'Finance [AND] Owners'


def test_exact_lookup_does_not_pick_wrong_short_profile():
    tr.add('Day', 'Day trading')
    tr.add('Holiday', 'Travel')
    assert tr.resolve_interests('day') == 'Day trading'
    assert tr.resolve_interests('Day decorated') == ''


def test_and_fingerprints_match_rpa_syntax():
    assert tr.fingerprint('Finance, Investing', 'Owners') == tr.fingerprint(r'Owners\Investing, Finance')
    assert tr.fingerprint('Finance, Investing') != tr.fingerprint('Finance | Investing')


def test_failed_share_can_be_retried_and_status_survives(monkeypatch):
    t = tr.add('Local', 'Finance')
    def denied(*args):
        raise RuntimeError('access denied')
    monkeypatch.setattr(gs, 'add_targeting_profile', denied)
    assert tr.share(t) == 'access denied'
    assert not tr.lookup(t.name).shared
    monkeypatch.setattr(gs, 'add_targeting_profile', lambda *a: None)
    assert tr.share(tr.lookup(t.name)) == ''
    assert tr.lookup(t.name).shared
    changed = tr.update(t.name, t.name, 'Finance, Travel')
    assert not changed.shared


def _service(rows=None, denied=False):
    s = MagicMock()
    req = s.spreadsheets().get.return_value
    if denied:
        req.execute.side_effect = RuntimeError('403')
    else:
        req.execute.return_value = {'sheets': [{'properties': {'title': 'Detailed targeting'}}]}
        s.spreadsheets().values().get().execute.return_value = {'values': rows or []}
    return s


def test_read_falls_back_after_request_denied(monkeypatch):
    denied, employee = _service(denied=True), _service([['New', 'Finance', 'Owners']])
    monkeypatch.setattr(gs, '_sheets_services', lambda: iter([('service', denied), ('employee', employee)]))
    p = gs.load_targeting_profiles(force=True)
    assert p['New'].narrow == 'Owners'
    assert not gs.last_error
    assert employee.spreadsheets().values().get.call_args.kwargs['range'].endswith('A2:C')


def test_successful_read_does_not_initialize_second_credentials(monkeypatch):
    def services():
        yield 'service', _service([['New', 'Finance']])
        raise AssertionError('unused OAuth credentials must not be refreshed')
    monkeypatch.setattr(gs, '_sheets_services', services)
    assert gs.load_targeting_profiles(force=True)['New'].interests == 'Finance'


def test_append_refuses_unverified_library_and_retry_is_idempotent(monkeypatch):
    from src.analysis_agent import gsheet_write
    write = MagicMock()
    monkeypatch.setattr(gsheet_write, 'get_write_service', write)
    with pytest.raises(gs.TargetingWriteError):
        gs.add_targeting_profile('A', 'Finance')
    write.assert_not_called()
    monkeypatch.setattr(gs, '_sheets_services', lambda: [('service', _service([['A', 'Finance']]))])
    assert gs.add_targeting_profile('a', 'finance').name == 'A'
    write.assert_not_called()
    with pytest.raises(gs.TargetingWriteError):
        gs.add_targeting_profile('A', 'Travel')
    write.assert_not_called()


def test_http_validation_and_local_edit_scope(monkeypatch):
    monkeypatch.setattr(gs, 'add_targeting_profile', lambda *a: None)
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web.app), base_url='http://127.0.0.1') as c:
            for body in ([], {'name': 5}, {'narrow': None}, {'action': ['delete']}):
                r = await c.post('/api/targeting-profiles', json=body)
                assert r.status_code == 400
            r = await c.post('/api/targeting-profiles', json={'name':'A', 'interests':'Finance'})
            assert r.json()['shared']
            r = await c.post('/api/targeting-profiles', json={'action':'update', 'name':'A', 'interests':'Finance, Travel'})
            assert not r.json()['shared']
            r = await c.post('/api/targeting-profiles', json={'action':'share', 'name':'A'})
            assert r.json()['shared']
    asyncio.run(scenario())


def test_targeting_ui_edit_cancel_retry():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node required for UI behavior')
    root = Path(__file__).resolve().parents[1]
    r = subprocess.run([node, str(root/'tests/targeting_ui_harness.cjs'), str(root/'src/web/templates/report.html')], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
