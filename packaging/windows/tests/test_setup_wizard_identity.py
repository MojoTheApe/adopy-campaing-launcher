"""ACL-1242: шаг «Sign in» в мастере установки — порядок шагов, пропуск, имя, привязка.

Только офлайн: вход Google подменён, любой сокет — ошибка. Почты фиктивные.
"""
from __future__ import annotations

import inspect
import json
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PACKAGING_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = PACKAGING_DIR.parents[1]
for entry in (str(PACKAGING_DIR), str(REPO_ROOT)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

import setup_wizard
from setup_wizard import (
    SetupWizard,
    WizardValidationError,
    bind_identity_name,
    connect_with_google,
    identity_name_seed,
    identity_sign_in,
)
from src.analysis_agent import google_identity as gi


def _no_network(*args, **kwargs):
    raise AssertionError("the test touched the network")


class _Var:
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class StepOrderTests(unittest.TestCase):
    def test_sign_in_sits_between_system_check_and_connections(self):
        steps = SetupWizard.STEPS
        self.assertEqual(steps[1:4], ("System check", "Sign in", "Connections"))
        self.assertEqual(len(steps), 8)

    def test_validation_numbers_follow_the_steps(self):
        source = inspect.getsource(SetupWizard._validate_step)
        for number, marker in ((2, "self.identity"), (4, "validate_campaign_manager_name"),
                               (5, "validate_stats_workbook"), (6, "apply_machine_configuration")):
            block = source[source.index(f"self.step == {number}"):]
            block = block[: block.find("elif self.step ==")] if "elif self.step ==" in block else block
            self.assertIn(marker, block, number)


class SkipPathTests(unittest.TestCase):
    def _wizard(self):
        wizard = SetupWizard.__new__(SetupWizard)
        wizard.step = 2
        wizard.identity = None
        wizard.identity_skipped = False
        return wizard

    def test_next_is_refused_until_signed_in_or_not_now(self):
        wizard = self._wizard()
        with self.assertRaisesRegex(WizardValidationError, "press Not now"):
            wizard._validate_step()

    def test_not_now_lets_the_step_pass(self):
        wizard = self._wizard()
        wizard.identity_skipped = True
        wizard._validate_step()

    def test_signed_in_lets_the_step_pass(self):
        wizard = self._wizard()
        wizard.identity = {"email": "alice@adopy.com"}
        wizard._validate_step()


class NameSeedTests(unittest.TestCase):
    def test_claim_name_is_matched_to_the_known_list(self):
        self.assertEqual(identity_name_seed({"name": "alice smith"}, ["Bob Roe", "Alice Smith"]), "Alice Smith")

    def test_without_a_list_the_claim_is_used(self):
        self.assertEqual(identity_name_seed({"name": "Alice Smith"}, None), "Alice Smith")

    def test_nothing_without_a_claim(self):
        self.assertEqual(identity_name_seed({}, ["Alice Smith"]), "")
        self.assertEqual(identity_name_seed(None), "")

    def test_existing_my_cm_name_is_not_overwritten(self):
        wizard = SetupWizard.__new__(SetupWizard)
        wizard.identity = {"name": "Alice Smith"}
        wizard.campaign_manager_name = _Var("Already Set")
        wizard.tk = mock.MagicMock()
        wizard.body = mock.MagicMock()
        with mock.patch.object(SetupWizard, "_text"):
            wizard._identity()
        self.assertEqual(wizard.campaign_manager_name.get(), "Already Set")


class BindTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = Path(self.tmp.name)
        patcher = mock.patch.object(socket.socket, "connect", _no_network)
        patcher.start()
        self.addCleanup(patcher.stop)
        env = mock.patch.dict("os.environ", {"MY_CM_NAME": ""}, clear=False)
        env.start()
        self.addCleanup(env.stop)

    def _signed_in_record(self):
        (self.config / "identity.json").write_text(json.dumps({
            "state": "verified", "email": "alice@adopy.com", "name": "Alice Smith", "cm_name": "",
            "checked_at": gi._stamp(gi._now()), "verified_at": gi._stamp(gi._now())}), encoding="utf-8")

    def test_bind_writes_the_name_with_cm_checked(self):
        self._signed_in_record()
        seen = []
        ok = bind_identity_name(self.config, "Alice Smith",
                                binder=lambda name, cm_checked=False: seen.append((name, cm_checked)))
        self.assertTrue(ok)
        self.assertEqual(seen, [("Alice Smith", True)])

    def test_bind_does_nothing_when_not_signed_in(self):
        seen = []
        self.assertFalse(bind_identity_name(self.config, "Alice Smith", binder=lambda *a, **k: seen.append(a)))
        self.assertEqual(seen, [])

    def test_bind_failure_does_not_stop_setup(self):
        self._signed_in_record()

        def broken(name, cm_checked=False):
            raise RuntimeError("disk")

        self.assertFalse(bind_identity_name(self.config, "Alice Smith", binder=broken))

    def test_config_dir_is_restored_after_sign_in(self):
        with mock.patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("ACL_CONFIG_DIR", None)
            result = identity_sign_in(self.config, sign_in=lambda: {"ok": True, "identity": {}})
            self.assertTrue(result["ok"])
            self.assertNotIn("ACL_CONFIG_DIR", os.environ)

    def test_sign_in_failure_is_a_plain_result(self):
        def boom():
            raise RuntimeError("secret body")

        result = identity_sign_in(self.config, sign_in=boom)
        self.assertFalse(result["ok"])
        self.assertNotIn("secret", result["message"])


class LoginHintTests(unittest.TestCase):
    def test_hint_goes_to_the_sheets_sign_in(self):
        seen = []
        with tempfile.TemporaryDirectory() as tmp, mock.patch("src.analysis_agent.connection_settings.sign_in",
                                                              lambda **kw: seen.append(kw) or {}):
            connect_with_google(tmp, checks=lambda: [], login_hint="alice@adopy.com")
        self.assertEqual(seen, [{"login_hint": "alice@adopy.com"}])

    def test_hint_reaches_the_oauth_flow_and_is_optional(self):
        from src.analysis_agent import connection_settings as cs
        from src.analysis_agent import gsheet_accounts as ga

        self.assertIn("login_hint", inspect.signature(ga._build_sheets_service_oauth).parameters)
        self.assertEqual(inspect.signature(cs._service).parameters["login_hint"].default, "")
        self.assertEqual(inspect.signature(cs.sign_in).parameters["login_hint"].default, "")


if __name__ == "__main__":
    unittest.main()
