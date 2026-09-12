"""Ожидание того, чего нет, — самая дорогая строка в прогоне.

`onboarding_modal` тратил ровно 43 секунды на каждую кампанию: пять заходов
кликнуть по кнопке одноразовой модалки, каждый со своим восьмисекундным
ожиданием видимости. Измерено по телеметрии за 9 сентября — плоский пол
43.2с при медиане 44.0с и четырнадцати прогонах. Плоский пол означает, что
модалки не было ни разу: это не разброс задержек, а выбранный до конца бюджет
таймаутов.

Проверяется здесь цена ожидания, а не разметка Meta. Двойник страницы тут
уместен: считаем время и число обращений, а не совпадение селекторов.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import meta_dom  # noqa: E402


class _Loc:
    """Локатор, который становится видимым через заданное время (или никогда)."""

    def __init__(self, visible_after: float | None, wait_cost_s: float = 0.0):
        self._at = None if visible_after is None else time.time() + visible_after
        self._cost = wait_cost_s
        self.visibility_checks = 0

    @property
    def first(self):
        return self

    def count(self):
        return 1

    def is_visible(self, timeout=0):
        self.visibility_checks += 1
        if self._at is None:
            # Настоящий локатор Playwright здесь ждёт весь таймаут.
            time.sleep(self._cost)
            return False
        return time.time() >= self._at


def test_an_absent_element_is_given_up_on_quickly():
    """Главное число: сколько стоит «его нет»."""
    started = time.time()
    got = meta_dom._first_visible(None, (_Loc(None),), budget_s=1.0, poll_s=0.05)
    took = time.time() - started
    assert got is None
    assert took < 2.0, f"отказ занял {took:.1f}с — бюджет не соблюдается"


def test_the_budget_is_not_multiplied_by_the_number_of_locators():
    """Та самая ошибка: два локатора по пять попыток — это не бюджет, а сумма.

    Прежний код давал каждой кнопке свой полный запас, и они складывались.
    """
    slow = [_Loc(None, wait_cost_s=0.05) for _ in range(2)]
    started = time.time()
    meta_dom._first_visible(None, tuple(slow), budget_s=1.0, poll_s=0.05)
    took = time.time() - started
    assert took < 2.0, f"два локатора обошлись в {took:.1f}с — бюджеты сложились"


def test_an_element_that_appears_late_is_still_caught():
    """Модалка встаёт поверх поля имени кампании и появляется не мгновенно.

    Быстрый отказ не должен превратиться в «не заметили».
    """
    loc = _Loc(visible_after=0.4)
    got = meta_dom._first_visible(None, (loc,), budget_s=3.0, poll_s=0.05)
    assert got is loc, "элемент появился в пределах бюджета, а его не увидели"


def test_the_first_of_several_wins():
    absent, present = _Loc(None), _Loc(0.0)
    got = meta_dom._first_visible(None, (absent, present), budget_s=1.0, poll_s=0.05)
    assert got is present


def test_a_locator_that_throws_does_not_end_the_search():
    """Страница на этом шаге бывает в любом состоянии."""

    class _Broken:
        @property
        def first(self):
            return self

        def count(self):
            raise RuntimeError("target closed")

    present = _Loc(0.0)
    assert meta_dom._first_visible(None, (_Broken(), present), budget_s=1.0, poll_s=0.05) is present


def test_the_onboarding_step_asks_before_it_clicks():
    """Сторож: вернуть прежний вариант — вернуть 43 секунды на кампанию."""
    source = (ROOT / "src" / "fb_ads_rpa" / "meta_dom.py").read_text(encoding="utf-8")
    block = source[source.index("# Live evidence (2026-07-23, first run ever"):]
    block = block[: block.index('_shot("after_onboarding_modal")')]
    assert "_first_visible" in block, "шаг снова кликает вслепую"
    assert "attempts=2" not in block, "вернулась вторая серия попыток по отсутствующей кнопке"


def test_the_pauses_between_actions_are_untouched():
    """Ускорять ожидание можно, ускорять действия — нельзя.

    Одинаковые интервалы между действиями были самой считываемой приметой в
    разборе 31 июля. `_first_visible` не трогает `_sleep_h` — он вообще про
    другое: там, где мы ждём, никаких событий не происходит.
    """
    import ast

    tree = ast.parse((ROOT / "src" / "fb_ads_rpa" / "meta_dom.py").read_text(encoding="utf-8"))
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "_first_visible"
    )
    called = {
        node.func.id
        for node in ast.walk(fn)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "_sleep_h" not in called, "ожидание полезло в человеческие паузы"


def test_that_guard_looks_at_calls_not_at_prose():
    """Обратная проверка: сторож дважды за вечер ловил собственный комментарий."""
    import ast

    probe = ast.parse(
        'def _first_visible():\n'
        '    """Человеческие паузы _sleep_h это не трогает."""\n'
        '    return 1\n'
    )
    fn = next(n for n in ast.walk(probe) if isinstance(n, ast.FunctionDef))
    called = {
        node.func.id
        for node in ast.walk(fn)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "_sleep_h" not in called, "упоминание в описании засчитано за вызов"
