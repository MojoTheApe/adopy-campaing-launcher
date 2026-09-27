# -*- coding: utf-8 -*-
"""Записанный поток в новом редакторе Meta «в один экран» (ACL-1327).

Прежде (ACL-1320) редактор `/adsmanager/manage/simple_create` был остановкой
аккаунта `simple_create_layout`, а 9e7d67471c6b (27.09) его не узнал вовсе и
46 с искал Next, которого там нет (`next_to_adset`). Теперь поток идёт по
разделам страницы: до группы и объявления — прокруткой, без Next; свёрнутые
карточки (бюджет, Audience, Placements) раскрываются перед прежними
обработчиками; цель Leads проверяется на месте; карточка условий Lead Ads,
оставшаяся после Website, — остановка аккаунта; второе и третье объявления
не создаются (блокирующая причина). Прежний мастер не меняется.

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


def test_an_objective_that_cannot_be_set_fails_the_campaign(sim):
    sim["state"]["objective"] = "failed"
    assert sim["run"]() == "objective_leads"
    assert ("level", "adset") not in sim["calls"]
    # Цель проверяется сразу после имени — до бюджета, а не перед группой.
    assert ("card", "Budget") not in sim["calls"]


def test_a_missing_section_is_the_layout_stop(sim):
    sim["state"]["level_ok"] = False
    assert sim["run"]() == "simple_create_layout"
    assert ("simple_create_layout", False) in _steps(sim["rows"])
    assert "Ad set / Ad sections" in failure_policy.reason("simple_create_layout")


def test_more_ads_are_not_created_and_block(sim):
    out: list[str] = []
    got = sim["run"](additional_ads=[{"ad_name": "Two", "creative_path": "b.png"},
                                     {"ad_name": "Three", "creative_path": "c.png"}],
                     soft_fails_out=out)
    assert got == ""
    assert sim["created"] == [], "в новом редакторе создавали объявления через дерево"
    more = [f for f in out if f.startswith("simple_create_more_ads:")]
    assert more and more[0].startswith("simple_create_more_ads:2 more ad(s)")
    assert failure_policy.severity(more[0]) == failure_policy.BLOCKING
    assert "only the first" in failure_policy.reason(more[0])


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
    assert not [c for c in calls if c[0] in ("level", "card", "objective", "terms")]
    assert not [r for r in sim["rows"] if r[0].startswith("simple_create")]


def test_the_classic_wizard_still_creates_more_ads(sim):
    sim["state"]["simple"] = False
    sim["page"].url = EDIT_URL
    sim["run"](additional_ads=[{"ad_name": "Two"}])
    assert sim["created"] == ["Set"]
