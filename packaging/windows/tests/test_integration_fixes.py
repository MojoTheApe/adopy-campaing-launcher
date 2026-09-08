"""Regression cases found while integrating the installer with production."""
import csv
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import diagnostics
import launcher
import machine_config as mc
import setup_wizard as wizard
import verify_core_isolation as guard


CLIENT = b'{"installed":{"client_id":"test-client","auth_uri":"https://accounts.example/auth","token_uri":"https://accounts.example/token"}}'


def configuration():
    return mc.MachineConfiguration('Test Operator', 'sheet' + 'x' * 30,
                                   (mc.AccountAssignment('Test Account', 'act_test'),), CLIENT)


def apply(config, root, **kwargs):
    mc.apply_machine_configuration(config, root, stats_workbook='stats.xlsx', upload_workbook='upload.xlsx', creatives_directory='creatives', **kwargs)


class ConfigurationTests(unittest.TestCase):
    def test_safety_env_replaces_spaced_exported_and_duplicate_keys(self):
        from dotenv import dotenv_values
        result = mc.merge_env('FB_RPA_ENGINE=unsafe\n export FB_RPA_ENGINE = unsafe2\nKEEP_ME=value\n', mc.SAFETY_ENV)
        self.assertEqual(dotenv_values(stream=io.StringIO(result))['FB_RPA_ENGINE'], 'recorded_flow')
        self.assertEqual(result.count('FB_RPA_ENGINE'), 1)
        self.assertIn('KEEP_ME=value', result)

    def test_repeat_setup_preserves_profiles_proxies_and_other_accounts(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root/'ad_account_map.csv').write_text('account_name,ad_account_id,page_id,multilogin_instance,folder_id,profile_id,proxy_pass,custom\nTest Account,act_test,page,B,folder,profile,proxy-secret,extra\nOther,act_other,,A,other-folder,other-profile,,keep\n', encoding='utf-8')
            apply(configuration(), root)
            rows = list(csv.DictReader(io.StringIO((root/'ad_account_map.csv').read_text(encoding='utf-8'))))
            self.assertEqual(rows[0]['profile_id'], 'profile')
            self.assertEqual(rows[0]['multilogin_instance'], 'B')
            self.assertEqual(rows[0]['proxy_pass'], 'proxy-secret')
            self.assertEqual(rows[0]['custom'], 'extra')
            self.assertEqual(rows[1]['account_name'], 'Other')

    def test_changed_account_identity_refuses_before_any_write(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw)
            old='account_name,ad_account_id\nTest Account,act_old\n'
            (root/'ad_account_map.csv').write_text(old, encoding='utf-8')
            with self.assertRaises(mc.MachineConfigError):
                apply(configuration(), root)
            self.assertEqual((root/'ad_account_map.csv').read_text(encoding='utf-8'), old)
            self.assertFalse((root/'.env').exists())

    def test_failed_replace_rolls_back_all_committed_files(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw)
            originals={root/'.env':b'KEEP=old\n', root/'ad_account_map.csv':b'account_name,ad_account_id\n', root/'oauth_client.json':CLIENT}
            for p,b in originals.items(): p.write_bytes(b)
            actual=mc.os.replace
            calls=[]
            def replace_once_fails(src,dst):
                calls.append(dst)
                if len(calls)==2: raise OSError('simulated full disk')
                return actual(src,dst)
            with mock.patch.object(mc.os,'replace',side_effect=replace_once_fails):
                with self.assertRaises(OSError): apply(configuration(),root)
            self.assertEqual({p:p.read_bytes() for p in originals},originals)


class WizardTests(unittest.TestCase):
    def test_google_checks_are_staged_until_finish_including_failure(self):
        from src.analysis_agent import gsheet_accounts
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw)
            old=b'{"client_id":"old-client","token":"keep-existing"}'
            (root/'token_accounts.json').write_bytes(old)
            service=mock.MagicMock()
            def build(client, token):
                self.assertNotEqual(Path(token).parent,root)
                self.assertFalse(Path(token).exists(), 'mismatched client token must not be reused')
                Path(token).write_bytes(b'{"client_id":"test-client","token":"new-token"}')
                return service
            with mock.patch.object(gsheet_accounts,'_build_sheets_service_oauth',side_effect=build):
                result=wizard.test_google_read_access(configuration(),root)
                self.assertIn(b'new-token',result.token)
                self.assertEqual((root/'token_accounts.json').read_bytes(),old)
                service.spreadsheets().get().execute.side_effect=RuntimeError('403 denied')
                with self.assertRaises(wizard.WizardValidationError):
                    wizard.test_google_read_access(configuration(),root)
            self.assertEqual((root/'token_accounts.json').read_bytes(),old)

    def test_delayed_exception_callback_keeps_its_message(self):
        w=wizard.SetupWizard.__new__(wizard.SetupWizard)
        w.busy=False;w.status=mock.MagicMock();w._render=mock.MagicMock();w._worker_failed=mock.MagicMock()
        callbacks=[]
        w.root=SimpleNamespace(after=lambda delay,fn:callbacks.append(fn))
        class Thread:
            def __init__(self,target,**kw): self.target=target
            def start(self): self.target()
        def fail(): raise RuntimeError('read denied')
        with mock.patch.object(wizard.threading,'Thread',Thread):
            w._run_worker(fail,lambda r:None)
        callbacks[0]()
        w._worker_failed.assert_called_once_with('read denied')

    def test_edited_tokens_cannot_reuse_a_previous_success(self):
        w=wizard.SetupWizard.__new__(wizard.SetupWizard)
        w.busy=False;w.step=5;w.multilogin_ok=True;w._verified_tokens={'A':'old'};w._tokens=lambda:{'A':'changed'}
        with self.assertRaises(wizard.WizardValidationError): w._validate_step()
        w.step=6;w.configuration=configuration();w._verified_google_configuration=w.configuration;w.google_ok=True;w.google_token=b'token'
        with mock.patch.object(wizard,'apply_machine_configuration') as apply_mock:
            with self.assertRaises(wizard.WizardValidationError): w._validate_step()
            apply_mock.assert_not_called()

    def test_changed_workspace_invalidates_google(self):
        w=wizard.SetupWizard.__new__(wizard.SetupWizard)
        w.google_ok=True;w.google_token=b'old';w._invalidate_google()
        self.assertFalse(w.google_ok);self.assertIsNone(w.google_token)


