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
import subprocess
import sys
import threading
import time
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


# ── STAGE-значок в шапке (ACL-1267) ────────────────────────────────────
#
# Решение владельца 22.09: процесс остаётся как есть (ветка → stage → main,
# релиз менеджерам — метка на main). Дешборд, запущенный из исходников, — это
# версия, на которой владелец работает, и в шапке он хочет видеть STAGE и
# сколько правок в ней ещё не залито в stage. Упакованная сборка — это то, что
# стоит у менеджеров: значка у неё нет.
#
# Считаем коммиты, которых нет в ветке stage проекта, без слияний и без копий,
# уже перенесённых в stage тем же патчем (`--cherry-pick`). Ветку ищем у того
# remote, который смотрит в репозиторий проекта: у владельца это `work`, на
# свежем клоне — `origin`. Свежесть — фоновым fetch раз в STAGE_REFRESH_S, чтобы
# отрисовка страницы никогда не ждала сеть.

PROJECT_REPO = "MojoTheApe/adopy-campaing-launcher"
STAGE_BRANCH = "stage"
STAGE_REFRESH_S = 600

_stage_lock = threading.Lock()
# `checked` = None — ещё ни разу: monotonic() на Windows считает от загрузки
# машины, и с нулём первая сверка откладывалась на 10 минут после старта.
_stage_state: dict[str, Any] = {"checked": None, "fetching": False, "last": None}


def _project_remote() -> str:
    """Remote, который смотрит в репозиторий проекта, или ""."""
    for line in _git_out("remote", "-v").splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[2] == "(fetch)":
            url = parts[1].lower().rstrip("/")
            if url.endswith(PROJECT_REPO.lower()) or url.endswith(PROJECT_REPO.lower() + ".git"):
                return parts[0]
    return ""


def _stage_count() -> dict[str, Any]:
    """Сколько коммитов этой версии нет в stage — по уже скачанной ветке."""
    remote = _project_remote()
    if not remote:
        return {"known": False, "ahead": None, "ref": "",
                "why": f"no git remote points at {PROJECT_REPO}"}
    ref = f"{remote}/{STAGE_BRANCH}"
    if not _git_out("rev-parse", "--verify", "--quiet", ref):
        return {"known": False, "ahead": None, "ref": ref, "why": f"{ref} has not been fetched"}
    count = _git_out("rev-list", "--count", "--no-merges", "--cherry-pick", "--right-only",
                     f"{ref}...HEAD")
    if not count.isdigit():
        return {"known": False, "ahead": None, "ref": ref, "why": f"could not compare with {ref}"}
    return {"known": True, "ahead": int(count), "ref": ref, "why": ""}


def _stage_refresh() -> None:
    # Неудачный fetch (сеть, таймаут) не отменяет счёт по уже скачанной ветке:
    # число остаётся, только не свежее. Иначе таймаут менял известное число
    # на «stage unknown».
    try:
        remote = _project_remote()
        if remote:
            _git("fetch", "--quiet", remote, STAGE_BRANCH, timeout=60)
    except Exception:  # noqa: BLE001 — сеть и git не должны ронять шапку
        pass
    try:
        result = _stage_count()
    except Exception as exc:  # pragma: no cover - defensive
        result = {"known": False, "ahead": None, "ref": "", "why": str(exc)[:120]}
    with _stage_lock:
        _stage_state["last"] = result
        _stage_state["checked"] = time.monotonic()
        _stage_state["fetching"] = False


def stage_status() -> dict[str, Any]:
    """Что показать в шапке: STAGE или ничего, и насколько версия ушла от stage.

    `channel` — "stage" для запуска из исходников, "release" для упакованной
    сборки (у менеджеров). Никогда не ждёт сеть: отдаёт последнее известное, а
    обновление запускает в фоне.
    """
    if getattr(sys, "frozen", False):
        return {"channel": "release"}
    with _stage_lock:
        last = _stage_state["last"]
        checked = _stage_state["checked"]
        stale = checked is None or time.monotonic() - checked > STAGE_REFRESH_S
        start = stale and not _stage_state["fetching"]
        if start:
            _stage_state["fetching"] = True
    if last is None:
        last = _stage_count()  # локально и быстро — без сети
        with _stage_lock:
            if _stage_state["last"] is None:
                _stage_state["last"] = last
    if start:
        threading.Thread(target=_stage_refresh, name="acl-stage-gap", daemon=True).start()
    return {"channel": "stage", **last}


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
