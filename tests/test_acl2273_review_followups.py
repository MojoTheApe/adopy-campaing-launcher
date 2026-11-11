"""ACL-2273: review follow-ups of ACL-2262 (slot uuid case, restart cooldown) and ACL-2003 (gate test)."""
import contextlib
from types import SimpleNamespace

from src.fb_api import warm
from src.web.profile_slots import ProfileSlots
from tests.test_acl1812_warm_sessions import ACT, PID, Rig


def _entry(pid):
    return SimpleNamespace(profile_id=pid, folder_id="F", multilogin_instance="A", ad_account_id=ACT)


def test_holds_matches_the_profile_whatever_its_case_or_spaces():
    q = SimpleNamespace(_cond=contextlib.nullcontext(), _running={"job-1": {"profile": PID}})
    for given in (PID, PID.upper(), f"  {PID.upper()} "):
        assert ProfileSlots.holds(q, "job-1", given) is True
    assert ProfileSlots.holds(q, "job-1", "-".join(["1" * 8, "1" * 4, "1" * 4, "1" * 4, "1" * 12])) is False
    assert ProfileSlots.holds(q, "job-2", PID) is False
    assert ProfileSlots.holds(q, "job-1") is True          # no profile given: any slot of the job


def test_upper_case_profile_id_in_the_map_does_not_make_a_job_wait_for_its_own_slot(tmp_path, monkeypatch):
    upper = PID.upper()
    rig = Rig(tmp_path)
    rig.scan_result[upper] = ("none", None)
    queue = rig.reg._slot_queue()
    queue.holds = lambda job_id, profile="": job_id == "job-1" and profile == PID   # the slot is recorded under the canonical id
    rig.reg._slot_queue = lambda: queue
    from src.fb_ads_rpa import run_telemetry
    monkeypatch.setattr(run_telemetry, "current_job_id", lambda: "job-1")
    assert rig.reg.open_one("Acc", _entry(upper))["ok"]
    assert not [c for c in rig.slot_calls if c[0] == "acquire"]


def test_waiting_for_a_closing_profile_does_not_restart_the_cooldown_clock(tmp_path, monkeypatch):
    monkeypatch.delenv("ACL_PROFILE_RESTART_COOLDOWN_S", raising=False)       # code default: 180 s
    rig = Rig(tmp_path)
    rig.warm_up()
    rig.checkout().release()                                  # the registry knows the profile
    rig.scan_result[PID] = ("closing", None)
    looks = []

    def sleep(seconds):
        looks.append(seconds)
        if len(looks) == 2:
            rig.scan_result[PID] = ("none", None)

    rig.reg._sleep = sleep
    result = rig.reg.open_one("Acc", _entry(PID))
    assert result["ok"], result
    assert result.get("code") != "restart_cooldown" and len(rig.started) >= 1
    assert rig.stops.last_stop(PID) is None, "our own wait must not record a fresh stop"


def test_a_profile_that_vanished_without_our_wait_still_gets_the_cooldown(tmp_path, monkeypatch):
    monkeypatch.delenv("ACL_PROFILE_RESTART_COOLDOWN_S", raising=False)
    rig = Rig(tmp_path)
    rig.warm_up()
    rig.checkout().release()
    rig.scan_result[PID] = ("none", None)                     # closed by hand: no wait of ours
    result = rig.reg.open_one("Acc", _entry(PID))
    assert not result["ok"] and result["code"] == "restart_cooldown"
