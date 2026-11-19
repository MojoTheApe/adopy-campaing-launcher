"""ACL-2822: быстрее открытие профиля — свой слот у гейта прокси, один список запущенных,
без мёртвого адреса folder_id и одна сводка на каждое открытие.

Только подставные MLX, часы и журнал: ни Multilogin, ни Meta.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.fb_ads_rpa import profile_proxy_gate as gate
from src.fb_api import mlx_session, open_timing
from src.fb_api import profile as prof
from src.fb_api import warm
from src.meta_campaign_agent import multilogin_launcher as ml

PID = "550e8400-e29b-41d4-a716-446655440000"


# --- 1. the proxy gate asks only the profile's own slot -----------------------------------

@pytest.fixture
def clean_gate(monkeypatch):
    gate._ok_cache.clear()
    gate._unknown_cache.clear()
    monkeypatch.delenv("FB_RPA_PROXY_GATE", raising=False)
    monkeypatch.setattr(gate, "_token_for", lambda inst: f"tok-{inst}")
    yield
    gate._ok_cache.clear()
    gate._unknown_cache.clear()


def test_gate_queries_only_the_own_slot(clean_gate):
    asked = []

    def fetch(pid, tok):
        asked.append(tok)
        return gate.ProxyConfig(known=False, reason="x")   # not found: used to go on to the other slot

    gate.ensure_profile_has_proxy(SimpleNamespace(profile_id=PID, multilogin_instance="B"), "Doc1", fetch=fetch)
    assert asked == ["tok-B"]


def test_checkout_passes_the_slot_of_the_account(monkeypatch):
    seen = []

    def ensure(entry, label):
        seen.append(entry.multilogin_instance)

    monkeypatch.setattr(gate, "ensure_profile_has_proxy", ensure)
    monkeypatch.setattr(mlx_session, "instance_of", lambda account: "B")
    reg = warm.WarmRegistry.__new__(warm.WarmRegistry)
    sess = SimpleNamespace(profile_id=PID, owner=0,
                           lock=SimpleNamespace(acquire=lambda: (_ for _ in ()).throw(RuntimeError("stop here"))))
    reg.session = lambda pid: sess
    with pytest.raises(RuntimeError, match="stop here"):
        reg.checkout(PID, account="Doc1", act="act_1", mode="read", make_graph=lambda t: None)
    assert seen == ["B"]


def test_checkout_without_a_known_slot_still_asks_both(monkeypatch, clean_gate):
    monkeypatch.setattr(mlx_session, "instance_of", lambda account: "")
    asked = []
    fetch = lambda pid, tok: (asked.append(tok), gate.ProxyConfig(known=False, reason="x"))[1]
    monkeypatch.setattr(gate, "fetch_proxy_config", fetch)
    assert warm._proxy_refusal(PID, warm._slot_of("Doc1"), "Doc1") == ""
    assert asked == ["tok-A", "tok-B"]


# --- 2. the running list: first answering endpoint, remembered ------------------------------

class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


def _launcher():
    cfg = SimpleNamespace(launcher_base_v2="http://l/api/v2", launcher_base_v1="http://l/api/v1")
    return SimpleNamespace(config=cfg, _headers=lambda: {})


@pytest.fixture
def running_get(monkeypatch):
    prof._RUNNING_ENDPOINT.clear()
    calls = []
    answers = {}

    def get(url, headers=None, timeout=None):
        calls.append(url)
        r = answers.get(url)
        if isinstance(r, Exception):
            raise r
        return r or _Resp(404, {})

    import requests
    monkeypatch.setattr(requests, "get", get)
    yield SimpleNamespace(calls=calls, answers=answers)
    prof._RUNNING_ENDPOINT.clear()


def test_running_list_stops_at_the_first_answering_endpoint(running_get):
    running_get.answers["http://l/api/v2/profile/running"] = _Resp(200, {"data": []})
    assert prof.running_state(_launcher(), PID) == ("not_running", None)
    assert running_get.calls == ["http://l/api/v2/profile/running"]


def test_running_list_remembers_the_working_endpoint(running_get):
    v1 = "http://l/api/v1/profile/running"
    running_get.answers[v1] = _Resp(200, {"data": [{"profile_id": PID, "port": 7001}]})
    assert prof.running_state(_launcher(), PID) == ("running", "7001")
    assert running_get.calls == ["http://l/api/v2/profile/running", v1]
    running_get.calls.clear()
    assert prof.running_state(_launcher(), PID) == ("running", "7001")
    assert running_get.calls == [v1]            # the dead v2 address is not asked again first


def test_running_list_none_answered_is_unknown(running_get):
    running_get.answers["http://l/api/v2/profile/running"] = RuntimeError("down")
    assert prof.running_state(_launcher(), PID) == ("unknown", None)
    assert len(running_get.calls) == 3


def test_open_profile_asks_the_running_list_once(monkeypatch):
    monkeypatch.setattr(gate, "ensure_profile_has_proxy", lambda *a, **k: None)
    asked = []
    monkeypatch.setattr(prof, "running_state", lambda l, p: (asked.append(1), ("running", "7001"))[1])
    from src.fb_api import stop_log
    monkeypatch.setattr(stop_log, "wait_or_refusal", lambda *a, **k: "")
    opened = prof.open_profile(PID, "F", "A", launcher_factory=lambda i: object(),
                               local_probe=lambda pid: ("none", None), sleep=lambda s: None)
    assert opened.port == "7001" and len(asked) == 1


# --- 3. folder-id endpoints -----------------------------------------------------------------

@pytest.fixture
def folder_world(monkeypatch):
    ml._folder_ok.clear()
    ml._folder_dead.clear()
    calls = []
    answers = {}

    def get(url, headers=None, timeout=None):
        calls.append(url)
        r = answers.get(url)
        if isinstance(r, Exception):
            raise r
        return r or _Resp(404, {})

    monkeypatch.setattr(ml.requests, "get", get)
    cfg = SimpleNamespace(launcher_base_v2="https://launcher.mlx.yt:45001/api/v2", automation_token="t")
    launcher = ml.MultiloginLauncher(cfg)
    yield SimpleNamespace(calls=calls, answers=answers, launcher=launcher)
    ml._folder_ok.clear()
    ml._folder_dead.clear()


ROOT = "https://launcher.mlx.yt:45001/profile?limit=100"
API = "https://api.multilogin.com/profile?limit=100"


def test_dead_folder_endpoint_is_not_asked_again(folder_world):
    folder_world.answers[API] = _Resp(200, {"data": {"profiles": [{"id": PID, "folder_id": "F9"}]}})
    assert folder_world.launcher._resolve_folder_id(PID) == "F9"
    assert folder_world.calls == [ROOT, API]
    folder_world.calls.clear()
    assert folder_world.launcher._resolve_folder_id(PID) == "F9"
    assert folder_world.calls == [API]          # the endpoint that answered goes first, the dead one is skipped


def test_all_endpoints_failing_on_the_first_call_marks_none_dead(folder_world):
    assert folder_world.launcher._resolve_folder_id(PID) is None
    assert ml._folder_dead == {}                # nobody answered, so nobody is blamed for 600 s
    folder_world.calls.clear()
    folder_world.launcher._resolve_folder_id(PID)
    assert folder_world.calls == [ROOT, API]    # a blip on both addresses does not disable the lookup


def test_failing_endpoint_is_dead_only_when_the_other_one_answered(folder_world):
    folder_world.answers[API] = _Resp(200, {"data": {"profiles": []}})
    folder_world.launcher._resolve_folder_id(PID)
    assert ROOT in ml._folder_dead and API not in ml._folder_dead


def test_an_endpoint_that_answers_later_is_remembered_not_dead(folder_world):
    folder_world.launcher._resolve_folder_id(PID)          # both fail: nothing remembered
    folder_world.answers[ROOT] = _Resp(200, {"data": {"profiles": []}})
    folder_world.launcher._resolve_folder_id(PID)          # ROOT answers now
    assert ROOT in ml._folder_ok and ROOT not in ml._folder_dead


def test_dead_folder_endpoint_is_retried_after_the_ttl(folder_world, monkeypatch):
    folder_world.answers[API] = _Resp(200, {"data": {"profiles": []}})
    folder_world.launcher._resolve_folder_id(PID)
    ml._folder_dead[ROOT] -= ml.DEAD_ENDPOINT_TTL_S + 1
    assert ROOT in ml._order_folder_endpoints([ROOT, API])


# --- 4. one summary event per open ------------------------------------------------------------

@pytest.fixture
def events(monkeypatch):
    got = []
    import src.acl_event_log as log
    monkeypatch.setattr(log, "emit", lambda t, p=None, **kw: got.append((t, p, kw)))
    return got


class _Launcher:
    def start_profile(self, folder_id, profile_id, *, headless=False):
        from src.meta_campaign_agent.multilogin_launcher import StartedProfile
        return StartedProfile(profile_id, "F1", port="7001", newly_started=True)


@pytest.fixture
def open_world(monkeypatch):
    monkeypatch.setattr(gate, "ensure_profile_has_proxy", lambda *a, **k: None)
    monkeypatch.setattr(prof, "running_state", lambda l, p: ("not_running", None))
    from src.fb_api import launch, stop_log
    monkeypatch.setattr(stop_log, "wait_or_refusal", lambda *a, **k: "")
    monkeypatch.setattr(launch, "_record", lambda *a, **k: None)
    monkeypatch.setattr(launch, "_PACER", launch.StartPacer(limit=lambda: 0))
    monkeypatch.setattr(mlx_session, "note_failure", lambda *a, **k: None)


def test_read_publish_open_writes_one_summary(events, open_world):
    with mlx_session.scope(account="Doc1"):
        prof.open_profile(PID, "F1", "B", launcher_factory=lambda i: _Launcher(),
                          local_probe=lambda pid: ("none", None), sleep=lambda s: None)
    assert len(events) == 1
    etype, payload, kw = events[0]
    assert etype == "profile.open" and kw["account"] == "Doc1"
    assert payload["slot"] == "B" and payload["outcome"] == "started"
    assert payload["total_s"] >= 0
    assert {"proxy_gate", "local_probe", "running_list", "sync_wait", "start"} <= set(payload["steps"])


def test_a_refused_open_still_writes_its_outcome(events, open_world, monkeypatch):
    from src.fb_ads_rpa import profile_trouble
    monkeypatch.setattr(gate, "ensure_profile_has_proxy",
                        lambda *a, **k: (_ for _ in ()).throw(profile_trouble.ProfileStop(profile_trouble.NO_PROXY, "x")))
    with pytest.raises(prof.ProfileError):
        prof.open_profile(PID, "F1", "A", launcher_factory=lambda i: _Launcher(),
                          local_probe=lambda pid: ("none", None), sleep=lambda s: None)
    assert len(events) == 1 and events[0][1]["outcome"] == profile_trouble.NO_PROXY


def test_warm_open_writes_one_event_not_two(events, monkeypatch):
    reg = warm.WarmRegistry.__new__(warm.WarmRegistry)
    reg._scan = lambda ids: {ids[0]: ("port", 7001)}
    reg._adopt = lambda label, pid, port: warm._result(True, "ok", "already_open")
    monkeypatch.setattr(warm, "_proxy_refusal", lambda *a: "")
    res = reg.open_one("Doc1", SimpleNamespace(profile_id=PID, folder_id="F", multilogin_instance="B"))
    assert res["code"] == "already_open"
    assert len(events) == 1
    etype, payload, kw = events[0]
    assert etype == "profile.open" and kw["account"] == "Doc1"
    assert payload["slot"] == "B" and payload["outcome"] == "already_open"
    assert {"proxy_gate", "local_scan"} <= set(payload["steps"])


def test_warm_open_that_starts_the_profile_still_gives_one_event(events, open_world, monkeypatch):
    """`open_one` -> `open_profile`: the inner steps join the outer timer, no second event."""
    timer = open_timing.OpenTimer("Doc1", "A", "warm")
    with open_timing.activate(timer):
        prof.open_profile(PID, "F1", "A", launcher_factory=lambda i: _Launcher(),
                          local_probe=lambda pid: ("none", None), sleep=lambda s: None)
    assert events == [] and "start" in timer.steps
    timer.emit("started")
    timer.emit("started")
    assert len(events) == 1


def test_an_event_log_failure_does_not_break_the_open(open_world, monkeypatch):
    import src.acl_event_log as log
    monkeypatch.setattr(log, "emit", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk")))
    opened = prof.open_profile(PID, "F1", "A", launcher_factory=lambda i: _Launcher(),
                               local_probe=lambda pid: ("none", None), sleep=lambda s: None)
    assert opened.port == "7001"
