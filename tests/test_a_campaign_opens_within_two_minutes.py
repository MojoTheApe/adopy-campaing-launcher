# -*- coding: utf-8 -*-
"""Кампания открывается за две минуты, даже когда её вкладка замерла (ACL-1320).

Прогон cf563b4ba594 (26.09, Doc19): первая кампания открывалась 19.2 / 16.3 /
1.3 мин в трёх прогонах на одном процессе профиля — рендерер долгоживущей
вкладки списка не отвечал. Теперь `_open_campaign` держит жёсткий бюджет:
раунд «Create → окно цели» не дольше `CAMPAIGN_OPEN_ROUND_S` (пока загрузка
не видна), раунд без окна — список заново и новый Create, третий раунд или
второе «не отвечает» — свежая вкладка того же профиля; не отвечает и она —
"profile_unresponsive", и публикатор перезапускает профиль через MLX API.

Поддельные часы и страницы; `_create_took`, `_wait_objective_dialog`,
`browser.fresh_tab` — сценарием.
"""
from __future__ import annotations

import sys
import time as _real_time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import browser as br  # noqa: E402
from src.fb_ads_rpa import meta_dom as md  # noqa: E402
from src.fb_ads_rpa import retry_policy  # noqa: E402

LIST_URL = "https://adsmanager.facebook.com/adsmanager/manage/campaigns?act=1"


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def time(self):
        return self.now

    def sleep(self, s):
        self.now += s


@pytest.fixture
def env(monkeypatch):
    clock = _Clock()
    ev: list[tuple] = []
    rows: list[dict] = []
    sc = SimpleNamespace(
        frozen=set(),          # страницы, которые не отвечают
        took="ok",
        dialog_at=None,        # (страница, момент) — когда окно цели на экране
        wait_out={},           # что вернуть в `out` ожидания окна цели
        fresh=[],              # что вернёт fresh_tab (по очереди)
    )
    monkeypatch.setattr(md, "time", SimpleNamespace(time=clock.time, sleep=clock.sleep, monotonic=clock.time))
    monkeypatch.setattr(md, "_sleep_h", lambda *a, **k: None)
    monkeypatch.setattr(md._telemetry, "record",
                        lambda step, ok=None, **kw: rows.append({"step": step, "ok": ok, **kw}))
    monkeypatch.setattr(md, "_tab_frozen", lambda p: (clock.sleep(md.LIVENESS_PROBE_S) or True)
                        if p.name in sc.frozen else False)
    monkeypatch.setattr(md, "_clear_blocking_overlays", lambda p, where, **kw: [])
    monkeypatch.setattr(md, "_open_list_again",
                        lambda p, url, deadline: ev.append(("list", p.name)) or clock.sleep(4))

    def took(p, url, timeout_s=8.0):
        clock.sleep(1)
        return sc.took

    monkeypatch.setattr(md, "_create_took", took)

    def wait(p, *, first_s=75.0, loading_max_s=180.0, out=None, **kw):
        ev.append(("wait", p.name, round(first_s, 1), round(loading_max_s, 1)))
        if out is not None:
            out.update(sc.wait_out or {"outcome": "first_s", "frozen": 0})
        if sc.dialog_at and sc.dialog_at[0] == p.name and clock.now + first_s >= sc.dialog_at[1]:
            clock.now = max(clock.now, sc.dialog_at[1])
            return True
        clock.sleep(first_s)
        return False

    monkeypatch.setattr(md, "_wait_objective_dialog", wait)

    def fresh(p, url, **kw):
        ev.append(("fresh", p.name, url))
        return sc.fresh.pop(0) if sc.fresh else None

    monkeypatch.setattr(br, "fresh_tab", fresh)
    monkeypatch.setattr(br, "wake_page_quietly", lambda p: ev.append(("wake", p.name)) or {})

    def press(p, where):
        ev.append(("press", p.name, where))
        clock.sleep(2)
        return True

    adopted: list[str] = []

    def run():
        return md._open_campaign(_P("old"), LIST_URL, press=press, shot=lambda label: None,
                                 on_new_page=lambda p: adopted.append(p.name))

    return SimpleNamespace(clock=clock, ev=ev, rows=rows, sc=sc, run=run, adopted=adopted, start=clock.now)


