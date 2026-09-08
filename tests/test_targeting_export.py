"""Failure-injected Sheets exports and the HTTP/builder/RPA boundaries, offline."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest

from config import paths
from src.analysis_agent import gsheet_targeting as gs, gsheet_write as gw, targeting_registry as tr
from src.web import app as web


class Sheets:
    """In-memory Sheets contract: a batch either commits wholly or leaves no edits."""
    def __init__(self, rows, *, title="Detailed targeting", failure="", columns=4):
        self.rows = deepcopy(rows)
        self.title, self.failure, self.columns = title, failure, columns
        self.height = max(len(rows), 2)
        self.service = MagicMock()
        api = self.service.spreadsheets.return_value
        api.get.return_value.execute.side_effect = lambda: {"sheets": [{"properties": {
            "title": self.title, "sheetId": 7,
            "gridProperties": {"rowCount": self.height, "columnCount": self.columns},
        }}]}
        api.values.return_value.get.side_effect = self.get
        api.batchUpdate.side_effect = self.batch
        self.api = api

    def get(self, **kw):
        def execute():
            if self.failure == "read":
                raise RuntimeError("synthetic read failure")
            assert kw['range'] in (f"'{self.title.replace(chr(39), chr(39) * 2)}'!A1:C",
                                   f"'{self.title.replace(chr(39), chr(39) * 2)}'!A2:C")
            start = 0 if '!A1:' in kw['range'] else 1
            return {'values': [r[:3] for r in self.rows[start:]]}
        return SimpleNamespace(execute=execute)

    def batch(self, **kw):
        def execute():
            if self.failure == 'before':
                raise RuntimeError('synthetic batch failure')
            candidate, height, columns = deepcopy(self.rows), self.height, self.columns
            for request in kw['body']['requests']:
                if 'appendDimension' in request:
                    r = request['appendDimension']
                    assert r['sheetId'] == 7 and r['length'] > 0
                    if r['dimension'] == 'ROWS':
                        height += r['length']
                    else:
                        assert r['dimension'] == 'COLUMNS'
                        columns += r['length']
                    continue
                r = request['updateCells']
                assert r['fields'] == 'userEnteredValue'
                area = r['range']
                assert area['sheetId'] == 7
                assert area['endRowIndex'] <= height and area['endColumnIndex'] <= columns
                while len(candidate) < height:
                    candidate.append([])
                for i in range(area['startRowIndex'], area['endRowIndex']):
                    cells = r['rows'][i-area['startRowIndex']]['values'] if i-area['startRowIndex'] < len(r['rows']) else []
                    while len(candidate[i]) < columns:
                        candidate[i].append('')
                    for j in range(area['startColumnIndex'], area['endColumnIndex']):
                        cell = cells[j-area['startColumnIndex']] if j-area['startColumnIndex'] < len(cells) else {}
                        candidate[i][j] = cell.get('userEnteredValue', {}).get('stringValue', '')
            self.rows, self.height, self.columns = candidate, height, columns
            if self.failure == 'after':
                raise RuntimeError('synthetic lost response after commit')
            return {}
        return SimpleNamespace(execute=execute)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, 'get_local_config_dir', lambda: str(tmp_path))
    monkeypatch.setattr(tr, '_store', lambda: tmp_path / 'targets.json')
    monkeypatch.setattr(gs, '_cache', gs._Cache())
    monkeypatch.setattr(gs, 'last_error', '')
    monkeypatch.setattr(gs, '_sheets_services', lambda: [])
    monkeypatch.setenv('GSHEET_TARGETING_SPREADSHEET_ID', 'synthetic-sheet')
    monkeypatch.setenv('GSHEET_TARGETING_TAB', 'Detailed targeting')


def use_sheet(monkeypatch, sheet):
    monkeypatch.setattr(gw, 'get_write_service', lambda: sheet.service)
    monkeypatch.setattr(gs, '_sheets_services', lambda: [('synthetic', sheet.service)])


@pytest.mark.parametrize('count', [0, 1, 501])
def test_export_replaces_all_profiles_preserving_header_and_other_columns(monkeypatch, count):
    original = [['Name', 'Interests', 'Narrow', 'Keep header']] + [[f'Old{i}', f'I{i}', '', 'Keep extra'] for i in range(501)]
    sheet = Sheets(original)
    use_sheet(monkeypatch, sheet)
    rows = [(f'New{i}', '=Literal interest', 'Owners') for i in range(count)]
    written, backup = gs.export_profiles(rows)
    assert written == count
    assert json.loads(Path(backup).read_text(encoding='utf-8')) == [r[:3] for r in original]
    assert sheet.rows[0] == original[0]
    assert [r[:3] for r in sheet.rows[1:count+1]] == [list(r) for r in rows]
    assert all(r[:3] == ['', '', ''] for r in sheet.rows[count+1:])
    assert all(r[3] == 'Keep extra' for r in sheet.rows[1:])
    sheet.api.values.return_value.clear.assert_not_called()
    sheet.api.values.return_value.update.assert_not_called()
    assert sheet.api.batchUpdate.call_count == 1


def test_export_expands_small_sheet_in_the_same_batch(monkeypatch):
    sheet = Sheets([['Header']], columns=1)
    use_sheet(monkeypatch, sheet)
    rows = [(f'T{i}', f'I{i}', '') for i in range(20)]
    assert gs.export_profiles(rows)[0] == 20
    assert sheet.rows[0] == ['Header']
    assert sheet.rows[1:] == [list(r) for r in rows]
    assert sheet.api.batchUpdate.call_count == 1


@pytest.mark.parametrize('failure', ['before', 'after'])
def test_export_failure_leaves_a_whole_library_and_exposes_backup(monkeypatch, tmp_path, failure):
    original = [['Header', 'I', 'N'], ['Old', 'Travel', '']]
    sheet = Sheets(original, failure=failure, columns=3)
    use_sheet(monkeypatch, sheet)
    tr.add('New', 'Finance')
    gs._cache.loaded_at = 100
    with pytest.raises(gs.TargetingWriteError) as error:
        tr.push_all()
    assert sheet.rows == (original if failure == 'before' else [original[0], ['New', 'Finance', '']])
    backup, = tmp_path.glob('targeting_sheet_backup_*.json')
    assert json.loads(backup.read_text(encoding='utf-8')) == original
    assert str(backup) in str(error.value)
    assert not tr.lookup('New').shared
    assert gs._cache.loaded_at == 0


@pytest.mark.parametrize('failure', ['read', 'disk'])
def test_failed_backup_blocks_every_mutation(monkeypatch, tmp_path, failure):
    original = [['Header', 'I', 'N'], ['Old', 'Travel', '']]
    sheet = Sheets(original, failure=failure)
    use_sheet(monkeypatch, sheet)
    if failure == 'disk':
        monkeypatch.setattr(gs.os, 'fsync', lambda *a: (_ for _ in ()).throw(OSError('disk full')))
    with pytest.raises((RuntimeError, OSError)):
        gs.export_profiles([('New', 'Finance', '')])
    assert sheet.rows == original
    sheet.api.batchUpdate.assert_not_called()
    sheet.api.values.return_value.clear.assert_not_called()
    assert not list(tmp_path.glob('targeting_sheet_backup_*'))


def test_backups_are_unique_and_allow_a_successfully_read_empty_tab(monkeypatch):
    sheet = Sheets([])
    use_sheet(monkeypatch, sheet)
    monkeypatch.setattr(gs.time, 'strftime', lambda *a: 'same-second')
    first = gs.snapshot_tab()
    sheet.rows = [['Header'], ['Old', 'Finance']]
    second = gs.snapshot_tab()
    assert first != second
    assert json.loads(Path(first).read_text(encoding='utf-8')) == []
    assert json.loads(Path(second).read_text(encoding='utf-8')) == sheet.rows


def test_snapshot_failure_without_credentials_is_not_an_empty_backup(tmp_path):
    with pytest.raises(gs.TargetingWriteError):
        gs.snapshot_tab()
    assert not list(tmp_path.glob('targeting_sheet_backup_*'))


def test_tab_quotes_are_escaped_in_backup(monkeypatch):
    sheet = Sheets([['H'], ['Old', 'Finance']], title="Team's targets")
    use_sheet(monkeypatch, sheet)
    monkeypatch.setenv('GSHEET_TARGETING_TAB', sheet.title)
    assert gs.export_profiles([('New', 'Travel', '')])[0] == 1


def test_corrupt_registry_blocks_export_before_any_google_access(monkeypatch):
    tr._store().write_text('{broken', encoding='utf-8')
    export = MagicMock()
    monkeypatch.setattr(gs, 'export_profiles', export)
    with pytest.raises(tr.TargetingError):
        tr.push_all()
    export.assert_not_called()
    assert tr._store().read_text(encoding='utf-8') == '{broken'


def test_no_export_can_opt_out_of_backup(monkeypatch):
    export = MagicMock()
    monkeypatch.setattr(gs, 'export_profiles', export)
    with pytest.raises(tr.TargetingError):
        tr.push_all(snapshot=False)
    export.assert_not_called()


def test_export_status_does_not_overwrite_concurrent_local_edits(monkeypatch):
    tr.add('Changed', 'Finance')
    tr.add('Same', 'Travel')
    def export(rows):
        assert rows == [('Changed', 'Finance', ''), ('Same', 'Travel', '')]
        tr.update('Changed', 'Changed', 'Investing')
        return 2, 'synthetic-backup.json'
    monkeypatch.setattr(gs, 'export_profiles', export)
    assert tr.push_all() == (2, 'synthetic-backup.json')
    assert tr.lookup('Changed').interests == 'Investing'
    assert not tr.lookup('Changed').shared
    assert tr.lookup('Same').shared


def test_local_status_failure_after_export_reports_success_and_backup(monkeypatch):
    tr.add('Local', 'Finance')
    def export(rows):
        monkeypatch.setattr(tr, '_save_raw', lambda *a: (_ for _ in ()).throw(OSError('disk full')))
        return 1, 'synthetic-backup.json'
    monkeypatch.setattr(gs, 'export_profiles', export)
    with pytest.raises(tr.TargetingError) as error:
        tr.push_all()
    assert 'Экспорт выполнен' in str(error.value)
    assert 'synthetic-backup.json' in str(error.value)


def test_export_api_requires_confirmation_and_uses_worker_thread(monkeypatch):
    import threading
    caller = threading.get_ident()
    calls = []
    def export():
        assert threading.get_ident() != caller
        calls.append(True)
        return 2, 'synthetic-backup.json'
    monkeypatch.setattr(tr, 'push_all', export)
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web.app), base_url='http://127.0.0.1') as client:
            for payload in ({'action':'export'}, {'action':'export','confirmation':True}):
                assert (await client.post('/api/targeting-profiles', json=payload)).status_code == 400
            assert not calls
            response = await client.post('/api/targeting-profiles', json={'action':'export','confirmation':'replace_shared_library'})
            assert response.status_code == 200
            assert response.json() == {'ok': True, 'exported': 2, 'backup': 'synthetic-backup.json'}
    asyncio.run(scenario())
    assert calls == [True]


def test_api_created_offline_target_reaches_builder_and_draft(monkeypatch, tmp_path):
    from src.fb_ads_rpa import enrich_draft as en
    from src.fb_ads_rpa.draft import CampaignDraft, AdSetDraft
    monkeypatch.setattr(gs, 'add_targeting_profile', lambda *a: (_ for _ in ()).throw(RuntimeError('offline')))
    monkeypatch.setattr(web, 'get_upload_fb_excel_path', lambda: str(tmp_path / 'missing.xlsx'))
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web.app), base_url='http://127.0.0.1') as client:
            r = await client.post('/api/targeting-profiles', json={
                'name': 'Investment Plus', 'interests': 'Finance, Investing', 'narrow': 'Owners',
            })
            assert r.status_code == 200 and not r.json()['shared']
            assert r.json()['profile']['name'] == 'Investment Plus'
            r = await client.get('/api/targeting-profiles')
            assert r.json()['duplicates'] == {'names': [], 'sets': []}
    asyncio.run(scenario())
    expected = 'Finance, Investing [AND] Owners'
    options = {p['code']: p['fb_interest'] for p in web._load_ref_options()['targetings']}
    assert options == {'Investment Plus': expected}
    monkeypatch.setattr(en, 'load_ad_account_map', lambda p: {})
    monkeypatch.setattr(en, 'resolve_account_entry', lambda *a: SimpleNamespace(
        ad_account_id='act_synthetic', page_id='page', profile_id='profile'))
    draft = CampaignDraft('Test', 'CL', 'Test', ad_sets=[AdSetDraft('Set', targeting_label='Investment Plus')])
    en.enrich_campaign_drafts([draft], account_label='Test', upload_fb_path=str(tmp_path/'missing.xlsx'),
        ad_account_map_path=str(tmp_path/'map'), creatives_root=str(tmp_path))
    assert draft.ad_sets[0].targeting_label == expected
