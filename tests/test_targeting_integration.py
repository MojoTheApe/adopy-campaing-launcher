"""Offline integration of the library, HTTP API, builder and RPA resolution."""
import asyncio
import json
import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from src.analysis_agent import team_library as tl, targeting_registry as tr
from src.web import app as web
from tests import targeting_worker


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(tr, '_store', lambda: tmp_path / 'targets.json')
    monkeypatch.delenv('ACL_LIBRARY_REPO', raising=False)
    monkeypatch.delenv('ACL_LIBRARY_TOKEN', raising=False)
    tl.use_backend(None)


def test_bad_registry_is_preserved(tmp_path):
    for content in ('{broken', '[]', '{"bad": {}}'):
        tr._store().write_text(content, encoding='utf-8')
        with pytest.raises(tr.TargetingError):
            tr.add('New', 'Finance')
        assert tr._store().read_text(encoding='utf-8') == content


def test_save_waits_out_a_briefly_held_registry(monkeypatch):
    # ACL-1419: Windows refuses os.replace (WinError 5) while the target is
    # open for a moment elsewhere; the save must wait, not drop the edit.
    real, refusals = os.replace, []

    def held_twice(src, dst):
        if len(refusals) < 2:
            refusals.append(dst)
            raise PermissionError(5, 'Access is denied')
        return real(src, dst)

    monkeypatch.setattr(os, 'replace', held_twice)
    tr.add('Held', 'Finance')
    assert len(refusals) == 2
    assert [t.name for t in tr.all_targets()] == ['Held']


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


def test_renamed_and_removed_targets_are_named_not_resolved():
    tr.add('A', 'Finance')
    tr.add('Alias', 'Travel')
    tr.update('A', 'A local', 'Investing', 'Owners')
    tr.remove('Alias')
    assert [t.name for t in tr.all_targets()] == ['A local']
    assert tr.lookup('A local').narrow == 'Owners'
    with pytest.raises(tr.TargetingError):
        tr.resolve_interests('A')
    with pytest.raises(tr.TargetingError):
        tr.resolve_interests('Alias')


def test_team_library_reaches_builder_without_excel(monkeypatch, tmp_path):
    tr.add('Local', 'Finance', 'Owners')
    lib = tr.snapshot()
    lib['targets']['New'] = tr.Target('New', 'Travel').to_dict()
    backend = tl.MemoryBackend()
    backend.put('targets.json', tl._dump(lib), None, 'seed')
    tl.use_backend(backend)
    monkeypatch.setattr(web, 'get_upload_fb_excel_path', lambda: str(tmp_path / 'missing.xlsx'))
    response = json.loads(web.api_targeting_profiles().body)
    assert {p['name'] for p in response['profiles']} == {'Local', 'New'}
    targets = {p['code']: p['fb_interest'] for p in web._load_ref_options()['targetings']}
    assert targets == {'Local': 'Finance [AND] Owners', 'New': 'Travel'}
    assert tr.resolve_interests('local') == targets['Local']
    tr.remove('New')
    assert 'New' not in {p['code'] for p in web._load_ref_options()['targetings']}
    tl.use_backend(None)


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


def test_http_validation_edit_delete_and_restore(monkeypatch):
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web.app), base_url='http://127.0.0.1') as c:
            for body in ([], {'name': 5}, {'narrow': None}, {'action': ['delete']}):
                r = await c.post('/api/targeting-profiles', json=body)
                assert r.status_code == 400
            r = await c.post('/api/targeting-profiles', json={'name':'A', 'interests':'Finance'})
            assert r.json()['ok']
            r = await c.post('/api/targeting-profiles', json={'action':'update', 'name':'A', 'interests':'Finance, Travel'})
            assert r.json()['profile']['interests'] == 'Finance, Travel'
            r = await c.post('/api/targeting-profiles', json={'action':'delete', 'name':'A'})
            assert r.json()['ok']
            listing = (await c.get('/api/targeting-profiles')).json()
            assert listing['profiles'] == [] and [d['name'] for d in listing['deleted']] == ['A']
            r = await c.post('/api/targeting-profiles', json={'action':'restore', 'name':'A'})
            assert r.json()['profile']['interests'] == 'Finance, Travel'
            r = await c.post('/api/targeting-profiles', json={'action':'share', 'name':'A'})
            assert r.status_code == 400
    asyncio.run(scenario())


def test_targeting_ui_edit_cancel_retry():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node required for UI behavior')
    root = Path(__file__).resolve().parents[1]
    r = subprocess.run([node, str(root/'tests/targeting_ui_harness.cjs'), str(root/'src/web/templates/report.html')], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