class _P:
    def __init__(self, name):
        self.name = name
        self.url = LIST_URL


def _budget_row(rows):
    return [r for r in rows if r["step"] == "campaign_open_budget"][-1]


def test_a_dialog_that_never_comes_ends_within_the_budget(env):
    env.sc.fresh = [_P("new")]
    assert env.run() == "objective_leads"
    spent = env.clock.now - env.start
    assert spent <= md.CAMPAIGN_OPEN_BUDGET_S + 10, spent
    row = _budget_row(env.rows)
    assert row["ok"] is False and row["note"] == "objective_leads"
    # Ни один раунд без видимой загрузки не ждёт окно дольше раунда.
    assert all(e[2] <= md.CAMPAIGN_OPEN_ROUND_S for e in env.ev if e[0] == "wait")


def test_a_round_without_the_dialog_goes_back_to_the_list_and_presses_again(env):
    env.sc.dialog_at = ("old", env.start + 70)
    assert env.run() == ""
    ev = env.ev
    first_wait = next(i for i, e in enumerate(ev) if e[0] == "wait")
    assert ev[first_wait + 1] == ("list", "old")
    assert ev[first_wait + 2] == ("press", "old", "create_retry")
    assert _budget_row(env.rows)["ok"] is True


def test_the_third_round_presses_create_in_a_fresh_tab(env):
    env.sc.fresh = [_P("new")]
    env.sc.dialog_at = ("new", env.start + 1_000)
    env.run()
    fresh = [i for i, e in enumerate(env.ev) if e[0] == "fresh"]
    assert len(fresh) == 1, "свежая вкладка — одна на открытие"
    presses = [e for e in env.ev if e[0] == "press"]
    assert [p[1] for p in presses[:2]] == ["old", "old"]
    assert presses[2][1] == "new", "третий Create — не в свежей вкладке"
    assert env.adopted == ["new"], "поток не узнал о свежей вкладке"
    assert env.ev[fresh[0]][2] == LIST_URL


def test_a_second_unanswered_probe_takes_the_fresh_tab_at_once(env):
    """Ожидание окна цели дважды не дождалось ответа вкладки — свежая
    вкладка уже во втором раунде, не в третьем."""
    env.sc.fresh = [_P("new")]
    env.sc.wait_out = {"outcome": "unresponsive", "frozen": 2}
    env.sc.dialog_at = ("new", env.start)
    assert env.run() == ""
    assert [e[0] for e in env.ev if e[0] != "wake"][:4] == ["press", "wait", "fresh", "press"]


def test_no_fresh_tab_means_the_profile_does_not_answer(env):
    env.sc.frozen = {"old"}
    assert env.run() == "profile_unresponsive"
    assert env.clock.now - env.start < md.CAMPAIGN_OPEN_BUDGET_S
    assert _budget_row(env.rows)["note"] == "profile_unresponsive"


def test_a_fresh_tab_that_freezes_too_means_the_profile_does_not_answer(env):
    env.sc.frozen = {"old", "new"}
    env.sc.fresh = [_P("new")]
    assert env.run() == "profile_unresponsive"
    assert not [e for e in env.ev if e[0] == "press"], "на мёртвой вкладке жали Create"


def test_visible_loading_may_use_the_rest_of_the_budget(env):
    """Пока Meta видимо грузит, раунд ждёт до конца бюджета: перезагрузка
    поверх «Loading creation» выбрасывала её (ACL-1313)."""
    env.sc.dialog_at = ("old", env.start + 30)
    env.run()
    wait = next(e for e in env.ev if e[0] == "wait")
    assert wait[2] <= md.CAMPAIGN_OPEN_ROUND_S
    assert wait[3] > md.CAMPAIGN_OPEN_ROUND_S


