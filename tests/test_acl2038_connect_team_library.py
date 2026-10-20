"""ACL-2038: подключение общей библиотеки с дешборда.

Токен не должен попасть никуда, кроме файла настроек машины и заголовка запроса
к GitHub. Все тесты работают с поддельной библиотекой (MemoryBackend / заглушка
GitHubBackend.check): ни настоящего acl-lib, ни настоящего токена здесь нет.
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.analysis_agent import ad_text_registry as ar  # noqa: E402
from src.analysis_agent import targeting_registry as tr  # noqa: E402
from src.analysis_agent import team_library as tl  # noqa: E402
from src.analysis_agent import team_library_connect as tlc  # noqa: E402

# Узнаваемое значение: если оно найдётся где-то, кроме файла настроек, тест упадёт.
TOKEN = "ghp_SECRETSECRETSECRET1234567890abcd"


@pytest.fixture(autouse=True)
def machine(tmp_path, monkeypatch):
    """Своя машина: настройки во временной папке, библиотека не подключена."""
    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")
    from config import paths

    monkeypatch.setattr(paths, "get_machine_env_path", lambda: str(env_file))
    for name in ("ACL_LIBRARY_REPO", "ACL_LIBRARY_TOKEN", "ACL_LIBRARY_BRANCH"):
        monkeypatch.setenv(name, "")
    tl.use_backend(None)
    tlc._pending.clear()
    yield env_file
    tl.use_backend(None)


@pytest.fixture
def fake_lib(monkeypatch):
    """GitHub заменён памятью: check() проходит, запись идёт в MemoryBackend."""
    mem = tl.MemoryBackend()
    calls = {"check": []}

    def check(self):
        calls["check"].append((self.repo, self.branch))
        if self.token.endswith("BAD"):
            raise tl.LibraryError("Could not read the team library: the library token is missing "
                                  "access or has expired.")

    monkeypatch.setattr(tl.GitHubBackend, "check", check)
    monkeypatch.setattr(tl, "backend", lambda: mem)
    mem.calls = calls
    return mem


# ── значения по умолчанию ───────────────────────────────────────────────────
def test_token_alone_uses_the_default_repository_and_branch(monkeypatch):
    monkeypatch.setenv("ACL_LIBRARY_TOKEN", TOKEN)
    b = tl.backend()
    assert (b.repo, b.branch) == ("MojoTheApe/acl-lib", "main")


def test_machine_settings_still_win_over_the_defaults(monkeypatch):
    monkeypatch.setenv("ACL_LIBRARY_TOKEN", TOKEN)
    monkeypatch.setenv("ACL_LIBRARY_REPO", "me/other-lib")
    monkeypatch.setenv("ACL_LIBRARY_BRANCH", "dev")
    b = tl.backend()
    assert (b.repo, b.branch) == ("me/other-lib", "dev")


def test_no_token_means_not_connected_even_though_a_default_repo_exists():
    assert tl.backend() is None and not tl.configured()
    assert tlc.state() == {"connected": False, "repo": "MojoTheApe/acl-lib"}


def test_a_repository_without_a_token_is_not_connected_and_connect_is_offered(monkeypatch):
    monkeypatch.setenv("ACL_LIBRARY_REPO", "me/other-lib")
    assert tl.backend() is None and not tl.configured()
    assert tlc.state() == {"connected": False, "repo": "me/other-lib"}


# ── токен: форма и утечки ──────────────────────────────────────────────────
@pytest.mark.parametrize("bad", ["", "   ", "short", "has space inside the token value", "line\nbreak" + "x" * 30,
                                 "a" * 300, "тoken" + "x" * 30])
def test_a_malformed_token_is_refused_before_any_request(bad, fake_lib):
    with pytest.raises(tlc.ConnectError) as err:
        tlc.connect(bad)
    assert bad.strip() not in str(err.value) or not bad.strip()
    assert fake_lib.calls["check"] == []


def test_connect_saves_the_token_to_machine_settings_only(fake_lib, machine, caplog):
    caplog.set_level(logging.DEBUG)
    out = tlc.connect(TOKEN)
    assert out["ok"] and out["seeded"]
    assert f"ACL_LIBRARY_TOKEN={TOKEN}" in machine.read_text(encoding="utf-8") or TOKEN in machine.read_text(encoding="utf-8")
    assert TOKEN not in json.dumps(out)
    assert TOKEN not in caplog.text
    from dotenv import dotenv_values
    assert dotenv_values(machine)["ACL_LIBRARY_TOKEN"] == TOKEN       # файл читается обратно
    assert fake_lib.calls["check"] == [("MojoTheApe/acl-lib", "main")]
    assert tlc.state()["connected"] is True and TOKEN not in json.dumps(tlc.state())


def test_a_wrong_token_says_why_and_changes_nothing(fake_lib, machine, monkeypatch):
    monkeypatch.setattr(tl, "backend", lambda: None)
    tr.add("Local only", "Chess")
    before = machine.read_text(encoding="utf-8")
    with pytest.raises(tlc.ConnectError) as err:
        tlc.connect("ghp_" + "x" * 30 + "BAD")
    assert "token" in str(err.value) and "ghp_" not in str(err.value)
    assert machine.read_text(encoding="utf-8") == before        # токен не сохранён
    assert tlc.state()["connected"] is False
    assert [t.name for t in tr.all_targets()] == ["Local only"]   # местные данные целы


def test_an_unexpected_error_gives_a_fixed_text_without_the_value(monkeypatch, fake_lib):
    def boom(self):
        raise RuntimeError(f"Invalid header value Bearer {self.token}")

    monkeypatch.setattr(tl.GitHubBackend, "check", boom)
    with pytest.raises(tlc.ConnectError) as err:
        tlc.connect(TOKEN)
    assert TOKEN not in str(err.value) and TOKEN not in repr(err.value)
    assert err.value.__cause__ is None and err.value.__suppress_context__   # цепочка не выдаст значение


def test_the_lost_connection_message_does_not_carry_the_token(monkeypatch):
    b = tl.GitHubBackend("a/b", TOKEN)
    import urllib.error

    def refuse(*a, **k):
        raise urllib.error.URLError(f"failed for {TOKEN}")

    monkeypatch.setattr("urllib.request.urlopen", refuse)
    with pytest.raises(tl.LibraryError) as err:
        b.check()
    assert TOKEN not in str(err.value)


def test_http_errors_are_redacted(monkeypatch):
    b = tl.GitHubBackend("a/b", TOKEN)
    monkeypatch.setattr(b, "_call", lambda *a, **k: (500, json.dumps({"message": f"oops {TOKEN}"}).encode()))
    with pytest.raises(tl.LibraryError) as err:
        b.check()
    assert TOKEN not in str(err.value)


def test_a_read_only_token_is_refused_with_a_reason(monkeypatch):
    b = tl.GitHubBackend("a/b", TOKEN)
    monkeypatch.setattr(b, "_call", lambda *a, **k: (200, json.dumps({"permissions": {"push": False}}).encode()))
    with pytest.raises(tl.LibraryError, match="cannot change"):
        b.check()
    monkeypatch.setattr(b, "_call", lambda *a, **k: (200, json.dumps({"permissions": {"push": True}}).encode()))
    b.check()


# ── наполнение: только недостающее ─────────────────────────────────────────
def test_connect_adds_local_items_and_a_second_connect_adds_none(fake_lib, monkeypatch):
    monkeypatch.setattr(tl, "backend", lambda: None)
    tr.add("Invest", "Finance")
    tr.add("Travel", "Hotels")
    ar.add("ES how much", "Head", "Body")
    monkeypatch.setattr(tl, "backend", lambda: fake_lib)
    first = tlc.connect(TOKEN)
    assert first["added"] == {"targets": 2, "ad_texts": 1}
    assert "2 targets and 1 ad text added" in tlc.summary(first["added"])
    second = tlc.connect(TOKEN)
    assert second["added"] == {"targets": 0, "ad_texts": 0}
    assert "Nothing from this machine needed adding" in tlc.summary(second["added"])


def test_connect_never_overwrites_or_deletes_what_the_team_has(fake_lib, monkeypatch):
    mem = fake_lib
    monkeypatch.setattr(tl, "backend", lambda: None)       # пока не подключены, правки только местные
    tr.add("Invest", "My value")             # то же имя, другое значение
    tr.add("Mine", "Chess")
    monkeypatch.setattr(tl, "backend", lambda: mem)

    team = tl.empty("targets")
    team["targets"]["Invest"] = {"name": "Invest", "interests": "Team value", "narrow": ""}
    mem.put("targets.json", tl._dump(team), None, "team")
    tlc.connect(TOKEN)
    remote = tl._parse("targets", mem.get("targets.json"))
    assert remote["targets"]["Invest"]["interests"] == "Team value"   # не перезаписано
    assert "Mine" in remote["targets"]


def test_a_failed_seed_keeps_the_connection_and_says_so(monkeypatch, fake_lib):
    def bad_seed():
        raise tl.LibraryError("GitHub is unreachable.")

    monkeypatch.setattr(tlc, "_seed_counts", lambda local: ({"targets": 1}, "GitHub is unreachable."))
    out = tlc.connect(TOKEN)
    assert out["ok"] and out["seeded"] is False and "unreachable" in out["seed_error"]
    assert tlc.state()["connected"] is True
    assert "Nothing from this machine" not in tlc.summary(out["added"], True)


# ── отключение ─────────────────────────────────────────────────────────────
def test_disconnect_removes_the_token_and_keeps_local_data(fake_lib, machine, monkeypatch):
    monkeypatch.setattr(tl, "backend", lambda: None)
    tr.add("Invest", "Finance")
    monkeypatch.setattr(tl, "backend", lambda: fake_lib)
    tlc.connect(TOKEN)
    tlc.disconnect()
    assert TOKEN not in machine.read_text(encoding="utf-8")
    assert tlc.state()["connected"] is False
    assert [t.name for t in tr.all_targets()] == ["Invest"]


# ── HTTP ───────────────────────────────────────────────────────────────────
@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from src.web import app as web_app

    return TestClient(web_app.app, base_url="http://127.0.0.1")


def test_the_endpoint_never_echoes_the_token(client, fake_lib, caplog):
    caplog.set_level(logging.DEBUG)
    r = client.post("/api/team-library/connect", json={"token": TOKEN})
    assert r.status_code == 200 and r.json()["ok"]
    assert TOKEN not in r.text and TOKEN not in caplog.text
    assert "added to the team library" in r.json()["message"] or "Nothing from this machine" in r.json()["message"]
    s = client.get("/api/team-library")
    assert s.json()["connected"] is True and TOKEN not in s.text


def test_the_endpoint_error_does_not_echo_the_token(client, fake_lib):
    bad = "ghp_" + "y" * 30 + "BAD"
    r = client.post("/api/team-library/connect", json={"token": bad})
    assert r.status_code == 400 and bad not in r.text and "token" in r.json()["error"]


@pytest.mark.parametrize("body", [{}, {"token": 5}, {"token": None}, [1, 2]])
def test_the_endpoint_refuses_a_missing_or_non_string_token(client, fake_lib, body):
    r = client.post("/api/team-library/connect", json=body)
    assert r.status_code == 400 and not r.json()["ok"]


def test_the_endpoint_turns_surprises_into_a_fixed_text(client, monkeypatch):
    def boom(token):
        raise RuntimeError(f"disk said {token}")

    monkeypatch.setattr(tlc, "connect", boom)
    r = client.post("/api/team-library/connect", json={"token": TOKEN})
    assert r.status_code == 500 and TOKEN not in r.text


def test_disconnect_endpoint(client, fake_lib):
    client.post("/api/team-library/connect", json={"token": TOKEN})
    assert client.post("/api/team-library/disconnect").json() == {"ok": True}
    assert client.get("/api/team-library").json()["connected"] is False


# ── страница ───────────────────────────────────────────────────────────────
def test_targeting_and_ad_titles_offer_connect_when_not_connected_and_disconnect_when_connected():
    src = (Path(__file__).resolve().parent.parent / "src" / "web" / "templates" / "report.html").read_text(encoding="utf-8")
    assert "tlDecorate(el('tg-status'), data.library);" in src
    assert "tlDecorate(el('at-status'), d.library);" in src
    assert "'Connect team library'" in src and "'Disconnect'" in src
    block = src[src.index("window.aclTeamLibrary = (function"): src.index("// ── Таргеты: общая библиотека")]
    assert 'type="password"' in block and 'autocomplete="new-password"' in block
    assert "console." not in block and "localStorage" not in block and "location.search" not in block
    assert "input.value = ''" in block        # поле очищается после отправки


def test_a_token_that_can_read_but_not_write_is_not_kept(monkeypatch, fake_lib, machine):
    """Права fine-grained токена на чтение не видны в проверке репозитория: отказ
    при записи откатывает подключение."""
    monkeypatch.setattr(tlc, "_seed_counts", lambda local: ({}, "Could not save to the team library: "
                        "the library token is missing access or has expired."))
    with pytest.raises(tlc.ConnectError, match="accepted for reading"):
        tlc.connect(TOKEN)
    assert TOKEN not in machine.read_text(encoding="utf-8") and tlc.state()["connected"] is False


def test_a_seed_that_did_not_arrive_is_retried_from_the_first_snapshot(monkeypatch, fake_lib):
    monkeypatch.setattr(tl, "backend", lambda: None)
    tr.add("Invest", "Finance")
    monkeypatch.setattr(tl, "backend", lambda: fake_lib)
    real = tlc.seed.seed
    calls = {"n": 0}

    def flaky(kind, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise tl.LibraryError("GitHub is unreachable.")
        return real(kind, **kw)

    monkeypatch.setattr(tlc.seed, "seed", flaky)
    first = tlc.connect(TOKEN)                       # targets не доехали, ad_texts доехали
    assert first["added"].get("targets") is None and first["seeded"] is False
    # открытие раздела подтянуло общую (пустую) копию поверх местной
    monkeypatch.setattr(tr, "snapshot", lambda: {"targets": {}})
    monkeypatch.setitem(tlc.seed.KINDS, "targets", (tr.snapshot,) + tlc.seed.KINDS["targets"][1:])
    second = tlc.connect(TOKEN)
    assert second["added"]["targets"] == 1           # взято из сохранённого снимка


def test_cross_site_post_is_refused(client, fake_lib):
    r = client.post("/api/team-library/connect", json={"token": TOKEN},
                    headers={"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"})
    assert r.status_code >= 400 and tlc.state()["connected"] is False
    r = client.post("/api/team-library/connect", json={"token": TOKEN}, headers={"Host": "evil.example"})
    assert r.status_code == 400


def test_disconnect_with_a_repository_set_does_not_strand_the_machine(client, fake_lib, monkeypatch):
    monkeypatch.setenv("ACL_LIBRARY_REPO", "MojoTheApe/acl-lib")
    client.post("/api/team-library/connect", json={"token": TOKEN})
    client.post("/api/team-library/disconnect")
    assert not tl.configured()
