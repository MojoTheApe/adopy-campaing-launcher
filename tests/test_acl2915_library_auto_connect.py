"""ACL-2915: вход в Google подключает общую библиотеку сам, без токена от байера.

Библиотека и хаб поддельные: ни настоящего токена, ни настоящего acl-lib здесь нет.
"""
from __future__ import annotations

import json
import logging

import pytest

from src.analysis_agent import ad_text_registry as ar
from src.analysis_agent import targeting_registry as tr
from src.analysis_agent import team_library as tl
from src.analysis_agent import team_library_connect as tlc
from src.web import hidden_accounts, user_accounts as ua

KEY = "buyer@adopy.test.1999999999." + "a" * 64
GOOGLE = "ya29.google-access-token"
HUB_TOKEN = "ghp_HUBHANDEDTOKEN1234567890abcdef"
OWN_TOKEN = "ghp_BUYEROWNTOKEN1234567890abcdef"
REPO = "Org/team-lib"


@pytest.fixture(autouse=True)
def machine(tmp_path, monkeypatch):
    local = tmp_path / "local"
    local.mkdir()
    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")
    from config import paths, token_vault

    monkeypatch.setattr(paths, "get_local_config_dir", lambda: str(local))
    monkeypatch.setattr(paths, "get_ad_account_map_path", lambda: str(local / "map.csv"))
    monkeypatch.setattr(paths, "get_hidden_accounts_path", lambda: str(local / "hidden.json"))
    monkeypatch.setattr(paths, "get_machine_env_path", lambda: str(env_file))
    monkeypatch.setattr(hidden_accounts, "get_hidden_accounts_path", lambda: str(local / "hidden.json"))
    monkeypatch.setattr(ua, "token_status", lambda: "ok")
    monkeypatch.setattr(token_vault, "available", lambda: True)
    monkeypatch.setattr(token_vault, "protect_bytes", lambda b: b"SEAL" + bytes(x ^ 0x5A for x in b))
    monkeypatch.setattr(token_vault, "unprotect_bytes", lambda b: bytes(x ^ 0x5A for x in b[4:]))
    monkeypatch.setattr(token_vault, "write_secret_text", lambda p, t: open(p, "w", encoding="utf-8").write(t))
    monkeypatch.setattr(token_vault, "read_secret_text", lambda p: open(p, encoding="utf-8").read())
    for name in ("ACL_LIBRARY_REPO", "ACL_LIBRARY_TOKEN", "ACL_LIBRARY_BRANCH"):
        monkeypatch.setenv(name, "")
    tl.use_backend(None)
    tlc._pending.clear()
    yield env_file
    tl.use_backend(None)


@pytest.fixture
def fake_lib(monkeypatch):
    """GitHub заменён памятью: check() проходит (кроме токена на BAD), запись идёт в MemoryBackend."""
    mem = tl.MemoryBackend()
    mem.checks = []

    def check(self):
        mem.checks.append((self.repo, self.token))
        if self.token.endswith("BAD"):
            raise tl.LibraryError("Could not read the team library: the library token is missing access or has expired.")

    monkeypatch.setattr(tl.GitHubBackend, "check", check)
    monkeypatch.setattr(tl, "backend", lambda: mem)
    return mem


class Hub:
    def __init__(self, library=None, access_status=200):
        self.library = {"repo": REPO, "token": HUB_TOKEN} if library is None else library
        self.access_status = access_status
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, dict(headers)))
        if url.endswith("/v1/session"):
            return 200, {}, {"key": KEY}
        if url.endswith("/v1/library-access"):
            if self.access_status != 200:
                return self.access_status, {}, {"error": "x"}
            return 200, {}, self.library
        if headers.get("If-None-Match") == '"e1"':
            return 304, {"etag": '"e1"'}, None
        return 200, {"etag": '"e1"'}, {"accounts": []}

    def library_calls(self):
        return [c for c in self.calls if c[1].endswith("/v1/library-access")]


def run(hub):
    return ua.sync(http=hub, token_provider=lambda: GOOGLE)


def env_text(machine):
    return machine.read_text(encoding="utf-8")


def stored_token(machine):
    from dotenv import dotenv_values

    from config import token_vault

    raw = dotenv_values(machine).get("ACL_LIBRARY_TOKEN") or ""
    return token_vault.decode_env_value(raw) if raw else ""


def test_buyer_without_token_gets_library_connected(fake_lib, machine):
    r = run(Hub())
    assert r["status"] == "updated"
    assert stored_token(machine) == HUB_TOKEN
    assert tlc.state()["connected"] is True
    assert fake_lib.checks == [(REPO, HUB_TOKEN)]
    assert "ACL_LIBRARY_REPO" in env_text(machine)  # репозиторий хаба отличается от того, что по умолчанию


