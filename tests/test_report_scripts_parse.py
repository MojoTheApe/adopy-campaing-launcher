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

#: сам шаблон — две проверки ниже смотрят на исходник, а не на отрисовку:
#: мусор перед доктайпом виден именно в файле
TPL = Path(__file__).resolve().parent.parent / "src" / "web" / "templates" / "report.html"

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


def test_the_template_starts_with_the_doctype():
    """Ничего не должно стоять перед `<!DOCTYPE html>`.

    Сегодня туда попали тридцать три строки JavaScript, и браузер показал их
    текстом поверх дешборда. Причина в моём же способе правки: срез шаблона
    оказался пустым, а `str.replace("", новое, 1)` молча вставляет текст в
    начало файла — тихая правка не туда, без единой жалобы.

    Прошлый сторож на разбор скриптов этого не поймал: мусор лежал **вне**
    `<script>`, а он проверяет содержимое скриптов. Проверка была и случай не
    покрывала — поэтому здесь проверяется само начало файла.
    """
    raw = TPL.read_text(encoding="utf-8")
    assert raw.lstrip().startswith("<!DOCTYPE html>"), (
        "перед доктайпом что-то появилось: " + raw[:200]
    )


def test_no_stray_javascript_outside_a_script_tag():
    """То же самое, но по существу: код в тексте страницы."""
    import re as _re

    raw = TPL.read_text(encoding="utf-8")
    without = _re.sub(r"<script[^>]*>.*?</script>", "", raw, flags=_re.S)
    for needle in ("var list =", "function render(", "box.innerHTML ="):
        assert needle not in without, f"«{needle}» оказалось вне <script>"
