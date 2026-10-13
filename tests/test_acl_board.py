"""Доска задач: разбор разметки, отметка по коммиту, сборка витрины."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts import acl_board  # noqa: E402


SAMPLE = """---
board: ACL
updated: 2026-09-07
---

# ACL — Task Board

Вводный текст, который не должен попасть в задачи.

```
- [ ] `acl-000` пример внутри забора #next
```

## 1 · Complete account control
> Аккаунт читается и управляется без человека.

### Check Spend & Metrics

- [x] `acl-101` Spend from the workbook #shipped @analysis
      Первая строка примечания.
      Вторая строка примечания.
- [ ] `acl-103` Live spend from the account #next @analysis

- [ ] `acl-104` Split into parts #next @dashboard needs:acl-112
  - [x] `acl-104a` Первая часть сделана
        Примечание подзадачи.
  - [ ] `acl-104b` Вторая часть needs:acl-111
  - [ ] Часть без id — коммитом не отметить

### Open/close ads/ad sets

- [ ] `acl-111` Pause an ad set #blocked @analysis
- [ ] `acl-112` Request ads_management #next @ops
  - [x] `acl-112a` Всё сделано, родитель закрыт сам собой

## 2 · Texts
> Новые материалы.

### Create new headlines

- [ ] `acl-501` Headlines from winners

## Log

