"""Текст падения остаётся на диске с первого раза.

За один день четыре теста мигнули: упали в полном прогоне и прошли
поодиночке. Каждый раз текста падения у нас не было — прогон запускали с
`| tail`, чтобы увидеть итог, и подробности уезжали вместе с прокруткой.
Разбор начинался с попытки воспроизвести, а мигающий тест на то и
мигающий, что воспроизводится не всегда: сегодняшний прогон, запущенный
специально ради текста, прошёл целиком.

Поэтому след оставляет сам прогон, без флагов, которые надо помнить.
Забытый флаг не работает ровно тогда, когда он нужнее всего.

Проверяется это настоящим прогоном pytest в отдельной папке: хуки
сохранения нельзя проверить, не запустив их.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFTEST = ROOT / "conftest.py"


def _run(tmp_path: Path, body: str) -> subprocess.CompletedProcess:
    """Прогон в своей папке: отчёт кладётся рядом с conftest, и здесь это
    временная папка, а не `output/` проекта."""
    (tmp_path / "conftest.py").write_text(CONFTEST.read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "test_sample.py").write_text(body, encoding="utf-8")
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "test_sample.py"],
        cwd=tmp_path, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


def _reports(tmp_path: Path) -> list[Path]:
    return sorted((tmp_path / "output" / "test-failures").glob("*.md"))


def test_the_failing_text_is_written_down(tmp_path):
    """Не «тест упал», а что именно он увидел: разбирают по этому."""
    _run(tmp_path, "def test_x():\n    got = {'Local', 'Stray'}\n    assert got == {'Local'}\n")
    files = _reports(tmp_path)
    assert files, "падение не оставило следа"
    text = files[-1].read_text(encoding="utf-8")
    assert "test_sample.py::test_x" in text, "не сказано, что именно упало"
    assert "Stray" in text, "в отчёте нет того, что тест увидел"
    assert "assert got ==" in text, "в отчёте нет строки, на которой упало"


def test_a_green_run_writes_nothing(tmp_path):
    """Иначе за месяц в папке сотня отчётов о том, что всё хорошо."""
    _run(tmp_path, "def test_x():\n    assert True\n")
    assert not (tmp_path / "output" / "test-failures").exists(), "зелёный прогон оставил отчёт"


def test_a_failure_in_setup_is_kept_too(tmp_path):
    """Падения в подготовке чаще других зависят от соседей по прогону —
    то есть ровно те, за которыми мы и охотимся."""
    body = (
        "import pytest\n"
        "@pytest.fixture\n"
        "def broken():\n"
        "    raise RuntimeError('подготовка развалилась')\n"
        "def test_x(broken):\n"
        "    assert True\n"
    )
    _run(tmp_path, body)
    files = _reports(tmp_path)
    assert files, "падение в подготовке следа не оставило"
    text = files[-1].read_text(encoding="utf-8")
    assert "подготовка развалилась" in text
    assert "setup" in text, "не сказано, что упало именно в подготовке"


def test_the_run_says_where_it_put_the_file(tmp_path):
    """Файл, о котором знают только те, кто о нём и так знал, — половина
    пользы."""
    result = _run(tmp_path, "def test_x():\n    assert False\n")
    assert "test-failures" in result.stdout, "прогон не сказал, куда сохранил"


def test_reporting_never_breaks_the_run(tmp_path):
    """Отчётность — служебное. Её поломка не имеет права менять итог
    прогона: иначе однажды зелёный прогон покраснеет из-за папки."""
    ok = _run(tmp_path, "def test_x():\n    assert True\n")
    assert ok.returncode == 0
    bad = _run(tmp_path, "def test_x():\n    assert False\n")
    assert bad.returncode == 1, "итог прогона изменился из-за отчётности"


def test_old_reports_do_not_pile_up():
    """Сотня отчётов за месяц — не архив, а мусор."""
    src = CONFTEST.read_text(encoding="utf-8")
    assert "_KEEP" in src and "def _trim" in src, "старые отчёты никто не убирает"


def test_the_reports_are_not_committed():
    """`output/` не коммитится — иначе чужие падения поедут в историю."""
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "output/*" in ignore, "папка отчётов попадёт в гит"
