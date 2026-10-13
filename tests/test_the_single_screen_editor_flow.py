# -*- coding: utf-8 -*-
"""Записанный поток в новом редакторе Meta «в один экран» (ACL-1327).

Прежде (ACL-1320) редактор `/adsmanager/manage/simple_create` был остановкой
аккаунта `simple_create_layout`, а 9e7d67471c6b (27.09) его не узнал вовсе и
46 с искал Next, которого там нет (`next_to_adset`). Теперь поток идёт по
разделам страницы: до группы и объявления — прокруткой, без Next; свёрнутые
карточки (бюджет, Audience, Placements) раскрываются перед прежними
обработчиками; цель Leads проверяется на месте; карточка условий Lead Ads,
оставшаяся после Website, — остановка аккаунта; второе и третье объявления
создаются «Create ad» из меню группы и заполняются как первое (ACL-1353).
Прежний мастер не меняется.

Поток гоняется целиком на подставной странице (`flow` из
test_create_quick_retry_returns_to_list.py); функции модуля
`simple_create` подменены и считают вызовы.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import failure_policy, profile_trouble, retry_policy  # noqa: E402
from src.fb_ads_rpa import meta_dom as dom  # noqa: E402
from src.fb_ads_rpa import run_telemetry as rt  # noqa: E402
from src.fb_ads_rpa import simple_create as sc  # noqa: E402
from tests.test_create_quick_retry_returns_to_list import flow  # noqa: E402,F401

#: Настоящий шаг текста — до того, как `flow` подменит его заглушкой.
_REAL_WRITE_AD_COPY = dom.write_ad_copy

SIMPLE_URL = ("https://adsmanager.facebook.com/adsmanager/manage/simple_create?act=1"
              "&selected_campaign_ids=2&selected_adset_ids=3&selected_ad_ids=4&sc_1nn=true")
EDIT_URL = "https://adsmanager.facebook.com/adsmanager/manage/campaigns/edit/standalone?act=1"


@pytest.fixture
def sim(flow, monkeypatch):
    """Подставной поток + счётчики нового редактора."""
    calls: list[tuple] = []
    rows: list[tuple] = []
    page = flow["page"]
    monkeypatch.setattr(dom, "_open_campaign", lambda *a, **k: "")
    monkeypatch.setattr(rt, "record", lambda step, ok=None, **kw: rows.append((step, ok, kw.get("note", ""))))
    state = {"simple": True, "level_ok": True, "objective": "leads", "terms": "", "detect_from": 0,
             "checks": 0}

    def detect(p):
        state["checks"] += 1
        return state["simple"] and state["checks"] > state["detect_from"]

    monkeypatch.setattr(sc, "is_simple_create", detect)
    monkeypatch.setattr(sc, "scroll_to_level",
                        lambda p, level, **kw: calls.append(("level", level))
                        or ({"ok": True} if state["level_ok"] else {"ok": False, "why": "no_section"}))
    monkeypatch.setattr(sc, "open_card", lambda p, title: calls.append(("card", title)) or "opened")
    monkeypatch.setattr(sc, "ensure_leads_objective",
                        lambda p: calls.append(("objective",)) or state["objective"])
    monkeypatch.setattr(sc, "lead_terms_block", lambda p, **kw: calls.append(("terms",)) or state["terms"])
    monkeypatch.setattr(dom, "_close_onboarding_tour", lambda p, where="": "")

    real_by_role = page.get_by_role.side_effect

    def by_role(role, name=None, **kw):
        if role == "button" and name == "Next":
            calls.append(("next",))
        return real_by_role(role, name=name, **kw)

    page.get_by_role.side_effect = by_role
    page.url = SIMPLE_URL
    created: list = []
    monkeypatch.setattr(dom, "create_additional_ad", lambda p, name: created.append(name) or True)

    def run(**kw):
        args = dict(campaign_name="Example", budget="10", adset_name="Set",
                    creative_path="a.png", ad_title="t", ad_body="b",
                    destination_url="https://example.com")
        args.update(kw)
        return dom.run_recorded_campaign_flow(page, **args)

    return {"run": run, "calls": calls, "rows": rows, "state": state, "page": page,
            "created": created, "monkeypatch": monkeypatch}


def _steps(rows):
    return [(r[0], r[1]) for r in rows]


def test_the_new_editor_is_walked_by_sections_without_next(sim):
    assert sim["run"]() == ""
    calls = sim["calls"]
    assert ("next",) not in calls, "в новом редакторе жали Next"
    assert [c for c in calls if c[0] == "level"] == [("level", "adset"), ("level", "ad")]
    cards = [c[1] for c in calls if c[0] == "card"]
    assert cards[:1] == ["Budget"] and "Audience" in cards and "Placements" in cards
    assert calls.count(("objective",)) == 1
    assert ("terms",) in calls
    steps = _steps(sim["rows"])
    assert ("simple_create_layout", False) not in steps
    assert ("simple_create_draft", True) in steps or any(s == "simple_create_draft" for s, _ in steps)


def test_the_order_follows_the_page(sim):
    sim["run"]()
    calls = sim["calls"]
    order = [calls.index(("card", "Budget")), calls.index(("level", "adset")), calls.index(("terms",)),
             calls.index(("card", "Audience")), calls.index(("card", "Placements")), calls.index(("level", "ad"))]
    assert order == sorted(order)


def test_the_switch_off_restores_the_old_stop(sim, monkeypatch):
    monkeypatch.setenv("FB_RPA_SIMPLE_CREATE", "0")
    assert sim["run"]() == "simple_create_layout"
    assert not [c for c in sim["calls"] if c[0] in ("level", "card")]


def test_a_late_recognised_editor_takes_the_new_path_before_next(sim):
    """9e7d67471c6b: после Continue адрес был прежним — узнали только перед Next."""
    sim["page"].url = EDIT_URL
    sim["state"]["detect_from"] = 1          # первая проверка (после Continue) — «нет»
    assert sim["run"]() == ""
    assert ("next",) not in sim["calls"]
    assert ("level", "adset") in sim["calls"]
    assert ("simple_create", None, "next_to_adset") in sim["rows"]


def test_the_lead_ads_terms_stop_the_account(sim):
    sim["state"]["terms"] = "Please read and accept Meta's Lead Ads Terms before creating your ad."
    got = sim["run"]()
    assert got == "lead_terms_not_accepted"
    assert got in profile_trouble.ACCOUNT_STOPS
    assert ("card", "Audience") not in sim["calls"], "после остановки продолжили группу"
    assert ("lead_terms_not_accepted", False) in _steps(sim["rows"])
    assert got in retry_policy.NON_RETRYABLE_FLOW_STEPS
    assert retry_policy.disposition(got).action == retry_policy.BLOCK_AND_REVIEW
    assert failure_policy.severity(got) == failure_policy.BLOCKING
    assert "Lead Ads Terms" in failure_policy.reason(got)


def test_an_objective_that_cannot_be_set_stops_without_a_second_draft(sim):
    """Ревью PR #340 (F4): к проверке цели черновик уже есть — повтор
    кампании бросил бы его и сделал новый. Отдельная неповторяемая остановка."""
    sim["state"]["objective"] = "failed"
    got = sim["run"]()
    assert got == "objective_inline"
    assert got in retry_policy.NON_RETRYABLE_FLOW_STEPS
    assert retry_policy.should_retry_campaign(got, completed_attempts=0) is False
    assert failure_policy.severity(got) == failure_policy.BLOCKING
    assert "not retried" in failure_policy.reason(got)
    assert got not in profile_trouble.ACCOUNT_STOPS
    assert ("level", "adset") not in sim["calls"]
    # Цель проверяется сразу после имени — до бюджета, а не перед группой.
    assert ("card", "Budget") not in sim["calls"]


def test_a_missing_section_is_the_layout_stop(sim):
    sim["state"]["level_ok"] = False
    assert sim["run"]() == "simple_create_layout"
    assert ("simple_create_layout", False) in _steps(sim["rows"])
    assert "Ad set / Ad sections" in failure_policy.reason("simple_create_layout")


def _creates(sim, results: list[dict] | None = None, listed: bool = True) -> list:
    """Подменить создание объявления N (ACL-1353); вызовы — в список."""
    seen: list = []
    queue = list(results or [])

    def fake(page, *, tag, known_names, **kw):
        seen.append((tag, list(known_names)))
        sim["calls"].append(("create_next_ad", tag))
        got = queue.pop(0) if queue else {}
        return {"ok": True, "why": "", "text": "", "pressed": ["adset_action_menu", "menu_create_ad"],
                "ads_before": len(seen), "ads_after": len(seen) + 1, "extra_shells": 0,
                "dialog": None, "capture": "", **got}

    sim["monkeypatch"].setattr(sc, "create_next_ad", fake)
    sim["monkeypatch"].setattr(sc, "ad_listed", lambda p, name, **kw: {"listed": listed, "ads": 2, "error": None})
    return seen


_THREE = [{"ad_name": "Two", "creative_path": "b.png", "ad_title": "T2", "ad_body": "B2"},
          {"ad_name": "Three", "creative_path": "c.png", "ad_title": "T3", "ad_body": "B3",
           "ad_body2": "B3 second"}]


def test_more_ads_are_created_and_filled_like_the_first(sim, monkeypatch):
    """ACL-1353: объявления 2 и 3 создаются в новом редакторе и заполняются
    теми же шагами, что объявление 1: имя, креатив, назначение, тексты."""
    seen = _creates(sim)
    uploads: list = []
    monkeypatch.setattr(dom, "upload_ad_creative_with_retry",
                        lambda p, ctype, path, **kw: uploads.append((path, kw)) or True)
    copies: list = []

    def copy(p, title, body, **kw):
        copies.append((title, body, kw.get("ad_name"), kw.get("body2")))
        if kw.get("body2"):
            kw["report"]["second_text"] = {"ok": True}
        return True

    monkeypatch.setattr(dom, "write_ad_copy", copy)
    sets: list = []
    monkeypatch.setattr(dom, "set_ad_destination_website", lambda p, url: sets.append(url) or True)
    monkeypatch.setattr(dom, "_browser_addon_none_checked", lambda p: True)   # перепроверка не нужна
    monkeypatch.setattr(dom, "_review_errors_visible", lambda p: False)
    out: list[str] = []
    assert sim["run"](additional_ads=_THREE, soft_fails_out=out) == ""
    assert [s[0] for s in seen] == ["ad2", "ad3"]
    # Новое объявление узнаётся по имени, которого нет среди собранных.
    assert seen[0][1] == ["Example Ad"] and seen[1][1] == ["Example Ad", "Two"]
    typed = [c.args[0] for c in sim["page"].get_by_placeholder.return_value.fill.call_args_list]
    assert "Two" in typed and "Three" in typed
    assert [u[0] for u in uploads] == ["a.png", "b.png", "c.png"]
    # Объявления 2+ — без возврата по адресу и без перезагрузки: адрес держит объявление 1.
    assert uploads[1][1] == {"edit_url": "", "reload": False} == uploads[2][1]
    assert [c[:3] for c in copies] == [("t", "b", "Example Ad"), ("T2", "B2", "Two"), ("T3", "B3", "Three")]
    assert copies[2][3] == "B3 second"
    assert len(sets) == 3
    assert not [f for f in out if f.split(":", 1)[0].startswith(("ad2", "ad3", "simple_create_more_ads"))], out
    assert sim["created"] == [], "в новом редакторе жали создание прежнего мастера"
    assert sim["page"].reload.call_count == 0


def test_the_new_ad_media_block_is_shown_before_its_destination(sim, monkeypatch):
    """ACL-1412 (d1755a59e139, ad2): после мастера медиа раздел объявления
    бывал размонтирован — `destination_section_missing`. Перед назначением
    объявлений 2+ блок Media (над Destination) возвращается на экран — только
    прокруткой, внутри `hold_open_ad`; у объявления 1 — как прежде."""
    _creates(sim)
    order: list = []
    monkeypatch.setattr(sc, "show_media_block",
                        lambda p, **kw: order.append(("show", getattr(dom._reload_block, "why", "")))
                        or {"mounted": True, "state": "filled", "via": "mounted"})
    monkeypatch.setattr(dom, "upload_ad_creative_with_retry", lambda p, ctype, path, **kw: True)
    monkeypatch.setattr(dom, "set_ad_destination_website",
                        lambda p, url: order.append(("destination",)) or True)
    monkeypatch.setattr(dom, "_browser_addon_none_checked", lambda p: True)
    monkeypatch.setattr(dom, "_review_errors_visible", lambda p: False)
    out: list[str] = []
    assert sim["run"](additional_ads=_THREE, soft_fails_out=out) == ""
    assert order == [("destination",),
                     ("show", "simple_create_new_ad"), ("destination",),
                     ("show", "simple_create_new_ad"), ("destination",)], order
    assert sim["page"].reload.call_count == 0 and sim["page"].goto.call_count == 0


def test_no_page_recovery_reloads_the_editor_around_the_new_ads(sim, monkeypatch):
    """Ревью PR #365, B1: ни восстановление зависшей страницы, ни закрытие окон
    не перезагружают новый редактор после «Create ad»."""
    _creates(sim)
    recovered: list = []
    monkeypatch.setattr(dom, "recover_stuck_page", lambda p, where, **kw: recovered.append(where) or True)
    overlays: list = []
    monkeypatch.setattr(dom, "_clear_blocking_overlays",
                        lambda p, where, **kw: overlays.append((where, kw)) or [])
    sim["run"](additional_ads=_THREE, soft_fails_out=[])
    assert not [w for w in recovered if w.startswith(("ad2", "ad3")) or w == "destination_sweep"], recovered
    assert ("loading_modal_reload", False, "skipped:simple_create_more_ads") in sim["rows"]
    assert ("ad2", {"may_reload": False}) in overlays and ("ad3", {"may_reload": False}) in overlays
    assert sim["page"].reload.call_count == 0 and sim["page"].goto.call_count == 0


def test_no_step_of_ads_2_and_3_may_reload_the_editor(sim, monkeypatch):
    """ACL-1353: пока заполняются объявления 2+ нового редактора, перезагрузки
    запрещены всем шагам (текст, креатив, восстановление) — адрес держит
    объявление 1. Для объявления 1 и после — как прежде."""
    _creates(sim)
    during: list = []

    def copy(p, title, body, **kw):
        during.append((kw.get("ad_name"), getattr(dom._reload_block, "why", "")))
        return True

    monkeypatch.setattr(dom, "write_ad_copy", copy)
    sim["run"](additional_ads=_THREE, soft_fails_out=[])
    assert during == [("Example Ad", ""), ("Two", "simple_create_new_ad"),
                      ("Three", "simple_create_new_ad")]
    assert getattr(dom._reload_block, "why", "") == ""


def test_the_reload_guard_refuses_every_reload_path(monkeypatch):
    from unittest.mock import MagicMock

    rows: list = []
    monkeypatch.setattr(rt, "record", lambda step, ok=None, **kw: rows.append((step, ok, kw.get("note", ""))))
    monkeypatch.setattr(dom, "_chrome_error_page", lambda p: "ERR_SSL_PROTOCOL_ERROR")
    page = MagicMock()
    with dom._reloads_blocked("simple_create_new_ad"):
        assert dom.recover_stuck_page(page, "ad2_before", editor=True) is False
        dom._reload_editor(page, "https://adsmanager.facebook.com/x")
    assert page.reload.call_count == 0 and page.goto.call_count == 0
    assert ("loading_modal_reload", False, "refused:simple_create_new_ad") in rows
    # Вне охраны — прежнее поведение: страница ошибки Chrome перезагружается.
    monkeypatch.setattr(dom, "_sleep_h", lambda *a, **k: None)
    monkeypatch.setattr("src.fb_ads_rpa.browser.wake_page", lambda p: {}, raising=False)
    dom.recover_stuck_page(page, "x", editor=True)
    assert page.reload.call_count == 1


def test_the_classic_wizard_fills_more_ads_without_the_guard(sim, monkeypatch):
    sim["state"]["simple"] = False
    sim["page"].url = EDIT_URL
    during: list = []
    monkeypatch.setattr(dom, "write_ad_copy",
                        lambda p, t, b, **kw: during.append(getattr(dom._reload_block, "why", "")) or True)
    sim["run"](additional_ads=[{"ad_name": "Two", "ad_title": "T2", "ad_body": "B2"}])
    assert during == ["", ""]


def test_a_lead_form_error_on_ad_2_is_repaired_without_opening_ads_in_the_tree(sim, monkeypatch):
    """Ревью PR #365, F2: ошибка Lead-формы на объявлении 2 — лечение ставит
    «None» и назначение на открытом объявлении; обхода по дереву и повторного
    открытия нет. Объявление 1 лечится как прежде (обход по дереву)."""
    _creates(sim)
    state = {"ad": "", "repaired": set()}

    def copy(p, title, body, **kw):
        state["ad"] = kw.get("ad_name")
        return _REAL_WRITE_AD_COPY(p, title, body, **kw)

    monkeypatch.setattr(dom, "write_ad_copy", copy)
    monkeypatch.setattr(dom, "_review_errors_visible",
                        lambda p: state["ad"] in ("Example Ad", "Two") and state["ad"] not in state["repaired"])
    monkeypatch.setattr(dom, "_heal_failed_component", lambda *a, **k: False)
    monkeypatch.setattr(dom, "_lead_terms_not_accepted", lambda p: False)
    monkeypatch.setattr(dom, "_select_browser_addon_none", lambda p: True)
    monkeypatch.setattr(dom, "_browser_addon_none_checked", lambda p: True)
    fills: list = []
    monkeypatch.setattr(dom, "fill_ad_copy", lambda p, t, b, report=None, **kw: fills.append(state["ad"]) or True)
    swept: list = []

    def sweep(p, names, url):
        swept.append((state["ad"], list(names)))
        state["repaired"].add(state["ad"])
        return []

    monkeypatch.setattr(dom, "verify_and_fix_ad_destinations", sweep)
    sets: list = []

    def dest(p, url):
        sets.append(state["ad"])
        state["repaired"].add(state["ad"])
        return True

    monkeypatch.setattr(dom, "set_ad_destination_website", dest)
    tree_opens: list = []
    monkeypatch.setattr(dom, "_reopen_ad_after_reload", lambda p, name: tree_opens.append(name) or True)
    out: list[str] = []
    sim["run"](additional_ads=_THREE[:1], soft_fails_out=out)
    assert fills == ["Example Ad", "Two"]
    assert swept == [("Example Ad", ["Example Ad"])], "объявление 2 чинили обходом по дереву"
    assert "Two" in sets, "назначение объявления 2 не поставили на месте"
    assert tree_opens == []
    assert ("browser_addon_repair", True, "open_ad_only") in sim["rows"]
    assert not [f for f in out if f.startswith("ad2_")], out


def test_a_failed_create_is_named_and_the_rest_are_not_pressed(sim, monkeypatch):
    seen = _creates(sim, [{"ok": False, "why": "no_new_ad_row",
                           "text": "Create ad did not add an ad to the ad set within 30 s",
                           "capture": "run1/05_simple_create_more_ads"}])
    monkeypatch.setattr(dom, "_browser_addon_none_checked", lambda p: False)   # обход был бы нужен
    sets: list = []
    monkeypatch.setattr(dom, "set_ad_destination_website", lambda p, url: sets.append(url) or True)
    out: list[str] = []
    sim["run"](additional_ads=_THREE, soft_fails_out=out)
    assert [s[0] for s in seen] == ["ad2"], "после отказа жали Create ad ещё раз"
    ad2 = [f for f in out if f.startswith("ad2_create:")]
    assert ad2 == ["ad2_create:no_new_ad_row - Create ad did not add an ad to the ad set within 30 s "
                   "(page capture: output/rpa_pages/run1/05_simple_create_more_ads)"]
    ad3 = [f for f in out if f.startswith("ad3_create:")]
    assert len(ad3) == 1 and ad3[0].startswith("ad3_create:not_tried - ")
    for f in ad2 + ad3:
        assert failure_policy.severity(f) == failure_policy.BLOCKING
        assert "ad shell was not created" in failure_policy.reason(f)
        assert retry_policy.disposition(f).action == retry_policy.BLOCK_AND_REVIEW
    # Что открыто — неизвестно: назначение на месте не ставится.
    assert len(sets) == 1
    assert ("destination_sweep_skipped", None, "simple_create_open_ad_unknown") in sim["rows"]


def test_an_extra_empty_ad_from_one_create_is_named(sim):
    _creates(sim, [{"extra_shells": 1}])
    out: list[str] = []
    sim["run"](additional_ads=_THREE[:1], soft_fails_out=out)
    assert "ad2_extra_empty_ad" in out
    assert "New Leads ad" in failure_policy.reason("ad2_extra_empty_ad")


def test_a_name_missing_from_the_ad_list_is_named(sim, monkeypatch):
    _creates(sim, listed=False)
    out: list[str] = []
    sim["run"](additional_ads=_THREE[:1], soft_fails_out=out)
    bad = [f for f in out if f.startswith("ad2_name:")]
    assert bad and bad[0].startswith("ad2_name:not_in_ad_list - ")
    assert failure_policy.severity(bad[0]) == failure_policy.BLOCKING
    assert ("ad2_listed", False, "not_in_ad_list") in sim["rows"]


def test_the_open_ad_destination_is_rechecked_and_only_its_failure_cleared(sim, monkeypatch):
    """Перепроверка назначения — у открытого (последнего) объявления, без
    дерева; снимается отказ именно его, отказ объявления 2 остаётся."""
    _creates(sim)
    monkeypatch.setattr(dom, "_browser_addon_none_checked", lambda p: False)
    answers = iter([True, False, False, True])      # ad1, ad2 ✗, ad3 ✗, перепроверка ad3 ✓
    monkeypatch.setattr(dom, "set_ad_destination_website", lambda p, url: next(answers))
    swept: list = []
    monkeypatch.setattr(dom, "verify_and_fix_ad_destinations", lambda p, names, url: swept.append(names) or [])
    out: list[str] = []
    sim["run"](additional_ads=_THREE, soft_fails_out=out)
    assert swept == [], "обход по дереву в новом редакторе"
    assert "ad2_destination" in out
    assert "ad3_destination" not in out
    assert ("destination_sweep_cleared", None, "") in sim["rows"]


def test_the_switch_leaves_more_ads_uncreated_and_blocking(sim, monkeypatch):
    seen = _creates(sim)
    monkeypatch.setenv(sc.MORE_ADS_PRESS_SETTING, "0")
    out: list[str] = []
    sim["run"](additional_ads=_THREE, soft_fails_out=out)
    assert seen == []
    more = [f for f in out if f.startswith("simple_create_more_ads:")]
    assert more == ["simple_create_more_ads:switched-off - 2 more ad(s) not created: creating more ads "
                    "in Meta's single-screen editor is switched off on this machine "
                    "(FB_RPA_SIMPLE_MORE_ADS_PRESS=0)"]
    assert failure_policy.severity(more[0]) == failure_policy.BLOCKING
    assert "switched off" in failure_policy.reason(more[0])


def test_single_ad_campaigns_create_nothing(sim):
    seen = _creates(sim)
    sim["run"]()
    assert seen == []


def test_the_destination_is_rechecked_on_the_page_not_through_the_tree(sim, monkeypatch):
    swept: list = []
    monkeypatch.setattr(dom, "verify_and_fix_ad_destinations", lambda p, names, url: swept.append(names) or [])
    monkeypatch.setattr(dom, "_browser_addon_none_checked", lambda p: False)   # обход нужен
    sets: list = []
    monkeypatch.setattr(dom, "set_ad_destination_website", lambda p, url: sets.append(url) or True)
    assert sim["run"]() == ""
    assert swept == [], "обход по дереву в новом редакторе"
    assert len(sets) == 2, "назначение не перепроверили на месте"


def test_the_classic_wizard_is_unchanged(sim):
    sim["state"]["simple"] = False
    sim["page"].url = EDIT_URL
    assert sim["run"]() == ""
    calls = sim["calls"]
    assert calls.count(("next",)) == 2, "прежний мастер: Next к группе и к объявлению"
    assert not [c for c in calls if c[0] in ("level", "objective", "terms")]
    # ACL-1611: карточку Audience пробуем в любой раскладке (нет сводки —
    # "absent", ничего не нажимается); других карточек прежний мастер не касается.
    assert [c for c in calls if c[0] == "card"] == [("card", "Audience")]
    assert not [r for r in sim["rows"] if r[0].startswith("simple_create")]


def test_the_classic_wizard_still_creates_more_ads(sim):
    sim["state"]["simple"] = False
    sim["page"].url = EDIT_URL
    sim["run"](additional_ads=[{"ad_name": "Two"}])
    assert sim["created"] == ["Set"]


# ── Create открыл новый редактор сразу, без окна цели ───────────────────

def test_create_landing_in_the_new_editor_is_a_press_that_took(monkeypatch):
    """Прежде такой адрес считался уходом со списка («left_list»): робот
    возвращался на список и жал Create ещё раз — ещё один черновик."""
    from unittest.mock import MagicMock

    page = MagicMock()
    page.url = SIMPLE_URL
    page.get_by_role.return_value.first.wait_for.side_effect = TimeoutError("no radio")
    monkeypatch.setattr(dom, "_tab_gone", lambda p: False)
    monkeypatch.setattr(dom, "_tab_frozen", lambda p: False)
    monkeypatch.setattr(dom, "_loading_visible", lambda p: False)
    monkeypatch.setattr(dom, "_sleep_h", lambda *a, **k: None)
    assert dom._create_took(page, "https://adsmanager.facebook.com/adsmanager/manage/campaigns?act=1",
                            timeout_s=1.0) == "ok"


def test_the_objective_wait_ends_when_the_new_editor_is_drawn(monkeypatch):
    from unittest.mock import MagicMock

    page = MagicMock()
    page.url = SIMPLE_URL
    page.get_by_role.return_value.first.wait_for.side_effect = TimeoutError("no radio")
    page.get_by_placeholder.return_value.first.is_visible.return_value = True
    rows: list = []
    monkeypatch.setattr(rt, "record", lambda step, ok=None, **kw: rows.append((step, kw.get("note"))))
    monkeypatch.setattr(dom, "_tab_gone", lambda p: False)
    monkeypatch.setattr(dom, "_tab_frozen", lambda p: False)
    monkeypatch.setattr(dom, "_loading_visible", lambda p: False)
    out: dict = {}
    assert dom._wait_objective_dialog(page, first_s=5, loading_max_s=5, poll_s=0.01, out=out) is True
    assert out["outcome"] == "simple_create"
    assert ("objective_wait", "simple_create") in rows


def test_the_objective_dialog_is_skipped_when_create_opened_the_new_editor(sim, monkeypatch):
    monkeypatch.setattr(dom, "_simple_create_url", lambda p: True)
    monkeypatch.setattr(dom, "_visible_now", lambda loc: False)   # окна цели нет
    page = sim["page"]
    pressed: list = []
    real = page.get_by_role.side_effect

    def by_role(role, name=None, **kw):
        if name in ("Continue", "Leads"):
            pressed.append(name)
        return real(role, name=name, **kw)

    page.get_by_role.side_effect = by_role
    assert sim["run"]() == ""
    assert "Continue" not in pressed, "жали Continue окна цели, которого нет"
    assert ("simple_create", None, "no_objective_dialog") in sim["rows"]
    assert sim["calls"].count(("objective",)) == 1, "цель в самом редакторе не проверили"


def test_the_objective_dialog_over_the_new_editor_is_still_answered(sim, monkeypatch):
    """Ревью PR #340 (F3): редактор уже отрисован, но поверх него окно цели
    (радио Leads видно) — жмём Leads и Continue, как прежде."""
    monkeypatch.setattr(dom, "_simple_create_url", lambda p: True)
    monkeypatch.setattr(dom, "_visible_now", lambda loc: True)
    page = sim["page"]
    pressed: list = []
    real = page.get_by_role.side_effect

    def by_role(role, name=None, **kw):
        if name == "Continue":
            pressed.append(name)
        return real(role, name=name, **kw)

    page.get_by_role.side_effect = by_role
    assert sim["run"]() == ""
    assert pressed == ["Continue"]
    assert ("simple_create", None, "no_objective_dialog") not in sim["rows"]


def test_the_editor_is_ready_only_when_its_name_field_is_drawn():
    """Ревью PR #340 (F3): один адрес — ещё не готовый редактор."""
    from unittest.mock import MagicMock

    page = MagicMock()
    page.url = SIMPLE_URL
    page.get_by_placeholder.return_value.first.is_visible.return_value = False
    assert dom._simple_create_ready(page) is False
    page.get_by_placeholder.return_value.first.is_visible.return_value = True
    assert dom._simple_create_ready(page) is True
    page.url = EDIT_URL
    assert dom._simple_create_ready(page) is False


