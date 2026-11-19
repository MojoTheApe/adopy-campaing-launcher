"""ACL-2821: приложение Multilogin вошло в пространство другого слота (Inhouse vs KLD).

Только подделки: ни Multilogin, ни Meta. Проверяется: 403 узнаётся на обоих слотах;
сообщение про пространство попадает на аккаунт этого слота и старт не зовётся;
публикация и чтение не повторяют старты отказанного слота; другой слот не затронут;
первый удачный старт снимает отказ.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_api import mlx_session as mlx  # noqa: E402
from src.fb_api import profile as prof  # noqa: E402
from src.meta_campaign_agent.multilogin_launcher import MultiloginLauncherError  # noqa: E402

E403 = "Start profile failed HTTP 403: restricted permission"
TO_KLD = "Multilogin app is signed in to Inhouse - switch it to KLD to open this account"
TO_INHOUSE = "Multilogin app is signed in to KLD - switch it to Inhouse to open this account"


def _pid(n: int) -> str:
    return f"{n:08d}-0000-4000-8000-000000000000"


class Launcher:
    def __init__(self, fail=False):
        self.fail, self.calls = fail, []

    def start_profile(self, folder, pid, headless=False):
        self.calls.append(pid)
        if self.fail:
            raise MultiloginLauncherError(E403, status=403, error_code="FORBIDDEN_REQUEST")
        return SimpleNamespace(port="9333", folder_id=folder or "F", ws_endpoint=None)


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    mlx.reset_for_tests()
    from src import acl_sentry
    from src.fb_ads_rpa import run_telemetry as rt
    from src.fb_api import telemetry as tm

    monkeypatch.setattr(rt, "TELEMETRY_PATH", tmp_path / "rpa.jsonl")
    monkeypatch.setattr(tm, "journal_path", lambda: tmp_path / "pilot.jsonl")
    monkeypatch.setattr(acl_sentry, "report_step_failure", lambda *a, **kw: "id")
    for slot in "AB":
        monkeypatch.setenv(f"MULTILOGIN_AUTOMATION_TOKEN_{slot}", "")
        monkeypatch.delenv(f"MULTILOGIN_WORKSPACE_{slot}", raising=False)
        monkeypatch.delenv(f"MULTILOGIN_WORKSPACE_ID_{slot}", raising=False)
    monkeypatch.setattr(prof, "running_state", lambda launcher, pid: ("not_running", None))
    yield
    mlx.reset_for_tests()


def _open(slot, launcher, n=1):
    return prof.open_profile(_pid(n), "F", slot, launcher_factory=lambda s: launcher,
                             local_probe=lambda p: ("none", None))


def test_403_is_classified_on_both_slots():
    assert mlx.classify(E403, "A") == mlx.WORKSPACE_MISMATCH
    assert mlx.classify("Start profile failed HTTP 403: x FORBIDDEN_REQUEST", "A") == mlx.WORKSPACE_MISMATCH
    assert mlx.classify(E403, "B") == mlx.SESSION_LOST
    mlx.note_started("A")
    assert mlx.classify(E403, "B") == mlx.WORKSPACE_MISMATCH
    assert mlx.classify("Start profile failed HTTP 500: boom", "A") == ""


def test_slot_a_refusal_names_the_workspaces_on_that_account():
    with mlx.scope(account="Doc120"):
        with pytest.raises(prof.ProfileError) as ei:
            _open("A", Launcher(fail=True))
    assert ei.value.code == "mlx_workspace_mismatch" and str(ei.value) == TO_INHOUSE


def test_slot_b_refusal_after_slot_a_worked_means_the_app_is_in_inhouse():
    mlx.note_started("A")
    with mlx.scope(account="KLD B93"):
        with pytest.raises(prof.ProfileError) as ei:
            _open("B", Launcher(fail=True))
    assert ei.value.code == "mlx_workspace_mismatch" and str(ei.value) == TO_KLD


def test_slot_b_refusal_without_evidence_stays_a_lost_session():
    with pytest.raises(prof.ProfileError) as ei:
        _open("B", Launcher(fail=True))
    assert ei.value.code == "mlx_session_lost"


def test_known_mismatch_skips_the_start_and_leaves_the_other_slot_alone():
    a, b = Launcher(), Launcher(fail=True)
    mlx.begin_batch("job")
    mlx.note_started("A")
    with mlx.scope(account="KLD B90", batch="job"):
        with pytest.raises(prof.ProfileError):
            _open("B", b, 1)
    assert len(b.calls) == 1
    with mlx.scope(account="KLD B91", batch="job"):
        with pytest.raises(prof.ProfileError) as ei:
            _open("B", b, 2)
    assert str(ei.value) == TO_KLD and ei.value.code == "mlx_workspace_mismatch"
    assert len(b.calls) == 1                      # второй старт не звался
    with mlx.scope(account="Doc120", batch="job"):
        assert _open("A", a, 3).port == "9333"    # слот A не затронут


def test_a_new_call_skips_without_a_start_once_the_other_slot_has_started():
    mlx.note_started("A")
    mlx.note_failure(mlx.WORKSPACE_MISMATCH, "B", "KLD B90")
    mlx.begin_batch("read-2")
    mlx.note_started("A")                          # в этой пачке приложение уже показало себя в Inhouse
    skip = mlx.refusal("read-2", "B", "KLD B92")
    assert skip and skip["skipped"] and skip["code"] == "mlx_workspace_mismatch" and skip["message"] == TO_KLD


def test_a_new_call_without_evidence_still_tries_once_so_a_switched_app_is_noticed():
    mlx.note_started("A")
    mlx.note_failure(mlx.WORKSPACE_MISMATCH, "B", "KLD B90")
    mlx.begin_batch("later")
    assert mlx.refusal("later", "B", "KLD B91") is None


def test_one_successful_start_clears_the_refusal():
    mlx.begin_batch("job")
    mlx.note_started("A")
    mlx.note_failure(mlx.WORKSPACE_MISMATCH, "B", "KLD B90")
    assert mlx.refusal("job", "B", "KLD B91")["skipped"] is True
    mlx.note_started("B")                          # пространство сменили — старт прошёл
    assert mlx.refusal("job", "B", "KLD B91") is None


def test_workspace_names_come_from_the_machine_env(monkeypatch):
    monkeypatch.setenv("MULTILOGIN_WORKSPACE_A", "Acme Media")
    monkeypatch.setenv("MULTILOGIN_WORKSPACE_B", "Blue Team")
    text = mlx.mismatch_message("B")
    assert "Acme Media" in text and "Blue Team" in text


def test_a_publish_batch_does_not_repeat_starts_in_a_refused_slot():
    b = Launcher(fail=True)
    for n in (1, 2, 3, 4):
        with mlx.scope(account="KLD B90", batch="publish-test"):
            with pytest.raises(prof.ProfileError):
                _open("B", b, n)
    assert len(b.calls) == 2                       # отказ + одна перепроверка; дальше защёлка
    assert mlx.refusal("publish-test", "A", "Doc120") is None


def test_publish_and_read_open_with_their_own_batch():
    import inspect

    from src.analysis_agent import account_spend_api as read_api
    from src.web import publish_api

    assert "batch=self._mlx_batch" in inspect.getsource(publish_api.ApiPublisher)
    assert 'batch=f"read-' in inspect.getsource(read_api)
