"""ACL-2037: фрагменты CHANGELOG сворачиваются в общий файл без конфликтов."""
from pathlib import Path

import pytest

from scripts import fold_changelog as fc

ROOT = Path(__file__).resolve().parents[1]


def _tree(tmp_path, existing="## ACL-1 — Old — 2026-01-01\n\nOld text.\n"):
    (tmp_path / fc.FRAGMENTS).mkdir(parents=True)
    (tmp_path / fc.CHANGELOG).write_text(fc.HEADER + existing, encoding="utf-8")
    return tmp_path


def _fragment(root, number, date, title="Thing"):
    path = root / fc.FRAGMENTS / f"ACL-{number}.md"
    path.write_text(f"## ACL-{number} — {title} — {date}\n\nBody {number}.\n", encoding="utf-8")
    return path


def test_fold_orders_newest_first_and_removes_fragments(tmp_path):
    root = _tree(tmp_path)
    a = _fragment(root, 10, "2026-10-18")
    b = _fragment(root, 11, "2026-10-19")
    c = _fragment(root, 12, "2026-10-19")
    assert fc.fold(root) == ["ACL-12", "ACL-11", "ACL-10"]
    text = (root / fc.CHANGELOG).read_text(encoding="utf-8")
    assert text.startswith(fc.HEADER + "## ACL-12 — ")
    assert text.index("ACL-12 —") < text.index("ACL-11 —") < text.index("ACL-10 —") < text.index("ACL-1 —")
    assert not any(p.exists() for p in (a, b, c))


def test_fold_without_fragments_changes_nothing(tmp_path):
    root = _tree(tmp_path)
    before = (root / fc.CHANGELOG).read_text(encoding="utf-8")
    assert fc.fold(root) == []
    assert (root / fc.CHANGELOG).read_text(encoding="utf-8") == before


def test_readme_is_not_a_fragment(tmp_path):
    root = _tree(tmp_path)
    (root / fc.FRAGMENTS / "README.md").write_text("# readme\n", encoding="utf-8")
    assert fc.fold(root) == []
    assert (root / fc.FRAGMENTS / "README.md").exists()


def test_check_does_not_modify(tmp_path):
    root = _tree(tmp_path)
    path = _fragment(root, 10, "2026-10-18")
    assert fc.fold(root, check=True) == ["ACL-10"]
    assert path.exists()
    assert "ACL-10" not in (root / fc.CHANGELOG).read_text(encoding="utf-8")


def test_rerun_does_not_duplicate_entry(tmp_path):
    root = _tree(tmp_path, "## ACL-10 — Thing — 2026-10-18\n\nBody 10.\n")
    _fragment(root, 10, "2026-10-18")
    fc.fold(root)
    assert (root / fc.CHANGELOG).read_text(encoding="utf-8").count("## ACL-10 — ") == 1


def test_new_entry_for_already_listed_task_is_kept(tmp_path):
    root = _tree(tmp_path, "## ACL-5 — Old — 2026-01-01\n\nOld.\n")
    path = root / fc.FRAGMENTS / "ACL-5-followup.md"
    path.write_text("## ACL-5 — Follow-up — 2026-02-01\n\nMore.\n", encoding="utf-8")
    fc.fold(root)
    text = (root / fc.CHANGELOG).read_text(encoding="utf-8")
    assert "Follow-up" in text and "Old." in text and not path.exists()


def test_bom_crlf_fragment_and_lf_output(tmp_path):
    root = _tree(tmp_path)
    raw = "\ufeff## ACL-7 — T — 2026-10-01\r\n\r\nB.\r\n".encode("utf-8")
    (root / fc.FRAGMENTS / "ACL-7.md").write_bytes(raw)
    fc.fold(root)
    assert b"\r\n" not in (root / fc.CHANGELOG).read_bytes()
    assert "## ACL-7 — T" in (root / fc.CHANGELOG).read_text(encoding="utf-8")


def test_missing_changelog_is_a_clean_error(tmp_path):
    root = _tree(tmp_path)
    _fragment(root, 3, "2026-10-01")
    (root / fc.CHANGELOG).unlink()
    assert fc.main(["--root", str(root)]) == 1


@pytest.mark.parametrize("name,content", [
    ("notes.md", "## ACL-1 — T — 2026-01-01\n"),
    ("ACL-5.md", "## ACL-6 — T — 2026-01-01\n"),
    ("ACL-5.md", "# no heading\n"),
    ("ACL-5.md", "## ACL-5 — T — yesterday\n"),
])
def test_bad_fragments_are_rejected_and_nothing_is_written(tmp_path, name, content):
    root = _tree(tmp_path)
    before = (root / fc.CHANGELOG).read_text(encoding="utf-8")
    (root / fc.FRAGMENTS / name).write_text(content, encoding="utf-8")
    with pytest.raises(ValueError):
        fc.fold(root)
    assert (root / fc.CHANGELOG).read_text(encoding="utf-8") == before


def test_main_reports_error_exit_code(tmp_path, capsys):
    root = _tree(tmp_path)
    (root / fc.FRAGMENTS / "bad.md").write_text("x", encoding="utf-8")
    assert fc.main(["--root", str(root)]) == 1
    assert "fold_changelog" in capsys.readouterr().err


def test_committed_fragments_are_valid_and_changelog_has_header():
    fc.read_fragments(ROOT / fc.FRAGMENTS)
    assert (ROOT / fc.CHANGELOG).read_text(encoding="utf-8").startswith(fc.HEADER)
