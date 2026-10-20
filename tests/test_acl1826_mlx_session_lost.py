"""ACL-1826: потеря общей сессии Multilogin (слот B) и отказ входа в прокси.

Только подделки: ни Multilogin, ни Meta, ни браузера. Проверяется:
403 «restricted permission» на слоте B узнаётся и получает код `mlx_session_lost`
(другие ошибки и тот же текст на слоте A — нет); после первой такой ошибки
остальные B-старты пачки пропускаются с той же причиной, слот A идёт дальше;
вход в прокси получает `proxy_auth`; каждое событие пишется в телеметрию и в
Sentry (время, слот, аккаунт); предполётная проверка токена не печатает токен;
Retry перечитывает только пропущенные.
"""
from __future__ import annotations

import asyncio
import base64
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_api import mlx_session as mlx  # noqa: E402
from src.fb_api import profile as prof  # noqa: E402
from src.meta_campaign_agent.multilogin_launcher import MultiloginLauncherError  # noqa: E402
from tests.test_acl1812_warm_sessions import Rig  # noqa: E402

LOST_TEXT = ("Multilogin on this computer lost the shared KLD sign-in (it was used on another computer). "
             "Sign in again in the Multilogin app here, then press Retry.")
PROXY_TEXT = "The proxy login for this profile is wrong or expired — update it in Multilogin"
E403 = "Start profile failed HTTP 403: restricted permission"
PROXY_ERR = "page.goto: net::ERR_INVALID_AUTH_CREDENTIALS at https://adsmanager.facebook.com/"


def _pid(n: int) -> str:
    return f"{n:08d}-0000-4000-8000-000000000000"


def _entry(n: int, slot: str):
    return SimpleNamespace(profile_id=_pid(n), folder_id="F", multilogin_instance=slot,
                           ad_account_id="act_1")


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    mlx.reset_for_tests()
    # Журналы и Sentry — во временную папку / в список: ничего не уходит наружу.
    from src.fb_ads_rpa import run_telemetry as rt
    from src.fb_api import telemetry as tm

    monkeypatch.setattr(rt, "TELEMETRY_PATH", tmp_path / "rpa_telemetry.jsonl")
    monkeypatch.setattr(tm, "journal_path", lambda: tmp_path / "pilot.jsonl")
    sent: list[dict] = []
    from src import acl_sentry

    monkeypatch.setattr(acl_sentry, "report_step_failure", lambda *a, **kw: sent.append({"args": a, **kw}) or "id")
    monkeypatch.setenv("MULTILOGIN_AUTOMATION_TOKEN_B", "")
    monkeypatch.delenv("MULTILOGIN_WORKSPACE_ID_B", raising=False)
    monkeypatch.setattr(prof, "running_state", lambda launcher, pid: ("not_running", None))
    yield sent
    mlx.reset_for_tests()


@pytest.fixture
def sentry(_clean):
    return _clean


