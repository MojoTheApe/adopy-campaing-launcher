# -*- coding: utf-8 -*-
"""Профиль, чьи кампании собрались чисто, робот закрывает сам (ACL-1444).

01.10 у тестировщика: «Publish all», шесть окон Mimic, четыре из них уже
ничего не делали («Browser left open for manual review»), память 92 %, и
ждущие аккаунты не стартовали — каждое окно держит 0,6–1,5 ГБ.

Решение:
* все кампании задания годны к публикации и черновик сохранён — мягкая
  остановка ЭТОГО профиля через MLX API (без kill), и только профиля,
  который робот сам поднял или к которому цеплялся по порту автоматизации;
* есть что проверять (do not publish, ошибка, отмена) — окно остаётся;
* окно, открытое руками, — не трогаем;
* `FB_RPA_KEEP_PROFILES_OPEN=1` — прежнее поведение, все окна остаются;
* кнопка «Close profile» на панели задания закрывает профиль по просьбе
  человека; пока на профиле идёт работа — отказ.
MLX, процессы и браузер здесь подменены.
"""
from __future__ import annotations

import asyncio
import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import browser as br  # noqa: E402
from src.fb_ads_rpa import rpa_publisher as rp  # noqa: E402
from src.fb_ads_rpa import run_telemetry as rt  # noqa: E402
from src.fb_ads_rpa.draft import RpaPublishResult  # noqa: E402
from src.web import publish_jobs as pj  # noqa: E402

PROFILE = "550e8400-e29b-41d4-a716-446655440000"
ACCOUNT = "FacebookDQ204 - Test"


@pytest.fixture(autouse=True)
def _switch_default(monkeypatch):
    # conftest держит прежнее поведение для всех остальных тестов.
    monkeypatch.delenv(rp.ENV_KEEP_PROFILES_OPEN, raising=False)


def _rows(step: str) -> list[dict]:
    path = Path(rt.TELEMETRY_PATH)
    if not path.is_file():
        return []
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [r for r in rows if r.get("step") == step]


# ── Провайдер: кого вправе закрыть робот ─────────────────────────────────

@pytest.fixture
def provider(monkeypatch):
    stops: list[dict] = []
    monkeypatch.setattr(br, "_find_mlx_profile_dir", lambda pid: Path("C:/mlx/" + pid))
    monkeypatch.setattr(br, "_profile_pids", lambda udd, pid: frozenset({101}))
    monkeypatch.setattr(br, "_ROBOT_PROFILES", {})
    p = br.MultiloginAttachOnlyProvider()

    def stop(entry, label, profile_id, udd, *, allow_kill=True):
        stops.append({"profile": profile_id, "allow_kill": allow_kill})
        if getattr(p, "fail", None):
            raise p.fail

    monkeypatch.setattr(p, "_stop_profile", stop)
    p.stops = stops
    return p


def _entry():
    return SimpleNamespace(profile_id=PROFILE, folder_id="f1", multilogin_instance="A")


def test_a_robot_profile_is_stopped_once_through_the_api_without_kill(provider):
    br._mark_robot_profile(PROFILE, "C:/mlx/" + PROFILE)
    assert provider.close_finished_profile(_entry(), ACCOUNT) == "closed"
    assert provider.stops == [{"profile": PROFILE, "allow_kill": False}]
    # Профиль закрыт — метка робота снята: открытое потом руками окно — уже
    # окно человека.
    assert PROFILE not in br._ROBOT_PROFILES


def test_a_hand_opened_window_is_not_stopped(provider):
    assert provider.close_finished_profile(_entry(), ACCOUNT) == "hand_opened"
    assert provider.stops == []


def test_a_robot_profile_reopened_by_hand_is_not_stopped(provider, monkeypatch):
    """Робот поднимал процесс 101, а сейчас работает 202 — окно человека."""
    br._mark_robot_profile(PROFILE, "C:/mlx/" + PROFILE)
    monkeypatch.setattr(br, "_profile_pids", lambda udd, pid: frozenset({202}))
    assert provider.close_finished_profile(_entry(), ACCOUNT) == "hand_opened"
    assert provider.stops == []


