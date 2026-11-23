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
TO_KLD = ("Multilogin refused to open this KLD profile - the Multilogin app may be signed in to "
          "Inhouse (switch it to KLD), or the KLD sign-in is in use on another computer")
TO_INHOUSE = ("Multilogin refused to open this Inhouse profile - the Multilogin app may be signed in to "
              "KLD (switch it to Inhouse), or the Inhouse sign-in is in use on another computer")


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
    mlx.note_started("A")                          # прошлые старты ничего не меняют: причину 403 не узнать
    assert mlx.classify(E403, "B") == mlx.SESSION_LOST
    assert mlx.classify("Start profile failed HTTP 500: boom", "A") == ""


def test_slot_a_refusal_names_the_workspaces_on_that_account():
    with mlx.scope(account="Doc120"):
        with pytest.raises(prof.ProfileError) as ei:
            _open("A", Launcher(fail=True))
    assert ei.value.code == "mlx_workspace_mismatch" and str(ei.value) == TO_INHOUSE


def test_slot_b_refusal_is_hedged_whatever_started_before():
    for earlier in ("", "A"):
        mlx.reset_for_tests()
        if earlier:
            mlx.note_started(earlier)
        with mlx.scope(account="KLD B93"):
            with pytest.raises(prof.ProfileError) as ei:
                _open("B", Launcher(fail=True))
        assert ei.value.code == "mlx_session_lost" and str(ei.value) == TO_KLD


def test_slot_a_refusal_rechecks_once_then_skips_and_leaves_slot_b_alone():
    a, b = Launcher(fail=True), Launcher()
    mlx.begin_batch("job")
    for n in (1, 2, 3):
        with mlx.scope(account=f"Doc12{n}", batch="job"):
            with pytest.raises(prof.ProfileError) as ei:
                _open("A", a, n)
        assert ei.value.code == "mlx_workspace_mismatch" and str(ei.value) == TO_INHOUSE
    assert len(a.calls) == 2                       # отказ + одна перепроверка, третий не звался
    with mlx.scope(account="KLD B90", batch="job"):
        assert _open("B", b, 4).port == "9333"     # другой слот не затронут


def test_first_refusal_is_not_latched_it_gets_one_recheck():
    mlx.begin_batch("job")
    mlx.note_failure(mlx.WORKSPACE_MISMATCH, "A", "Doc120")
    assert mlx.refusal("job", "A", "Doc121") is None            # перепроверка: старт зовётся
    mlx.note_failure(mlx.WORKSPACE_MISMATCH, "A", "Doc121")
    skip = mlx.refusal("job", "A", "Doc122")
    assert skip and skip["skipped"] and skip["code"] == "mlx_workspace_mismatch" and skip["message"] == TO_INHOUSE


def test_a_start_elsewhere_does_not_skip_a_new_batch():
    mlx.note_started("B")
    mlx.begin_batch("later")
    assert mlx.refusal("later", "A", "Doc120") is None


def test_one_successful_start_clears_the_refusal():
    mlx.begin_batch("job")
    mlx.note_failure(mlx.WORKSPACE_MISMATCH, "A", "Doc120")
    mlx.refusal("job", "A", "Doc121")
    mlx.note_failure(mlx.WORKSPACE_MISMATCH, "A", "Doc121")
    assert mlx.refusal("job", "A", "Doc122")["skipped"] is True
    mlx.note_started("A")                          # пространство сменили - старт прошёл
    assert mlx.refusal("job", "A", "Doc122") is None


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