def _jwt(claims: dict) -> str:
    def part(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    return f"{part({'alg': 'HS256'})}.{part(claims)}.SIGNATURE-NEVER-PRINTED"


# ───────────────────────── распознавание ─────────────────────────

@pytest.mark.parametrize("text,slot,want", [
    (E403, "B", mlx.SESSION_LOST),
    ("Start profile failed HTTP 403: Request is not allowed FORBIDDEN_REQUEST", "B", mlx.SESSION_LOST),
    (E403, "A", ""),                                    # слот A: тот же текст не подменяем
    ("Start profile failed HTTP 500: internal error", "B", ""),
    ("Start profile failed HTTP 403: some other reason", "B", ""),
    ("cant lock profile", "B", ""),
    (PROXY_ERR, "A", mlx.PROXY_AUTH),
    (PROXY_ERR, "B", mlx.PROXY_AUTH),
    ("", "B", ""),
])
def test_classification_of_start_errors(text, slot, want):
    assert mlx.classify(text, slot) == want


def test_launcher_error_carries_status_and_code(monkeypatch):
    from src.meta_campaign_agent import multilogin_launcher as ml
    from src.meta_campaign_agent.multilogin_config import MultiloginInstanceConfig

    class Resp:
        status_code = 403
        text = "{}"

        def json(self):
            return {"status": {"message": "restricted permission", "error_code": "FORBIDDEN_REQUEST"}}

    monkeypatch.setattr(ml.requests, "get", lambda *a, **kw: Resp())
    launcher = ml.MultiloginLauncher(MultiloginInstanceConfig("B", "tok", "http://x/api/v2"))
    with pytest.raises(MultiloginLauncherError) as ei:
        launcher.start_profile("F", _pid(1))
    assert ei.value.status == 403 and ei.value.error_code == "FORBIDDEN_REQUEST"
    assert mlx.classify(ei.value, "B") == mlx.SESSION_LOST
    assert mlx.classify(ei.value, "A") == ""


# ───────────────────────── API-путь: open_profile ─────────────────────────

class FakeLauncher:
    def __init__(self, slot, fail=None):
        self.slot, self.fail, self.calls = slot, fail, []

    def start_profile(self, folder, pid, headless=False):
        self.calls.append(pid)
        if self.fail:
            raise self.fail
        return SimpleNamespace(port="9333", folder_id=folder or "F", ws_endpoint=None)


def _open(slot, launcher, n=1):
    return prof.open_profile(_pid(n), "F", slot, launcher_factory=lambda s: launcher,
                             local_probe=lambda p: ("none", None))


def test_slot_b_403_gets_stable_code_and_message(sentry):
    launcher = FakeLauncher("B", MultiloginLauncherError(E403, status=403, error_code="FORBIDDEN_REQUEST"))
    with mlx.scope(account="KLD B93"):
        with pytest.raises(prof.ProfileError) as ei:
            _open("B", launcher)
    assert ei.value.code == "mlx_session_lost" and str(ei.value) == LOST_TEXT


def test_slot_a_403_is_not_reclassified():
    launcher = FakeLauncher("A", MultiloginLauncherError(E403, status=403))
    with pytest.raises(MultiloginLauncherError):
        _open("A", launcher)


def test_other_start_errors_pass_through_untouched():
    launcher = FakeLauncher("B", MultiloginLauncherError("Start profile failed HTTP 500: boom", status=500))
    with pytest.raises(MultiloginLauncherError):
        _open("B", launcher)


def test_proxy_auth_message_and_code():
    launcher = FakeLauncher("A", RuntimeError(PROXY_ERR))
    with pytest.raises(prof.ProfileError) as ei:
        _open("A", launcher)
    assert ei.value.code == "proxy_auth" and str(ei.value) == PROXY_TEXT


def test_every_occurrence_is_recorded_with_time_slot_and_account(tmp_path, sentry):
    launcher = FakeLauncher("B", MultiloginLauncherError(E403, status=403))
    with mlx.scope(account="KLD B93"):
        with pytest.raises(prof.ProfileError):
            _open("B", launcher)
    rpa = [json.loads(l) for l in (tmp_path / "rpa_telemetry.jsonl").read_text(encoding="utf-8").splitlines()]
    row = [r for r in rpa if r["step"] == "mlx_start_failed"][0]
    assert row["ts"] and row["ok"] is False
    assert row["diag"] == {"code": "mlx_session_lost", "slot": "B", "account": "KLD B93"}
    pilot = [json.loads(l) for l in (tmp_path / "pilot.jsonl").read_text(encoding="utf-8").splitlines()]
    assert pilot[0]["event"] == "mlx_start_failed" and pilot[0]["ts"] and pilot[0]["slot"] == "B"
    assert len(sentry) == 1
    assert sentry[0]["reason"] == "mlx_session_lost" and sentry[0]["account"] == "KLD B93"
    assert sentry[0]["diag"] == {"slot": "B"}


# ───────────────────────── пропуск остатка пачки ─────────────────────────

class BatchRig:
    """Реестр, чей запуск идёт настоящим `open_profile` с подставными лаунчерами."""

    def __init__(self, tmp_path):
        self.rig = Rig(tmp_path)
        self.launchers = {"A": FakeLauncher("A"),
                          "B": FakeLauncher("B", MultiloginLauncherError(E403, status=403))}
        self.rig.reg._start_profile = self._start

    def _start(self, pid, folder, instance):
        opened = prof.open_profile(pid, folder, instance, launcher_factory=lambda s: self.launchers[s],
                                   local_probe=lambda p: ("none", None))
        self.rig.scan_result[pid] = ("port", 9333)
        return opened


def _items(order):
    return [(f"{slot}{i}", _entry(i, slot)) for i, slot in enumerate(order, 1)]


def test_batch_skips_remaining_slot_b_and_slot_a_continues(tmp_path):
    b = BatchRig(tmp_path)
    results = b.rig.reg.open_many(_items(["B", "A", "B", "B", "A"]), job_id="job1")
    assert results["B1"]["code"] == "mlx_session_lost" and not results["B1"].get("skipped")
    for label in ("B3", "B4"):
        assert results[label]["code"] == "mlx_session_lost" and results[label]["skipped"] is True
        assert results[label]["message"] == LOST_TEXT
    assert results["A2"]["ok"] and results["A5"]["ok"]
    assert len(b.launchers["B"].calls) == 1            # в MLX сходили один раз
    assert len(b.launchers["A"].calls) == 2            # слот A не затронут


def test_skip_is_recorded_but_sentry_hears_only_the_real_failure(tmp_path, sentry):
    b = BatchRig(tmp_path)
    b.rig.reg.open_many(_items(["B", "B", "B"]), job_id="job2")
    rows = [json.loads(l) for l in (tmp_path / "rpa_telemetry.jsonl").read_text(encoding="utf-8").splitlines()]
    steps = [r["step"] for r in rows]
    assert steps.count("mlx_start_failed") == 1 and steps.count("mlx_start_skipped") == 2
    assert len(sentry) == 1


def test_a_new_batch_starts_clean_and_retry_touches_only_the_skipped(tmp_path):
    b = BatchRig(tmp_path)
    first = b.rig.reg.open_many(_items(["B", "A", "B", "B"]), job_id="j1")
    skipped = [k for k, v in first.items() if v.get("skipped")]
    assert skipped == ["B3", "B4"]
    # Человек вошёл в Multilogin заново — лаунчер B снова работает.
    b.launchers["B"].fail = None
    b.launchers["B"].calls.clear()
    b.launchers["A"].calls.clear()
    items = {label: e for label, e in _items(["B", "A", "B", "B"])}
    second = b.rig.reg.open_many([(l, items[l]) for l in skipped], job_id="j2")
    assert all(r["ok"] for r in second.values())
    assert b.launchers["B"].calls == [_pid(3), _pid(4)]
    assert b.launchers["A"].calls == []                  # A1/A2 не трогали


def test_a_still_broken_retry_fails_once_and_skips_the_rest(tmp_path):
    b = BatchRig(tmp_path)
    b.rig.reg.open_many(_items(["B", "B", "B"]), job_id="j1")
    b.launchers["B"].calls.clear()
    second = b.rig.reg.open_many(_items(["B", "B", "B"])[1:], job_id="j2")
    assert len(b.launchers["B"].calls) == 1
    assert [bool(r.get("skipped")) for r in second.values()] == [False, True]


# ───────────────────────── пачки со страницы (batch в форме) ─────────────────────────

def test_refusal_is_per_batch_and_per_slot():
    mlx.begin_batch("b1")
    mlx.begin_batch("b2")
    mlx.note_failure(mlx.SESSION_LOST, "B", "KLD B93")
    assert mlx.refusal("b1", "B", "KLD B90")["code"] == "mlx_session_lost"
    assert mlx.refusal("b1", "A", "Doc120") is None
    assert mlx.refusal("", "B", "x") is None             # без пачки (одиночное нажатие) не пропускаем
    mlx.begin_batch("b3")                                # новая пачка (Retry) — чистая
    assert mlx.refusal("b3", "B", "KLD B90") is None


class _FormReq:
    def __init__(self, fields):
        self._fields = dict(fields)

    async def form(self, **_limits):  # read_form передаёт пределы разбора
        return self._fields


@pytest.mark.parametrize("handler,extra", [
    ("account_spend_read", {"date_from": "2026-10-01", "date_to": "2026-10-01"}),
    ("account_spend_read_countries", {"date_from": "2026-10-01", "date_to": "2026-10-01"}),
    ("campaign_inventory_read", {"date_from": "2026-10-01", "date_to": "2026-10-01"}),
])
def test_endpoints_skip_slot_b_after_the_loss_and_serve_slot_a(monkeypatch, handler, extra):
    from src.analysis_agent import account_spend, campaign_inventory, country_split
    from src.web import app as web

    started = []
    monkeypatch.setattr(mlx, "instance_of", lambda a: "B" if a.startswith("KLD") else "A")
    class Started(dict):
        def to_dict(self):
            return dict(self)

    def fake(*a, **kw):
        started.append(a[0])
        return Started(state="running")

    for mod, fn in ((account_spend, "start_read"), (country_split, "start_split"), (campaign_inventory, "start_read")):
        monkeypatch.setattr(mod, fn, fake)
    monkeypatch.setattr(web, "_spend_period", lambda a, b, c: ("2026-10-01", "2026-10-01"))
    mlx.begin_batch("page1")
    mlx.note_failure(mlx.SESSION_LOST, "B", "KLD B93")

    def call(account):
        fields = {"account": account, "batch": "page1", **extra}
        resp = asyncio.run(getattr(web, handler)(_FormReq(fields)))
        return resp.status_code, json.loads(resp.body)

    status, body = call("KLD B90")
    assert status == 409 and body["code"] == "mlx_session_lost" and body["error"] == LOST_TEXT
    assert "KLD B90" not in started
    status, body = call("Doc120")
    assert status != 409 and "Doc120" in started


def test_publish_endpoint_skips_slot_b_with_the_same_reason(monkeypatch):
    from src.web import app as web

    monkeypatch.setattr(web, "_parse_build_upload_form", lambda form: ("MX", "KLD B90", [{"name": "C1"}]))
    monkeypatch.setattr(mlx, "instance_of", lambda a: "B")
    mlx.begin_batch("pa-1")
    mlx.note_failure(mlx.SESSION_LOST, "B", "KLD B93")
    form = {"batch": "pa-1", "user_confirmed": "1"}
    resp = asyncio.run(web.publish_jobs_start(_FormReq(form)))
    body = json.loads(resp.body)
    assert resp.status_code == 409 and body["code"] == "mlx_session_lost" and body["error"] == LOST_TEXT


def test_publish_job_error_text_becomes_the_clear_message(monkeypatch):
    from src.web import publish_jobs

    monkeypatch.setattr(mlx, "instance_of", lambda a: "B")
    job_id = "j-1826"
    with publish_jobs._jobs_lock:
        publish_jobs._jobs[job_id] = {"account_label": "KLD B93", "errors": [], "results": [], "status": "running"}
    try:
        publish_jobs._add_error(job_id, "MultiloginLauncherError: " + E403)
        publish_jobs._add_error(job_id, "boom: " + PROXY_ERR)
        errors = list(publish_jobs._jobs[job_id]["errors"])
    finally:
        with publish_jobs._jobs_lock:
            publish_jobs._jobs.pop(job_id, None)
    assert errors == [LOST_TEXT, PROXY_TEXT]


# ───────────────────────── RPA-путь (чтение спенда) ─────────────────────────

def test_rpa_read_start_failure_carries_the_code(monkeypatch):
    from src.analysis_agent import account_spend as sp

    class FakeProvider:
        def open(self, entry, label, **kw):
            raise MultiloginLauncherError(E403, status=403, error_code="FORBIDDEN_REQUEST")

    class Q:
        def acquire(self, *a, **k):
            return {"shared": False}

        def release(self, *a):
            pass

        def grants(self, *a):
            return 0

    from src.fb_ads_rpa import browser as rb

    monkeypatch.setattr(rb, "get_browser_provider", lambda name: FakeProvider())
    monkeypatch.setattr(sp, "_slot_queue", lambda: Q())
    monkeypatch.setattr("src.fb_api.stop_log.refusal", lambda pid, *a: "")
    with pytest.raises(sp.ProfileNotAttachable) as ei:
        sp._start_closed(_entry(1, "B"), "KLD B93", sp._ReadOpen("KLD B93"))
    assert ei.value.code == "mlx_session_lost" and str(ei.value) == LOST_TEXT


def test_rpa_open_profile_button_carries_the_code(monkeypatch):
    from src.analysis_agent import account_spend as sp
    from src.fb_ads_rpa import browser as rb

    class FakeProvider:
        def open(self, entry, label, **kw):
            raise RuntimeError(PROXY_ERR)

    class FakeDirect:
        def attach_existing_only(self, *a, **k):
            return None

        def profile_is_running(self, *a):
            return False

    monkeypatch.setattr(rb, "get_browser_provider", lambda name: FakeProvider())
    monkeypatch.setattr(rb, "MultiloginDirectCDPProvider", FakeDirect, raising=False)
    monkeypatch.setattr(sp, "_mlx_profile_dir", lambda *a: None)
    monkeypatch.setattr("src.fb_api.stop_log.refusal", lambda pid, *a: "")
    ok, msg = sp.open_profile_for_automation.__wrapped__(_pid(1), "F", "A", "Doc120") \
        if hasattr(sp.open_profile_for_automation, "__wrapped__") else sp.open_profile_for_automation(_pid(1), "F", "A", "Doc120")
    assert ok is False and str(msg) == PROXY_TEXT and msg.code == "proxy_auth"


# ───────────────────────── предполётная проверка токена ─────────────────────────

NOW = 1_800_000_000.0
WS = "550e8400-e29b-41d4-a716-446655440000"


def _env(**claims):
    base = {"workspaceID": WS, "exp": NOW + 86400 * 20, "isAutomation": True}
    base.update(claims)
    return {"MULTILOGIN_AUTOMATION_TOKEN_B": _jwt(base), "MULTILOGIN_WORKSPACE_ID_B": WS}


def test_token_preflight_accepts_a_good_token():
    assert mlx.token_preflight("B", env=_env(), now=NOW)["ok"] is True


def test_token_preflight_rejects_expired_and_foreign_workspace():
    expired = mlx.token_preflight("B", env=_env(exp=NOW - 5), now=NOW)
    assert expired["ok"] is False and expired["code"] == mlx.TOKEN_EXPIRED
    foreign = mlx.token_preflight("B", env=_env(workspaceID="6ba7b810-9dad-11d1-80b4-00c04fd430c8"), now=NOW)
    assert foreign["ok"] is False and foreign["code"] == mlx.TOKEN_WORKSPACE


def test_token_preflight_never_returns_or_logs_the_token(caplog):
    env = _env(exp=NOW - 5)
    token = env["MULTILOGIN_AUTOMATION_TOKEN_B"]
    caplog.set_level("DEBUG")
    out = mlx.token_preflight("B", env=env, now=NOW)
    assert token not in json.dumps(out) and token not in caplog.text and "SIGNATURE" not in json.dumps(out)


@pytest.mark.parametrize("value", ["", "not-a-jwt", "a.b.c"])
def test_unreadable_token_does_not_block(value):
    assert mlx.token_preflight("B", env={"MULTILOGIN_AUTOMATION_TOKEN_B": value}, now=NOW)["ok"] is True


def test_expired_token_stops_the_whole_slot_b_batch_before_any_start(monkeypatch, tmp_path):
    for k, v in _env(exp=1).items():
        monkeypatch.setenv(k, v)
    b = BatchRig(tmp_path)
    b.launchers["B"].fail = None
    results = b.rig.reg.open_many(_items(["B", "A", "B"]), job_id="jt")
    assert results["B1"]["code"] == mlx.TOKEN_EXPIRED and results["B3"]["code"] == mlx.TOKEN_EXPIRED
    assert b.launchers["B"].calls == []                  # ни одного старта слота B
    assert results["A2"]["ok"]


# ───────────────────────── страница ─────────────────────────

def test_page_scripts_send_batch_show_one_banner_and_retry_only_the_skipped():
    report = (ROOT / "src/web/templates/report.html").read_text(encoding="utf-8")
    assert "body.append('batch', batch)" in report and "amapBatchBanner(btn, scope, stopped, per" in report
    assert "amapRunJobs(btn, scope, stopped, per)" in report            # Retry = только пропущенные
    assert "fd.set('batch', batch.id)" in report                         # Publish all
    camp = (ROOT / "src/web/templates/_campaign_table.html").read_text(encoding="utf-8")
    assert "body.append('batch', batch)" in camp and "stopped.map(function (x) { return x.job; })" in camp
    assert "data-cb-stop-retry" in report and "onlyKeys.indexOf" in report
    assert "amapRunSplit(job.account, job.btn, per, batch)" in report
    warm_js = (ROOT / "src/web/static/warm_sessions.js").read_text(encoding="utf-8")
    assert "runOpen(grid, s.stopped)" in warm_js
    cmds = (ROOT / "src/web/static/acm_commands.js").read_text(encoding="utf-8")
    assert LOST_TEXT in cmds and PROXY_TEXT in cmds


# ── поведение страницы: Publish all -> баннер -> Retry (Node) ────────────────

def _retry_run() -> dict:
    """Настоящий cbPublishAll/cbNewRun/showQueue страницы в Node: три формы, две
    останавливает mlx_session_lost, затем нажатие Retry в баннере."""
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("нет node — исполнить код страницы нечем")
    r = subprocess.run([node, str(ROOT / "tests" / "acl1826_retry_harness.cjs"),
                        str(ROOT / "src/web/templates/report.html")],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_publish_all_shows_one_banner_for_the_two_stopped_forms():
    out = _retry_run()
    assert sorted(p["account"] for p in out["first"]) == ["UL1", "UL2", "UL3"]
    assert out["banner"].count("data-cb-stop-retry") == 1               # одна строка на пачку
    assert "2 accounts not sent" in out["banner"] and out["handler_present"]


def test_retry_resends_exactly_the_stopped_forms_with_one_batch_id_per_run():
    out = _retry_run()
    first, retry = out["first"], out["retry"]
    # Первый запуск: все три — под одним id пачки.
    assert len({p["batch"] for p in first}) == 1 and first[0]["batch"]
    # Retry: только остановленные (UL1 уже собран и не едет второй раз), id один на запуск.
    assert sorted(p["account"] for p in retry) == ["UL2", "UL3"]
    assert len({p["batch"] for p in retry}) == 1 and retry[0]["batch"]
    assert retry[0]["batch"] != first[0]["batch"]                       # новый запуск — новая пачка
    assert out["alerts"] == []
