# -*- coding: utf-8 -*-
"""Single-screen editor: Advantage+ audience before the age step (ACL-1544).

03.10, Facebook_1443 (live build f09a6db), runs 04509d5eed75 and 0e86c42023da:
every campaign logged `advantage_plus_off` ok=false `still_on` with diag
`{"flipped_switch": true, "picked_off": false, "badge": "on", "badge_after": "on"}`
right after `further_limit_*` succeeded. The same campaigns then logged
`advantage_plus_audience` ok=true `off` after the age step.

Что показали снимки (`audience_controls_badge_on_2026_10_03.html`):

* `card_run_a`, `card_run_b` — карточка Audience целиком: бейдж
  «Advantage+ on», а тело уже в режиме контролов — Locations, Age с живыми
  Minimum/Maximum age, Gender, Custom audiences, Detailed targeting,
  Languages и ссылка «Switch to recommended setup». Строки «Further limit
  the reach of your ads» нет. Это и есть результат шага Further limit.
  Бейдж до возраста «on» всегда: Meta снимает его, только когда в аудитории
  появляется жёсткое условие (возраст без «Use as a suggestion»), и это
  проверяет итоговый шаг `advantage_plus_audience` (252 «off» из 258 в
  живой телеметрии). `advantage_plus_off` до возраста не прошёл ни разу:
  417 записей из 417 — `still_on`.
* `campaign_toggle` — тумблер кампании в шапке редактора («In draft»,
  aria-label «Campaign toggle»). `_turn_off_advantage_plus_leads`
  поднимался от панели Campaign score на пять уровней и жал первый
  переключатель внутри — этот тумблер. `flipped_switch: true` значил
  «выключил кампанию»: на снимках тех же прогонов до шага тумблер
  aria-checked=true (b3a333ff43ab/00, cb854b07857b/00), после — false;
  в прогонах с `flipped_switch: false` он оставался true (e5beb2079e39,
  0131b6fa3476, df5344da3a17).
* `leads_row` — строка «Advantage+ leads campaign · On» в Campaign score:
  кнопка-строка, переключателя внутри нет.

Положительный признак «до возраста всё как надо» — режим контролов в
карточке Audience. По-настоящему «on» — рекомендованный режим (строка
Further limit на месте, контролов и «Switch to recommended setup» нет):
`audience_suggest_layout_2026_09_26.html` · suggest_classic.
"""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

from src.fb_ads_rpa import failure_policy as fp
from src.fb_ads_rpa import meta_dom as md
from src.fb_ads_rpa import run_telemetry
from tests.bench_support import FIXTURES, ROOT, browser, fragment  # noqa: F401

FIXTURE = FIXTURES / "audience_controls_badge_on_2026_10_03.html"
SUGGEST = FIXTURES / "audience_suggest_layout_2026_09_26.html"
BADGES = FIXTURES / "audience_advantage_on_2026_09_24.html"
_CYRILLIC = re.compile("[Ѐ-ӿ]")

#: Шпион тумблера кампании: любое нажатие на него пишется в window.toggled.
_SPY = r"""() => {
  window.toggled = [];
  const t = document.querySelector('input[aria-label="Campaign toggle"]');
  if (t) t.addEventListener('click', () => window.toggled.push(t.checked));
}"""


@pytest.fixture
def page(browser, monkeypatch, tmp_path):
    monkeypatch.setattr(md, "_sleep_h", lambda *a, **k: None)
    monkeypatch.setattr(run_telemetry, "TELEMETRY_PATH", tmp_path / "telemetry.jsonl")
    ctx = browser.new_context(viewport={"width": 1600, "height": 1000})
    ctx.route("**/*", lambda route: route.abort())
    p = ctx.new_page()
    try:
        yield p
    finally:
        ctx.close()


def _rows(tmp_path, step="advantage_plus_off"):
    f = tmp_path / "telemetry.jsonl"
    if not f.exists():
        return []
    return [r for r in (json.loads(line) for line in f.read_text(encoding="utf-8").splitlines()
                        if line.strip()) if r["step"] == step]