"""


def board():
    return acl_board.parse(SAMPLE)


def test_review_promotes_to_merged_without_reopening_checkbox():
    review, _ = acl_board.apply_commit(SAMPLE, "abc1", "closes acl-103", branch="feature/example")
    merged, changed = acl_board.apply_commit(review, "abc2", "closes acl-103", branch="master")
    assert changed == ["acl-103"]
    assert acl_board.parse(merged).by_id("acl-103").shown_status == "merged"
    assert acl_board.apply_commit(merged, "abc2", "closes acl-103", branch="master") == (merged, [])
    assert acl_board.apply_commit(merged, "abc3", "closes acl-103", branch="feature/other") == (merged, [])


def test_parent_merges_only_when_all_review_children_have_landed():
    text = "## 1 · A\n### B\n- [x] `acl-001` Parent #review\n  - [x] `acl-001a` A #review\n  - [x] `acl-001b` B #review\n"
    first, _ = acl_board.apply_commit(text, "abc1", "closes acl-001a", branch="master")
    assert acl_board.parse(first).by_id("acl-001").shown_status == "review"
    second, _ = acl_board.apply_commit(first, "abc2", "closes acl-001b", branch="master")
    assert acl_board.parse(second).by_id("acl-001").shown_status == "merged"


def test_shipped_headline_does_not_include_completed_review():
    text = "## 1 · A\n### B\n- [x] `acl-001` Ready #review\n"
    rendered = acl_board.render(acl_board.parse(text))
    assert '<div class="big">0<i>/1</i></div>' in rendered


def test_areas_and_capabilities():
    b = board()
    assert [a.number for a in b.areas] == ["1", "2"]
    assert b.areas[0].title == "Complete account control"
    assert b.areas[0].blurb.startswith("Аккаунт читается")
    assert [c.title for c in b.areas[0].caps] == [
        "Check Spend & Metrics",
        "Open/close ads/ad sets",
    ]


def test_fenced_example_is_not_a_task():
    assert board().by_id("acl-000") is None


def test_title_has_no_markers_left():
    t = board().by_id("acl-101")
    assert t.title == "Spend from the workbook"
    assert t.status == "shipped"
    assert t.track == "analysis"
    assert t.is_done


def test_note_joins_indented_lines():
    assert board().by_id("acl-101").note == (
        "Первая строка примечания. Вторая строка примечания."
    )


def test_missing_status_defaults_to_idea():
    t = board().by_id("acl-501")
    assert t.status == "idea"
    assert not t.is_done


def test_blocked_survives_parsing():
    assert board().by_id("acl-111").status == "blocked"


def test_progress_counts_only_that_area():
    # acl-101 отмечен рукой, acl-112 закрыт единственной сделанной частью
    assert board().areas[0].progress == (2, 5)
    assert board().areas[1].progress == (0, 1)


def test_subtasks_hang_off_their_task():
    t = board().by_id("acl-104")
    assert [s.id for s in t.subs] == ["acl-104a", "acl-104b", ""]
    assert t.sub_progress == (1, 3)
    assert not t.is_done


def test_subtask_note_stays_on_the_subtask():
    b = board()
    assert b.by_id("acl-104a").note == "Примечание подзадачи."
    assert b.by_id("acl-104").note == ""


def test_all_parts_done_closes_the_task():
    t = board().by_id("acl-112")
    assert t.is_done and not t.done


def test_needs_is_parsed_and_leaves_the_title_clean():
    b = board()
    assert b.by_id("acl-104").needs == ["acl-112"]
    assert b.by_id("acl-104").title == "Split into parts"
    assert b.by_id("acl-104b").needs == ["acl-111"]
    assert b.by_id("acl-104b").title == "Вторая часть"


def test_subtask_inherits_the_track():
    assert board().by_id("acl-104a").track == "dashboard"


def test_by_id_finds_subtasks_but_area_progress_does_not_count_them():
    b = board()
    assert b.by_id("acl-104a") is not None
    assert b.parent_of("acl-104a").id == "acl-104"
    assert b.parent_of("acl-104") is None
    assert all(t.id in {"acl-101", "acl-103", "acl-104", "acl-111", "acl-112", "acl-501"}
               for t in b.tasks)


def test_commit_ticks_a_subtask_without_touching_the_parent():
    new, ticked = acl_board.apply_commit(SAMPLE, "abc1234", "acl-104b сделано")
    assert ticked == ["acl-104b"]
    b = acl_board.parse(new)
    assert b.by_id("acl-104b").is_done
    assert not b.by_id("acl-104").is_done  # часть без id ещё открыта


def test_last_part_closes_the_parent_too():
    once, _ = acl_board.apply_commit(SAMPLE, "abc1234", "acl-104b готово")
    text = once.replace("- [ ] Часть без id", "- [x] Часть без id")
    b = acl_board.parse(text)
    assert b.by_id("acl-104").is_done
    line = next(l for l in text.splitlines() if "`acl-104`" in l and l.startswith("- ["))
    assert line.startswith("- [ ]")  # в файле галочка не ставилась — её выводит доска


def test_parent_line_is_ticked_when_a_commit_closes_the_last_part():
    text = SAMPLE.replace("- [ ] Часть без id — коммитом не отметить",
                          "- [x] Часть без id — коммитом не отметить")
    new, ticked = acl_board.apply_commit(text, "abc1234", "acl-104b готово")
    # родитель теперь тоже в списке: он закончился этим коммитом, и без записи
    # в журнале его карточка не знала бы, на какой ветке искать работу
    assert ticked == ["acl-104b", "acl-104"]
    parent_line = next(
        l for l in new.splitlines() if "`acl-104`" in l and l.startswith("- [")
    )
    assert parent_line.startswith("- [x]")
    assert "#merged" in parent_line


def test_subtask_line_gets_no_status_tag():
    new, _ = acl_board.apply_commit(SAMPLE, "abc1234", "acl-104b готово")
    line = next(l for l in new.splitlines() if "`acl-104b`" in l)
    assert line.strip().startswith("- [x]")
    assert "#shipped" not in line


@pytest.mark.parametrize(
    "message, expected",
    [
        # закрывающее слово обязательно — с обеих сторон от id
        ("feat: живой спенд, closes acl-103", ["acl-103"]),
        ("Board: acl-103", ["acl-103"]),
        ("fix: acl-111 и acl-112 одним куском", ["acl-111", "acl-112"]),
        ("acl-103 закрыто", ["acl-103"]),
        ("acl-104b готово", ["acl-104b"]),
        ("сделано acl-104 и acl-104b", ["acl-104", "acl-104b"]),
        ("CLOSES ACL-103 в верхнем регистре", ["acl-103"]),
        ("closes acl-103 дважды closes acl-103", ["acl-103"]),
        # упоминания — не закрытия; на этом уже обожглись
        ("рефакторинг без задач", []),
        ("зависимость needs:acl-105 в любой строке", []),
        ("правил разбор acl-103, но не доделал", []),
        ("готовлюсь к acl-103", []),
        ("префикс не слово: prefix acl-103", []),
        # одна буква после номера — подзадача, две и больше — уже не id
        ("closes acl-103xy", []),
        ("closes acl-103-beta", []),
        ("тег релиза acl-release-2026-09-03e не задача", []),
    ],
)
def test_ids_in_commit(message, expected):
    assert acl_board.ids_in_commit(message) == expected


def test_apply_commit_ticks_and_logs():
    new, ticked = acl_board.apply_commit(
        SAMPLE, "abc1234", "feat: живой спенд, closes acl-103", when="2026-09-08"
    )
    assert ticked == ["acl-103"]
    task = acl_board.parse(new).by_id("acl-103")
    assert task.is_done and task.status == "merged"
    assert "- [x] `acl-103`" in new
    assert "`abc1234`" in new
    after_log = new.splitlines()[new.splitlines().index("## Log") + 1 :]
    first_entry = next(l for l in after_log if l.startswith("- "))
    assert first_entry.startswith("- 2026-09-08")
    assert "updated: 2026-09-08" in new


def test_apply_commit_replaces_the_old_status_tag():
    new, _ = acl_board.apply_commit(SAMPLE, "abc1234", "acl-111 закрыто")
    line = next(l for l in new.splitlines() if "`acl-111`" in l)
    assert "#blocked" not in line
    assert line.count("#merged") == 1


def test_apply_commit_is_idempotent():
    once, _ = acl_board.apply_commit(SAMPLE, "abc1234", "acl-103 готово")
    twice, ticked = acl_board.apply_commit(once, "def5678", "acl-103 готово")
    assert ticked == []
    assert twice == once


def test_unknown_id_changes_nothing():
    new, ticked = acl_board.apply_commit(SAMPLE, "abc1234", "closes acl-999 — такой задачи нет")
    assert ticked == [] and new == SAMPLE


def test_render_is_whole_and_escapes():
    text = SAMPLE.replace("Headlines from winners", "Headlines <b>& sons</b>")
    html = acl_board.render(acl_board.parse(text))
    assert "<title>ACL Task Wall</title>" in html
    assert "Headlines &lt;b&gt;&amp; sons&lt;/b&gt;" in html
    assert "Complete account control" in html
    assert html.count('class="area"') == 2
    assert "{" not in html.split("<style>")[0]


def test_render_marks_every_task_with_its_status():
    html = acl_board.render(board())
    for status in ("shipped", "next", "blocked", "idea"):
        assert f'data-status="{status}"' in html


def test_render_survives_an_empty_board():
    html = acl_board.render(acl_board.parse("# пусто\n"))
    assert "<title>" in html


def test_tilts_repeat_so_the_wall_does_not_jitter():
    a = [next(g) for g in [acl_board._tilts()] * 1 for _ in range(12)]
    b = []
    gen = acl_board._tilts()
    for _ in range(12):
        b.append(next(gen))
    assert a == b


def test_board_path_honours_the_env(monkeypatch, tmp_path):
    monkeypatch.setenv(acl_board.BOARD_ENV, str(tmp_path / "b.md"))
    assert acl_board.board_path() == tmp_path / "b.md"


def test_sync_without_the_vault_does_not_fail_the_commit(monkeypatch, tmp_path):
    monkeypatch.setenv(acl_board.BOARD_ENV, str(tmp_path / "нет-такого.md"))
    args = type("A", (), {"rev": "HEAD", "out": ""})()
    assert acl_board.cmd_sync(args, tmp_path) == 0


AMBIGUOUS = ("## 1 · Area\n### Cap\n"
             "- [ ] `acl-001` One\n"
             "  - [ ] `acl-002a` First duplicate\n"
             "- [ ] `acl-003` Two\n"
             "  - [ ] `acl-002a` Second duplicate\n"
             "- [ ] `acl-004` Unique\n")


@pytest.mark.parametrize("branch", ["master", "feature/example"])
def test_ambiguous_id_does_not_tick_or_log_either_task(branch):
    assert acl_board.apply_commit(
        AMBIGUOUS, "abc1234", "fix acl-002a", branch=branch,
    ) == (AMBIGUOUS, [])


def test_ambiguous_ids_do_not_block_unique_tasks_in_the_same_commit():
    new, ticked = acl_board.apply_commit(AMBIGUOUS, "abc1234", "fix acl-002a, acl-004")
    assert ticked == ["acl-004"]
    before_unique = AMBIGUOUS.split("- [ ] `acl-004`")[0]
    assert new.startswith(before_unique)
    assert "- [x] `acl-004` Unique #merged" in new


def test_unique_child_does_not_automatically_tick_an_ambiguous_parent():
    text = ("## 1 · Area\n### Cap\n"
            "- [ ] `acl-001` Parent one\n"
            "  - [ ] `acl-001a` Unique child\n"
            "- [ ] `acl-001` Parent two\n")
    new, ticked = acl_board.apply_commit(text, "abc1234", "fix acl-001a, acl-001")
    assert ticked == ["acl-001a"]
    assert "- [x] `acl-001a` Unique child" in new
    assert "- [ ] `acl-001` Parent one" in new
    assert "- [ ] `acl-001` Parent two" in new


def test_sync_warns_and_preserves_board_with_only_ambiguous_ids(monkeypatch, tmp_path, capsys):
    from argparse import Namespace
    src = tmp_path / "board.md"
    src.write_text(AMBIGUOUS, encoding="utf-8")
    monkeypatch.setenv(acl_board.BOARD_ENV, str(src))
    monkeypatch.setenv(acl_board.OUT_ENV, str(tmp_path / "wall.html"))
    monkeypatch.setattr(acl_board, "head_commit", lambda *args: ("abc1234", "fix acl-002a"))
    assert acl_board.cmd_sync(Namespace(rev="HEAD", branch="master"), tmp_path) == 0
    assert src.read_text(encoding="utf-8") == AMBIGUOUS
    output = capsys.readouterr().out
    assert "пропущены" in output and "acl-002a" in output
    assert "уже в этом состоянии" not in output
    assert (tmp_path / "wall.html").is_file()


# ── галочка и статус — про разное ────────────────────────────────────


BRANCHED = """## 1 · Область
### Способность

