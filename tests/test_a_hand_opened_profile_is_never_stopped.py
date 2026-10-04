# -*- coding: utf-8 -*-
"""Профиль, открытый руками, робот не останавливает (ACL-1320).

Живой прогон 26.09 15:21–15:57 UTC (новые аккаунты): владелец входил в
Facebook в только что открытых руками профилях, а робот на Publish видел
«running but no CDP debug port», писал «locked (opened manually in MLX).
Stopping it via MLX API and restarting with automation enabled…» и
останавливал окно. Если Multilogin ещё не сохранил свежую сессию Facebook,
она пропадала вместе с окном (так было и в выпущенной сборке).

Теперь: профиль открыт руками (нет порта автоматизации, робот его не
поднимал) — задание этого аккаунта сразу кончается `profile_open_by_hand` с
текстом «This Multilogin profile is open by hand. Close its window (so
Multilogin saves the session), then press Publish again.», окно не трогается.
Перезапуск через MLX API остаётся только для профилей, которые робот сам
поднимал раньше (или к которым цеплялся по порту автоматизации). Простой
браузер — никогда.

ACL-1388/1562: теперь робот такое окно мягко закрывает и поднимает сам и
сразу; эти тесты идут с машинным выключателем `FB_RPA_REOPEN_HAND_WINDOW=0`
(tests/conftest.py) и проверяют прежний стоп, к которому он возвращает.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import acl_sentry  # noqa: E402
from src.fb_ads_rpa import browser as br  # noqa: E402
from src.fb_ads_rpa import failure_policy, retry_policy  # noqa: E402
from src.fb_ads_rpa import profile_trouble as pt  # noqa: E402

PROFILE = "550e8400-e29b-41d4-a716-446655440000"
MESSAGE = ("This Multilogin profile is open by hand. Close its window "
           "(so Multilogin saves the session), then press Publish again.")


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setattr(br, "_ROBOT_PROFILES", {})
    monkeypatch.setattr(br.time, "sleep", lambda s: None)
    monkeypatch.setattr(br, "_find_mlx_profile_dir", lambda pid: "C:/mlx/p")
    ev: list = []
    prov = br.MultiloginAttachOnlyProvider()
    monkeypatch.setattr(prov._direct, "_try_attach_existing", lambda *a, **kw: None)
    monkeypatch.setattr(prov._direct, "_profile_process_running", lambda *a: True)
    monkeypatch.setattr(prov, "_stop_profile", lambda *a: ev.append("stop"))
    monkeypatch.setattr(br, "_kill_chrome_for_user_data_dir", lambda *a, **k: ev.append("kill"))
    monkeypatch.setattr(prov, "_connect_started", lambda started, pid: SimpleNamespace(page="page"))
    return SimpleNamespace(prov=prov, ev=ev)


def _entry():
    return SimpleNamespace(profile_id=PROFILE, folder_id="f-1", multilogin_instance="A")


def test_a_hand_opened_profile_fails_fast_and_is_not_stopped(provider, monkeypatch):
    monkeypatch.setattr(provider.prov, "_start_via_mlx_api",
                        lambda *a, **kw: provider.ev.append("start") or "started")
    with pytest.raises(pt.ProfileStop) as stop:
        provider.prov.open(_entry(), "Facebook_6540")
    assert stop.value.code == pt.OPEN_BY_HAND
    assert MESSAGE in str(stop.value)
    assert provider.ev == [], "окно, открытое руками, остановили или подняли заново"


def test_a_profile_the_robot_started_earlier_is_still_restarted(provider, monkeypatch):
    br._mark_robot_profile(PROFILE)
    monkeypatch.setattr(provider.prov, "_start_via_mlx_api",
                        lambda *a, **kw: provider.ev.append("start") or "started")
    conn = provider.prov.open(_entry(), "Facebook_6540")
    assert conn.page == "page"
    assert provider.ev == ["start"]


class _Launcher:
    """MLX отвечает «профиль занят» — как на окно, открытое руками."""

    def __init__(self):
        self.stops = 0
        self.starts = 0

    def start_profile(self, folder_id, profile_id, *, headless=False):
        from src.meta_campaign_agent.multilogin_launcher import MultiloginLauncherError, StartedProfile

        self.starts += 1
        if self.starts == 1:
            raise MultiloginLauncherError("Start profile failed HTTP 400: browser process is running")
        return StartedProfile(profile_id=profile_id, folder_id=folder_id, port="58906")

    def stop_profile(self, profile_id, folder_id=None):
        self.stops += 1


@pytest.fixture
def mlx(monkeypatch):
    import src.meta_campaign_agent.multilogin_config as cfg_mod
    import src.meta_campaign_agent.multilogin_launcher as launcher_mod

    fake = _Launcher()
    monkeypatch.setattr(cfg_mod, "get_multilogin_instance", lambda inst: SimpleNamespace(instance_id=inst))
    monkeypatch.setattr(launcher_mod, "MultiloginLauncher", lambda cfg: fake)
    monkeypatch.setattr(br, "_ROBOT_PROFILES", {})
    monkeypatch.setattr(br.time, "sleep", lambda s: None)
    kills: list = []
    monkeypatch.setattr(br, "_kill_chrome_for_user_data_dir", lambda *a, **k: kills.append(a))
    return SimpleNamespace(fake=fake, kills=kills)


def test_an_mlx_lock_on_a_hand_opened_profile_is_not_broken(mlx):
    """Процесс не виден, а MLX говорит «browser process is running» — тоже
    окно, открытое руками: ни stop через API, ни kill."""
    with pytest.raises(pt.ProfileStop) as stop:
        br.MultiloginAttachOnlyProvider()._start_via_mlx_api(_entry(), "Facebook_9943", PROFILE, "C:/mlx/p")
    assert stop.value.code == pt.OPEN_BY_HAND
    assert mlx.fake.stops == 0 and mlx.kills == []


def test_a_profile_the_robot_started_becomes_its_own(mlx):
    mlx.fake.starts = 1   # следующий старт удачный
    started = br.MultiloginAttachOnlyProvider()._start_via_mlx_api(_entry(), "Facebook_9943", PROFILE, "C:/mlx/p")
    assert started.port == "58906"
    assert br._robot_profile(PROFILE) is True


def test_attaching_over_the_automation_port_makes_the_profile_the_robots(provider, monkeypatch):
    monkeypatch.setattr(provider.prov._direct, "_try_attach_existing",
                        lambda *a, **kw: SimpleNamespace(page="attached"))
    monkeypatch.setattr(provider.prov._direct, "_discover_owned_debug_port", lambda udd: 9222)
    provider.prov.open(_entry(), "FacebookDoc19")
    assert br._robot_profile(PROFILE) is True


def test_a_port_not_owned_by_the_profile_does_not_make_it_the_robots(provider, monkeypatch):
    """Ревью PR #323 (F6): нестрогий поиск порта (старый DevToolsActivePort,
    общий 19222) — не доказательство, что профиль поднимал робот."""
    monkeypatch.setattr(provider.prov._direct, "_try_attach_existing",
                        lambda *a, **kw: SimpleNamespace(page="attached"))
    monkeypatch.setattr(provider.prov._direct, "_discover_owned_debug_port", lambda udd: None)
    provider.prov.open(_entry(), "FacebookDoc19")
    assert br._robot_profile(PROFILE) is False


def test_an_unowned_silent_port_never_stops_the_profile(provider, monkeypatch):
    def attach(*a, **kw):
        raise br.ProfileUnresponsive("silent")

    monkeypatch.setattr(provider.prov._direct, "_try_attach_existing", attach)
    monkeypatch.setattr(provider.prov._direct, "_discover_owned_debug_port", lambda udd: None)
    monkeypatch.setattr(provider.prov, "_start_via_mlx_api",
                        lambda *a, **kw: provider.ev.append("start") or "started")
    with pytest.raises(pt.ProfileStop) as stop:
        provider.prov.open(_entry(), "FacebookDoc19")
    assert stop.value.code == pt.OPEN_BY_HAND
    assert provider.ev == []


def test_the_publisher_ends_the_accounts_job_with_the_message(monkeypatch):
    from src.fb_ads_rpa.draft import AdDraft, AdSetDraft, CampaignDraft
    from src.fb_ads_rpa.rpa_publisher import RpaPublisher

    pub = RpaPublisher.__new__(RpaPublisher)
    pub.hand_off = pub.dry_run = False
    pub.selectors_path = None
    pub._conn = None
    pub._built_at = None
    opened: list = []

    def ensure(label):
        opened.append(label)
        raise pt.ProfileStop(pt.OPEN_BY_HAND, "no_automation_port")

    monkeypatch.setattr(pub, "_ensure_browser", ensure)
    drafts = [CampaignDraft(n, "MX", "Facebook_6540",
                            ad_sets=[AdSetDraft("Set", ads=[AdDraft(ad_title="t", ad_body="b")])])
              for n in ("A", "B")]
    results = [pub.publish_draft(d) for d in drafts]
    assert opened == ["Facebook_6540"]
    for got in results:
        assert got.success is False
        assert got.blocking_failures == [pt.OPEN_BY_HAND]
        assert MESSAGE in got.error


def test_open_by_hand_is_a_blocking_stop_with_the_message():
    assert pt.MESSAGES[pt.OPEN_BY_HAND] == MESSAGE
    assert pt.OPEN_BY_HAND in pt.ACCOUNT_STOPS
    assert retry_policy.disposition(pt.OPEN_BY_HAND).action == retry_policy.BLOCK_AND_REVIEW
    assert pt.OPEN_BY_HAND in retry_policy.BLOCKING_REVIEW_STEPS
    assert failure_policy.severity(pt.OPEN_BY_HAND) == failure_policy.BLOCKING
    assert MESSAGE in failure_policy.reason(pt.OPEN_BY_HAND)
    assert acl_sentry._reason_kind(str(pt.ProfileStop(pt.OPEN_BY_HAND))) == "open-by-hand"


def test_the_dashboard_tells_the_buyer_to_close_the_window():
    from src.web import publish_progress

    assert publish_progress._BLOCKERS[pt.OPEN_BY_HAND] == MESSAGE
    html = (ROOT / "src" / "web" / "templates" / "report.html").read_text(encoding="utf-8")
    assert "the window may restart" not in html
    # ACL-1562: подсказка Publish больше не просит закрывать окно руками —
    # робот закрывает его сам через Multilogin.
    assert "a window opened by hand is closed through Multilogin" in html
    assert "the robot will not close it for you" not in html
    src = (ROOT / "src" / "fb_ads_rpa" / "rpa_publisher.py").read_text(encoding="utf-8")
    assert "restarting the profile itself when it has no debug port" not in src


def test_a_robot_profile_reopened_by_hand_is_the_owners_window(provider, monkeypatch):
    """Робот поднимал профиль (процесс 101), тот умер, человек открыл его
    руками (процесс 202) — это уже окно человека: не останавливаем."""
    pids = {"now": frozenset({101})}
    monkeypatch.setattr(br, "_profile_pids", lambda udd, pid: pids["now"])
    br._mark_robot_profile(PROFILE, "C:/mlx/p")
    monkeypatch.setattr(provider.prov, "_start_via_mlx_api",
                        lambda *a, **kw: provider.ev.append("start") or "started")
    pids["now"] = frozenset({202})
    with pytest.raises(pt.ProfileStop) as stop:
        provider.prov.open(_entry(), "Facebook_9943")
    assert stop.value.code == pt.OPEN_BY_HAND
    assert provider.ev == []
    pids["now"] = frozenset({101})
    provider.prov.open(_entry(), "Facebook_9943")
    assert provider.ev == ["start"], "свой же процесс робота без порта не перезапустили"


# ── Ревью PR #323 (F7) ────────────────────────────────────────────────────

def test_a_failed_restart_at_attach_is_profile_restart_failed(provider, monkeypatch):
    """Робот остановил не отвечавший профиль при подключении, а поднять не
    смог (15:40, ECONNREFUSED) — `profile_restart_failed` и строка телеметрии."""
    def attach(*a, **kw):
        raise br.ProfileUnresponsive("silent")

    def start(*a, **kw):
        raise RuntimeError("BrowserType.connect_over_cdp: connect ECONNREFUSED 127.0.0.1:56240")

    monkeypatch.setattr(provider.prov._direct, "_try_attach_existing", attach)
    monkeypatch.setattr(provider.prov._direct, "_discover_owned_debug_port", lambda udd: 9222)
    monkeypatch.setattr(provider.prov, "_start_via_mlx_api", start)
    rows: list = []
    from src.fb_ads_rpa import run_telemetry as rt
    monkeypatch.setattr(rt, "record", lambda step, ok=None, **kw: rows.append((step, ok, kw.get("note"))))
    with pytest.raises(pt.ProfileStop) as stop:
        provider.prov.open(_entry(), "Facebook_9943")
    assert stop.value.code == pt.RESTART_FAILED
    assert "ECONNREFUSED" in stop.value.detail
    assert ("profile_restarted_unresponsive", False, "attach") in rows


def test_an_attach_time_stop_reaches_sentry(monkeypatch):
    from src.fb_ads_rpa import sentry_signal
    from src.fb_ads_rpa.draft import AdDraft, AdSetDraft, CampaignDraft
    from src.fb_ads_rpa.rpa_publisher import RpaPublisher

    sent: list = []
    monkeypatch.setattr(sentry_signal, "report_campaign_stop",
                        lambda step, **kw: sent.append((step, kw.get("campaign"))) or "")
    pub = RpaPublisher.__new__(RpaPublisher)
    pub.hand_off = pub.dry_run = False
    pub.selectors_path = None
    pub._conn = None
    pub._built_at = None

    def ensure(label):
        raise pt.ProfileStop(pt.OPEN_BY_HAND, "no_automation_port")

    monkeypatch.setattr(pub, "_ensure_browser", ensure)
    draft = CampaignDraft("A", "MX", "Facebook_6540",
                          ad_sets=[AdSetDraft("Set", ads=[AdDraft(ad_title="t", ad_body="b")])])
    pub.publish_draft(draft)
    assert sent == [(pt.OPEN_BY_HAND, "A")]


def test_restart_telemetry_carries_no_part_of_the_profile_id(monkeypatch):
    """Ревью PR #323 (F11): префикс uuid профиля вычиститель не ловит."""
    from src.fb_ads_rpa import run_telemetry as rt

    rows: list = []
    monkeypatch.setattr(rt, "record", lambda step, ok=None, **kw: rows.append(kw))
    br._record_profile_restart(_entry(), PROFILE, ok=False, where="attach", error="x")
    text = repr(rows)
    assert rows and PROFILE[:4] not in text and "profile" not in rows[0]["diag"]
