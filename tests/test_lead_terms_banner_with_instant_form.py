# -*- coding: utf-8 -*-
"""Баннер условий Lead Generation при «Instant form» — не приговор (ACL-1313).

Прогон 24.09 (GT Day, d3e690189a6e): после перезагрузки в
`_reveal_ad_copy_fields` Meta снова ставит в Browser add-ons «Instant form», и
баннер про условия Lead Generation относится к этой форме, а не к странице —
у этой же страницы первое объявление прошло. Прежде
`_repair_blocked_copy_editor` видел баннер и сдавался с
`lead_terms_not_accepted`; теперь, если «None» не выбрано, ставит «None», как
при любой другой блокировке. Если «None» стоит (или не прочитать) — баннер
по-прежнему значит «условия принимает человек».
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import meta_dom as md  # noqa: E402


@pytest.fixture
def env(monkeypatch):
    state = {
        "banner": True,
        "none_checked": [False, True],   # до лечения, после лечения
        "selects": 0,
        "reopened": [],
        "records": [],
        "review_errors": False,
    }

    def none_checked(page):
        seq = state["none_checked"]
        return seq.pop(0) if len(seq) > 1 else seq[0]

    def select_none(page):
        state["selects"] += 1
        return True

    monkeypatch.setattr(md, "_lead_terms_not_accepted", lambda page: state["banner"])
    monkeypatch.setattr(md, "_browser_addon_none_checked", none_checked)
    monkeypatch.setattr(md, "_select_browser_addon_none", select_none)
    monkeypatch.setattr(md, "recover_stuck_page", lambda *a, **k: False)
    monkeypatch.setattr(md, "verify_and_fix_ad_destinations",
                        lambda page, names, url: state["reopened"].append((tuple(names), url)))
    monkeypatch.setattr(md, "_review_errors_visible", lambda page: state["review_errors"])
    monkeypatch.setattr(md, "_sleep_h", lambda *a, **k: None)
    monkeypatch.setattr(md._telemetry, "record",
                        lambda step, ok=None, **kw: state["records"].append((step, ok, kw.get("note"))))
    return state


def test_the_banner_with_instant_form_selected_is_repaired(env):
    """Баннер есть, но «None» не выбрано: ставим «None» и открываем объявление."""
    assert md._repair_blocked_copy_editor(object(), "Ad 2", "https://example.com") is True
    assert env["selects"] == 1
    assert env["reopened"] == [(("Ad 2",), "https://example.com")]
    steps = [r[0] for r in env["records"]]
    assert "lead_terms_not_accepted" not in steps
    assert ("browser_addon_repair", True, None) in env["records"]


def test_the_banner_with_none_selected_is_still_the_page_terms(env):
    env["none_checked"] = [True]
    assert md._repair_blocked_copy_editor(object(), "Ad 2", "u") is False
    assert env["selects"] == 0 and env["reopened"] == []
    assert ("lead_terms_not_accepted", False, "page_must_accept_lead_terms") in env["records"]


def test_the_banner_with_an_unreadable_addon_block_is_still_the_page_terms(env):
    """«Не прочитать» (None) — не повод лечить: условий робот не примет."""
    env["none_checked"] = [None]
    assert md._repair_blocked_copy_editor(object(), "Ad 2", "u") is False
    assert env["selects"] == 0
    assert ("lead_terms_not_accepted", False, "page_must_accept_lead_terms") in env["records"]


def test_without_the_banner_the_ordinary_repair_runs(env):
    env["banner"] = False
    env["none_checked"] = [True]
    assert md._repair_blocked_copy_editor(object(), "", "u") is True
    assert env["selects"] == 1
    assert "lead_terms_not_accepted" not in [r[0] for r in env["records"]]
