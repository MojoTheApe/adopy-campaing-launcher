"""Причины отказа пострановой разбивки — по-английски (ACL-1462, ревью PR #463).

Всё, что `read_country_split` кладёт в `out.error`, уходит на экран — в
подсказку «countries failed». Сторож разбирает country_split.py токенизатором
и ищет кириллицу в строковых литералах, кроме докстрингов. В JS-блоках
(тройные кавычки) русские `//`-комментарии разрешены: это комментарии кода.
"""
from __future__ import annotations

import io
import re
import tokenize
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "analysis_agent" / "country_split.py"
CYR = re.compile(r"[А-Яа-яЁё]")
_SKIP = {tokenize.NL, tokenize.COMMENT}
_STMT_START = {tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT, tokenize.ENCODING}
_FSTRING_START = getattr(tokenize, "FSTRING_START", None)
_FSTRING_MIDDLE = getattr(tokenize, "FSTRING_MIDDLE", None)
_FSTRING_END = getattr(tokenize, "FSTRING_END", None)


def _literals(source: str):
    """(строка, текст, докстринг ли) для каждого строкового литерала."""
    toks = [t for t in tokenize.generate_tokens(io.StringIO(source).readline)
            if t.type not in _SKIP]
    i = 0
    while i < len(toks):
        t = toks[i]
        if t.type == tokenize.STRING or (_FSTRING_START is not None and t.type == _FSTRING_START):
            start = i
            if t.type == tokenize.STRING:
                text = t.string
            else:
                parts = [t.string]
                depth = 1
                while depth:
                    i += 1
                    nt = toks[i]
                    if nt.type == _FSTRING_START:
                        depth += 1
                    elif nt.type == _FSTRING_END:
                        depth -= 1
                    if nt.type == _FSTRING_MIDDLE or nt.type in (_FSTRING_START, _FSTRING_END):
                        parts.append(nt.string)
                text = "".join(parts)
            prev = toks[start - 1].type if start else tokenize.ENCODING
            nxt = toks[i + 1].type if i + 1 < len(toks) else tokenize.ENDMARKER
            doc = prev in _STMT_START and nxt in (tokenize.NEWLINE, tokenize.ENDMARKER)
            yield t.start[0], text, doc
        i += 1


def _visible(text: str) -> str:
    if text.lstrip("rbfuRBFU").startswith(('"""', "'''")):
        return "\n".join(re.sub(r"(^|\s)//.*$", "", ln) for ln in text.splitlines())
    return text


def russian_literals(source: str) -> list[str]:
    return [f"{no}: {text.strip()[:100]}" for no, text, doc in _literals(source)
            if not doc and CYR.search(_visible(text))]


def test_country_split_reasons_are_english():
    found = russian_literals(SRC.read_text(encoding="utf-8"))
    assert not found, "русский текст в причинах отказа:\n" + "\n".join(found)


def test_the_guard_catches_a_russian_reason():
    sample = (
        'def f(x):\n'
        '    """Докстринг можно по-русски."""\n'
        '    js = """\n  // комментарий JS можно\n  return 1;\n"""\n'
        '    raise ValueError(f"{x} нет в карте аккаунтов")\n'
    )
    found = russian_literals(sample)
    assert len(found) == 1 and "нет в карте" in found[0]


def test_period_failures_read_in_english():
    from src.analysis_agent import country_split as cs

    class _Page:
        def evaluate(self, *_a, **_k):
            raise RuntimeError("boom")

    ok, note = cs._set_period(_Page(), "2026-08-16", "2026-08-16")
    assert ok is False and note.startswith("could not look for the period button")