- [x] `acl-201` Готово, но живёт в ветке #review @dashboard
- [x] `acl-202` Готово и роздано
- [ ] `acl-203` Ещё нет #next
  - [x] `acl-203a` Часть готова в ветке #review

## Log

"""


def test_a_ticked_task_keeps_the_status_written_by_hand():
    b = acl_board.parse(BRANCHED)
    t = b.by_id("acl-201")
    assert t.is_done, "работа сделана"
    assert t.shown_status == "review", "но не влита — доска не должна врать"


def test_a_ticked_task_without_a_tag_is_shipped():
    assert acl_board.parse(BRANCHED).by_id("acl-202").shown_status == "shipped"


def test_all_parts_done_does_not_override_a_hand_written_status():
    sub = acl_board.parse(BRANCHED).by_id("acl-203a")
    assert sub.is_done and sub.shown_status == "review"


def test_counts_use_the_shown_status():
    counts = acl_board.parse(BRANCHED).counts()
    assert counts["review"] == 1 and counts["shipped"] == 1


def test_a_commit_on_a_feature_branch_marks_review_not_shipped():
    new, ticked = acl_board.apply_commit(
        SAMPLE, "abc1234", "closes acl-103", branch="feature/timeframe"
    )
    assert ticked == ["acl-103"]
    line = next(l for l in new.splitlines() if "`acl-103`" in l)
    assert "#review" in line and "#shipped" not in line
    assert acl_board.parse(new).by_id("acl-103").shown_status == "review"


def test_a_commit_on_master_marks_merged():
    new, _ = acl_board.apply_commit(SAMPLE, "abc1234", "closes acl-103", branch="master")
    line = next(l for l in new.splitlines() if "`acl-103`" in l)
    assert "#merged" in line


def test_the_log_says_which_branch_when_it_is_not_master():
    new, _ = acl_board.apply_commit(
        SAMPLE, "abc1234", "closes acl-103", when="2026-09-07", branch="feature/x"
    )
    entry = next(l for l in new.splitlines() if l.startswith("- 2026-09-07"))
    assert "`feature/x`" in entry
    on_master, _ = acl_board.apply_commit(
        SAMPLE, "abc1234", "closes acl-103", when="2026-09-07", branch="master"
    )
    assert "feature" not in next(
        l for l in on_master.splitlines() if l.startswith("- 2026-09-07")
    )


def test_render_shows_review_on_a_ticked_task():
    html = acl_board.render(acl_board.parse(BRANCHED))
    assert 'data-status="review"' in html
    assert "In review" in html


def test_a_subtask_closed_on_a_branch_says_review():
    """«Сделано» и «доехало» — разные вещи, и галочка второе не несёт."""
    new, ticked = acl_board.apply_commit(
        SAMPLE, "abc1234", "closes acl-104b", branch="feature/x"
    )
    assert ticked == ["acl-104b"]
    line = next(l for l in new.splitlines() if "`acl-104b`" in l)
    assert line.strip().startswith("- [x]")
    assert "#review" in line
    assert acl_board.parse(new).by_id("acl-104b").shown_status == "review"


def test_a_subtask_closed_on_master_has_an_explicit_merged_tag():
    new, _ = acl_board.apply_commit(SAMPLE, "abc1234", "closes acl-104b", branch="master")
    line = next(l for l in new.splitlines() if "`acl-104b`" in l)
    assert "#merged" in line
    assert acl_board.parse(new).by_id("acl-104b").shown_status == "merged"


def test_filter_preserves_expanded_areas(tmp_path):
    import re
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required for the generated board script")
    script = tmp_path / "board.js"
    script.write_text(re.search(r"<script>(.*?)</script>", acl_board.render(board()), re.S).group(1), encoding="utf-8")
    subprocess.run([node, str(Path(__file__).with_name("board_filter_harness.cjs")), str(script)], check=True, capture_output=True, text=True)


def test_hook_finds_neighbor_worktree_with_spaces_and_unicode(tmp_path):
    import os
    import shutil
    import subprocess
    shell, git = shutil.which("sh"), shutil.which("git")
    if not shell or not git:
        pytest.skip("Git and sh required")
    primary = tmp_path / "production"
    neighbor = tmp_path / "ACL задачи with spaces"
    def run(*args):
        return subprocess.run([git, "-C", str(primary), *args], check=True, capture_output=True, text=True)
    primary.mkdir()
    run("init")
    run("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "--allow-empty", "-m", "fixture")
    run("worktree", "add", "-b", "board", str(neighbor))
    (neighbor / "scripts").mkdir()
    (neighbor / "scripts/acl_board.py").write_text("# fixture", encoding="utf-8")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "python"
    fake.write_text('#!/bin/sh\nprintf "SCRIPT=%s\\n" "$1"\n', encoding="utf-8")
    fake.chmod(0o755)
    result = subprocess.run([shell, str(Path(acl_board.__file__).parent / "hooks/post-commit")], cwd=primary,
                            env=dict(os.environ, PATH=str(bindir) + os.pathsep + os.environ["PATH"]),
                            check=True, capture_output=True, text=True, encoding="utf-8")
    assert "SCRIPT=" + str(neighbor / "scripts/acl_board.py").replace("\\", "/") in result.stdout.replace("\\", "/")  # linux-ok: normalises a separator in the output, runs on any OS