def test_a_failed_api_stop_keeps_the_window_and_kills_nothing(provider):
    br._mark_robot_profile(PROFILE, "C:/mlx/" + PROFILE)
    provider.fail = RuntimeError("HTTP 500")
    assert provider.close_finished_profile(_entry(), ACCOUNT) == "stop_failed"
    assert provider.stops == [{"profile": PROFILE, "allow_kill": False}]


# ── Публикатор: когда закрывать ──────────────────────────────────────────

class _Provider:
    def __init__(self, outcome="closed"):
        self.calls: list = []
        self.closed_keeping: list = []
        self.outcome = outcome

    def close_finished_profile(self, entry, account_label):
        self.calls.append(account_label)
        return self.outcome

    def close(self, entry, *, keep_browser_open=False):
        self.closed_keeping.append(keep_browser_open)


def _publisher(monkeypatch, *, saved=True, provider=None):
    pub = rp.RpaPublisher.__new__(rp.RpaPublisher)
    pub._provider = provider or _Provider()
    pub._conn = SimpleNamespace(page=SimpleNamespace(url="https://adsmanager.facebook.com/x/edit?act=1"),
                                close=lambda keep_browser_open=False: None)
    pub._entry = _entry()
    pub._cancelled = False
    monkeypatch.setattr(rp.RpaPublisher, "_draft_saved", staticmethod(lambda page: saved))
    return pub


def test_clean_results_close_the_profile_once(monkeypatch):
    pub = _publisher(monkeypatch)
    assert pub.finish_job(ACCOUNT, clean=True) == "closed"
    assert pub._provider.calls == [ACCOUNT]
    # Сначала отпустили CDP (окно не трогая), потом закрыли через API.
    assert pub._provider.closed_keeping == [True]
    assert pub._conn is None and pub._entry is None
    rows = _rows("profile_closed_after_job")
    assert rows and rows[-1]["ok"] is True and rows[-1]["note"] == "closed"


@pytest.mark.parametrize("why,setup", [
    ("kept_problem", lambda pub, mp: None),
    ("kept_switch", lambda pub, mp: mp.setenv(rp.ENV_KEEP_PROFILES_OPEN, "1")),
    ("kept_cancelled", lambda pub, mp: setattr(pub, "_cancelled", True)),
    ("kept_unsaved", lambda pub, mp: mp.setattr(rp.RpaPublisher, "_draft_saved", staticmethod(lambda p: False))),
    ("kept_no_session", lambda pub, mp: setattr(pub, "_conn", None)),
    ("kept_provider", lambda pub, mp: setattr(pub, "_provider", SimpleNamespace(close=lambda *a, **k: None))),
])
def test_the_window_stays_open(monkeypatch, why, setup):
    pub = _publisher(monkeypatch)
    provider = pub._provider
    setup(pub, monkeypatch)
    clean = why != "kept_problem"
    assert pub.finish_job(ACCOUNT, clean=clean) == why
    assert provider.calls == []
    rows = _rows("profile_closed_after_job")
    assert rows and rows[-1]["ok"] is None and rows[-1]["note"] == why


def test_a_failed_stop_is_a_failed_row(monkeypatch):
    pub = _publisher(monkeypatch, provider=_Provider(outcome="stop_failed"))
    assert pub.finish_job(ACCOUNT, clean=True) == "stop_failed"
    assert _rows("profile_closed_after_job")[-1]["ok"] is False


def test_a_stop_pressed_while_waiting_for_autosave_keeps_the_window(monkeypatch):
    """Ревью F2: Stop во время ожидания «All edits saved»."""
    pub = _publisher(monkeypatch)

    def saved_but_stopped(page):
        pub._cancelled = True
        return True

    monkeypatch.setattr(rp.RpaPublisher, "_draft_saved", staticmethod(saved_but_stopped))
    assert pub.finish_job(ACCOUNT, clean=True) == "kept_cancelled"
    assert pub._provider.calls == []


