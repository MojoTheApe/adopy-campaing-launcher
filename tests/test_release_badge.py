"""Значок версии в шапке: выпуск вместо ветки stage (ACL-2638).

Сборку из исходников сравниваем с последним выпуском acl-releases: равна —
имя выпуска; впереди — «+N tasks not in release» с подсказкой; тестовая сборка
с ветками задач — выпуск и id задач; выпуск неизвестен — «release unknown».
Сеть замокана, отрисовка шапки сети не ждёт.
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.web import acl_update as u  # noqa: E402

TAG = "acl-release-2026-11-14.1636"
NAME = "2026-11-14.1636"


def _run(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _commit(repo, name, msg):
    (repo / name).write_text(msg + "\n")
    _run(repo, "add", name)
    _run(repo, "commit", "-q", "-m", msg)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """Клон: main проекта = выпуск, tag на нём; поверх — свои правки."""
    _run(tmp_path, "init", "-q")
    _run(tmp_path, "config", "user.email", "t@example.com")
    _run(tmp_path, "config", "user.name", "t")
    _run(tmp_path, "config", "commit.gpgsign", "false")
    _commit(tmp_path, "a.txt", "base")
    _run(tmp_path, "tag", TAG)
    _run(tmp_path, "remote", "add", "work", "https://github.com/" + u.PROJECT_REPO + ".git")
    _run(tmp_path, "update-ref", "refs/remotes/work/main", "HEAD")
    monkeypatch.setattr(u, "_ROOT", tmp_path)
    monkeypatch.setattr(u, "_badge_state", {"checked": None, "fetching": False, "last": None})
    monkeypatch.setattr(u, "_latest_release_tag", lambda: TAG)
    return tmp_path


def _main_moves_on(repo):
    _run(repo, "update-ref", "refs/remotes/work/main", "HEAD")


def test_the_project_remote_is_found_by_its_url_not_its_name(repo):
    _run(repo, "remote", "add", "origin", "https://github.com/JenyaFarber/fb_agents.git")
    assert u._project_remote() == "work"


@pytest.mark.parametrize("url", [
    "git@github.com:MojoTheApe/adopy-campaing-launcher.git",
    "https://github.com/mojotheape/Adopy-Campaing-Launcher/",
])
def test_the_remote_is_found_in_other_url_forms(repo, url):
    _run(repo, "remote", "set-url", "work", url)
    assert u._project_remote() == "work"


def test_a_build_equal_to_the_release_shows_its_name(repo):
    got = u._compare(TAG)
    assert got["known"] and got["kind"] == "equal"
    assert got["text"] == f"release {NAME}"


def test_a_build_ahead_by_tasks_counts_tasks_and_lists_them(repo):
    _commit(repo, "b.txt", "[ACL-2611] Fix the thing")
    _commit(repo, "c.txt", "feat(x): second part (ACL-2611)")
    _commit(repo, "d.txt", "ACL-2629 — Another one")
    _main_moves_on(repo)
    got = u._compare(TAG)
    assert got["kind"] == "ahead"
    assert got["text"] == "+2 tasks not in release"
    assert got["tasks"] == ["ACL-2611", "ACL-2629"]
    assert "ACL-2611 — second part" in got["tooltip"]
    assert "ACL-2629 — Another one" in got["tooltip"]


def test_one_task_is_singular(repo):
    _commit(repo, "b.txt", "[ACL-7] One")
    _main_moves_on(repo)
    assert u._compare(TAG)["text"] == "+1 task not in release"


def test_commits_are_counted_only_when_no_task_is_named(repo):
    _commit(repo, "b.txt", "tidy things")
    _commit(repo, "c.txt", "tidy more")
    _main_moves_on(repo)
    got = u._compare(TAG)
    assert got["text"] == "+2 commits not in release" and got["tasks"] == []


def test_merge_commits_do_not_count(repo):
    _run(repo, "checkout", "-q", "-b", "side")
    _commit(repo, "b.txt", "[ACL-5] Side work")
    _run(repo, "checkout", "-q", "-")
    _commit(repo, "e.txt", "main side")
    _run(repo, "merge", "-q", "--no-ff", "-m", "Merge ACL-999 batch", "side")
    _main_moves_on(repo)
    got = u._compare(TAG)
    assert got["tasks"] == ["ACL-5"]


def test_a_test_build_shows_the_release_plus_the_extra_tasks(repo):
    _commit(repo, "b.txt", "[ACL-2611] On main already")
    _main_moves_on(repo)
    _commit(repo, "c.txt", "[ACL-2629] Task branch")
    _commit(repo, "d.txt", "[ACL-2637] Task branch two")
    got = u._compare(TAG)
    assert got["kind"] == "test"
    assert got["text"] == f"release {NAME} + ACL-2611, ACL-2629, ACL-2637"
    assert "ACL-2629 — Task branch" in got["tooltip"]


def test_a_test_build_without_task_ids_falls_back_to_commits(repo):
    _commit(repo, "b.txt", "wip")
    got = u._compare(TAG)
    assert got["kind"] == "test" and got["text"] == f"release {NAME} + 1 commit"


def test_without_a_main_ref_a_build_ahead_is_not_called_a_test_build(repo):
    _commit(repo, "b.txt", "[ACL-1] X")
    _run(repo, "update-ref", "-d", "refs/remotes/work/main")
    assert u._compare(TAG)["kind"] == "ahead"


def test_a_build_older_than_the_release_says_so(repo):
    _commit(repo, "b.txt", "[ACL-3] New")
    _run(repo, "tag", "acl-release-2026-11-20.1")
    _run(repo, "reset", "-q", "--hard", "HEAD~1")
    got = u._compare("acl-release-2026-11-20.1")
    assert got["known"] and got["text"] == "older than release 2026-11-20.1"


@pytest.mark.parametrize("tag", ["", "acl-release-missing"])
def test_empty_or_unfetched_release_is_unknown(repo, tag):
    got = u._compare(tag)
    assert got["known"] is False and got["text"] == "release unknown"


def test_not_a_git_checkout_is_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(u, "_ROOT", tmp_path)
    got = u._compare(TAG)
    assert got["text"] == "release unknown"


def test_unreachable_release_api_gives_unknown_not_a_stale_number(repo, monkeypatch):
    _commit(repo, "b.txt", "[ACL-1] X")
    _main_moves_on(repo)
    u._badge_refresh()
    assert u._badge_state["last"]["text"] == "+1 task not in release"

    def down():
        raise OSError("offline")

    monkeypatch.setattr(u, "_latest_release_tag", down)
    u._badge_refresh()
    last = u._badge_state["last"]
    assert last["text"] == "release unknown" and last["known"] is False
    assert u._badge_state["fetching"] is False


def test_an_empty_release_answer_is_unknown(repo, monkeypatch):
    monkeypatch.setattr(u, "_latest_release_tag", lambda: "")
    u._badge_refresh()
    assert u._badge_state["last"]["text"] == "release unknown"


def test_the_api_answer_is_parsed_and_odd_shapes_are_ignored(monkeypatch):
    import io
    import json as _json

    def fake(payload):
        class R(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        return lambda req, timeout=None: R(_json.dumps(payload).encode())

    monkeypatch.setattr(u.urllib.request, "urlopen", fake({"tag_name": f" {TAG} "}))
    assert u._latest_release_tag() == TAG
    for odd in ({}, {"tag_name": None}, {"tag_name": 5}, [1, 2]):
        monkeypatch.setattr(u.urllib.request, "urlopen", fake(odd))
        assert u._latest_release_tag() == ""


def test_the_managers_build_shows_no_badge(monkeypatch):
    monkeypatch.setattr(u.sys, "frozen", True, raising=False)
    assert u.release_badge() == {"channel": "release"}


def _join_refresh_threads():
    """Фоновые потоки значка не должны переживать тест."""
    import threading

    for t in threading.enumerate():
        if t.name == "acl-release-gap":
            t.join(timeout=10)
            assert not t.is_alive()


def test_the_header_never_waits_for_the_network(repo, monkeypatch):
    import threading

    release = threading.Event()
    monkeypatch.setattr(u, "_badge_refresh", lambda: release.wait(10))
    started = time.monotonic()
    got = u.release_badge()
    waited = time.monotonic() - started
    release.set()
    _join_refresh_threads()
    assert waited < 2, "шапка ждала сеть"
    assert got["channel"] == "source" and got["text"] == "release unknown"


def test_the_first_call_after_start_refreshes_at_once(repo, monkeypatch):
    started = []
    monkeypatch.setattr(u, "_badge_refresh", lambda: started.append(1))
    assert u._badge_state["checked"] is None
    u.release_badge()
    _join_refresh_threads()
    assert started == [1]


def test_a_second_call_while_fetching_does_not_start_another(repo, monkeypatch):
    import threading

    release = threading.Event()
    started = []
    monkeypatch.setattr(u, "_badge_refresh", lambda: (started.append(1), release.wait(10)))
    u.release_badge()
    u.release_badge()
    release.set()
    _join_refresh_threads()
    assert started == [1]


# ── шапка ─────────────────────────────────────────────────────────────────

def _header(stage_status):
    from src.web.app import templates

    src = (ROOT / "src/web/templates/report.html").read_text(encoding="utf-8")
    head = src[src.index("<title>") - 80: src.index("</title>") + 8]
    meta = src[src.index("{%- set rel = installed_release or {} %}"): src.index("</dl>", src.index('id="acl-version"')) + 5]
    ctx = {"release_badge": stage_status, "installed_release": {"short": "abc123def456"},
           "report": {"date": "2026-09-22", "source_excel": "x.xlsx"}, "built_at": ""}
    return (templates.env.from_string(head).render(**ctx),
            templates.env.from_string(meta).render(**ctx))


def test_the_header_shows_the_text_and_the_tooltip_without_stage():
    status = {"channel": "source", "known": True, "text": "+2 tasks not in release",
              "tooltip": "Not in release 2026-11-14.1636 yet:\nACL-1 — A & B"}
    title, meta = _header(status)
    assert "<title>Adopy</title>" in title
    assert "+2 tasks not in release" in meta and "ACL-1 — A &amp; B" in meta
    assert "stage" not in (title + meta).lower()
    assert "abc123def456" in meta, "версия рядом остаётся"


def test_an_unknown_release_is_said_not_hidden():
    _, meta = _header({"channel": "source", "known": False, "text": "release unknown",
                       "tooltip": "The latest release could not be determined: offline."})
    assert "release unknown" in meta and "acl-release-gap unknown" in meta and "offline" in meta


def test_a_release_header_has_no_badge():
    title, meta = _header({"channel": "release"})
    assert "<title>Adopy</title>" in title
    assert 'id="acl-release-gap"' not in meta


def test_the_badge_template_has_no_stage_reference():
    src = (ROOT / "src/web/templates/report.html").read_text(encoding="utf-8")
    a = src.index('id="acl-version"')
    block = src[a: src.index("</dl>", a)]
    assert "stage" not in block.lower()
    assert "STAGE ·" not in src and "not on stage" not in src
