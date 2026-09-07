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
    assert ticked == ["acl-104b"]
    parent_line = next(
        l for l in new.splitlines() if "`acl-104`" in l and l.startswith("- [")
    )
    assert parent_line.startswith("- [x]")
    assert "#shipped" in parent_line


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
    assert task.is_done and task.status == "shipped"
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
    assert line.count("#shipped") == 1


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