def test_another_job_waiting_for_the_profile_keeps_it_open(monkeypatch):
    """Ревью F6: следующее задание на тот же профиль поднимет его сразу."""
    pub = _publisher(monkeypatch)
    assert pub.finish_job(ACCOUNT, clean=True, other_job_waits=True) == "kept_next_job"
    assert pub._provider.calls == []


def test_the_failed_close_log_is_scrubbed(monkeypatch, caplog):
    """Ревью F3: id профиля из ошибки MLX в лог не попадает."""
    class _Raises(_Provider):
        def close_finished_profile(self, entry, account_label):
            raise RuntimeError(f"HTTP 500 for profile {PROFILE}")

    pub = _publisher(monkeypatch, provider=_Raises())
    with caplog.at_level("WARNING", logger=rp.log.name):
        assert pub.finish_job(ACCOUNT, clean=True) == "stop_failed"
    assert "HTTP 500" in caplog.text and PROFILE not in caplog.text


@pytest.mark.parametrize("value", ["1", "true", "yes", "on"])
def test_the_switch_values(monkeypatch, value):
    monkeypatch.setenv(rp.ENV_KEEP_PROFILES_OPEN, value)
    assert rp.keep_profiles_open()


def test_the_switch_is_off_by_default():
    assert not rp.keep_profiles_open()


def test_the_editor_must_say_all_edits_saved(monkeypatch):
    from src.fb_ads_rpa import meta_dom as md

    monkeypatch.setattr(md, "wait_editor_saved", lambda page, **kw: None)
    editor = SimpleNamespace(url="https://adsmanager.facebook.com/adsmanager/manage/campaigns/edit?act=1")
    monkeypatch.setattr(md, "_all_edits_saved", lambda page: True)
    assert rp.RpaPublisher._draft_saved(editor)
    monkeypatch.setattr(md, "_all_edits_saved", lambda page: False)
    assert not rp.RpaPublisher._draft_saved(editor)
    # Новый редактор «в один экран» (ACL-1327) — тоже редактор (ревью F1).
    simple = SimpleNamespace(url="https://adsmanager.facebook.com/adsmanager/manage/simple_create?act=1")
    assert not rp.RpaPublisher._draft_saved(simple)
    # Любая другая страница — закрыто по умолчанию.
    assert not rp.RpaPublisher._draft_saved(SimpleNamespace(url="https://www.facebook.com/checkpoint/"))
    assert not rp.RpaPublisher._draft_saved(SimpleNamespace(url=""))
    monkeypatch.setattr(md, "_all_edits_saved", lambda page: True)
    assert rp.RpaPublisher._draft_saved(simple)
    monkeypatch.setattr(md, "_all_edits_saved", lambda page: False)
    # Список кампаний — несохранённого нет.
    listing = SimpleNamespace(url="https://adsmanager.facebook.com/adsmanager/manage/campaigns?act=1")
    assert rp.RpaPublisher._draft_saved(listing)
    # `next=simple_create` в параметрах списка — всё ещё список.
    nxt = SimpleNamespace(url="https://adsmanager.facebook.com/adsmanager/manage/campaigns?act=1&next=simple_create")
    assert rp.RpaPublisher._draft_saved(nxt)


# ── Задание: вердикт и строка в панели ───────────────────────────────────

def _ok(name):
    return RpaPublishResult(campaign_name=name, success=True)


def _blocked(name):
    r = RpaPublishResult(campaign_name=name, success=True)
    r.blocking_failures.append("ad_creative_upload")
    return r


