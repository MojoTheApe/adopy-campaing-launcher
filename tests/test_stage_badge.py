"""STAGE-значок в шапке и «+N not on stage» (ACL-1267).

Решение владельца 22.09: процесс остаётся — ветка → stage → main, релиз
менеджерам — метка на main. Дешборд, запущенный из исходников, — версия, на
которой владелец работает; в шапке он видит STAGE и сколько правок в ней ещё
не залито в stage. Упакованная сборка (у менеджеров) значка не показывает.
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


def _run(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """Клон с веткой stage у remote проекта и своими правками сверху."""
    _run(tmp_path, "init", "-q")
    _run(tmp_path, "config", "user.email", "t@example.com")
    _run(tmp_path, "config", "user.name", "t")
    _run(tmp_path, "config", "commit.gpgsign", "false")
    (tmp_path / "a.txt").write_text("base\n")
    _run(tmp_path, "add", "a.txt")
    _run(tmp_path, "commit", "-q", "-m", "base")
    _run(tmp_path, "remote", "add", "work", "https://github.com/" + u.PROJECT_REPO + ".git")
    _run(tmp_path, "update-ref", "refs/remotes/work/stage", "HEAD")
    monkeypatch.setattr(u, "_ROOT", tmp_path)
    monkeypatch.setattr(u, "_stage_state", {"checked": None, "fetching": False, "last": None})
    return tmp_path


def _commit(repo, name, text):
    (repo / name).write_text(text)
    _run(repo, "add", name)
    _run(repo, "commit", "-q", "-m", name)


def test_the_project_remote_is_found_by_its_url_not_its_name(repo):
    _run(repo, "remote", "add", "origin", "https://github.com/JenyaFarber/fb_agents.git")
    assert u._project_remote() == "work"


def test_changes_not_on_stage_are_counted(repo):
    _commit(repo, "b.txt", "one\n")
    _commit(repo, "c.txt", "two\n")
    got = u._stage_count()
    assert got == {"known": True, "ahead": 2, "ref": "work/stage", "why": ""}


def test_a_copy_already_on_stage_is_not_counted(repo):
    """Задача, перенесённая в stage тем же патчем, «не на stage» не считается."""
    _run(repo, "checkout", "-q", "-b", "side")
    _commit(repo, "b.txt", "same change\n")
    _run(repo, "update-ref", "refs/remotes/work/stage", "HEAD")
    _run(repo, "checkout", "-q", "-")
    _run(repo, "cherry-pick", "side")
    _commit(repo, "d.txt", "only here\n")
    assert u._stage_count()["ahead"] == 1


def test_without_the_project_remote_it_says_so(repo):
    _run(repo, "remote", "remove", "work")
    got = u._stage_count()
    assert got["known"] is False and u.PROJECT_REPO in got["why"]


def test_an_unfetched_stage_is_said_not_zero(repo):
    _run(repo, "update-ref", "-d", "refs/remotes/work/stage")
    got = u._stage_count()
    assert got["known"] is False and "not been fetched" in got["why"]


def test_the_managers_build_shows_no_badge(monkeypatch):
    monkeypatch.setattr(u.sys, "frozen", True, raising=False)
    assert u.stage_status() == {"channel": "release"}


def test_the_header_never_waits_for_the_network(repo, monkeypatch):
    monkeypatch.setattr(u, "_stage_refresh", lambda: time.sleep(5))
    started = time.monotonic()
    got = u.stage_status()
    assert time.monotonic() - started < 2, "шапка ждала fetch"
    assert got["channel"] == "stage" and got["known"] is True and got["ahead"] == 0


# ── шапка и вкладка ───────────────────────────────────────────────────────

def _header(stage_status):
    from src.web.app import templates

    src = (ROOT / "src/web/templates/report.html").read_text(encoding="utf-8")
    head = src[src.index("<title>") - 80: src.index("</title>") + 8]
    meta = src[src.index("{%- set rel = installed_release or {} %}"): src.index("</dl>", src.index('id="acl-version"')) + 5]
    ctx = {"stage_status": stage_status, "installed_release": {"short": "abc123def456"},
           "report": {"date": "2026-09-22", "source_excel": "x.xlsx"}, "built_at": ""}
    return (templates.env.from_string(head).render(**ctx),
            templates.env.from_string(meta).render(**ctx))


def test_stage_shows_the_badge_the_gap_and_the_tab_title():
    title, meta = _header({"channel": "stage", "known": True, "ahead": 119, "ref": "work/stage", "why": ""})
    assert "<title>STAGE · Adopy</title>" in title
    assert 'id="acl-stage"' in meta and "STAGE" in meta and "+119 not on stage" in meta
    assert "abc123def456" in meta, "версия рядом остаётся"


def test_an_unknown_gap_is_said_not_hidden():
    _, meta = _header({"channel": "stage", "known": False, "ahead": None, "ref": "", "why": "no git remote"})
    assert "stage unknown" in meta and "acl-stage unknown" in meta and "no git remote" in meta


def test_a_release_header_is_unchanged():
    title, meta = _header({"channel": "release"})
    assert "<title>Adopy</title>" in title and "STAGE" not in title
    assert 'id="acl-stage"' not in meta


def test_a_fetch_timeout_keeps_the_known_count(repo, monkeypatch):
    """Ревью: таймаут fetch заменял известное число на «stage unknown»."""
    _commit(repo, "b.txt", "one\n")
    real = u._git

    def slow(*args, **kw):
        if args and args[0] == "fetch":
            raise subprocess.TimeoutExpired(cmd="git fetch", timeout=60)
        return real(*args, **kw)

    monkeypatch.setattr(u, "_git", slow)
    u._stage_refresh()
    assert u._stage_state["last"] == {"known": True, "ahead": 1, "ref": "work/stage", "why": ""}
    assert u._stage_state["fetching"] is False


def test_the_first_call_after_start_fetches_at_once(repo, monkeypatch):
    """Ревью: с checked=0 первая сверка ждала 10 минут от загрузки машины."""
    started = []
    monkeypatch.setattr(u, "_stage_refresh", lambda: started.append(1))
    monkeypatch.setattr(u.time, "monotonic", lambda: 5.0)
    u.stage_status()
    time.sleep(0.2)
    assert started == [1]


@pytest.mark.parametrize("url", [
    "git@github.com:MojoTheApe/adopy-campaing-launcher.git",
    "https://github.com/mojotheape/Adopy-Campaing-Launcher/",
])
def test_the_remote_is_found_in_other_url_forms(repo, url):
    _run(repo, "remote", "set-url", "work", url)
    assert u._project_remote() == "work"