def _editor(card: str) -> str:
    """Редактор как на снимке: шапка с тумблером кампании, карточка Audience,
    панель Campaign score со строкой «Advantage+ leads campaign». Общая
    обёртка — чтобы прежний подъём от панели доходил до тумблера, как вживую."""
    return (f"<div id='editor'><header>{fragment(FIXTURE, 'campaign_toggle')}</header>"
            f"<main style='width:900px'>{card}</main>"
            f"<aside><div>Campaign score</div><div>You're using our recommended setup.</div>"
            f"{fragment(FIXTURE, 'leads_row')}</aside></div>")


def _no_fallback_clicks(monkeypatch) -> list[str]:
    """Запасные попытки прежнего выключения — без действия, только запись."""
    calls: list[str] = []
    monkeypatch.setattr(md, "_turn_off_advantage_plus_leads", lambda p: calls.append("switch") or False)
    monkeypatch.setattr(md, "_pick_advantage_plus_leads_off", lambda p: calls.append("menu") or False)
    monkeypatch.setattr(md, "_click_xpath", lambda p, xp, *a, **k: calls.append("xpath") or False)
    return calls


# ── чтение: режим карточки Audience ───────────────────────────────────────

@pytest.mark.parametrize("name", ["card_run_a", "card_run_b"])
def test_the_03_10_card_is_in_controls_mode_with_the_badge_on(page, name):
    page.set_content(_editor(fragment(FIXTURE, name)))
    assert md._audience_badge_on(page) is True
    mode = md._audience_mode(page)
    assert mode["mode"] == "controls"
    assert {"Locations", "Age", "Gender", "Detailed targeting", "Languages"} <= set(mode["controls"])
    assert mode["switch_back"] is True and mode["further_limit"] is False


def test_the_recommended_setup_card_is_not_controls_mode(page):
    page.set_content(fragment(SUGGEST, "suggest_classic"))
    assert md._audience_badge_on(page) is True
    mode = md._audience_mode(page)
    assert mode["mode"] == "recommended"
    assert mode["further_limit"] is True and mode["switch_back"] is False


def test_controls_with_a_suggested_age_are_controls_mode(page):
    """7cfdc772376d/02: контролы, возраст «Your suggestion», бейдж «on»."""
    page.set_content(fragment(SUGGEST, "controls_suggestion_on"))
    assert md._audience_mode(page)["mode"] == "controls"


def test_a_badge_without_the_card_body_has_no_mode(page):
    page.set_content(BADGES.read_text(encoding="utf-8"))
    assert md._audience_mode(page)["mode"] is None


def test_the_1443_recommended_card_reads_recommended(page):
    """b3a333ff43ab/00 (29.09, 1443): свёрнутая карточка в рекомендованном
    режиме. Сводка показывает те же подписи, что и режим контролов (Locations,
    Age, Gender, Detailed targeting, Languages — заголовки четвёртого уровня
    внутри карточки), строка «Further limit» — в скрытом теле."""
    page.set_content(_editor(fragment(FIXTURE, "card_recommended_collapsed")))
    assert md._audience_badge_on(page) is True
    mode = md._audience_mode(page)
    assert {"Locations", "Age", "Gender", "Detailed targeting", "Languages"} <= set(mode["controls"])
    assert mode["mode"] == "recommended"
    assert mode["further_limit"] is True and mode["switch_back"] is False


def test_the_same_labels_without_switch_back_are_never_controls(page):
    """Единственная защита от ложного «off»: без «Switch to recommended setup»
    подписи контролов ничего не доказывают. Тело карточки убрано — остаётся
    только сводка с пятью подписями."""
    page.set_content(_editor(fragment(FIXTURE, "card_recommended_collapsed")))
    page.evaluate("() => document.querySelectorAll('main [hidden]').forEach(e => e.remove())")
    mode = md._audience_mode(page)
    assert {"Locations", "Age", "Gender", "Detailed targeting", "Languages"} <= set(mode["controls"])
    assert mode["further_limit"] is False and mode["switch_back"] is False
    assert mode["mode"] is None