class _JobPublisher:
    instances: list = []

    def __init__(self, results):
        self.results = list(results)
        self.finished: list = []
        self.closed = 0

    def build_drafts(self, *a, **kw):
        return [SimpleNamespace(name=r.campaign_name, errors=[]) for r in self.results]

    def publish_draft(self, draft):
        return next(r for r in self.results if r.campaign_name == draft.name)

    def finish_job(self, account_label, *, clean, other_job_waits=False):
        self.finished.append(clean)
        self.waits = other_job_waits
        if clean and other_job_waits:
            return "kept_next_job"
        return "closed" if clean else "kept_problem"

    def close(self, **kw):
        self.closed += 1


def _run(monkeypatch, results, publisher_cls=None, *, extra_jobs=None):
    made: list = []
    for jid, job in (extra_jobs or {}).items():
        monkeypatch.setitem(pj._jobs, jid, job)
    monkeypatch.setattr(pj, "profile_key_for", lambda label: "P-" + label.split()[0])

    def factory(**kw):
        pub = (publisher_cls or _JobPublisher)(results)
        made.append(pub)
        return pub

    monkeypatch.setattr(pj, "RpaPublisher", factory)
    monkeypatch.setattr(pj, "_write_draft_status", lambda *a, **k: None)
    woke: list = []
    monkeypatch.setattr(pj._slots, "wake", lambda: woke.append(1))
    job_id = "job-1444"
    monkeypatch.setitem(pj._jobs, job_id, {"errors": [], "results": [], "status": "running",
                                           "account_label": ACCOUNT, "total": len(results),
                                           "done_count": 0, "creative_notices": [],
                                           "cancel_requested": False})
    pj._run_rpa(job_id, [], account_label=ACCOUNT, country="MX", activate=False)
    return pj._jobs[job_id], made[0], woke


def test_a_clean_job_closes_its_profile_and_says_so(monkeypatch):
    job, pub, woke = _run(monkeypatch, [_ok("C1"), _ok("C2")])
    assert pub.finished == [True]
    assert job["profile_closed"] is True
    assert rp.PROFILE_CLOSED_NOTICE in job["creative_notices"]
    assert rp.PROFILE_CLOSED_NOTICE == "Closed the profile — all campaigns are ready"
    assert pub.waits is False
    assert job["status"] == "done"


def test_a_job_with_a_problem_keeps_its_window(monkeypatch):
    job, pub, _ = _run(monkeypatch, [_ok("C1"), _blocked("C2")])
    assert pub.finished == [False]
    assert not job.get("profile_closed")
    assert rp.PROFILE_CLOSED_NOTICE not in job["creative_notices"]


def test_a_job_cancelled_mid_run_keeps_its_window(monkeypatch):
    """Ревью F8: Stop посреди задания — `clean=False`."""
    class _StopAfterFirst(_JobPublisher):
        def publish_draft(self, draft):
            pj._jobs["job-1444"]["cancel_requested"] = True
            return super().publish_draft(draft)

    job, pub, _ = _run(monkeypatch, [_ok("C1"), _ok("C2")], _StopAfterFirst)
    assert pub.finished == [False]
    assert not job.get("profile_closed") and job["status"] == "cancelled"


def test_a_partial_job_keeps_its_window(monkeypatch):
    """Ревью F8: задание оборвалось на второй кампании — итог один из двух."""
    class _DiesOnSecond(_JobPublisher):
        def publish_draft(self, draft):
            if draft.name == "C2":
                raise RuntimeError("browser gone")
            return super().publish_draft(draft)

    monkeypatch.setattr("src.acl_sentry.report_job_exception", lambda *a, **k: None)
    job, pub, _ = _run(monkeypatch, [_ok("C1"), _ok("C2")], _DiesOnSecond)
    assert len(job["results"]) == 1 and job["total"] == 2
    assert pub.finished == [False] and not job.get("profile_closed")


