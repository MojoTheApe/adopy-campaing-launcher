"""Копия доски в репозитории: у плана работ появляется история.

Источник живёт в Obsidian, у которого нет ни гита, ни корзины. 8 сентября одна
неудачная правка вырезала из доски целую область — восемь задач, — и
восстановилось это только потому, что незадолго до того была опубликована
витрина, а в её HTML осталось содержимое. Удача, а не запасной план.

Копия пишется сборкой и **не является источником**: читать её как истину
нельзя. Она нужна ровно для одного — чтобы у плана были история и те же три
адреса, что у кода.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import acl_board as b  # noqa: E402


def test_the_snapshot_is_written_beside_the_code(tmp_path):
    out = b.write_snapshot(tmp_path, "# доска\n")
    assert out == tmp_path / b.SNAPSHOT
    assert out.read_text(encoding="utf-8") == "# доска\n"


def test_an_unchanged_board_does_not_touch_the_file(tmp_path):
    """Иначе копия висит в `git status` на каждом коммите.

    Человек перестанет читать это место, и настоящее изменение затеряется
    среди ложных.
    """
    b.write_snapshot(tmp_path, "# доска\n")
    before = (tmp_path / b.SNAPSHOT).stat().st_mtime_ns
    assert b.write_snapshot(tmp_path, "# доска\n") is None, "файл переписан впустую"
    assert (tmp_path / b.SNAPSHOT).stat().st_mtime_ns == before


def test_a_changed_board_is_written_and_reported(tmp_path):
    b.write_snapshot(tmp_path, "# доска\n")
    out = b.write_snapshot(tmp_path, "# доска\n- новая задача\n")
    assert out is not None, "изменение не отмечено — человек не узнает, что коммитить"
    assert "новая задача" in out.read_text(encoding="utf-8")


def test_a_failure_does_not_break_the_build(tmp_path):
    """Копия важна, но не важнее сборки витрины.

    Каталог вместо файла — заведомо негодная цель.
    """
    target = tmp_path / b.SNAPSHOT
    target.parent.mkdir(parents=True)
    target.mkdir()
    assert b.write_snapshot(tmp_path, "# доска\n") is None


def test_rendering_leaves_an_exact_copy(tmp_path, monkeypatch):
    """Копия совпадает с источником посимвольно.

    Копия «почти как источник» хуже отсутствия: по ней восстановят не то, что
    было, и заметят это не сразу.
    """
    source = tmp_path / "board.md"
    text = (
        "## 1 · Area\n\n> Зачем она.\n\n### Cap\n\n"
        "- [x] `acl-001` Сделано #shipped @dashboard\n"
        "      Примечание с «кавычками» и — тире.\n"
    )
    source.write_text(text, encoding="utf-8")
    monkeypatch.setenv(b.BOARD_ENV, str(source))

    repo = tmp_path / "repo"
    (repo / "output").mkdir(parents=True)

    class _Args:
        out = str(repo / "output" / "wall.html")

    assert b.cmd_render(_Args(), repo) == 0
    assert b.snapshot_path(repo).read_text(encoding="utf-8") == text


def test_the_snapshot_lives_in_the_repository_not_outside(tmp_path):
    """Путь относительный: копия должна попадать под гит, а не рядом с ним."""
    assert not b.SNAPSHOT.is_absolute()
    assert b.snapshot_path(tmp_path).is_relative_to(tmp_path)
