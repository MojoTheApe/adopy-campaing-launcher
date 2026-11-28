"""Tell the page whether a newer ACL release exists, and ask for a restart into it.

Updating used to mean three git commands in a terminal. That is a real barrier
for a campaign manager whose whole working day happens inside this page: the
machine that most needs a fix is exactly the one whose owner will not open
PowerShell to get it. So the check and the request live here, one click away.

What this module deliberately does NOT do is update anything. The dashboard
cannot replace its own code safely — the app imports parts of itself lazily, so
swapping files under a live server means later requests load a different version
than the one that started. start_dashboard.bat already knows how to do it
properly: gate the candidate, build its runtime in isolation, smoke-test it, and
roll back if the result does not come up healthy. All that is missing is a way
to ask it to run that path now instead of at the next launch.

Hence: this module answers two questions and leaves a marker. Everything that
can go wrong stays where the recovery logic already lives.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config.paths import get_local_config_dir

_ROOT = Path(__file__).resolve().parents[2]

# Written by the dashboard, consumed by start_dashboard.bat's hold loop. Two
# separate markers on purpose: the launcher swaps this one for `update_once`
# before relaunching, so a crash between the two can never leave a file that
# makes every start restart again.
RESTART_MARKER = "restart_request.json"

# start_dashboard.bat sets this before launching serve.py. Without a launcher
# watching, exiting would just leave the buyer with no dashboard and no way to
# get it back except the terminal we are trying to spare them.
SUPERVISED_ENV = "ACL_SUPERVISED"

# A page load asks for this; several tabs must not each cost a network fetch.
_CACHE_TTL_S = 120.0
_cache: tuple[float, dict[str, Any]] | None = None
_cache_lock = threading.Lock()


def _git(*args: str, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(_ROOT),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _git_out(*args: str, timeout: int = 30) -> str:
    try:
        result = _git(*args, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _release_tag(sha: str) -> str:
    """The release name for a commit, or "" — a commit can carry no release tag."""
    if not sha:
        return ""
    tags = [t.strip() for t in _git_out("tag", "--points-at", sha, "--list", "acl-release-*").splitlines()]
    return sorted(t for t in tags if t)[-1] if any(tags) else ""


def _describe(sha: str) -> dict[str, str]:
    return {"sha": sha, "short": sha[:12], "tag": _release_tag(sha)}


_installed: dict[str, str] | None = None


def installed() -> dict[str, str]:
    """Which release this dashboard is running.

    Shown in the header because there was nowhere to look. On 2026-09-03 a second
    campaign manager was asked three times whether he had updated, and neither of
    us could tell: restarting the dashboard changes nothing about the version, so
    "I reopened it" and "I updated it" look identical from both sides.

    Cached: a running process cannot change its own checkout — the launcher
    updates the code and then starts a new one.
    """
    global _installed
    if _installed is None and getattr(sys, "frozen", False):
        from src.packaged_runtime import build_identity
        _installed = build_identity() or {"sha": "", "short": "", "tag": "unknown Windows build"}
    if _installed is None:
        head = _git_out("rev-parse", "HEAD").lower()
        _installed = _describe(head) if head else {"sha": "", "short": "", "tag": ""}
    return dict(_installed)


# ── Значок версии в шапке (ACL-2638, раньше STAGE-значок ACL-1267) ──────
#
# После пакетной доставки v2 (09.10) через ветку stage ничего не идёт, поэтому
# сравнение с ней только росло и ничего не говорило. Теперь сборку, запущенную
# из исходников, сравниваем с последним опубликованным выпуском
# (MojoTheApe/acl-releases, Latest): равна выпуску — показываем его имя; впереди —
# «+N tasks not in release» (задачи ACL по темам коммитов, коммиты — только если
# задачи не распознаны); тестовая сборка с ветками задач поверх main — выпуск и
# id этих задач. Выпуск неизвестен или недоступен — «release unknown».
# Упакованная сборка (у менеджеров) значка не показывает.
#
# Сеть — только в фоне раз в RELEASE_REFRESH_S: отрисовка шапки отдаёт последнее
# известное и никогда не ждёт. До первой сверки — «release unknown».

PROJECT_REPO = "MojoTheApe/adopy-campaing-launcher"
RELEASES_REPO = "MojoTheApe/acl-releases"
RELEASE_TAG_PREFIX = "acl-release-"
RELEASE_REFRESH_S = 600
RELEASE_API_TIMEOUT_S = 10

_badge_lock = threading.Lock()
# `checked` = None — ещё ни разу: monotonic() на Windows считает от загрузки
# машины, и с нулём первая сверка откладывалась бы на 10 минут после старта.
_badge_state: dict[str, Any] = {"checked": None, "fetching": False, "last": None}

_TASK_RE = re.compile(r"\bACL-(\d+)\b")
_TITLE_PREFIX_RE = re.compile(r"^\w+(\([^)]*\))?!?:\s*")


def _project_remote() -> str:
    """Remote, который смотрит в репозиторий проекта, или ""."""
    for line in _git_out("remote", "-v").splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[2] == "(fetch)":
            url = parts[1].lower().rstrip("/")
            if url.endswith(PROJECT_REPO.lower()) or url.endswith(PROJECT_REPO.lower() + ".git"):
                return parts[0]
    return ""


def _unknown(why: str, release: str = "") -> dict[str, Any]:
    return {"known": False, "kind": "unknown", "release": release, "text": "release unknown",
            "tooltip": f"The latest release could not be determined: {why}.", "tasks": [],
            "commits": 0, "why": why}


def _latest_release_tag() -> str:
    """Имя последнего выпуска (Latest в acl-releases) или "" — сеть, только из фона."""
    req = urllib.request.Request(
        f"https://api.github.com/repos/{RELEASES_REPO}/releases/latest",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "acl-dashboard"})
    with urllib.request.urlopen(req, timeout=RELEASE_API_TIMEOUT_S) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    tag = data.get("tag_name") if isinstance(data, dict) else ""
    return tag.strip() if isinstance(tag, str) else ""


def _release_name(tag: str) -> str:
    return tag[len(RELEASE_TAG_PREFIX):] if tag.startswith(RELEASE_TAG_PREFIX) else tag


def _tasks_in(rev_args: list[str]) -> tuple[list[tuple[str, str]], int]:
    """Задачи ACL в выборке коммитов (без слияний): ([(id, название)], число коммитов)."""
    out = _git_out("log", "--no-merges", "--format=%x1e%s%x1f%b", *rev_args)
    seen: dict[str, str] = {}
    commits = 0
    for rec in out.split("\x1e"):
        if not rec.strip():
            continue
        commits += 1
        subject, _, body = rec.partition("\x1f")
        for num in _TASK_RE.findall(subject + "\n" + body):
            key = f"ACL-{num}"
            if key not in seen:
                seen[key] = _TITLE_PREFIX_RE.sub("", _TASK_RE.sub("", subject).strip(" []()—–-:·\t"))
    tasks = sorted(seen.items(), key=lambda kv: int(kv[0][4:]))
    return tasks, commits


def _tooltip_lines(tasks: list[tuple[str, str]]) -> str:
    return "\n".join(f"{i} — {t}" if t else i for i, t in tasks)


def _compare(tag: str) -> dict[str, Any]:
    """Сравнить HEAD с выпуском `tag` по уже скачанным тегам и ветке main."""
    if not tag:
        return _unknown("no release is published")
    name = _release_name(tag)
    head = _git_out("rev-parse", "HEAD").lower()
    if not head:
        return _unknown("this copy is not a git checkout", name)
    rel_sha = _git_out("rev-parse", "--verify", "--quiet", f"refs/tags/{tag}^{{commit}}").lower()
    if not rel_sha:
        return _unknown(f"release {name} is not in this checkout yet", name)
    if rel_sha == head:
        return {"known": True, "kind": "equal", "release": name, "text": f"release {name}",
                "tooltip": f"This build is release {name}.", "tasks": [], "commits": 0, "why": ""}
    tasks, commits = _tasks_in([f"{rel_sha}..{head}"])
    if commits == 0:
        return {"known": True, "kind": "behind", "release": name, "text": f"older than release {name}",
                "tooltip": f"Release {name} has changes this build does not have.",
                "tasks": [], "commits": 0, "why": ""}
    # Тестовая сборка — в ней есть коммиты, которых нет в main проекта (ветки задач).
    remote = _project_remote()
    on_main = bool(remote) and bool(_git_out("rev-parse", "--verify", "--quiet", f"{remote}/main"))
    extra = 0
    if on_main:
        extra = _tasks_in(["--cherry-pick", "--right-only", f"{remote}/main...{head}"])[1]
    ids = [i for i, _ in tasks]
    if extra:
        if tasks:
            text = f"release {name} + " + ", ".join(ids)
            tip = f"Release {name} plus:\n" + _tooltip_lines(tasks)
        else:
            text = f"release {name} + {commits} commit{'s' if commits != 1 else ''}"
            tip = f"Release {name} plus {commits} commit(s) that name no ACL task."
        return {"known": True, "kind": "test", "release": name, "text": text, "tooltip": tip,
                "tasks": ids, "commits": commits, "why": ""}
    if tasks:
        n = len(tasks)
        text = f"+{n} task{'s' if n != 1 else ''} not in release"
        tip = f"Not in release {name} yet:\n" + _tooltip_lines(tasks)
    else:
        text = f"+{commits} commit{'s' if commits != 1 else ''} not in release"
        tip = f"{commits} commit(s) not in release {name}; none of them names an ACL task."
    return {"known": True, "kind": "ahead", "release": name, "text": text, "tooltip": tip,
            "tasks": ids, "commits": commits, "why": ""}


def _badge_refresh() -> None:
    result: dict[str, Any]
    try:
        tag = _latest_release_tag()
        if not tag:
            result = _unknown("the release list is empty")
        else:
            remote = _project_remote()
            if remote and not _git_out("rev-parse", "--verify", "--quiet", f"refs/tags/{tag}^{{commit}}"):
                _git("fetch", "--quiet", remote, "main", "--tags", timeout=60)
            result = _compare(tag)
    except Exception as exc:  # noqa: BLE001 — сеть и git не должны ронять шапку
        result = _unknown(type(exc).__name__)
    with _badge_lock:
        _badge_state["last"] = result
        _badge_state["checked"] = time.monotonic()
        _badge_state["fetching"] = False


def release_badge() -> dict[str, Any]:
    """Что показать в шапке: значок версии для запуска из исходников.

    `channel` — "source" для запуска из исходников, "release" для упакованной
    сборки (у менеджеров значка нет). Никогда не ждёт сеть: отдаёт последнее
    известное («release unknown» до первой сверки), обновление идёт в фоне.
    """
    if getattr(sys, "frozen", False):
        return {"channel": "release"}
    with _badge_lock:
        last = _badge_state["last"]
        checked = _badge_state["checked"]
        stale = checked is None or time.monotonic() - checked > RELEASE_REFRESH_S
        start = stale and not _badge_state["fetching"]
        if start:
            _badge_state["fetching"] = True
    if start:
        threading.Thread(target=_badge_refresh, name="acl-release-gap", daemon=True).start()
    return {"channel": "source", **(last or _unknown("not checked yet"))}


def is_supervised() -> bool:
    return not getattr(sys, "frozen", False) and (os.environ.get(SUPERVISED_ENV) or "").strip() == "1"


def marker_path() -> Path:
    return Path(get_local_config_dir()) / RESTART_MARKER


def _no_update(current: dict[str, str], note: str) -> dict[str, Any]:
    return {
        "ok": True,
        "available": False,
        "current": current,
        "candidate": None,
        "note": note,
        "supervised": is_supervised(),
    }


def check(*, fetch: bool = True, use_cache: bool = True) -> dict[str, Any]:
    """Is there a promoted release newer than the one running?

    Answers with the same gate the launcher enforces, so the button can never
    offer an update the launcher would then refuse — a "nothing happened" that
    looks exactly like a broken button.
    """
    global _cache
    if use_cache:
        with _cache_lock:
            if _cache and (time.monotonic() - _cache[0]) < _CACHE_TTL_S:
                return dict(_cache[1])

    result = _check_uncached(fetch=fetch)
    if use_cache and result.get("ok"):
        with _cache_lock:
            _cache = (time.monotonic(), dict(result))
    return result


def _check_uncached(*, fetch: bool) -> dict[str, Any]:
    if getattr(sys, "frozen", False):
        return _no_update(installed(), "Windows preview: install updates with the approved Setup executable. Automatic updates are disabled.")
    head = _git_out("rev-parse", "HEAD").lower()
    if not head:
        # A zip download rather than a clone, or git missing from PATH. Not an
        # error to show anyone: it simply means this install cannot self-update.
        return {
            "ok": False,
            "available": False,
            "current": _describe(""),
            "candidate": None,
            "note": "This copy of ACM is not a git checkout, so it cannot update itself.",
            "supervised": is_supervised(),
        }
    current = _describe(head)

    if fetch:
        try:
            fetched = _git("fetch", "--quiet", "origin", "main", "--tags", timeout=120)
        except (OSError, subprocess.SubprocessError):
            return _no_update(current, "Could not reach GitHub — staying on the installed version.")
        if fetched.returncode != 0:
            return _no_update(current, "Could not reach GitHub — staying on the installed version.")

    candidate_sha = _git_out("rev-parse", "origin/main^{commit}").lower()
    if not candidate_sha:
        return _no_update(current, "The release channel is unavailable on this machine.")
    if candidate_sha == head:
        return _no_update(current, "ACM is up to date.")

    from scripts.acl_update_gate import UNVERIFIED_NOTE, validate_candidate

    allowed, detail = validate_candidate("origin/main")
    if not allowed:
        return _no_update(current, f"A newer commit exists but is not a promoted release ({detail}).")

    candidate = _describe(candidate_sha)
    note = f"Update available: {candidate['tag'] or candidate['short']}."
    # ACL-1357: гейт пропустил выпуск без проверки CI через API — причину
    # показываем, чтобы машину без токена было видно, а не только в Sentry.
    unverified = UNVERIFIED_NOTE in detail
    if unverified:
        reason = detail.split(UNVERIFIED_NOTE + ":", 1)[-1].split("; accepted", 1)[0].strip()
        note += f" This machine could not verify its CI result ({reason})."
    return {
        "ok": True,
        "available": True,
        "current": current,
        "candidate": candidate,
        "note": note,
        "gate": "unverified" if unverified else "verified",
        "supervised": is_supervised(),
    }


def forget_cache() -> None:
    global _cache
    with _cache_lock:
        _cache = None


def request_restart(*, target: str = "") -> Path:
    """Leave the marker the launcher's hold loop is watching for."""
    path = marker_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": 1,
        "reason": "update",
        "target": target,
        "requested_at": datetime.now(timezone.utc).isoformat(),
    }
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(temp, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
    return path


def schedule_exit(delay_s: float = 1.5) -> None:
    """Stop the server shortly after this request's response has been sent.

    os._exit rather than a graceful uvicorn shutdown: the point is to end this
    process so the launcher can replace its code, and a shutdown that a stuck
    background thread can delay is the one case that must not happen here — the
    user is looking at a page that says ACL is restarting.
    """

    def _stop() -> None:
        time.sleep(delay_s)
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(0)

    threading.Thread(target=_stop, daemon=True, name="acl-update-exit").start()
