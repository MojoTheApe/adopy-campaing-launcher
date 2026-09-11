"""Всё, чем чтение цепляется за экраны Meta, лежит в одном месте.

Причина не в порядке ради порядка. Meta двигает контролы сразу для всех:
10 сентября она переписала формулировки, и упало восемь шагов публикации —
чтение смотрит на те же экраны и ломается так же. Литералы, разбросанные по
файлу, означают, что чинить придётся по одному, находя их поиском.

И это готовит переезд в RPA-трек (`acl-108d`): перенести один блок — работа
механическая, собрать его заново по всему файлу — нет.
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SRC = ROOT / "src" / "analysis_agent" / "account_spend.py"

#: Что считается обращением к странице Meta: CSS-селектор с ролью, атрибутом
#: или тестовым идентификатором. Обычные строки — имена файлов, ключи, адреса —
#: под это не попадают.
LOOKS_LIKE_SELECTOR = re.compile(
    r"""\[(?:role|aria-|data-|aria_)|\brole\s*=\s*["']|:has-text\(""",
    re.I,
)


def _module() -> ast.Module:
    return ast.parse(SRC.read_text(encoding="utf-8"))


def _names_defined_before_first_function() -> set[str]:
    tree = _module()
    first_def = min(
        (n.lineno for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))),
        default=10**9,
    )
    out = set()
    for node in tree.body:
        if isinstance(node, ast.Assign) and node.lineno < first_def:
            for t in node.targets:
                if isinstance(t, ast.Name):
                    out.add(t.id)
    return out


def test_the_block_exists_and_is_named():
    """Блок должен быть видно глазами, а не только по именам."""
    assert "Чем чтение цепляется за экраны Meta" in SRC.read_text(encoding="utf-8")


def test_every_selector_is_a_module_level_name():
    """Ни одного селектора внутри функций.

    Это и есть проверяемое правило: найденный в теле функции литерал означает,
    что блок начал расползаться обратно.
    """
    tree = _module()
    stray: list[str] = []
    for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
        for node in ast.walk(fn):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if LOOKS_LIKE_SELECTOR.search(node.value):
                    stray.append(f"{fn.name}:{node.lineno} {node.value[:60]!r}")
    assert not stray, "селекторы расползлись по функциям:\n  " + "\n  ".join(stray)


def test_the_names_are_all_up_top():
    """Определения стоят до первой функции — то есть действительно в одном месте."""
    defined = _names_defined_before_first_function()
    for name in (
        "SEL_ACCOUNT_NAME", "SEL_PERIOD", "SEL_PERIOD_FALLBACK",
        "RE_PERIOD_DATE", "SEL_TOTALS_LABEL", "SEL_BUTTON", "SEL_GRID",
        "_FIND_TOTALS_JS", "_SCROLL_TO_TOTALS_JS",
    ):
        assert name in defined, f"{name} определён не в общем блоке"


def test_nothing_lost_its_meaning_on_the_way():
    """Перенос — не повод молча изменить поведение."""
    from src.analysis_agent import account_spend as sp

    assert sp.SEL_ACCOUNT_NAME[0] == '[aria-label*="Ad account" i]'
    assert 'span:has-text("Yesterday")' in sp.SEL_PERIOD
    assert sp.SEL_TOTALS_LABEL == '[data-acl-totals="1"]'
    assert sp.SEL_BUTTON == '[role="button"]'

    pat = re.compile(sp.RE_PERIOD_DATE, re.I)
    assert pat.search("Sep 6, 2026"), "дата в подписи кнопки перестала опознаваться"
    assert not pat.search("Yesterday")


def test_the_javascript_still_marks_the_row_it_finds():
    """Надпись помечается атрибутом — иначе к ней не подвести курсор.

    `evaluate` ручку не отдаёт, и связь между поиском и наведением держится
    только на этом атрибуте. Разъехавшись, они сломают шаг молча.
    """
    from src.analysis_agent import account_spend as sp

    assert "data-acl-totals" in sp._FIND_TOTALS_JS
    assert "data-acl-totals" in sp.SEL_TOTALS_LABEL


def test_this_check_would_notice_a_stray_selector():
    """Обратная проверка: сторож, который ничего не ловит, — хуже отсутствующего."""
    probe = ast.parse('def f():\n    page.query_selector(\'[role="button"]\')\n')
    stray = [
        node.value
        for fn in [n for n in ast.walk(probe) if isinstance(n, ast.FunctionDef)]
        for node in ast.walk(fn)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        and LOOKS_LIKE_SELECTOR.search(node.value)
    ]
    assert stray == ['[role="button"]'], "образец не узнаёт обычный селектор"