# ── шаг до возраста ───────────────────────────────────────────────────────

@pytest.mark.parametrize("name", ["card_run_a", "card_run_b"])
def test_the_03_10_capture_passes_without_any_click(page, tmp_path, monkeypatch, name):
    """Вердикт по снимку 03.10: режим контролов — шаг пройден, кликать нечего."""
    calls = _no_fallback_clicks(monkeypatch)
    page.set_content(_editor(fragment(FIXTURE, name)))
    page.evaluate(_SPY)
    assert md._ensure_manual_audience(page) is False
    assert calls == []
    assert page.evaluate("window.toggled") == []
    row = _rows(tmp_path)[-1]
    assert row["ok"] is True and row["note"] == "controls_mode"
    assert row["diag"]["badge"] == "on" and row["diag"]["mode"] == "controls"


def test_the_campaign_toggle_is_never_taken_for_the_advantage_switch(page):
    """Прежний подъём от панели Campaign score нажимал тумблер кампании."""
    page.set_content(_editor(fragment(FIXTURE, "card_run_a")))
    page.evaluate(_SPY)
    before = page.evaluate("document.querySelector('input[aria-label=\"Campaign toggle\"]').checked")
    assert md._turn_off_advantage_plus_leads(page) is False
    assert page.evaluate("window.toggled") == []
    after = page.evaluate("document.querySelector('input[aria-label=\"Campaign toggle\"]').checked")
    assert after == before


def test_a_switch_in_the_leads_row_itself_is_still_turned_off(page):
    """Старая раскладка: переключатель в самой строке «Advantage+ leads campaign»."""
    page.set_content(
        fragment(FIXTURE, "campaign_toggle")
        + "<div><div>Advantage+ leads campaign</div>"
          "<div id='sw' role='switch' aria-checked='true' "
          "onclick=\"this.setAttribute('aria-checked','false')\">toggle</div></div>")
    page.evaluate(_SPY)
    assert md._turn_off_advantage_plus_leads(page) is True
    assert page.get_attribute("#sw", "aria-checked") == "false"
    assert page.evaluate("window.toggled") == []


def test_a_foreign_named_switch_in_the_leads_row_is_not_pressed(page):
    """Строка подписи короткая, но переключатель в ней — «Campaign toggle»."""
    page.set_content(
        "<div><div>Advantage+ leads campaign</div>"
        "<input type='checkbox' role='switch' aria-label='Campaign toggle' aria-checked='true' checked></div>")
    page.evaluate(_SPY)
    assert md._turn_off_advantage_plus_leads(page) is False
    assert page.evaluate("window.toggled") == []


def test_a_switch_beyond_the_label_row_is_not_pressed(page):
    """Безымянный переключатель в трёх уровнях от подписи, но за пределами её
    строки (текст предка длиннее 80 символов) — не наш."""
    page.set_content(
        "<div><div>Campaign score: you're using our recommended setup for budget, audience "
        "and placements.</div><div><span>Advantage+ leads campaign</span></div>"
        "<div id='other' role='switch' aria-checked='true' "
        "onclick=\"this.setAttribute('aria-checked','false')\">x</div></div>")
    assert md._turn_off_advantage_plus_leads(page) is False
    assert page.get_attribute("#other", "aria-checked") == "true"


def test_really_still_on_is_a_failure_with_its_mode(page, tmp_path, monkeypatch):
    """Рекомендованный режим (строка Further limit на месте) — настоящий «on»."""
    calls = _no_fallback_clicks(monkeypatch)
    page.set_content(fragment(SUGGEST, "suggest_classic"))
    assert md._ensure_manual_audience(page) is True
    assert "switch" in calls
    row = _rows(tmp_path)[-1]
    assert row["ok"] is False and row["note"] == "still_on"
    assert row["diag"]["mode"] == "recommended"


