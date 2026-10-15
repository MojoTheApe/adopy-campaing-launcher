# -*- coding: utf-8 -*-
"""Advantage+ audience читается по бейджу карточки Audience, и только по нему (ACL-1313).

Раскладка 09.2026: Advantage+ audience — не переключатель, а надпись в
заголовке карточки `#advantage-state-audience-section-header`. Такая же
«Advantage+ on» стоит у Placements (`#advantage-state-placement-section-
header`), поэтому поиск по всей странице читал аудиторию как «on» и после
выключения (снимок 8587f7668f86/02_languages: Audience — off, Placements — on).

Включённая Advantage+ audience = возраст лишь «подсказка», и Meta показывает
рекламу вне него, поэтому итог проверяется после возраста, при «on» один раз
снимается «Use as a suggestion», а отказ `advantage_plus_audience` блокирует
черновик.

Фикстуры — заголовки обеих карточек из снимков b30c3b2b6be9/00 (обе «on») и
8587f7668f86/02 (аудитория «off», размещения «on»).
"""
from __future__ import annotations

import ast
import json
import os
import sys
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import failure_policy  # noqa: E402
from src.fb_ads_rpa import meta_dom as md  # noqa: E402
from src.fb_ads_rpa import run_telemetry  # noqa: E402

FIX = Path(__file__).parent / "fixtures" / "ads_manager"
BOTH_ON = (FIX / "audience_advantage_on_2026_09_24.html").read_text(encoding="utf-8")
AUDIENCE_OFF = (FIX / "audience_off_placements_on_2026_09_24.html").read_text(encoding="utf-8")


def _cards(headers: str) -> str:
    """Заголовки снимка по своим карточкам: Audience, затем Placements."""
    aud, place = headers.strip().split("\n", 1)
    return (f"<section><div>Audience</div>{aud}</section>"
            f"<section><div>Placements</div>{place}</section>")


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=os.environ.get("ACL_BROWSER_EXECUTABLE") or None)
        try:
            yield b
        finally:
            b.close()


@pytest.fixture
def page(browser, monkeypatch, tmp_path):
    monkeypatch.setattr(md, "_sleep_h", lambda *a, **k: None)
    # Телеметрия — во временный файл, не в живой output/.
    monkeypatch.setattr(run_telemetry, "TELEMETRY_PATH", tmp_path / "telemetry.jsonl")
    ctx = browser.new_context(viewport={"width": 1280, "height": 800})
    p = ctx.new_page()
    p.route("**/*", lambda route: route.abort())
    try:
        yield p
    finally:
        ctx.close()


def _rows(tmp_path, step="advantage_plus_audience"):
    f = tmp_path / "telemetry.jsonl"
    if not f.exists():
        return []
    return [r for r in (json.loads(line) for line in f.read_text(encoding="utf-8").splitlines() if line.strip())
            if r["step"] == step]


# ── чтение бейджа ─────────────────────────────────────────────────────────

def test_the_audience_badge_reads_on(page):
    page.set_content(_cards(BOTH_ON))
    assert md._audience_badge_on(page) is True


def test_placements_on_does_not_make_the_audience_on(page):
    """Снимок 8587f7668f86/02: у аудитории «off», у размещений «on»."""
    page.set_content(_cards(AUDIENCE_OFF))
    assert md._audience_badge_on(page) is False


def test_placements_on_does_not_make_the_audience_on_whatever_the_order(page):
    aud, place = AUDIENCE_OFF.strip().split("\n", 1)
    page.set_content(f"<section>{place}</section><section>{aud}</section>")
    assert md._audience_badge_on(page) is False


def test_without_the_audience_card_the_answer_is_unknown(page):
    """Бейдж размещений «on» на странице есть, карточки Audience нет — None."""
    _aud, place = BOTH_ON.strip().split("\n", 1)
    page.set_content(f"<section><div>Placements</div>{place}</section>"
                     "<div>Advantage+ on</div>")
    assert md._audience_badge_on(page) is None


# ── итоговая проверка ─────────────────────────────────────────────────────

_SUGGESTION = """
<div id="agerow"><div>Age</div><div><div>35</div><div>65+</div></div>
  <label><input type="checkbox" id="sug" %s><span>Use as a suggestion</span></label>
</div>
<script>
  document.getElementById('sug').addEventListener('click', () => {
    if (window.__flip) for (const el of document.querySelectorAll(
        '#advantage-state-audience-section-header div'))
      if (el.children.length === 0 && /Advantage\\+ on/.test(el.textContent)) el.textContent = 'Advantage+ off';
  });
</script>
"""


def _age_card(headers: str, *, suggestion_checked: bool, flips: bool) -> str:
    return (_cards(headers) + _SUGGESTION % ("checked" if suggestion_checked else "")
            + f"<script>window.__flip = {'true' if flips else 'false'};</script>")


def test_audience_already_off_passes_without_touching_the_suggestion(page, tmp_path):
    page.set_content(_age_card(AUDIENCE_OFF, suggestion_checked=False, flips=False))
    assert md.verify_audience_advantage_off(page) is True
    row = _rows(tmp_path)[-1]
    assert row["ok"] is True and row["note"] == "off"
    assert row["diag"]["retried_suggestion"] is False
    assert row["diag"]["age"] == ["35", "65+"]


