# -*- coding: utf-8 -*-
"""Имя верхнего уровня в модулях RPA определено один раз (ACL-1313).

meta_dom.py — тринадцать тысяч строк, и второе `def _click_marked` в его
конце молча подменило первое: новое брало CSS-селектор, старое — метку, и
открытие настроек аккаунта и выбор кампанийного бюджета стали нажимать
«ничто». Питон такое не запрещает, линтера в прогоне нет — значит, проверка
здесь.
"""
from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src" / "fb_ads_rpa"


@pytest.mark.parametrize("path", sorted(SRC.glob("*.py")), ids=lambda p: p.name)
def test_no_function_or_class_is_defined_twice(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = Counter(n.name for n in tree.body
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)))
    twice = sorted(k for k, v in names.items() if v > 1)
    assert not twice, f"{path.name}: defined more than once: {twice}"