def test_hub_repo_equal_to_default_is_not_written(fake_lib, machine):
    run(Hub({"repo": tl.DEFAULT_REPO, "token": HUB_TOKEN}))
    assert stored_token(machine) == HUB_TOKEN
    assert "ACL_LIBRARY_REPO" not in env_text(machine)


def test_existing_token_is_kept_and_hub_is_not_asked(fake_lib, machine, monkeypatch):
    monkeypatch.setenv("ACL_LIBRARY_TOKEN", OWN_TOKEN)
    hub = Hub()
    run(hub)
    assert hub.library_calls() == []
    assert HUB_TOKEN not in env_text(machine) and fake_lib.checks == []
    import os

    assert os.environ["ACL_LIBRARY_TOKEN"] == OWN_TOKEN


def test_connect_also_runs_on_304_and_only_once_when_connected(fake_lib, machine):
    hub = Hub()
    run(hub)                      # 200: подключили
    r = run(hub)                  # 304: токен уже стоит, хаб за ним не ходим
    assert r["status"] == "not_modified"
    assert len(hub.library_calls()) == 1


def test_existing_items_are_added_missing_only_and_never_overwritten_or_deleted(fake_lib, machine):
    tr.add("Local only", "Chess")
    tr.add("Team existing", "Local version of it")
    ar.add("Local headline", "Local body", "")
    # В общей библиотеке уже есть цель с тем же именем, но другим содержимым, и запись в «удалённых»
    team = {"name": "Team existing", "interests": "Team version", "narrow": ""}
    gone = {"name": "Blocked deleted", "interests": "Gone", "narrow": ""}
    remote = {"version": 1, "targets": {"Team existing": team}, "renamed": {},
              "deleted": {"Blocked deleted": {"data": gone, "deleted_by": "fixture", "deleted_at": "2026-10-23"}}}
    fake_lib.put(tl.FILES["targets"][0], json.dumps(remote), None, "fixture")

    run(Hub())

    saved = tl._parse("targets", fake_lib.get(tl.FILES["targets"][0]))
    assert "Local only" in saved["targets"]                       # недостающее добавлено в общую
    assert saved["targets"]["Team existing"] == team              # чужое не перезаписано
    assert saved["deleted"] == remote["deleted"]                  # история удалений цела
    assert "Local only" in {t.name for t in tr.all_targets()}     # и местное на месте


@pytest.mark.parametrize("status", [401, 403, 503, 500, 404])
def test_no_valid_key_or_refused_user_connects_nothing(fake_lib, machine, status):
    run(Hub(access_status=status))
    assert stored_token(machine) == "" and fake_lib.checks == []
    assert tlc.state()["connected"] is False


@pytest.mark.parametrize("library", [{}, {"repo": REPO}, {"token": HUB_TOKEN}, {"repo": 5, "token": 7},
                                     {"repo": "not a repo", "token": HUB_TOKEN}, {"repo": REPO, "token": "x y"}])
def test_bad_hub_answer_connects_nothing(fake_lib, machine, library):
    run(Hub(library))
    assert stored_token(machine) == "" and tlc.state()["connected"] is False


def test_hub_down_or_no_session_connects_nothing_and_never_raises(fake_lib, machine):
    def boom(*a, **k):
        raise OSError("no network")

    r = ua.sync(http=boom, token_provider=lambda: GOOGLE)
    assert r["status"] in ("offline", "no_session")
    assert stored_token(machine) == ""
    r = ua.sync(http=Hub(), token_provider=lambda: "")
    assert r["status"] == "no_session" and stored_token(machine) == ""


def test_a_token_that_github_refuses_is_not_saved_and_retried_next_time(fake_lib, machine):
    bad = Hub({"repo": REPO, "token": "ghp_" + "x" * 30 + "BAD"})
    r = run(bad)
    assert r["ok"] and stored_token(machine) == ""
    assert tlc.state()["connected"] is False
    good = Hub()
    run(good)
    assert stored_token(machine) == HUB_TOKEN


def test_token_never_reaches_logs_responses_or_state_files(fake_lib, machine, caplog, tmp_path):
    caplog.set_level(logging.DEBUG)
    out = run(Hub())
    bad = run(Hub({"repo": REPO, "token": "ghp_" + "y" * 30 + "BAD"}))
    blob = json.dumps(out) + json.dumps(bad) + caplog.text
    assert "ghp_" not in blob                          # ни выданный, ни отвергнутый токен не виден
    assert HUB_TOKEN not in env_text(machine)          # в настройках машины токен запечатан
    for f in (tmp_path / "local").glob("*"):
        text = f.read_text(encoding="utf-8", errors="ignore")
        assert HUB_TOKEN not in text, f.name


def test_button_connect_still_works_as_before(fake_lib, machine):
    out = tlc.connect(OWN_TOKEN)
    assert out["ok"] and stored_token(machine) == OWN_TOKEN
    assert "ACL_LIBRARY_REPO" not in env_text(machine)
