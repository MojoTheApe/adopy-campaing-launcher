"""Тексты отказа чтения расхода — по-английски (ACL-1287).

`error` / `how` из account_spend.py рисуются в ячейке расхода строки аккаунта
как есть. Сторож разбирает файл через ast и ищет кириллицу в строковых
литералах. Разрешены: докстринги, JS-блоки (многострочные литералы — в них
русские `//`-комментарии кода) и сообщения логгера (в экран не попадают).
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "analysis_agent" / "account_spend.py"
CYR = re.compile(r"[А-Яа-яЁё]")
_LOG_METHODS = {"debug", "info", "warning", "error", "exception", "critical"}


def _offenders(source: str) -> list[tuple[int, str]]:
    tree = ast.parse(source)
    allowed: set[int] = set()
    for node in ast.walk(tree):
        if (isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                and node.body and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)):
            allowed.add(id(node.body[0].value))
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in _LOG_METHODS and node.args
                and isinstance(node.args[0], ast.Constant)):
            allowed.add(id(node.args[0]))
    bad = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in allowed and "\n" not in node.value
                and CYR.search(node.value)):
            bad.append((node.lineno, node.value[:80]))
    return bad


def test_no_cyrillic_reaches_the_spend_cell():
    bad = _offenders(SRC.read_text(encoding="utf-8"))
    assert not bad, f"Russian text in account_spend.py string literals: {bad}"


def test_guard_catches_a_russian_reason():
    src = 'def f():\n    return {"error": "кабинет не найден"}\n'
    assert _offenders(src) == [(2, "кабинет не найден")]


def test_guard_allows_logger_and_docstring():
    src = 'def f():\n    """Док."""\n    log.error("сбой %s", 1)\n'
    assert _offenders(src) == []
