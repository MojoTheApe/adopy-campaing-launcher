# -*- coding: utf-8 -*-
"""Прокрутка до секции и подпункты плейсментов — без лишних ожиданий (ACL-1313).

* `_scroll_until_mounted`: после каждой прокрутки сидели фиксированные 0,9 с и
  только потом смотрели, смонтировалась ли секция. Теперь смотрим всё это
  время: секция, появившаяся через 0,15 с, не ждёт остаток. Потолок прежний.
* Подпункты плейсментов: по одному `page.evaluate` на подпункт (до 18 за
  заход, два захода на категорию) — теперь один вызов на заход, каждый
  подпункт тем же `PLACEMENTS_SUBITEM_JS`.

Поддельные часы и счёт вызовов; разметка подпунктов — синтетическая, в
офлайн-Chrome.
"""
from __future__ import annotations

import ast
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import meta_dom as md  # noqa: E402


# ── _scroll_until_mounted ─────────────────────────────────────────────────

class _MountPage:
    """Секция монтируется через `mount_after` секунд после первой прокрутки."""

    def __init__(self, clock, mount_after):
        self.clock = clock
        self.mount_at = None
        self.mount_after = mount_after
        self.scrolls = 0

    def evaluate(self, js, *args):
        if args:                                   # scroll_js(frac)
            self.scrolls += 1
            if self.mount_at is None:
                self.mount_at = self.clock["now"] + self.mount_after
            return True
        return self.mount_at is not None and self.clock["now"] >= self.mount_at


@pytest.fixture
def clock(monkeypatch):
    c = {"now": 0.0}

    def sleep(s):
        c["now"] += s

    monkeypatch.setattr(md, "time", SimpleNamespace(time=lambda: c["now"], sleep=sleep))
    monkeypatch.setattr(md, "_sleep_h", lambda base, spread=0.3: sleep(base))
    return c


def test_a_quickly_mounted_section_does_not_wait_out_the_settle(clock):
    page = _MountPage(clock, mount_after=0.1)
    assert md._scroll_until_mounted(page, "present()") is True
    assert page.scrolls == 1
    assert clock["now"] < 0.5, clock["now"]


def test_a_slow_section_still_gets_the_full_settle_per_scroll(clock):
    page = _MountPage(clock, mount_after=0.85)
    assert md._scroll_until_mounted(page, "present()") is True
    assert page.scrolls == 1, "секцию, пришедшую за 0,85 с, бросили раньше потолка 0,9 с"


def test_an_absent_section_costs_no_more_than_before(clock):
    page = _MountPage(clock, mount_after=10_000)
    assert md._scroll_until_mounted(page, "present()") is False
    assert page.scrolls == len(md._MOUNT_FRACS)
    per_scroll = clock["now"] / page.scrolls
    assert per_scroll <= 0.9 + md._MOUNT_POLL_S + 1e-6


# ── подпункты плейсментов ─────────────────────────────────────────────────

@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=os.environ.get("ACL_BROWSER_EXECUTABLE") or None)
        try:
            yield b
        finally:
            b.close()


@pytest.fixture
def page(browser):
    ctx = browser.new_context(viewport={"width": 1000, "height": 800})
    p = ctx.new_page()
    try:
        yield p
    finally:
        ctx.close()


# Как у Meta: подпись и галочка в общей строке, состояние — в aria-checked и
# обновляется НЕ в обработчике клика, а следующей отрисовкой (setTimeout 0).
# Снятая галочка убирает свою строку — строки ниже переезжают.
_SUBITEMS_HTML = """
<div data-rpa-placements-marker>
  <div><span>Locked</span><input type=checkbox disabled aria-checked="false"></div>
  %s
</div>
<script>
  window.evals = 0;
  document.querySelectorAll('input[type=checkbox]:not([disabled])').forEach(cb => {
    cb.addEventListener('click', (e) => {
      e.preventDefault();
      const next = cb.getAttribute('aria-checked') !== 'true';
      setTimeout(() => {
        cb.setAttribute('aria-checked', String(next));
        if (!next) cb.parentElement.style.marginTop = '0px';
      }, 0);
    });
  });
</script>
"""


def _rows(states: dict[str, bool]) -> str:
    return "".join(
        f"<div style='margin-top:20px'><span>{name}</span>"
        f"<input type=checkbox aria-checked='{'true' if on else 'false'}'></div>"
        for name, on in states.items())


def _state(page, name):
    return page.evaluate(
        "(n) => { const s = [...document.querySelectorAll('span')].find(e => e.textContent === n);"
        " return s.parentElement.querySelector('input').getAttribute('aria-checked'); }", name)


class _Counting:
    def __init__(self, page):
        self._page = page
        self.calls = 0

    def evaluate(self, js, *a):
        self.calls += 1
        return self._page.evaluate(js, *a)


def test_all_subitems_of_a_pass_are_set_in_one_call(page):
    page.set_content(_SUBITEMS_HTML % _rows({
        "Facebook Feed": False, "Instagram feed": False, "Facebook Marketplace": True,
        "Facebook Right Column": True, "Instagram Stories": False}))
    items = [("Facebook Feed", True), ("Instagram Feed", True), ("Instagram Stories", True),
             ("Facebook Marketplace", False), ("Facebook Right Column", False)]
    counting = _Counting(page)
    res = md._set_placement_subitems(counting, items)
    assert counting.calls == 1
    assert len(res) == len(items)
    page.wait_for_timeout(50)
    assert _state(page, "Facebook Feed") == "true"
    assert _state(page, "Instagram feed") == "true", "регистр подписи сверяется, как прежде"
    assert _state(page, "Facebook Marketplace") == "false"
    assert _state(page, "Facebook Right Column") == "false"


def test_each_item_answers_like_the_single_call_did(page):
    page.set_content(_SUBITEMS_HTML % _rows({"Facebook Feed": True}))
    res = md._set_placement_subitems(page, [
        ("Facebook Feed", True), ("Nowhere", True), ("Locked", False), ("Locked", True)])
    assert res[0]["ok"] is True
    assert res[1] == {"ok": False, "why": "строка не найдена"}
    assert res[2]["ok"] is True and "недоступна" in res[2]["why"]
    assert res[3]["ok"] is False


def test_nothing_to_set_costs_no_call():
    class _Never:
        def evaluate(self, *a):
            raise AssertionError("вызов страницы без подпунктов")

    assert md._set_placement_subitems(_Never(), []) == []


def test_fill_placements_sets_subitems_through_the_batch():
    """`fill_placements` больше не зовёт `PLACEMENTS_SUBITEM_JS` по одному."""
    tree = ast.parse((ROOT / "src/fb_ads_rpa/meta_dom.py").read_text(encoding="utf-8"))
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "fill_placements")
    names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
    assert "PLACEMENTS_SUBITEM_JS" not in names
    assert "_set_placement_subitems" in names