def test_audience_on_is_fixed_by_unchecking_the_suggestion_once(page, tmp_path):
    """Галочка «Use as a suggestion» осталась — сняли, и Meta выключила Advantage+."""
    page.set_content(_age_card(BOTH_ON, suggestion_checked=True, flips=True))
    assert md.verify_audience_advantage_off(page) is True
    assert page.eval_on_selector("#sug", "el => el.checked") is False
    row = _rows(tmp_path)[-1]
    assert row["ok"] is True and row["diag"]["retried_suggestion"] is True


def test_audience_that_stays_on_fails_and_says_so(page, tmp_path):
    page.set_content(_age_card(BOTH_ON, suggestion_checked=True, flips=False))
    assert md.verify_audience_advantage_off(page) is False
    row = _rows(tmp_path)[-1]
    assert row["ok"] is False and row["note"] == "still_on"
    assert row["diag"]["retried_suggestion"] is True


def test_a_missing_audience_card_is_not_a_pass(page, tmp_path):
    _aud, place = BOTH_ON.strip().split("\n", 1)
    page.set_content(place)
    assert md.verify_audience_advantage_off(page) is False
    assert _rows(tmp_path)[-1]["note"] == "badge_not_found"


# ── политика отказа ───────────────────────────────────────────────────────

def test_the_failure_blocks_the_draft_with_a_human_reason():
    assert failure_policy.severity("advantage_plus_audience") == failure_policy.BLOCKING
    reason = failure_policy.reason("advantage_plus_audience")
    assert "unclassified" not in reason
    assert "Advantage+" in reason


def test_the_recorded_flow_checks_the_audience_after_the_age():
    """Проверка стоит в `run_recorded_campaign_flow` после возраста, а её отказ
    идёт в soft_fails как `advantage_plus_audience`."""
    src = (ROOT / "src/fb_ads_rpa/meta_dom.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "run_recorded_campaign_flow")
    names = [(n.lineno, n.func.id) for n in ast.walk(fn)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
    verify = [ln for ln, name in names if name == "verify_audience_advantage_off"]
    age = [ln for ln, name in names if name == "fill_audience_age"]
    assert verify and age and min(age) < min(verify)
    body = ast.get_source_segment(src, fn)
    assert 'soft_fails.append("advantage_plus_audience")' in body


# ── выключение до возраста: без бейджа «on» ничего не кликается (25.09) ────
#
# `_ensure_manual_audience` читал состояние по всей странице и видел «on» у
# Placements / кампанийного переключателя — 162 записи still_on, каждая с
# кликами впустую. Теперь он читает бейдж Audience и при «off» или без бейджа
# не трогает ничего, оставляя строку телеметрии с прочитанным состоянием.

def _spy_clicks(monkeypatch, *, flips_badge=False):
    clicks: list[str] = []

    def _turn_off(p):
        clicks.append("switch")
        if flips_badge:
            p.evaluate(r"""() => { for (const el of document.querySelectorAll(
                '#advantage-state-audience-section-header *'))
              if (el.children.length === 0 && /Advantage\+ on/.test(el.textContent))
                el.textContent = 'Advantage+ off'; }""")
        return flips_badge

    monkeypatch.setattr(md, "_turn_off_advantage_plus_leads", _turn_off)
    monkeypatch.setattr(md, "_pick_advantage_plus_leads_off", lambda p: clicks.append("menu") or False)
    monkeypatch.setattr(md, "_click_xpath", lambda p, xp, *a, **k: clicks.append("xpath") or False)
    return clicks


_CAMPAIGN_SWITCH_ON = ("<div><div>Advantage+ leads campaign</div>"
                       "<div role='switch' aria-checked='true'>toggle</div></div>")


def test_audience_off_with_placements_on_is_not_clicked(page, tmp_path, monkeypatch):
    """Снимок 8587f7668f86/02 + кампанийный переключатель «On»: прежнее
    чтение по странице отвечало «on» и кликало."""
    clicks = _spy_clicks(monkeypatch)
    page.set_content(_cards(AUDIENCE_OFF) + _CAMPAIGN_SWITCH_ON)
    assert md._ensure_manual_audience(page) is False
    assert clicks == []
    row = _rows(tmp_path, "advantage_plus_off")[-1]
    assert row["ok"] is None and row["note"] == "already_off"
    assert row["diag"]["badge"] == "off"


def test_no_audience_badge_means_no_clicks(page, tmp_path, monkeypatch):
    """Карточки Audience нет, а «Advantage+ on» у Placements есть — не кликаем."""
    clicks = _spy_clicks(monkeypatch)
    _aud, place = BOTH_ON.strip().split("\n", 1)
    page.set_content(f"<section><div>Placements</div>{place}</section>" + _CAMPAIGN_SWITCH_ON)
    assert md._ensure_manual_audience(page) is None
    assert clicks == []
    row = _rows(tmp_path, "advantage_plus_off")[-1]
    assert row["note"] == "badge_not_found" and row["diag"]["badge"] is None


def test_audience_badge_on_is_still_switched_off(page, tmp_path, monkeypatch):
    clicks = _spy_clicks(monkeypatch, flips_badge=True)
    page.set_content(_cards(BOTH_ON))
    assert md._ensure_manual_audience(page) is False
    assert clicks == ["switch"]
    row = _rows(tmp_path, "advantage_plus_off")[-1]
    assert row["ok"] is True and row["note"] == "switched"
    assert row["diag"]["badge"] == "on" and row["diag"]["badge_after"] == "off"