def test_a_hanging_editor_shell_is_not_taken_for_an_empty_screen(monkeypatch):
    """Ревью PR #340 (F2): Create открыл заготовку нового редактора, а она
    висит с полосами «Loading...» — это не пустой экран: `idle` вернул бы
    поток на список и дал второй Create (второй черновик). Ждём до потолка и
    отдаём заготовку потоку — зависшую страницу лечит шаг имени кампании."""
    from unittest.mock import MagicMock

    page = MagicMock()
    page.url = SIMPLE_URL
    page.get_by_role.return_value.first.wait_for.side_effect = TimeoutError("no radio")
    page.get_by_placeholder.return_value.first.is_visible.return_value = False
    monkeypatch.setattr(rt, "record", lambda *a, **k: None)
    monkeypatch.setattr(dom, "_tab_gone", lambda p: False)
    monkeypatch.setattr(dom, "_tab_frozen", lambda p: False)
    monkeypatch.setattr(dom, "_loading_visible", lambda p: False)
    monkeypatch.setattr(dom, "_stall_evidence", lambda *a, **k: None)
    out: dict = {}
    got = dom._wait_objective_dialog(page, first_s=0.05, idle_s=0.05, loading_max_s=0.4,
                                     poll_s=0.01, out=out)
    assert out["outcome"] == "simple_create_shell"
    assert got is True
