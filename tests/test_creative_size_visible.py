"""Вес креатива виден при выборе, а не через десять минут загрузки.

10 сентября в одной папке лежали файлы от 30 КБ до 1.5 ГБ, и по имени они не
отличались ничем. Байер выбирает вслепую, а узнаёт о своём выборе тогда, когда
загрузка уже идёт.

**Времени здесь намеренно нет.** Замеры того же дня: три файла одной кампании
были 24-26 МБ, а грузились 537, 326, 62 и 90 секунд — восьмикратный разброс
при одинаковом весе. Оценка «столько-то минут» была бы выдуманным числом:
размер её не предсказывает. Показываем то, что знаем точно.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TPL = ROOT / "src" / "web" / "templates" / "report.html"


# ── сервер отдаёт вес ─────────────────────────────────────────────────────

@pytest.fixture
def creatives(tmp_path, monkeypatch):
    from src.web import app as webapp

    (tmp_path / "sub").mkdir()
    (tmp_path / "small.mp4").write_bytes(b"x" * 2048)
    (tmp_path / "sub" / "big.mp4").write_bytes(b"x" * 5000)
    (tmp_path / "pic.jpg").write_bytes(b"x" * 10)
    monkeypatch.setattr(webapp, "get_creatives_root", lambda: str(tmp_path))
    return tmp_path


def _files() -> dict:
    """Так же, как соседние проверки этого репозитория: зовём обработчик прямо."""
    import asyncio
    import json as _json

    from src.web import app as webapp

    body = asyncio.run(webapp.api_creatives()).body
    return {f["filename"]: f for f in _json.loads(body)["files"]}


def test_every_file_carries_its_size(creatives):
    by_name = _files()
    assert by_name["small.mp4"]["size_bytes"] == 2048
    assert by_name["sub"[:0] + "big.mp4"]["size_bytes"] == 5000
    assert by_name["pic.jpg"]["size_bytes"] == 10


def test_a_file_that_cannot_be_measured_does_not_break_the_list(creatives, monkeypatch):
    """Список файлов важнее одного размера: пропасть должен размер, не список."""
    real = Path.stat

    def _boom(self, *a, **k):
        if self.name == "small.mp4":
            raise OSError("нет доступа")
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "stat", _boom)
    by_name = _files()
    assert by_name["small.mp4"]["size_bytes"] == 0
    assert by_name["big.mp4"]["size_bytes"] == 5000


# ── страница его показывает ───────────────────────────────────────────────

def _js() -> str:
    src = TPL.read_text(encoding="utf-8")
    start = src.index("window.cbSizeTag = function")
    return src[start : src.index("};", start) + 2]


def test_the_picker_shows_the_size_next_to_the_name():
    src = TPL.read_text(encoding="utf-8")
    row = src[src.index("var html = matches.map"):]
    row = row[: row.index("}).join('');")]
    assert "cbSizeTag(f.size_bytes)" in row, "вес не попадает в строку выбора"


def test_megabytes_below_a_gigabyte_and_gigabytes_above():
    js = _js()
    assert "GB" in js and "MB" in js
    assert "mb >= 1000" in js, "гигабайтные файлы показываются как «1500 MB»"


def test_a_heavy_file_is_marked_apart():
    """Двадцать мегабайт и полтора гигабайта не должны выглядеть одинаково."""
    js = _js()
    assert "CB_HEAVY_MB" in js
    assert "'warn' : 'muted'" in js, "тяжёлый файл ничем не выделен"


def test_no_time_estimate_is_promised():
    """Сторож на честность: размер не предсказывает время, и обещать его нельзя.

    Разброс 10 сентября — восьмикратный при одинаковом весе. Строка «≈9 минут»
    выглядела бы знанием, которого у нас нет.
    """
    js = _js()
    for word in ("min", "sec", "мин", "сек", "ETA", "estimate"):
        assert word not in js, f"в подписи появилась оценка времени ({word!r})"


def test_a_file_with_no_size_shows_nothing():
    """Ноль означает «не смогли измерить», а не «файл пустой»."""
    assert "if (!bytes) return '';" in _js()


def test_the_escaping_of_the_name_is_untouched():
    """Имя файла приходит с диска и в разметку идёт только экранированным."""
    src = TPL.read_text(encoding="utf-8")
    row = src[src.index("var html = matches.map"):]
    row = row[: row.index("}).join('');")]
    assert len(re.findall(r"aclEscHtml\(", row)) >= 3
