"""Журнал не теряет записи, когда в него пишут одновременно.

Проверки здесь **выполняют** запись в несколько писателей и считают строки, а
не осматривают код. 15.09 это было единственным способом увидеть поломку:
формат журнала выбран правильно, обоснование в докстроке верное — и всё равно
на Windows терялась каждая шестая запись, потому что там `open(path, "a")`
эмулируется как «перейти в конец, затем записать».

Проверки падают на прежнем способе записи и проходят на `acl_log.append_line`.
Первая из них — `test_plain_append_loses_lines_on_this_platform` — держит
именно это различие: если однажды платформа станет честной, она об этом
скажет, и модуль можно будет упростить.
"""
from __future__ import annotations

import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.acl_log import JournalWriteError, append_line

WRITERS = 12
PER_WRITER = 60
EXPECTED = WRITERS * PER_WRITER


def _record(who: int, n: int) -> str:
    # Запись примерно того же размера, что настоящая: у `placements` отчёт
    # доходил до шести килобайт, у обычного шага — сотни байт.
    return json.dumps({"who": who, "n": n, "diag": "x" * 400}, ensure_ascii=False)


def _run(writer, path: Path) -> tuple[int, int]:
    """Запустить писателей одновременно и вернуть (целых строк, битых)."""
    gate = threading.Barrier(WRITERS, timeout=60)

    def work(i: int) -> None:
        gate.wait()
        for k in range(PER_WRITER):
            writer(path, _record(i, k))

    with ThreadPoolExecutor(max_workers=WRITERS) as pool:
        list(pool.map(work, range(WRITERS)))

    good = bad = 0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line:
            continue
        try:
            json.loads(line)
            good += 1
        except ValueError:
            bad += 1
    return good, bad


def _plain_append(path: Path, line: str) -> None:
    """Как писали журналы до 15.09."""
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def test_the_journal_keeps_every_line_under_concurrent_writers(tmp_path):
    """Главная проверка: ни одна запись не пропала и ни одна не порвалась."""
    path = tmp_path / "journal.jsonl"
    good, bad = _run(lambda p, l: append_line(p, l), path)
    assert bad == 0, f"{bad} строк склеились — писатели наложились друг на друга"
    assert good == EXPECTED, f"потеряно {EXPECTED - good} записей из {EXPECTED}"


@pytest.mark.skipif(sys.platform != "win32", reason="потеря воспроизводится на Windows")
def test_plain_append_loses_lines_on_this_platform(tmp_path):
    """Прежний способ действительно теряет записи — иначе чинить нечего.

    Эта проверка охраняет смысл модуля. Если она однажды станет зелёной,
    значит платформа (или Python) перестала эмулировать дописывание, и
    `acl_log` можно свести к обычному `open(..., "a")`.
    """
    path = tmp_path / "plain.jsonl"
    good, _ = _run(_plain_append, path)
    assert good < EXPECTED, (
        "прежний способ ничего не потерял — перечитать acl_log: "
        "возможно, замок больше не нужен"
    )


def test_a_line_without_a_newline_does_not_glue_to_the_next(tmp_path):
    """Строка без перевода склеилась бы со следующей — та же потеря, но видимая."""
    path = tmp_path / "journal.jsonl"
    append_line(path, '{"n": 1}')
    append_line(path, '{"n": 2}\n')
    assert path.read_text(encoding="utf-8").splitlines() == ['{"n": 1}', '{"n": 2}']


def test_the_parent_directory_is_created(tmp_path):
    """Журнал пишется до того, как кто-либо создал `output/`."""
    path = tmp_path / "нет" / "такой" / "папки" / "journal.jsonl"
    append_line(path, '{"n": 1}')
    assert path.exists()


def test_a_write_failure_is_not_swallowed(tmp_path):
    """Отказ записи должен быть слышен.

    Прежде оба журнала глушили любую ошибку через `except Exception: pass`:
    нехватка места, отсутствие прав и занятый файл выглядели одинаково —
    никак. Вызывающий волен не падать из-за журнала, но это должно быть его
    решением.
    """
    # Путь, который не может быть файлом: на его месте папка.
    busy = tmp_path / "занято"
    busy.mkdir()
    with pytest.raises(JournalWriteError):
        append_line(busy, '{"n": 1}')


# ── между процессами ──────────────────────────────────────────────────────
#
# Замок в памяти сюда не дотягивается: у каждого процесса он свой. Проверяем
# отдельно, потому что этот случай настоящий — дешборд и запуск из командной
# строки пишут в один журнал, и 15.09 так терялось 149 записей из 480.

_CHILD = """
import sys
sys.path.insert(0, sys.argv[1])
from src.acl_log import append_line
from pathlib import Path
import json
path, who, per = Path(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4])
for k in range(per):
    append_line(path, json.dumps({"who": who, "n": k, "diag": "x" * 400}))
"""

PROCS = 6
PROC_PER = 40


def test_separate_processes_do_not_overwrite_each_other(tmp_path):
    import subprocess

    child = tmp_path / "child.py"
    child.write_text(_CHILD, encoding="utf-8")
    path = tmp_path / "shared.jsonl"

    running = [
        subprocess.Popen(
            [sys.executable, str(child), str(ROOT), str(path), str(i), str(PROC_PER)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        for i in range(PROCS)
    ]
    for p in running:
        _, err = p.communicate(timeout=180)
        assert p.returncode == 0, err.decode("utf-8", "replace")[:400]

    good = bad = 0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line:
            continue
        try:
            json.loads(line)
            good += 1
        except ValueError:
            bad += 1
    assert bad == 0, f"{bad} строк склеились между процессами"
    assert good == PROCS * PROC_PER, f"потеряно {PROCS * PROC_PER - good} записей"