class LauncherOwnershipTests(unittest.TestCase):
    def test_config_override_is_shared_with_runtime(self):
        with tempfile.TemporaryDirectory() as raw, mock.patch.dict(os.environ,{'ACL_CONFIG_DIR':raw}):
            self.assertEqual(launcher._local_acl_dir(),Path(raw).resolve())

    def test_stale_pid_or_unrelated_executable_is_never_terminated(self):
        import psutil
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw);state=root/'server.json';exe=root/'ACL.exe'
            payload={'pid':42,'port':9090,'executable':str(exe),'created_at':100.0}
            state.write_text(json.dumps(payload),encoding='utf-8')
            process=mock.MagicMock();process.exe.return_value=str(exe);process.cmdline.return_value=[str(exe),'--server'];process.create_time.return_value=101.0
            with mock.patch.object(launcher,'_server_state_path',return_value=state), mock.patch.object(psutil,'Process',return_value=process), mock.patch.object(sys,'executable',str(exe)):
                self.assertEqual(launcher._stop_server(),2)
                process.terminate.assert_not_called()
                process.create_time.return_value=100.0
                self.assertEqual(launcher._stop_server(),0)
                process.terminate.assert_called_once()
                process.terminate.reset_mock();process.exe.return_value=str(root/'unrelated.exe')
                self.assertEqual(launcher._stop_server(),2);process.terminate.assert_not_called()

    def test_setup_does_not_write_while_dashboard_runs(self):
        with mock.patch.object(launcher,'_port_is_in_use',return_value=True), mock.patch.object(launcher,'_show_message'), mock.patch.object(wizard,'run_setup_wizard') as run:
            self.assertFalse(launcher._run_setup());run.assert_not_called()


class GuardTests(unittest.TestCase):
    def test_deletions_and_moves_are_detected_in_all_git_states(self):
        for state in ('unstaged','staged','committed','moved'):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as raw:
                root=Path(raw)
                def git(*args): return subprocess.check_output(['git',*args],cwd=root,text=True).strip()
                git('init','-q');git('config','user.name','Test');git('config','user.email','test@example.invalid')
                (root/'src').mkdir();(root/'src/core.py').write_text('core=True\n',encoding='utf-8')
                git('add','src/core.py');git('commit','-qm','baseline');base=git('rev-parse','HEAD')
                manifest=guard.BaselineManifest(base,'master','test',frozenset(),('packaging/windows/',))
                if state=='moved':
                    (root/'packaging/windows').mkdir(parents=True)
                    git('mv','src/core.py','packaging/windows/core.py');git('commit','-qm','move')
                else:
                    (root/'src/core.py').unlink()
                    if state in ('staged','committed'): git('add','src/core.py')
                    if state=='committed': git('commit','-qm','delete')
                _,blocked=guard.verify_isolation(root,manifest)
                self.assertIn('src/core.py',blocked)
                integrated=replace(manifest,runtime_policy='integrated')
                _,blocked=guard.verify_isolation(root,integrated)
                self.assertEqual(bool(blocked),state in ('unstaged','staged'))


class DiagnosticTests(unittest.TestCase):
    def test_quoted_credentials_and_modern_uuid_are_removed(self):
        source='{"refresh_token": "sensitive-refresh", "client_secret":"sensitive-client", "access_token":"sensitive-access"} profile=' + '12345678' + '-1234-7234-9234-' + '123456789abc'
        sanitized=diagnostics.sanitize_text(source)
        for value in ('sensitive-refresh','sensitive-client','sensitive-access','123456789abc'):
            self.assertNotIn(value,sanitized)

    def test_archive_names_and_summary_do_not_leak_local_values(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw);(root/'logs').mkdir()
            (root/'.env').write_text('PRIVATE_LABEL=PrivateAccount\nFLAG=true\n',encoding='utf-8')
            (root/'logs/PrivateAccount.log').write_text('PrivateAccount startup',encoding='utf-8')
            manifest=root/'manifest.json';manifest.write_text('{"version":"test"}',encoding='utf-8')
            with zipfile.ZipFile(diagnostics.export_diagnostics(root,manifest)) as archive:
                self.assertNotIn('PrivateAccount',' '.join(archive.namelist()))
                json.loads(archive.read('summary.json'))
                self.assertNotIn(b'PrivateAccount',archive.read('logs/01.txt'))


if __name__=='__main__': unittest.main()
