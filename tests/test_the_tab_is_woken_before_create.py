# -*- coding: utf-8 -*-
"""Вкладку будят до первого Create — и окно не всплывает (ACL-1320).

Первое нажатие Create шло во вкладку, которая жила в фоне с прошлого
прогона: Chrome мог держать её придержанной или замороженной. Теперь
`_open_campaign` до первого нажатия зовёт `wake_page_quietly`: свёрнутое
окно — в обычное состояние за краем экрана, троттлинг снят заново, но
`bring_to_front` нет — окно профиля остаётся за краем экрана (решение 19.09).
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import browser as br  # noqa: E402
from tests.test_a_campaign_opens_within_two_minutes import env, _P  # noqa: E402,F401


def test_the_first_create_goes_to_a_woken_tab(env):
    env.sc.dialog_at = ("old", env.start)
    assert env.run() == ""
    kinds = [e[0] for e in env.ev]
    assert kinds.index("wake") < kinds.index("press")
    assert kinds.count("wake") == 1, "будим один раз, до первого нажатия"


def test_a_frozen_tab_is_not_woken_through_a_cdp_session(env):
    env.sc.frozen = {"old"}
    env.run()
    assert ("wake", "old") not in env.ev


def _page(state: str, ev: list):
    class _Session:
        def send(self, method, params=None):
            ev.append(method)
            return {"bounds": {"windowState": state}}

        def detach(self):
            pass

    ctx = SimpleNamespace(new_cdp_session=lambda p: _Session())

    class _Page:
        context = ctx

        def bring_to_front(self):
            ev.append("bring_to_front")

    return _Page()


def test_a_minimised_window_is_restored_off_screen_without_coming_forward(monkeypatch):
    ev: list = []
    monkeypatch.setattr(br, "_restore_offscreen", lambda ctx, p: ev.append("offscreen") or True)
    monkeypatch.setattr(br, "_disable_background_throttling", lambda ctx, p: ev.append("unthrottle"))
    out = br.wake_page_quietly(_page("minimized", ev))
    assert out == {"minimized": True, "restored": True}
    assert "offscreen" in ev and "unthrottle" in ev
    assert "bring_to_front" not in ev


def test_a_normal_window_stays_where_it_is(monkeypatch):
    ev: list = []
    monkeypatch.setattr(br, "_restore_offscreen", lambda ctx, p: ev.append("offscreen") or True)
    monkeypatch.setattr(br, "_disable_background_throttling", lambda ctx, p: ev.append("unthrottle"))
    br.wake_page_quietly(_page("normal", ev))
    assert "offscreen" not in ev and "bring_to_front" not in ev
    assert ev[-1] == "unthrottle"