def test_the_job_verdict_counts_results(monkeypatch):
    def verdict(results, total, errors=()):
        monkeypatch.setitem(pj._jobs, "v", {"results": [r.to_job_dict() for r in results],
                                            "errors": list(errors), "total": total})
        return pj._job_is_clean("v")

    assert verdict([_ok("C1"), _ok("C2")], 2)
    assert not verdict([_ok("C1")], 2)
    assert not verdict([], 0)
    assert not verdict([_ok("C1")], 1, errors=["RPA error: x"])
    assert not verdict([_ok("C1"), _blocked("C2")], 2)


def test_another_job_on_the_profile_is_passed_to_the_publisher(monkeypatch):
    other = {"j-next": {"status": "queued", "account_label": "FacebookDQ204 - Second label"}}
    job, pub, _ = _run(monkeypatch, [_ok("C1")], extra_jobs=other)
    assert pub.waits is True and not job.get("profile_closed")


def test_a_finished_job_on_the_profile_does_not_count(monkeypatch):
    other = {"j-old": {"status": "done", "account_label": ACCOUNT}}
    job, pub, _ = _run(monkeypatch, [_ok("C1")], extra_jobs=other)
    assert pub.waits is False and job["profile_closed"] is True


def test_a_publisher_without_finish_job_is_just_closed(monkeypatch):
    class _Old(_JobPublisher):
        finish_job = None

    job, pub, _ = _run(monkeypatch, [_ok("C1")], _Old)
    assert pub.closed == 1 and not job.get("profile_closed")


def test_a_failing_finish_still_lets_go_of_the_browser(monkeypatch):
    class _Boom(_JobPublisher):
        def finish_job(self, account_label, *, clean, other_job_waits=False):
            raise RuntimeError("x")

    job, pub, _ = _run(monkeypatch, [_ok("C1")], _Boom)
    assert pub.closed == 1 and job["status"] == "done"


# ── Кнопка «Close profile» ───────────────────────────────────────────────

@pytest.fixture
def button(monkeypatch):
    stops: list = []
    state = SimpleNamespace(fail=None)

    def close_account_profile(label):
        stops.append(label)
        if state.fail:
            raise state.fail
        return "closed"

    monkeypatch.setattr(rp, "close_account_profile", close_account_profile)
    monkeypatch.setattr(pj, "profile_key_for", lambda label: "P-" + label.split()[0])
    woke: list = []
    monkeypatch.setattr(pj._slots, "wake", lambda: woke.append(1))

    def job(job_id, status, label=ACCOUNT):
        monkeypatch.setitem(pj._jobs, job_id, {"job_id": job_id, "status": status, "account_label": label,
                                               "errors": [], "results": [], "creative_notices": []})

    state.stops, state.job, state.woke = stops, job, woke
    return state


def test_the_button_stops_the_profile(button):
    button.job("j1", "done")
    got = pj.close_profile("j1")
    assert got["ok"] is True and button.stops == [ACCOUNT]
    assert pj._jobs["j1"]["profile_closed"] is True
    assert button.woke


def test_the_button_is_refused_while_a_job_runs_on_the_profile(button):
    button.job("j1", "done")
    button.job("j2", "running")
    got = pj.close_profile("j1")
    assert got["ok"] is False and got["code"] == "busy" and button.stops == []


@pytest.mark.parametrize("status", ["pending", "queued", "running", "cancelling"])
def test_the_button_on_a_running_job_is_refused(button, status):
    button.job("j1", status)
    assert pj.close_profile("j1")["code"] == "busy"
    assert button.stops == []


def test_a_job_on_another_profile_does_not_block_the_button(button):
    button.job("j1", "done")
    button.job("j2", "running", label="FacebookDoc19 - Other")
    assert pj.close_profile("j1")["ok"] is True


def test_the_button_waits_for_nobody_holding_the_profile(button):
    """Чтение спенда держит замок профиля — кнопка не ждёт, а отказывает."""
    button.job("j1", "done")
    lease = pj.publish_lease("P-" + ACCOUNT.split()[0])
    held, release = threading.Event(), threading.Event()

    def hold():
        with lease:
            held.set()
            release.wait(5)

    t = threading.Thread(target=hold)
    t.start()
    held.wait(5)
    try:
        assert pj.close_profile("j1")["code"] == "busy"
    finally:
        release.set()
        t.join(5)
    assert button.stops == []


