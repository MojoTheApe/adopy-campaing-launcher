"""Тексты отказа чтения расхода — по-английски (ACL-1287).

`error` / `how` из account_spend.py рисуются в ячейке расхода строки аккаунта
как есть. Сторож разбирает файлы через ast и ищет кириллицу в строковых
литералах. Разрешены: докстринги, русские `//` и `/* */` комментарии внутри
JS-блоков (многострочные литералы) и сообщения логгера (вызовы на получателе
log/logger/logging — в экран не попадают). Остальной текст в JS-блоках тоже
проверяется. Файлы: account_spend.py, account_spend_api.py (Graph в профиле,
ACL-1760) и mlx_session.py (humanize кормит ту же ячейку).
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRCS = [
    ROOT / "src" / "analysis_agent" / "account_spend.py",
    ROOT / "src" / "analysis_agent" / "account_spend_api.py",
    ROOT / "src" / "fb_api" / "mlx_session.py",
]
CYR = re.compile(r"[А-Яа-яЁё]")
_LOG_METHODS = {"debug", "info", "warning", "error", "exception", "critical"}
_LOG_RECEIVERS = {"log", "logger", "logging", "_log", "_logger", "LOG", "LOGGER"}


def _visible(text: str) -> str:
    """Многострочный литерал без `//` и `/* */` комментариев кода."""
    if "\n" not in text:
        return text
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return "\n".join(re.sub(r"(^|\s)//.*$", "", ln) for ln in text.splitlines())


def _is_logger_call(node: ast.Call) -> bool:
    f = node.func
    if not (isinstance(f, ast.Attribute) and f.attr in _LOG_METHODS):
        return False
    v = f.value
    name = v.id if isinstance(v, ast.Name) else v.attr if isinstance(v, ast.Attribute) else ""
    return name in _LOG_RECEIVERS


def _offenders(source: str) -> list[tuple[int, str]]:
    tree = ast.parse(source)
    allowed: set[int] = set()
    for node in ast.walk(tree):
        if (isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                and node.body and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)):
            allowed.add(id(node.body[0].value))
        if (isinstance(node, ast.Call) and _is_logger_call(node) and node.args
                and isinstance(node.args[0], ast.Constant)):
            allowed.add(id(node.args[0]))
    bad = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in allowed and CYR.search(_visible(node.value))):
            bad.append((node.lineno, node.value[:80]))
    return bad


def test_no_cyrillic_reaches_the_spend_cell():
    for src in SRCS:
        bad = _offenders(src.read_text(encoding="utf-8"))
        assert not bad, f"Russian text in {src.name} string literals: {bad}"


def test_guard_catches_a_russian_reason():
    src = 'def f():\n    return {"error": "кабинет не найден"}\n'
    assert _offenders(src) == [(2, "кабинет не найден")]


def test_guard_allows_logger_and_docstring():
    src = 'def f():\n    """Док."""\n    log.error("сбой %s", 1)\n'
    assert _offenders(src) == []


def test_guard_catches_a_russian_reason_inside_a_js_block():
    src = 'JS = """\n// комментарий можно\n/* и такой */\nreturn {error: \'нет доступа\'};\n"""\n'
    found = _offenders(src)
    assert len(found) == 1 and "нет доступа" in found[0][1]


def test_guard_allows_js_comments_only():
    src = 'JS = """\n// комментарий\n/* ещё\nкомментарий */\nreturn 1;\n"""\n'
    assert _offenders(src) == []


def test_guard_does_not_exempt_non_logger_error_calls():
    src = 'def f(x):\n    x.error("сбой")\n'
    assert _offenders(src) == [(2, "сбой")]
