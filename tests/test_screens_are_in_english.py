"""Everything the dashboard shows is in English (ACL-1462).

01.10 владелец увидел русский текст в окне перед Publish. Правило проекта —
всё, что видит человек, по-английски; комментарии в коде остаются русскими.
Сторож ищет кириллицу в шаблонах вне комментариев. Разрешены только проверки,
которые узнают старые сохранённые ошибки со словом «профил».
"""
from __future__ import annotations

import re
from pathlib import Path

TEMPLATES = Path(__file__).resolve().parents[1] / "src" / "web" / "templates"
CYR = re.compile(r"[А-Яа-яЁё]")
#: Не текст для человека, а распознавание старых русских ошибок в хранилище.
ALLOWED = (re.compile(r"/профил\|profile/i\.test"), re.compile(r"'профил' in sr\.error"))


def _blank(m: re.Match) -> str:
    return "\n" * m.group(0).count("\n")


def _visible_lines(text: str):
    text = re.sub(r"\{#.*?#\}", _blank, text, flags=re.S)
    text = re.sub(r"<!--.*?-->", _blank, text, flags=re.S)
    text = re.sub(r"/\*.*?\*/", _blank, text, flags=re.S)
    for no, line in enumerate(text.splitlines(), 1):
        yield no, re.sub(r"(^|\s)//.*$", "", line)


def test_no_russian_text_reaches_the_screen():
    found = []
    for tpl in sorted(TEMPLATES.glob("*.html")):
        for no, line in _visible_lines(tpl.read_text(encoding="utf-8")):
            if CYR.search(line) and not any(a.search(line) for a in ALLOWED):
                found.append(f"{tpl.name}:{no}: {line.strip()[:100]}")
    assert not found, "русский текст на экране:\n" + "\n".join(found)


def test_the_publish_popup_speaks_english():
    page = (TEMPLATES / "report.html").read_text(encoding="utf-8")
    assert "Do not lock the screen while the build runs" in page
    assert "confirmLabel: (warn || lostTargeting.length) ? 'Build anyway'" in page
