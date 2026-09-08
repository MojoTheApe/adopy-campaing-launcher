"""Скрипты отчёта должны разбираться браузером.

Ошибка, ради которой написан этот файл: 8 сентября в строку подсказки попал
настоящий перевод строки внутри кавычек. Одна испорченная строковая константа
роняет весь блок `<script>` — и вместе с ним перестаёт открываться календарь,
хотя правка была совсем про другое. На экране это выглядит как «даты больше не
кликабельны», и по такому описанию причину не угадать.

Ни один тест этого не поймал. Проверки шаблона искали текст, а текст был на
месте: сломан был синтаксис. Здесь страница отдаётся настоящему разборщику
JavaScript — тому же, что стоит в браузере.

Проверять надо именно отрисованную страницу: интересующий блок содержит Jinja,
и разбирать шаблон как есть бессмысленно.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

_SCRIPT = re.compile(
    r"<script(?![^>]*\bsrc=)(?![^>]*type=\"application/json\")[^>]*>(.*?)</script>",
    re.S,
)


def _rendered() -> str:
    # Отрисовка берётся у соседнего теста, а не собирается заново: контекст
    # шаблона растёт, и всякая рукописная копия рано или поздно падает на
    # ключе, добавленном последним, вместо того, что проверяется.
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from test_web_drill import _render_html

    return _render_html({"countries": []})


def _node() -> str | None:
    return shutil.which("node")


@pytest.mark.skipif(_node() is None, reason="нет node — разобрать скрипты нечем")
def test_every_script_in_the_rendered_page_parses():
    blocks = _SCRIPT.findall(_rendered())
    assert blocks, "в отрисованной странице не нашлось ни одного скрипта"

    broken = []
    for i, body in enumerate(blocks):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / f"block{i}.js"
            f.write_text(body, encoding="utf-8")
            r = subprocess.run(
                [_node(), "--check", str(f)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            if r.returncode != 0:
                head = (r.stderr or "").strip().splitlines()[:6]
                broken.append(f"блок {i}:\n" + "\n".join(head))

    assert not broken, (
        "страница отдаёт браузеру нерабочий JavaScript — сломанный блок "
        "выключает всё, что в нём объявлено:\n\n" + "\n\n".join(broken)
    )