def test_ai_manual_chooser_is_one_create_press_and_opened(env):
    """ACL-1726: выбор AI/Manual — нажатие взялось, второго Create нет."""
    env.sc.took = "ai_manual_chooser"
    env.sc.wait_out = {"outcome": "ai_manual_chooser", "frozen": 0}
    env.sc.dialog_at = ("old", env.start)
    assert env.run() == ""
    assert len([e for e in env.ev if e[0] == "press"]) == 1
    took = [r for r in env.rows if r["step"] == "create_took"]
    assert took and took[-1]["ok"] is True and took[-1]["note"] == "ai_manual_chooser"
    row = _budget_row(env.rows)
    assert row["ok"] is True and row["diag"]["last"] == "ai_manual_chooser"


def test_a_frozen_tab_with_the_chooser_is_not_opened(env):
    env.sc.took = "ai_manual_chooser"
    env.sc.frozen = {"old"}
    env.sc.wait_out = {"outcome": "unresponsive", "frozen": 2}
    assert env.run() == "profile_unresponsive"
    assert _budget_row(env.rows)["ok"] is False


def test_wait_does_not_ask_a_frozen_tab_about_the_chooser(monkeypatch):
    """ACL-1641/1320: на зависшей вкладке выбор не проверяется."""
    asked = []
    monkeypatch.setattr(md, "_tab_gone", lambda p: False)
    monkeypatch.setattr(md, "_tab_frozen", lambda p: True)
    monkeypatch.setattr(md, "_ai_manual_chooser_visible",
                        lambda p: asked.append(1) or True)
    monkeypatch.setattr(md, "_StallWatch",
                        lambda *a, **k: SimpleNamespace(check=lambda **kw: False))
    monkeypatch.setattr(md, "_stall_evidence", lambda *a, **k: None)
    monkeypatch.setattr(md._telemetry, "record", lambda *a, **k: None)
    monkeypatch.setattr(md, "_pace", lambda *a, **k: None)

    class _Loc:
        first = None

        def wait_for(self, **kw):
            raise TimeoutError

    loc = _Loc()
    loc.first = loc
    page = SimpleNamespace(get_by_role=lambda *a, **k: loc, url=LIST_URL)
    out = {}
    assert md._wait_objective_dialog(page, first_s=5, poll_s=0.01, out=out) is False
    assert out["outcome"] == "unresponsive" and not asked


def test_the_budget_is_two_minutes():
    assert md.CAMPAIGN_OPEN_BUDGET_S == 120.0 and md.CAMPAIGN_OPEN_ROUND_S <= 45.0
    assert md.CAMPAIGN_OPEN_FRESH_TAB_ROUND == 3


def test_profile_unresponsive_is_a_retryable_stop():
    assert retry_policy.disposition("profile_unresponsive").action == retry_policy.RETRY_CAMPAIGN


# ── публикатор: перезапуск профиля и смена страницы ───────────────────────

def _publish(monkeypatch, fails, restart):
    from src.fb_ads_rpa import ads_manager_flow as amf
    from src.fb_ads_rpa.draft import AdDraft, AdSetDraft, CampaignDraft

    monkeypatch.setenv("FB_RPA_ENGINE", "recorded_flow")
    monkeypatch.setattr(amf.AdsManagerFlow, "_goto_campaigns", lambda self, draft, result: None)
    monkeypatch.setattr(br, "put_window_back", lambda page: None)
    seen: list = []
    queue = list(fails)

    def flow_run(page, **kw):
        seen.append(page)
        return queue.pop(0) if queue else ""

    monkeypatch.setattr(amf, "run_recorded_campaign_flow", flow_run)

    class _Time:
        def __getattr__(self, name):
            return getattr(_real_time, name)

        @staticmethod
        def sleep(s):
            pass

    monkeypatch.setattr(amf, "time", _Time())
    replaced: list = []
    flow = amf.AdsManagerFlow("old-page", on_page_replaced=replaced.append, restart_profile=restart)
    draft = CampaignDraft("Campaign A", "MX", "Account",
                          ad_sets=[AdSetDraft("Set A", ads=[AdDraft(ad_title="t", ad_body="b")])])
    return flow.publish_campaign(draft), seen, replaced


