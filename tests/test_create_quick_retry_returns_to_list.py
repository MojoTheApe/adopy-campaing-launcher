# -*- coding: utf-8 -*-
"""Промах по Create: быстрый повтор и возврат на список кампаний (ACL-1313, 25.09).

887b2d: нажатие Create открыло Audiences. Прежде робот ждал окно цели 75 с,
перезагружал ЭТУ страницу (на ней кнопки Create нет вовсе) и ждал снова.
Теперь `run_recorded_campaign_flow` за секунды видит, что нажатие не взялось
(`_create_took`), возвращается на адрес списка, если нажатие увело с него, и
жмёт ещё раз; а в долгом круге ожидания окна цели возврат на список идёт до
`recover_stuck_page`.

Поток гоняется целиком на подставной странице (`MagicMock`, как в
test_additional_ad_without_creative_has_no_text.py); `_create_took` и
`_wait_objective_dialog` подменены сценарием.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import browser as br  # noqa: E402
from src.fb_ads_rpa import meta_dom as dom  # noqa: E402
from src.fb_ads_rpa import page_capture, screenshots  # noqa: E402
from src.fb_ads_rpa import run_telemetry as rt  # noqa: E402

_REAL_FIRST_VISIBLE = dom._first_visible

LIST_URL = "https://adsmanager.facebook.com/adsmanager/manage/campaigns?act=1"
AUDIENCES = "https://adsmanager.facebook.com/adsmanager/audiences?act=1"


@pytest.fixture
def flow(monkeypatch, tmp_path):
    monkeypatch.setattr(rt, "TELEMETRY_PATH", tmp_path / "telemetry.jsonl")
    events: list[tuple] = []
    page = MagicMock()
    loc = MagicMock()
    loc.first = loc
    loc.bounding_box.return_value = None
    loc.input_value.return_value = "Example"
    loc.evaluate.return_value = None
    for name in ("get_by_text", "get_by_placeholder", "locator"):
        getattr(page, name).return_value = loc
        getattr(loc, name).return_value = loc
    loc.get_by_role.return_value = loc

    radio_absent = {"on": False, "loc": None}

    def by_role(role, name=None, **kw):
        if name == "Create":
            events.append(("create_button",))
        if role == "radio" and name == "Leads" and radio_absent["on"]:
            return radio_absent["loc"]
        return loc

    page.get_by_role.side_effect = by_role
    page.evaluate.return_value = None
    page.url = LIST_URL

    def goto(url, **kw):
        events.append(("goto", url))
        page.url = url

    page.goto.side_effect = goto

    monkeypatch.setattr(br, "ensure_editor_window", lambda p, section: True)
    monkeypatch.setattr(dom, "_clear_blocking_overlays", MagicMock(return_value=[]))
    for name in ("_recover_from_browser_error", "_dismiss_floating_banners", "_sleep_h",
                 "wait_editor_saved", "_ensure_campaign_budget", "scroll_to_section",
                 "click_conversion_location_dropdown", "pick_website_conversion_option",
                 "is_website_conversion_selected", "select_conversion_event_lead",
                 "fill_audience_age", "fill_placements", "upload_ad_creative_with_retry",
                 "write_ad_copy", "set_ad_destination_website"):
        monkeypatch.setattr(dom, name, MagicMock(return_value=True))
    monkeypatch.setattr(dom, "_roots", lambda p: [p])
    monkeypatch.setattr(dom, "_first_visible", lambda *a, **kw: None)
    monkeypatch.setattr(dom, "_loading_visible", lambda p: False)
    monkeypatch.setattr(dom, "verify_and_fix_ad_destinations", lambda p, names, url: [])
    monkeypatch.setattr(rt, "window_state", lambda p: {})
    monkeypatch.setattr(rt, "page_context", lambda p: {})
    monkeypatch.setattr(screenshots, "capture", lambda *a, **kw: None)
    monkeypatch.setattr(page_capture, "capture", lambda *a, **kw: None)

    def recover(p, step, **kw):
        events.append(("recover", step))
        return step == "objective"

    monkeypatch.setattr(dom, "recover_stuck_page", recover)
    # ACL-1320: третий раунд `_open_campaign` жмёт Create в свежей вкладке.
    fresh: dict = {"page": page}
    monkeypatch.setattr(br, "fresh_tab",
                        lambda p, url, **kw: events.append(("fresh_tab", url)) or fresh["page"])
    took = ["ok"]
    waits = [True]

    def create_took(p, list_url, timeout_s=8.0):
        events.append(("create_took", list_url))
        result = took.pop(0) if len(took) > 1 else took[0]
        if result == "left_list":
            page.url = AUDIENCES   # нажатие открыло Audiences
        return result

    def wait_dialog(p, **kw):
        events.append(("wait_dialog",))
        ok = waits.pop(0) if len(waits) > 1 else waits[0]
        if not ok:
            page.url = AUDIENCES   # за время ожидания страница ушла со списка
        return ok

    monkeypatch.setattr(dom, "_create_took", create_took)
    monkeypatch.setattr(dom, "_wait_objective_dialog", wait_dialog)

    def run():
        return dom.run_recorded_campaign_flow(
            page, campaign_name="Example", budget="10", adset_name="Set",
            creative_path="a.png", ad_title="t", ad_body="b",
            destination_url="https://example.com")

    return {"run": run, "events": events, "took": took, "waits": waits, "page": page,
            "radio_absent": radio_absent, "fresh": fresh}


def _after(events, first, then):
    """`then` встречается после первого `first`."""
    i = events.index(first)
    return then in events[i + 1:]


def test_a_press_that_left_the_list_goes_back_and_presses_again(flow):
    flow["took"][:] = ["left_list", "ok"]
    flow["run"]()
    ev = flow["events"]
    assert ("goto", LIST_URL) in ev, "со страницы Audiences на список не вернулись"
    assert _after(ev, ("goto", LIST_URL), ("create_button",)), "после возврата Create не нажали"
    assert [e for e in ev if e[0] == "create_took"] == [("create_took", LIST_URL)] * 2
    assert ("recover", "objective") not in ev, "быстрый повтор должен обойтись без перезагрузки"


def test_a_press_that_did_nothing_is_repeated_on_the_spot(flow):
    flow["took"][:] = ["nothing", "ok"]
    flow["run"]()
    ev = flow["events"]
    assert not any(e[0] == "goto" for e in ev)
    assert _after(ev, ("create_took", LIST_URL), ("create_button",))
    assert len([e for e in ev if e[0] == "create_took"]) == 2


def test_a_press_that_took_is_not_repeated(flow):
    flow["run"]()
    ev = flow["events"]
    assert len([e for e in ev if e[0] == "create_took"]) == 1
    i = ev.index(("create_took", LIST_URL))
    assert ("create_button",) not in ev[i + 1: ev.index(("wait_dialog",))]


def test_the_quick_rounds_are_bounded(flow):
    flow["took"][:] = ["nothing"]
    flow["run"]()
    assert len([e for e in flow["events"] if e[0] == "create_took"]) == 2


def test_the_long_wait_returns_to_the_list_before_reloading(flow):
    """Окно цели не пришло, страница ушла со списка: список заново и новый
    Create — без перезагрузки поверх чужой страницы (ACL-1320: без неё вовсе)."""
    flow["waits"][:] = [False, True]
    flow["run"]()
    ev = flow["events"]
    first_wait = ev.index(("wait_dialog",))
    assert ("goto", LIST_URL) in ev[first_wait:]
    assert _after(ev[first_wait:], ("goto", LIST_URL), ("create_button",))
    assert not [e for e in ev if e[0] == "recover" and e[1] in ("objective", "create")]


def test_an_absent_objective_dialog_fails_after_one_bounded_wait(flow, monkeypatch):
    """ACL-1313: после всех повторов окна цели нет — отказ за ~10 с, а не за
    пять заходов `_click` по 8 с (живьём ~83 с вместе с накладкой)."""
    from types import SimpleNamespace

    clock = {"now": 1000.0}
    monkeypatch.setattr(dom, "time", SimpleNamespace(
        time=lambda: clock["now"], sleep=lambda s: clock.__setitem__("now", clock["now"] + s)))
    monkeypatch.setattr(dom, "_first_visible", _REAL_FIRST_VISIBLE)

    class _AbsentRadio:
        first = property(lambda self: self)

        def count(self):
            return 0

        def is_visible(self, timeout=0):
            return False

        def wait_for(self, state="visible", timeout=30000):
            clock["now"] += timeout / 1000
            raise TimeoutError("absent")

    flow["radio_absent"].update(on=True, loc=_AbsentRadio())
    flow["waits"][:] = [True]   # окно «пришло», но радио уже нет
    start = clock["now"]
    assert flow["run"]() == "objective_leads"
    assert clock["now"] - start <= dom.OBJECTIVE_RADIO_LAST_WAIT_S + 1


def test_the_last_create_of_the_retry_loop_gets_its_own_wait(flow):
    """6eee2acfb2ae (25.09): после второго Create окно цели пришло через ~13 с,
    а поток ждал только 10 с `OBJECTIVE_RADIO_LAST_WAIT_S`. Теперь за
    последним нажатием идёт ещё одно полное ожидание окна."""
    flow["waits"][:] = [False, False, True]
    flow["run"]()
    ev = flow["events"]
    waits = [i for i, e in enumerate(ev) if e == ("wait_dialog",)]
    assert len(waits) == 3, ev
    creates = [i for i, e in enumerate(ev) if e == ("create_button",)]
    assert waits[-1] > creates[-1], "за последним Create окно цели не ждали"


def test_no_extra_wait_when_the_dialog_came(flow):
    flow["waits"][:] = [True]
    flow["run"]()
    assert flow["events"].count(("wait_dialog",)) == 1