def test_the_1443_recommended_card_is_still_on_and_fails(page, tmp_path, monkeypatch):
    calls = _no_fallback_clicks(monkeypatch)
    page.set_content(_editor(fragment(FIXTURE, "card_recommended_collapsed")))
    assert md._ensure_manual_audience(page) is True
    assert calls[:1] == ["switch"]
    row = _rows(tmp_path)[-1]
    assert row["ok"] is False and row["note"] == "still_on"
    assert row["diag"]["mode"] == "recommended" and row["diag"]["badge_after"] == "on"


def test_an_unreadable_card_is_unconfirmed_not_failed(page, tmp_path, monkeypatch):
    """Бейдж «on», а тела карточки не прочитать — не гадаем: `unconfirmed`."""
    _no_fallback_clicks(monkeypatch)
    page.set_content(BADGES.read_text(encoding="utf-8"))
    assert md._ensure_manual_audience(page) is None
    row = _rows(tmp_path)[-1]
    assert row["ok"] is None and row["note"] == "unconfirmed"
    assert row["diag"]["mode"] is None


# ── политика и сводка ─────────────────────────────────────────────────────

def test_still_on_blocks_with_a_clear_english_reason():
    assert fp.severity("advantage_plus_off") == fp.BLOCKING
    reason = fp.reason("advantage_plus_off")
    assert "unclassified" not in reason.lower()
    assert "recommended" in reason and "Advantage+" in reason
    assert not _CYRILLIC.search(reason)


def test_still_on_reads_as_a_plain_problem():
    p = fp.problem("advantage_plus_off:recommended setup")
    assert p["severity"] == fp.BLOCKING
    assert not p["title"].startswith(("Step ", "Unclassified"))
    assert "Advantage+" in p["title"]
    assert not any(_CYRILLIC.search(s) for s in (p["title"], p["impact"], p["action"]))
    assert p["note"] == "recommended setup"


def test_the_recorded_flow_blocks_on_still_on_before_the_age():
    """`run_recorded_campaign_flow`: выключение до возраста — своим вызовом,
    его «on» идёт в soft_fails как `advantage_plus_off`, а возраст не
    повторяет выключение."""
    src = (ROOT / "src/fb_ads_rpa/meta_dom.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "run_recorded_campaign_flow")
    calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
    ensure = [n.lineno for n in calls if n.func.id == "_ensure_manual_audience"]
    age = [n for n in calls if n.func.id == "fill_audience_age"]
    assert ensure and age and min(ensure) < min(n.lineno for n in age)
    assert all(any(k.arg == "ensure_manual" and isinstance(k.value, ast.Constant)
                   and k.value.value is False for k in n.keywords) for n in age)
    # Ветка `if _ensure_manual_audience(page) is True:` кладёт ровно эту строку.
    guard = [n for n in ast.walk(fn) if isinstance(n, ast.If)
             and isinstance(n.test, ast.Compare) and isinstance(n.test.left, ast.Call)
             and getattr(n.test.left.func, "id", "") == "_ensure_manual_audience"]
    assert len(guard) == 1
    appended = [c.args[0].value for st in guard[0].body for c in ast.walk(st)
                if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                and c.func.attr == "append" and getattr(c.func.value, "id", "") == "soft_fails"
                and c.args and isinstance(c.args[0], ast.Constant)]
    assert appended == ["advantage_plus_off:recommended setup, no audience controls"]


def test_fill_audience_age_can_skip_the_switch_off(page, monkeypatch):
    seen = []
    monkeypatch.setattr(md, "_ensure_manual_audience", lambda p: seen.append("ensure") or False)
    monkeypatch.setattr(md, "_fill_audience_age_controls", lambda p, a, b: False)
    page.set_content("<div>Age</div>")
    assert md.fill_audience_age(page, 35, 65, ensure_manual=False) is False
    assert seen == []
    assert md.fill_audience_age(page, 35, 65) is False
    assert seen == ["ensure"]