def test_an_unresponsive_profile_is_restarted_once_and_the_campaign_retried(monkeypatch):
    calls: list = []
    result, seen, replaced = _publish(monkeypatch, ["profile_unresponsive"],
                                      lambda: calls.append(1) or "new-page")
    assert calls == [1]
    assert seen == ["old-page", "new-page"], "повтор кампании — не в перезапущенном профиле"
    assert replaced == ["new-page"]
    assert result.success is True
    assert "profile_restarted_unresponsive:ok" in result.steps_completed


def test_a_second_unresponsive_open_is_not_restarted_again(monkeypatch):
    calls: list = []
    result, seen, _ = _publish(monkeypatch, ["profile_unresponsive", "profile_unresponsive"],
                               lambda: calls.append(1) or "new-page")
    assert calls == [1]
    assert result.success is False
    assert "recorded_flow_retry_blocked:profile_unresponsive:no_restart" in result.steps_completed


def test_a_failed_restart_does_not_retry_in_the_dead_profile(monkeypatch):
    result, seen, _ = _publish(monkeypatch, ["profile_unresponsive"], lambda: None)
    assert seen == ["old-page"]
    assert result.success is False
    assert "profile_restarted_unresponsive:failed" in result.steps_completed


def test_the_flow_page_follows_a_fresh_tab(monkeypatch):
    """`run_recorded_campaign_flow` сообщает свежую вкладку — поток и
    владелец соединения работают дальше в ней."""
    from src.fb_ads_rpa import ads_manager_flow as amf

    replaced: list = []
    flow = amf.AdsManagerFlow("old-page", on_page_replaced=replaced.append)
    flow._adopt_page("new-page")
    assert flow.page == "new-page" and replaced == ["new-page"]


# ── MLX: перезапуск через API своего рабочего пространства ────────────────

def test_the_restart_stops_through_the_profiles_own_workspace(monkeypatch):
    from src.meta_campaign_agent import multilogin_config, multilogin_launcher

    ev: list = []
    monkeypatch.setattr(multilogin_config, "get_multilogin_instance",
                        lambda inst: ev.append(("instance", inst)) or SimpleNamespace(instance_id=inst))

    class _Launcher:
        def __init__(self, cfg):
            self.cfg = cfg

        def stop_profile(self, profile_id, folder_id=None):
            ev.append(("stop", self.cfg.instance_id, profile_id))

    monkeypatch.setattr(multilogin_launcher, "MultiloginLauncher", _Launcher)
    monkeypatch.setattr(br.time, "sleep", lambda s: None)
    monkeypatch.setattr(br, "_find_mlx_profile_dir", lambda pid: None)
    rows: list = []
    from src.fb_ads_rpa import run_telemetry as rt
    monkeypatch.setattr(rt, "record", lambda step, ok=None, **kw: rows.append((step, ok, kw)))
    provider = br.MultiloginAttachOnlyProvider()
    monkeypatch.setattr(provider, "_start_via_mlx_api",
                        lambda entry, label, pid, udd, **kw: ev.append(("start", pid)) or "started")
    monkeypatch.setattr(provider, "_connect_started", lambda started, pid: SimpleNamespace(page="new-page"))
    entry = SimpleNamespace(profile_id="p-1", folder_id="f-1", multilogin_instance="B")
    conn = provider.restart_unresponsive(entry, "KLD_BFL_S15")
    assert conn.page == "new-page"
    assert ev[:3] == [("instance", "B"), ("stop", "B", "p-1"), ("start", "p-1")], ev
    assert ("profile_restarted_unresponsive", True) in [r[:2] for r in rows]


