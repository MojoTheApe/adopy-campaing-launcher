# -*- coding: utf-8 -*-
"""ACL-2091, первый шаг: чтение не закрывает профиль с проблемой аккаунта.

До этого чтение (ACL-1580) закрывало поднятый им профиль при любом исходе —
и разлогиненный тоже: строка просила «log in inside Multilogin», а окна уже
не было. Теперь при logged out, checkpoint, зависшей вкладке и блоке 368
профиль, который чтение само подняло, остаётся открытым, а его место в
очереди держится как окно «на проверку» (`review`). Прочие отказы закрывают
профиль, как раньше; уже открытый профиль чтение не трогает, как раньше.

Multilogin и Meta подменены стендами ACL-1580 (RPA) и ACL-1760 (API).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.analysis_agent import account_spend as sp  # noqa: E402
from src.analysis_agent import account_spend_api as api  # noqa: E402
from src.analysis_agent import country_split as cs  # noqa: E402
from src.fb_api import graph as g  # noqa: E402
from src.fb_api import token_capture as tc  # noqa: E402
from src.web import profile_slots as ps  # noqa: E402

from tests.test_acl1760_api_read import ACCOUNT as API_ACCOUNT, D, World, err  # noqa: E402
from tests.test_reads_open_a_closed_profile import (  # noqa: E402,F401
    ACCOUNT,
    DAY,
    LOGIN,
    PROFILE,
    machine,
    page_reads,
    reads,
    world,
)


# ── какие исходы держат окно ───────────────────────────────────────────────

@pytest.mark.parametrize("reason, kind", [
    (g.LoggedOut("x"), "logged_out"),
    (g.Checkpoint("x"), "checkpoint"),
    (g.PageUnresponsive("x"), "page_unresponsive"),
    (g.PolicyBlocked("x", code=368), "policy_block"),
    (sp.LOGGED_OUT_CODE, "logged_out"),
    (api.ApiReadError("x", code=sp.LOGGED_OUT_CODE), "logged_out"),
])
def test_account_problems_keep_the_window(reason, kind):
    assert sp.keep_open_problem(reason) == kind


@pytest.mark.parametrize("reason", [
    None, "", "restart_cooldown", "call_budget", g.RateLimited("x"), g.PageFailed("x"),
    g.GraphError("x", code=100) if hasattr(g, "GraphError") else RuntimeError("x"),
    RuntimeError("navigation exploded"),
])
def test_other_outcomes_do_not(reason):
    assert sp.keep_open_problem(reason) == ""


# ── RPA-чтение ─────────────────────────────────────────────────────────────

def _names(world):
    return [e[0] for e in world.ev if isinstance(e, tuple)]


def _kept(queue):
    return [r for r in queue.snapshot()["running"] if r["kept"]]


def test_a_logged_out_profile_the_read_started_stays_open(reads):
    reads.page_kw = {"url": LOGIN}
    out = sp.read_via_rpa(ACCOUNT, DAY, DAY)
    assert out.error_code == sp.LOGGED_OUT_CODE
    assert _names(reads).count("start") == 1
    assert "stop" not in _names(reads), "the logged-out window was closed"
    assert "kill" not in reads.ev
    assert reads.automation.get(PROFILE), "the profile must stay open"
    kept = _kept(reads.queue)
    assert len(kept) == 1 and kept[0]["kept_kind"] == ps.KEPT_REVIEW
    rows = [r for r in reads.rows if r[0] == "spend_profile_kept_for_problem"]
    assert len(rows) == 1 and rows[0][2]["diag"]["problem"] == "logged_out"
    assert not [r for r in reads.rows if r[0] == "spend_profile_closed_after_read"]


def test_the_country_read_keeps_a_logged_out_profile_open(reads):
    reads.page_kw = {"url": LOGIN}
    out = cs.read_country_split(ACCOUNT, DAY)
    assert out.error_code == sp.LOGGED_OUT_CODE
    assert "stop" not in _names(reads)
    assert len(_kept(reads.queue)) == 1


def test_an_ordinary_failure_still_closes_the_profile(reads):
    reads.page_kw = {"fail": RuntimeError("report exploded")}
    cs.read_country_split(ACCOUNT, DAY)
    assert _names(reads).count("stop") == 1
    assert reads.queue.snapshot()["running"] == []


# ── API-чтение внутри профиля ──────────────────────────────────────────────

@pytest.fixture
def api_world(monkeypatch, tmp_path):
    w = World(monkeypatch, tmp_path)
    w.local = ("none", None)
    w.started_now = True                       # профиль поднимает само чтение
    kinds: list[tuple] = []
    w.queue.keep = lambda job_id, label="", kind="review": (
        w.queue.calls.append(("keep", job_id, label)), kinds.append(kind))
    w.kept_kinds = kinds
    return w


def _calls(w):
    return [c[0] for c in w.queue.calls]


def _assert_kept(w):
    assert w.soft_stops == [], "the profile with the problem was closed"
    assert _calls(w) == ["acquire", "keep"]
    assert w.kept_kinds == [ps.KEPT_REVIEW]
    assert w.timers == [], "no grace timer: the window is not closed later either"


def test_api_logged_out_page_keeps_the_profile_open(api_world):
    api_world.page.state_value = {"kind": "login", "has_token": False}
    r = api.read_via_api(API_ACCOUNT, D, D)
    assert r.error_code == sp.LOGGED_OUT_CODE
    _assert_kept(api_world)


def test_api_checkpoint_page_keeps_the_profile_open(api_world):
    api_world.page.state_value = {"kind": "checkpoint", "has_token": False}
    r = api.read_via_api(API_ACCOUNT, D, D)
    assert r.state == "failed" and "checkpoint" in r.error.lower()
    _assert_kept(api_world)


def test_api_policy_block_keeps_the_profile_open(api_world):
    api_world.page.routes["/ACT||"] = err(368)
    r = api.read_via_api(API_ACCOUNT, D, D)
    assert r.state == "failed"
    _assert_kept(api_world)


def test_api_frozen_tab_keeps_the_profile_open_and_drops_the_tab(api_world, monkeypatch):
    class Frozen(tc.InPageGraph):
        def wait_ready(self, *a, **kw):
            raise g.PageUnresponsive("the Ads Manager tab stopped answering")

    monkeypatch.setattr(api, "_make_graph", lambda tab: Frozen(tab, sleep=lambda s: None))
    r = api.read_via_api(API_ACCOUNT, D, D)
    assert r.state == "failed"
    _assert_kept(api_world)
    assert api_world.cdp.closed_targets == ["T-mine"], "the frozen tab itself is dropped"


def test_api_ordinary_failure_still_closes_the_profile(api_world):
    api_world.page.routes["/ACT||"] = err(100)
    api.read_via_api(API_ACCOUNT, D, D)
    assert api_world.soft_stops == [API_ACCOUNT]
    assert "keep" not in _calls(api_world)


def test_api_problem_on_an_already_open_profile_changes_nothing(api_world):
    api_world.local = ("port", 9222)
    api_world.started_now = False
    api_world.page.state_value = {"kind": "login", "has_token": False}
    api.read_via_api(API_ACCOUNT, D, D)
    assert api_world.soft_stops == [] and "keep" not in _calls(api_world)
