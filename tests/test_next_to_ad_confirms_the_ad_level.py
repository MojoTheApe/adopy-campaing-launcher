# -*- coding: utf-8 -*-
"""`next_to_ad` проверяет, что редактор открыл уровень объявления (ACL-1693).

07.10, LAPTOP-UA006 (81c1bfbb5456, окно 1536×730, dpr 1,25): `next_to_ad`
записался ok, но редактор остался на «Ad sets - Edit» (`levels: ["ad_set"]`),
и пять попыток `ad_name` (`not_on_ad_level`) и креатив (`wizard_not_opened`)
съели ~4 минуты. Теперь после Next робот читает уровень, при необходимости
закрывает перекрывающее уведомление и нажимает ещё раз, а отказ называет
причину на самом шаге навигации. Браузера нет: страница подменена чтением.
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import meta_dom as md  # noqa: E402

AD_SET = {"levels": ["ad_set"], "dialogs": 0, "wizard": "",
          "next": {"found": True, "visible": True, "in_view": True, "covered": False}}
AD = {"levels": ["ad"], "dialogs": 0, "wizard": "", "next": {"found": False}}


class Script:
    """Страница: список ответов чтения; нажатие Next может «перевести» её."""

    def __init__(self, states, reached_on_press=None, press_result=True):
        self.states = list(states)
        self.reads = 0
        self.presses = 0
        self.cleared = 0
        self.reached_on_press = reached_on_press
        self.press_result = press_result

    def read(self, _page):
        self.reads += 1
        return self.states[min(self.reads - 1, len(self.states) - 1)]

    def press(self):
        self.presses += 1
        if self.reached_on_press is not None and self.presses >= self.reached_on_press:
            self.states = [AD]
            self.reads = 0
        return self.press_result

    def clear(self):
        self.cleared += 1
        return ["WhatsApp Status included"]


def run(script, **kw):
    return md.reach_ad_level(object(), script.press, script.clear, read_state=script.read,
                             sleep=lambda s: None, wait_ticks=3, **kw)


def test_ad_level_right_away_needs_no_second_press():
    s = Script([AD])
    got = run(s)
    assert got["ok"] and got["level"] == "ad" and got["presses"] == 1
    assert s.presses == 0 and s.cleared == 0


def test_slow_transition_is_waited_for_not_repressed():
    s = Script([AD_SET, AD_SET, AD])
    got = run(s)
    assert got["ok"] and s.presses == 0


def test_still_on_ad_set_clears_the_notice_and_presses_again():
    s = Script([AD_SET], reached_on_press=1)
    got = run(s)
    assert got["ok"] and got["presses"] == 2
    assert s.cleared == 1 and got["cleared"] == ["WhatsApp Status included"]


def test_never_reaching_ad_level_names_the_reason():
    s = Script([AD_SET])
    got = run(s)
    assert not got["ok"] and got["presses"] == 3 and s.presses == 2
    assert got["why"] == "next_clicked_no_change" and got["level"] == "ad_set"


def test_reasons_follow_what_the_page_shows():
    covered = {**AD_SET, "next": {**AD_SET["next"], "covered": True}}
    outside = {**AD_SET, "next": {**AD_SET["next"], "in_view": False}}
    dialog = {**AD_SET, "dialogs": 1}
    missing = {**AD_SET, "next": {"found": False}}
    assert run(Script([covered]))["why"] == "next_covered"
    assert run(Script([outside]))["why"] == "next_outside_viewport"
    assert run(Script([dialog]))["why"] == "dialog_open"
    assert run(Script([missing]))["why"] == "next_not_found"


def test_a_press_that_finds_no_button_stops_at_once():
    s = Script([AD_SET], press_result=False)
    got = run(s)
    assert not got["ok"] and got["why"] == "next_not_found" and s.presses == 1


def test_unreadable_page_is_not_pressed_blindly():
    s = Script([{}])
    got = run(s)
    assert got["ok"] and got["level"] == "unknown" and s.presses == 0


def test_read_error_counts_as_unknown():
    def boom(_page):
        raise RuntimeError("gone")

    got = md.reach_ad_level(object(), lambda: True, lambda: [], read_state=boom,
                            sleep=lambda s: None, wait_ticks=2)
    assert got["ok"] and got["level"] == "unknown"


def test_classic_wizard_sidebar_level_counts():
    assert md._ad_level_verdict({"levels": [], "wizard": "ad"}) == "ad"
    assert md._ad_level_verdict({"levels": [], "wizard": "ad_set"}) == "ad_set"


def test_ad_level_appearing_after_a_failed_re_press_is_ok():
    """Ревью F1: Next на уровне объявления нет — провал нажатия не отказ."""
    class Late(Script):
        def press(self):
            self.presses += 1
            self.states = [AD]  # переход закончился, пока искали Next
            self.reads = 0
            return False

    s = Late([AD_SET])
    got = run(s)
    assert got["ok"] and got["level"] == "ad" and got["why"] == ""
    assert s.presses == 1


def test_markers_beat_the_sidebar_guess():
    """Ревью F3: метки ad set и догадка «ad» по дереву — решают метки."""
    assert md._ad_level_verdict({"levels": ["ad_set"], "wizard": "ad"}) == "ad_set"
    assert md._ad_level_verdict({"levels": ["ad", "ad_set"], "wizard": "ad_set"}) == "ad"
    assert md._ad_level_verdict({"levels": ["campaign"], "wizard": "ad"}) == "ad"


def test_sidebar_guess_with_ad_set_markers_is_still_stuck():
    s = Script([{**AD_SET, "wizard": "ad"}])
    got = run(s)
    assert not got["ok"] and got["level"] == "ad_set"


def test_the_flow_checks_the_level():
    src = inspect.getsource(md.run_recorded_campaign_flow)
    assert "reach_ad_level(" in src
    assert 'name="Next", exact=True' in src


from tests.test_destination_and_ad_name_say_why import flow  # noqa: E402,F401


def test_stuck_on_the_ad_set_returns_the_blocking_step(flow, monkeypatch):
    """Ревью F2/F4: подтверждённый «ad set» — блокирующая остановка, не повтор."""
    from src.fb_ads_rpa import failure_policy, retry_policy

    monkeypatch.setattr(md, "reach_ad_level", lambda *a, **k: {
        "ok": False, "why": "next_clicked_no_change", "presses": 3, "cleared": [], "level": "ad_set",
        "state": {"levels": ["ad_set"]}})
    result, _soft, rows, _ = flow()
    assert result == "next_to_ad_stuck_on_adset"
    failed = [r for r in rows if r["step"] == "next_to_ad" and r["ok"] is False]
    assert failed and failed[-1]["note"] == "next_clicked_no_change"
    step = "next_to_ad_stuck_on_adset"
    assert step in retry_policy.BLOCKING_REVIEW_STEPS
    assert step in retry_policy.NON_RETRYABLE_FLOW_STEPS
    assert step not in retry_policy.CAMPAIGN_RETRY_STEPS
    assert retry_policy.should_retry_campaign(step, completed_attempts=0) is False
    assert failure_policy.severity(step) == failure_policy.BLOCKING
    assert "unclassified" not in failure_policy.reason(step)


def test_the_stuck_stop_blocks_the_campaign_with_a_reason_and_no_retry(monkeypatch):
    from tests.test_objective_dialog_waits_while_loading import _publish_with_failures
    from src.fb_ads_rpa import failure_policy

    step = "next_to_ad_stuck_on_adset"
    result, sleeps = _publish_with_failures(monkeypatch, [step])
    assert sleeps == [], "не повторяется"
    assert result.success is False
    assert step in result.blocking_failures
    assert failure_policy.reason(step) in result.error