def test_an_attached_profile_that_does_not_answer_is_restarted(monkeypatch):
    """Прицепились к уже открытому профилю, а он не отвечает (ни вкладка, ни
    свежая): остановка через MLX API и запуск заново, не ожидание старой."""
    ev: list = []
    provider = br.MultiloginAttachOnlyProvider()
    monkeypatch.setattr(br, "_find_mlx_profile_dir", lambda pid: "C:/mlx/p-1")

    def attach(pid, udd, **kw):
        ev.append(("attach", kw.get("check_responsive")))
        raise br.ProfileUnresponsive("silent")

    monkeypatch.setattr(provider._direct, "_try_attach_existing", attach)
    monkeypatch.setattr(provider._direct, "_profile_process_running", lambda *a: True)
    # Порт слушает процесс именно этого профиля (ревью PR #323, F6).
    monkeypatch.setattr(provider._direct, "_discover_owned_debug_port", lambda udd: 9222)
    monkeypatch.setattr(provider, "_stop_profile", lambda *a: ev.append(("stop",)))
    monkeypatch.setattr(provider, "_start_via_mlx_api", lambda *a, **kw: ev.append(("start",)) or "s")
    monkeypatch.setattr(provider, "_connect_started", lambda started, pid: SimpleNamespace(page="p"))
    rows: list = []
    from src.fb_ads_rpa import run_telemetry as rt
    monkeypatch.setattr(rt, "record", lambda step, ok=None, **kw: rows.append((step, ok, kw.get("note"))))
    entry = SimpleNamespace(profile_id="p-1", folder_id="f-1", multilogin_instance="A")
    provider.open(entry, "FacebookDoc19")
    assert ev == [("attach", True), ("stop",), ("start",)]
    assert ("profile_restarted_unresponsive", True, "attach") in rows


def test_the_attach_check_hands_a_dead_profile_up(monkeypatch):
    """`_connect_playwright(check_responsive=True)`: вкладка молчит, свежая не
    открылась — `ProfileUnresponsive`, а не подключение к мёртвой вкладке."""
    class _Page:
        url = LIST_URL

        def wait_for_function(self, *a, **k):
            raise TimeoutError("Timeout 5000ms exceeded.")

    class _Ctx:
        pages = [_Page()]

    browser = SimpleNamespace(contexts=[_Ctx()])
    pw = SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=lambda url: browser), stop=lambda: None)
    monkeypatch.setattr(br, "sync_playwright", lambda: SimpleNamespace(start=lambda: pw))
    monkeypatch.setattr(br, "ensure_playwright_browsers_path", lambda: None)
    monkeypatch.setattr(br, "_unminimize_before_attach", lambda b: [])
    monkeypatch.setattr(br, "fresh_tab", lambda p, url, **kw: None)
    touched: list = []
    monkeypatch.setattr(br, "_disable_background_throttling", lambda c, p: touched.append("throttle"))
    with pytest.raises(br.ProfileUnresponsive):
        br._connect_playwright(cdp_url="http://127.0.0.1:1", provider="multilogin_direct",
                               debug_port="1", ws_endpoint=None, check_responsive=True)
    assert touched == [], "до вопроса о живости — ни одного CDP-вызова к вкладке"


def test_the_publisher_restarts_through_its_provider_and_follows_the_page(monkeypatch):
    """`RpaPublisher._flow`: смена вкладки доходит до соединения, перезапуск
    идёт через `restart_unresponsive` провайдера, и дальше соединение — новое."""
    from src.fb_ads_rpa.rpa_publisher import RpaPublisher

    pub = RpaPublisher.__new__(RpaPublisher)
    pub.selectors_path = None
    calls: list = []
    new_conn = SimpleNamespace(page="restarted-page", close=lambda **kw: None)
    pub._provider = SimpleNamespace(
        restart_unresponsive=lambda entry, label: calls.append((entry, label)) or new_conn,
        close=lambda entry, **kw: None)
    pub._entry = "entry"
    old_conn = SimpleNamespace(page="old-page", close=lambda **kw: calls.append("disconnect"))
    pub._conn = old_conn
    flow = pub._flow(old_conn, "FacebookDoc19")
    flow._adopt_page("fresh-tab")
    assert old_conn.page == "fresh-tab", "соединение не узнало о свежей вкладке"
    assert flow._restart_profile() == "restarted-page"
    assert calls == ["disconnect", ("entry", "FacebookDoc19")]
    assert pub._conn is new_conn


def test_the_last_create_of_a_round_gets_a_real_window(env, monkeypatch):
    """Ревью PR #323 (F2): два промаха подряд (нажатие увело со списка),
    возврат на список по 20 с — раунд почти съеден, но окну цели после
    последнего нажатия дают не меньше `CAMPAIGN_OPEN_AFTER_CREATE_S`."""
    env.sc.took = "left_list"
    monkeypatch.setattr(md, "_back_to_list", lambda p, u: env.clock.sleep(20))
    env.run()
    waits = [e for e in env.ev if e[0] == "wait"]
    assert waits[0][2] >= md.CAMPAIGN_OPEN_AFTER_CREATE_S, waits[0]
    assert all(w[2] <= w[3] for w in waits), "окно шире общего бюджета"


