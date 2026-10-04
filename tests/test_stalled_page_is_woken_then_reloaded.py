# -*- coding: utf-8 -*-
"""Подвисшая вкладка: сначала разбудить, перезагрузить — только если висит
(просьба владельца 25.09: «если аккаунт подвис — перезагрузи или выведи
вперёд, цель — открыть как можно быстрее»).

Телеметрия трёх прогонов 25.09 — паузы ≥ 60 с перед create, campaign_name,
audience_age, placements, креативом. `_StallWatch` в циклах ожидания после
`STALL_BUDGET_S` один раз зовёт `_unstick`: `wake_page` (окно из свёрнутого
за край экрана, вкладка вперёд, троттлинг снят), а перезагрузка — только
при зависшей загрузке или не отвечающей странице, не больше одной на шаг и
не в редакторе с несохранёнными правками. Проверяется на поддельных часах и
страницах.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import browser as br  # noqa: E402
from src.fb_ads_rpa import meta_dom as md  # noqa: E402
from tests.test_additional_ad_without_creative_has_no_text import flow  # noqa: E402,F401


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
    ev: list = []
    rows: list = []
    state = {"responsive": True, "loading": False, "saved": True}
    monkeypatch.setattr(md, "time", SimpleNamespace(time=clock.time, sleep=clock.sleep, monotonic=clock.time))
    monkeypatch.setattr(md, "_UNSTUCK_RELOADED", set())
    monkeypatch.setattr(md._telemetry, "record", lambda step, ok=None, **kw: rows.append({"step": step, **kw}))
    monkeypatch.setattr(md._telemetry, "current_run_id", lambda: "run1")
    monkeypatch.setattr(br, "wake_page", lambda p: ev.append("wake") or {"front": True})
    monkeypatch.setattr(md, "_page_responsive", lambda p, timeout_s=5.0: state["responsive"])
    monkeypatch.setattr(md, "_loading_visible", lambda p: state["loading"])

    def recover(p, where, *, editor, force=False):
        ev.append(("recover", where, editor))
        return state["saved"] or not editor

    monkeypatch.setattr(md, "recover_stuck_page", recover)
    monkeypatch.setattr(md, "_reload_editor", lambda p, edit_url="": ev.append("reload"))
    monkeypatch.setattr(md, "_reload_awake", lambda p, where: ev.append("reload") or True)
    page = SimpleNamespace(url="https://adsmanager.facebook.com/adsmanager/manage/adsets/edit?x=1")
    return SimpleNamespace(clock=clock, ev=ev, rows=rows, state=state, page=page)


def test_the_watch_waits_for_its_budget_and_fires_once(env):
    watch = md._StallWatch(env.page, "campaign_name")
    env.clock.now += md.STALL_BUDGET_S - 1
    assert watch.check() == "" and env.ev == []
    env.clock.now += 2
    assert watch.check() == "woke"
    assert watch.check() == ""
    assert env.ev == ["wake"]
    row = env.rows[-1]
    assert row["step"] == "stall_unstuck" and row["note"] == "woke"
    assert row["diag"]["where"] == "campaign_name" and row["diag"]["waited_s"] >= md.STALL_BUDGET_S


def test_a_live_page_is_only_woken_never_reloaded(env):
    assert md._unstick(env.page, "create", editor=False) == "woke"
    assert env.ev == ["wake"]


def test_a_stuck_spinner_in_the_editor_goes_through_the_saved_check_once(env):
    env.state["loading"] = True
    assert md._unstick(env.page, "placements", editor=True) == "reloaded"
    assert md._unstick(env.page, "placements", editor=True) == "woke", "вторая перезагрузка на том же шаге"
    assert env.ev.count(("recover", "placements", True)) == 1


def test_unsaved_edits_refuse_the_reload(env):
    env.state.update(loading=True, saved=False)
    assert md._unstick(env.page, "audience_age", editor=True) == "reload_refused"


def test_an_unresponsive_editor_is_not_reloaded(env):
    env.state["responsive"] = False
    assert md._unstick(env.page, "ad_name", editor=True) == "unsaved_or_unknown"
    assert "reload" not in env.ev and not [e for e in env.ev if e[0] == "recover"]


def test_an_unresponsive_campaign_list_is_reloaded(env):
    env.state["responsive"] = False
    assert md._unstick(env.page, "create", editor=False) == "reloaded"
    assert "reload" in env.ev


def test_a_wake_only_watch_never_reloads(env):
    env.state["loading"] = True
    assert md._unstick(env.page, "objective_wait", editor=False, reload=False) == "woke"
    assert env.ev == ["wake"]


def test_the_objective_wait_wakes_the_tab_but_does_not_reload(env):
    """Окна цели нет 60+ с, крутится «Loading creation»: будим, не перезагружаем."""
    env.state["loading"] = True

    class _Radio:
        def wait_for(self, state="visible", timeout=30000):
            env.clock.now += timeout / 1000
            raise TimeoutError("Timeout")

    page = SimpleNamespace(url="https://adsmanager.facebook.com/adsmanager/manage/campaigns",
                           get_by_role=lambda *a, **k: SimpleNamespace(first=_Radio()))
    assert md._wait_objective_dialog(page, loading_max_s=120) is False
    assert env.ev == ["wake"]
    assert [r["note"] for r in env.rows if r["step"] == "stall_unstuck"] == ["woke"]


def test_the_responsiveness_probe_is_bounded():
    class _Hung:
        def wait_for_function(self, js, timeout=None):
            raise TimeoutError(f"Timeout {timeout}ms exceeded.")

    class _Closed:
        def wait_for_function(self, js, timeout=None):
            raise RuntimeError("Target page, context or browser has been closed")

    class _Live:
        def wait_for_function(self, js, timeout=None):
            assert timeout == 5000
            return True

    assert md._page_responsive(_Hung()) is False
    assert md._page_responsive(_Closed()) is None
    assert md._page_responsive(_Live()) is True


def test_the_flow_click_and_fill_retries_are_watched():
    import ast
    src = (ROOT / "src/fb_ads_rpa/meta_dom.py").read_text(encoding="utf-8")
    flow = next(n for n in ast.parse(src).body
                if isinstance(n, ast.FunctionDef) and n.name == "run_recorded_campaign_flow")
    inner = {n.name: ast.get_source_segment(src, n) for n in flow.body if isinstance(n, ast.FunctionDef)}
    # ACL-1573: повторы нажатия и ввода живут в `_click_tries` / `_fill_tries`
    # (снаружи — сторож чужих окон `foreign_windows.guarded`); сторож застоя
    # должен остаться именно в повторах, и `_click` / `_fill` должны их звать.
    for name in ("_click_tries", "_fill_tries"):
        assert "watch = _watch(where)" in inner[name] and "watch.check()" in inner[name], name
    assert "_click_tries(" in inner["_click"] and "_windows.guarded(" in inner["_click"]
    assert "_fill_tries(" in inner["_fill"] and "_windows.guarded(" in inner["_fill"]


# ── повторное ревью PR #314: ключ — шаг; на ad2+ только будим ────────────

def test_the_watch_decides_editor_or_list_by_the_address(env):
    """S3: не отвечающий редактор не перезагружается, даже когда сторож сам
    определяет, где он, — по адресу `.../edit`."""
    env.state["responsive"] = False
    watch = md._StallWatch(env.page, "ad_name")
    env.clock.now += md.STALL_BUDGET_S + 1
    assert watch.check() == "unsaved_or_unknown"
    assert "reload" not in env.ev
    lst = SimpleNamespace(url="https://adsmanager.facebook.com/adsmanager/manage/campaigns?act=1")
    watch = md._StallWatch(lst, "create")
    env.clock.now += md.STALL_BUDGET_S + 1
    assert watch.check() == "reloaded" and "reload" in env.ev


def test_flow_watches_are_keyed_by_step_and_never_reload_an_additional_ad(flow, monkeypatch):
    """Ключ сторожа — имя шага, а не `str(locator)` (у всех одинаковый
    префикс URL кадра); на втором объявлении сторож только будит — иначе
    перезагрузка открыла бы другое объявление, и имя ad2 легло бы в него."""
    made: list[tuple[str, bool]] = []

    class _Rec:
        def __init__(self, page, where, *, budget_s=None, editor=None, reload=True):
            made.append((where, reload))

        fired = ""

        def check(self, *, frozen=False):
            return ""

    monkeypatch.setattr(md, "_StallWatch", _Rec)
    flow["fail_upload"] = set()
    flow["run"]()
    wheres = dict(made)
    assert {"create", "campaign_name", "adset_name", "ad_name", "ad2_name"} <= set(wheres), made
    assert wheres["ad_name"] is True and wheres["campaign_name"] is True
    assert wheres["ad2_name"] is False, "на втором объявлении сторож не должен перезагружать"
    assert all(not w.startswith("<Locator") for w in wheres)
