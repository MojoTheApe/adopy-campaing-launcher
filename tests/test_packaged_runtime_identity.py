import json
import sys

from src import packaged_runtime
from src.web import acl_update
from src.fb_ads_rpa import telemetry_sync


def test_packaged_version_and_update_check_never_need_git(tmp_path, monkeypatch):
    sha='f'*40
    (tmp_path/'acl-build-manifest.json').write_text(json.dumps({'runtime_source_sha':sha,'version':'0.1.0-test'}),encoding='utf-8')
    monkeypatch.setattr(sys,'frozen',True,raising=False)
    monkeypatch.setattr(sys,'_MEIPASS',str(tmp_path),raising=False)
    monkeypatch.setenv('ACL_SUPERVISED','1')
    monkeypatch.setattr(acl_update,'_installed',None)
    monkeypatch.setattr(telemetry_sync,'_VERSION_CACHE',None)
    def no_git(*a,**kw): raise AssertionError('frozen build must use embedded identity')
    monkeypatch.setattr(acl_update,'_git',no_git)
    monkeypatch.setattr(telemetry_sync,'_git',no_git)
    assert packaged_runtime.build_identity()['sha']==sha
    assert acl_update.installed()['sha']==sha
    state=acl_update.check(use_cache=False)
    assert state['ok'] and not state['available'] and not state['supervised']
    assert sha[:12] in telemetry_sync.flow_version()


def test_frozen_runtime_data_is_outside_program_files(tmp_path, monkeypatch):
    from config import paths
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setenv('ACL_CONFIG_DIR', str(tmp_path/'machine'))
    assert paths.get_runtime_workspace() == tmp_path/'machine/workspace'
    monkeypatch.setattr(sys, 'frozen', False)
    assert paths.get_runtime_workspace() == paths.PROJECT_ROOT