def test_the_create_window_never_outgrows_the_budget(env, monkeypatch):
    env.sc.took = "left_list"
    monkeypatch.setattr(md, "CAMPAIGN_OPEN_BUDGET_S", 60.0)
    monkeypatch.setattr(md, "_back_to_list", lambda p, u: env.clock.sleep(20))
    env.run()
    waits = [e for e in env.ev if e[0] == "wait"]
    assert waits and waits[0][2] < md.CAMPAIGN_OPEN_AFTER_CREATE_S
    assert env.clock.now - env.start <= 60.0 + md.OBJECTIVE_POLL_S + 10


def test_a_tab_that_answers_the_long_probe_is_not_replaced(env, monkeypatch):
    """Ревью PR #323 (F8): ожидание окна цели дважды не дождалось ответа
    вкладки (`unresponsive`), но длинная проба перед свежей вкладкой
    ответила — вкладку не закрываем, второй раунд идёт в ней."""
    env.sc.wait_out = {"outcome": "unresponsive", "frozen": md.OBJECTIVE_FROZEN_POLLS}
    probes: list = []
    monkeypatch.setattr(md, "_page_responsive",
                        lambda p, timeout_s=5.0: probes.append(timeout_s) or True)
    env.sc.dialog_at = ("old", env.start + 60)
    assert env.run() == ""
    assert md.FROZEN_CONFIRM_S in probes
    assert not [e for e in env.ev if e[0] == "fresh"], "занятую вкладку закрыли как зависшую"


def test_a_tab_that_stays_silent_on_the_long_probe_is_replaced(env, monkeypatch):
    env.sc.wait_out = {"outcome": "unresponsive", "frozen": md.OBJECTIVE_FROZEN_POLLS}
    monkeypatch.setattr(md, "_page_responsive", lambda p, timeout_s=5.0: False)
    env.sc.fresh = [_P("new")]
    env.sc.dialog_at = ("new", env.start + 60)
    assert env.run() == ""
    assert [e for e in env.ev if e[0] == "fresh"]


def test_the_budget_is_a_hard_ceiling(env, monkeypatch):
    """Ревью PR #323 (F9): 120 с — потолок, а не пожелание. Медленные
    нажатия (по 10 с), после каждого вкладка на миг не отвечает — раунды
    короткие: ни одно нажатие не начинается, когда ждать его окно уже
    некогда, и открытие не уходит за бюджет больше, чем на одно нажатие."""
    starts: list = []

    def press(p, where):
        starts.append(env.clock.now - env.start)
        env.clock.sleep(10)
        return True

    env.sc.took = "frozen"
    # Без свежей вкладки: проверяется только потолок нажатий.
    monkeypatch.setattr(md, "CAMPAIGN_OPEN_FRESH_TAB_ROUND", 99)
    monkeypatch.setattr(md, "_page_responsive", lambda p, timeout_s=5.0: True)
    got = md._open_campaign(_P("old"), LIST_URL, press=press, shot=lambda label: None)
    assert got == "objective_leads"
    assert len(starts) >= 4, starts
    assert starts and all(t <= md.CAMPAIGN_OPEN_BUDGET_S - md.CAMPAIGN_OPEN_MIN_PRESS_S for t in starts), starts
    assert env.clock.now - env.start <= md.CAMPAIGN_OPEN_BUDGET_S + 25 + md.OBJECTIVE_POLL_S


def test_steps_inside_the_open_see_its_deadline(env, monkeypatch):
    seen: list = []

    def press(p, where):
        seen.append(md._open_left())
        return True

    env.sc.dialog_at = ("old", env.start + 10)
    md._open_campaign(_P("old"), LIST_URL, press=press, shot=lambda label: None)
    assert seen and seen[0] <= md.CAMPAIGN_OPEN_BUDGET_S
    assert md._open_left() == float("inf"), "срок открытия остался висеть после него"