def test_a_failed_button_stop_is_reported(button, caplog):
    button.job("j1", "done")
    button.fail = RuntimeError(f"HTTP 500 for {PROFILE}")
    with caplog.at_level("WARNING", logger=pj.__name__):
        got = pj.close_profile("j1")
    assert got["ok"] is False and got["code"] == "failed"
    assert got["error"] == "Multilogin could not close the profile. Close its window in Multilogin."
    assert not pj._jobs["j1"].get("profile_closed")
    assert button.woke == []
    # Причина — в логе, без id профиля.
    assert "HTTP 500" in caplog.text and PROFILE not in caplog.text
    # Замок профиля отпущен: следующая попытка не «busy».
    button.fail = None
    assert pj.close_profile("j1")["ok"] is True


def test_the_button_on_an_unknown_job(button):
    assert pj.close_profile("nope")["code"] == "not_found"


def test_the_close_profile_route(button):
    import httpx
    from src.web.app import app

    button.job("j1", "done")
    button.job("j2", "done")
    button.job("j3", "running", label="FacebookDoc19 - Other")

    async def post(job_id):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            return await client.post(f"/publish-jobs/{job_id}/close-profile")

    ok = asyncio.run(post("j1"))
    assert ok.status_code == 200 and ok.json()["ok"] is True
    assert asyncio.run(post("j3")).status_code == 409
    assert asyncio.run(post("missing")).status_code == 404
    button.fail = RuntimeError("HTTP 500")
    assert asyncio.run(post("j2")).status_code == 502


def test_close_account_profile_uses_the_graceful_stop(monkeypatch):
    calls: list = []
    monkeypatch.setattr(rp, "_resolve_account", lambda label: _entry())
    monkeypatch.setattr(br, "stop_profile_on_request",
                        lambda entry, label: calls.append((entry.profile_id, label)) or "closed")
    assert rp.close_account_profile(ACCOUNT) == "closed"
    assert calls == [(PROFILE, ACCOUNT)]


def test_close_account_profile_needs_a_profile(monkeypatch):
    monkeypatch.setattr(rp, "_resolve_account", lambda label: None)
    with pytest.raises(RuntimeError):
        rp.close_account_profile(ACCOUNT)


def test_stop_profile_on_request_never_kills(monkeypatch):
    calls: list = []
    monkeypatch.setattr(br, "_find_mlx_profile_dir", lambda pid: None)
    monkeypatch.setattr(br, "_ROBOT_PROFILES", {PROFILE: frozenset({1})})
    monkeypatch.setattr(br.MultiloginAttachOnlyProvider, "_stop_profile",
                        lambda self, entry, label, pid, udd, *, allow_kill=True:
                        calls.append(allow_kill))
    assert br.stop_profile_on_request(_entry(), ACCOUNT) == "closed"
    assert calls == [False] and PROFILE not in br._ROBOT_PROFILES


# ── Страница: кнопка на панели задания ───────────────────────────────────

def test_the_job_panel_has_a_close_profile_button():
    html = (ROOT / "src" / "web" / "templates" / "report.html").read_text(encoding="utf-8")
    assert "function cbCloseProfileHtml(" in html
    assert "/close-profile" in html
    assert "Close profile" in html
    # Только у закончившегося задания и пока профиль не закрыт.
    assert "job.profile_closed" in html


def test_a_hand_opened_window_is_kept_on_purpose_not_a_failure(monkeypatch):
    pub = _publisher(monkeypatch, provider=_Provider(outcome="hand_opened"))
    assert pub.finish_job(ACCOUNT, clean=True) == "hand_opened"
    assert _rows("profile_closed_after_job")[-1]["ok"] is None
