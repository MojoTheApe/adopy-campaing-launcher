"""Общая библиотека таргетов и текстов между машинами (ACL-1241).

Две «машины» — два каталога конфигурации над одним хранилищем в памяти.
Проверяется то, ради чего всё затевалось: добавленное, изменённое, удалённое и
восстановленное одним видят все; правка без связи не сохраняется нигде.
"""
from __future__ import annotations

import json

import pytest

from src.analysis_agent import ad_text_registry as ar
from src.analysis_agent import targeting_registry as tr
from src.analysis_agent import team_library as tl


@pytest.fixture
def team(tmp_path, monkeypatch):
    backend = tl.MemoryBackend()
    tl.use_backend(backend)
    monkeypatch.setattr(tl, "_status", {})

    def on(machine: str) -> None:
        d = tmp_path / machine
        d.mkdir(exist_ok=True)
        (d / "selected_cm.txt").write_text(machine, encoding="utf-8")
        monkeypatch.setenv("ACL_CONFIG_DIR", str(d))
        tl._pulled.clear()

    yield backend, on
    tl.use_backend(None)


def names(kind="targets"):
    return [t.name for t in (tr.all_targets() if kind == "targets" else ar.all_texts())]


def test_a_target_added_on_one_machine_reaches_the_other(team):
    backend, on = team
    on("Anna")
    tr.add("Invest", "Finance, Trading")
    on("Boris")
    assert names() == []
    tr.pull()
    assert names() == ["Invest"]
    assert tr.lookup("Invest").added_by == "Anna"
    assert backend.commits == ["Add target Invest — Anna"]


def test_publishing_refreshes_the_copy_before_resolving(team):
    _, on = team
    on("Anna")
    tr.add("Invest", "Finance", "Owners")
    on("Boris")
    assert tr.resolve_interests("invest") == "Finance [AND] Owners"


def test_a_deletion_reaches_everyone_and_says_who_and_when(team):
    _, on = team
    on("Anna")
    tr.add("Invest", "Finance")
    on("Boris")
    tr.pull()
    tr.remove("Invest")
    on("Anna")
    tr.pull()
    assert names() == []
    [gone] = tr.recently_deleted()
    assert gone["name"] == "Invest" and gone["deleted_by"] == "Boris" and gone["deleted_at"]
    assert gone["item"]["interests"] == "Finance"
    with pytest.raises(tr.TargetingError, match="removed from the library"):
        tr.resolve_interests("Invest")


def test_a_restore_reaches_everyone(team):
    _, on = team
    on("Anna")
    tr.add("Invest", "Finance")
    tr.remove("Invest")
    on("Boris")
    tr.restore("Invest")
    on("Anna")
    tr.pull()
    assert names() == ["Invest"] and tr.recently_deleted() == []
    assert tr.lookup("Invest").updated_by == "Boris"


def test_adding_the_same_name_again_brings_it_back_from_deleted(team):
    _, on = team
    on("Anna")
    tr.add("Invest", "Finance")
    tr.remove("Invest")
    tr.add("Invest", "Crypto")
    assert tr.recently_deleted() == [] and tr.lookup("Invest").interests == "Crypto"


def test_an_edit_reaches_everyone_and_a_rename_is_named_at_publish(team):
    _, on = team
    on("Anna")
    tr.add("Invest", "Finance")
    on("Boris")
    tr.update("Invest", "Invest Pro", "Finance, Stocks")
    on("Anna")
    tr.pull()
    assert names() == ["Invest Pro"] and tr.recently_deleted() == []
    with pytest.raises(tr.TargetingError, match="renamed to “Invest Pro”"):
        tr.resolve_interests("Invest")


def test_the_duplicate_check_sees_what_others_added_since_the_last_refresh(team):
    """Копия у Бориса старая, но проверка идёт по свежей версии библиотеки."""
    _, on = team
    on("Boris")
    tr.pull()
    on("Anna")
    tr.add("Invest", "Finance, Trading")
    on("Boris")
    with pytest.raises(tr.TargetingError, match="already exists as “Invest”"):
        tr.add("Money", "Trading, Finance")
    with pytest.raises(tr.TargetingError, match="already taken"):
        tr.add("invest", "Crypto")


def test_editing_something_removed_elsewhere_is_refused_by_name(team):
    _, on = team
    on("Anna")
    tr.add("Invest", "Finance")
    on("Boris")
    tr.pull()
    on("Anna")
    tr.remove("Invest")
    on("Boris")
    with pytest.raises(tr.TargetingError, match="removed on another machine"):
        tr.update("Invest", "Invest", "Finance, Stocks")


class Racing(tl.MemoryBackend):
    """Между чтением и записью файл меняет другая машина — один раз."""

    def __init__(self, sneak):
        super().__init__()
        self.sneak = sneak

    def put(self, path, text, sha, message):
        if self.sneak:
            sneak, self.sneak = self.sneak, None
            sneak(self)
        return super().put(path, text, sha, message)


def test_two_managers_saving_at_once_both_keep_their_change(tmp_path, monkeypatch):
    def other_machine(backend):
        data = tl._parse("targets", backend.get("targets.json"))
        data["targets"]["Crypto"] = tr.Target("Crypto", "Bitcoin").to_dict()
        tl.MemoryBackend.put(backend, "targets.json", tl._dump(data), backend.get("targets.json").sha, "other")

    monkeypatch.setenv("ACL_CONFIG_DIR", str(tmp_path))
    tl.use_backend(Racing(other_machine))
    try:
        tr.add("Invest", "Finance")
        assert names() == ["Crypto", "Invest"]
    finally:
        tl.use_backend(None)


def test_a_racing_duplicate_is_caught_on_the_retry(tmp_path, monkeypatch):
    def other_machine(backend):
        data = tl._parse("targets", backend.get("targets.json"))
        data["targets"]["Money"] = tr.Target("Money", "Finance").to_dict()
        tl.MemoryBackend.put(backend, "targets.json", tl._dump(data), backend.get("targets.json").sha, "other")

    monkeypatch.setenv("ACL_CONFIG_DIR", str(tmp_path))
    tl.use_backend(Racing(other_machine))
    try:
        with pytest.raises(tr.TargetingError, match="already exists as “Money”"):
            tr.add("Invest", "Finance")
    finally:
        tl.use_backend(None)


class Down(tl.MemoryBackend):
    def __init__(self):
        super().__init__()
        self.down = False

    def get(self, path):
        if self.down:
            raise tl.LibraryError("GitHub is unreachable: timed out")
        return super().get(path)


def test_without_github_nothing_is_saved_and_the_copy_still_publishes(tmp_path, monkeypatch):
    monkeypatch.setenv("ACL_CONFIG_DIR", str(tmp_path))
    backend = Down()
    tl.use_backend(backend)
    monkeypatch.setattr(tl, "_status", {})
    try:
        tr.add("Invest", "Finance")
        backend.down = True
        tl._pulled.clear()
        with pytest.raises(tr.TargetingError, match="unreachable.*Nothing was saved"):
            tr.add("Crypto", "Bitcoin")
        with pytest.raises(tr.TargetingError, match="Nothing was saved"):
            tr.remove("Invest")
        assert names() == ["Invest"]
        assert tr.resolve_interests("Invest") == "Finance"
        state = tr.pull(force=True)
        assert state["ok"] is False and "unreachable" in state["error"] and state["synced_at"]
    finally:
        tl.use_backend(None)


def test_ad_texts_travel_the_same_way(team):
    backend, on = team
    on("Anna")
    ar.add("ES how much", "¿Cómo funciona?", "Cuánto dinero\n\ntienes hoy", language="es")
    on("Boris")
    assert [t.body for t in ar.builder_texts()] == ["Cuánto dinero\n\ntienes hoy"]
    ar.remove("ES how much")
    on("Anna")
    ar.pull()
    assert names("ad_texts") == [] and ar.recently_deleted()[0]["deleted_by"] == "Boris"
    ar.restore("ES how much")
    assert json.loads(backend.files["ad_texts.json"][0])["texts"]["ES how much"]["language"] == "ES"


def test_a_damaged_library_file_changes_nothing(team):
    backend, on = team
    on("Anna")
    tr.add("Invest", "Finance")
    backend.files["targets.json"] = ("{not json", "broken")
    with pytest.raises(tr.TargetingError, match="damaged"):
        tr.add("Crypto", "Bitcoin")
    assert backend.files["targets.json"][0] == "{not json"
    assert names() == ["Invest"]


def test_not_connected_the_registry_works_on_this_machine(tmp_path, monkeypatch):
    monkeypatch.setenv("ACL_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("ACL_LIBRARY_REPO", raising=False)
    monkeypatch.delenv("ACL_LIBRARY_TOKEN", raising=False)
    tl.use_backend(None)
    tr.add("Invest", "Finance")
    tr.remove("Invest")
    assert tr.pull() == {"connected": False, "ok": True, "error": "", "synced_at": ""}
    assert tr.restore("Invest").interests == "Finance"


# ── первое наполнение ─────────────────────────────────────────────────────

def test_seeding_uploads_this_machines_library_once(team):
    from scripts import seed_team_library as seed
    backend, on = team
    tl.use_backend(None)
    on("Evgeni")
    tr.add("Invest", "Finance")
    ar.add("ES how much", "H", "B")
    tl.use_backend(backend)
    assert seed.seed("targets", add_missing=False, dry_run=False) == (1, [])
    assert seed.seed("ad_texts", add_missing=False, dry_run=False) == (1, [])
    n, skipped = seed.seed("targets", add_missing=False, dry_run=False)
    assert n == 0 and "already has 1" in skipped[0]
    on("Anna")
    tr.pull()
    assert names() == ["Invest"]


def test_add_missing_skips_taken_names_deleted_names_and_same_values(team):
    from scripts import seed_team_library as seed
    backend, on = team
    on("Evgeni")
    tr.add("Invest", "Finance")
    tr.add("Old", "Travel")
    tr.remove("Old")
    tl.use_backend(None)
    on("Anna")
    tr.add("Invest", "Other")        # имя занято
    tr.add("Old", "Travel, Hotels")  # имя в Recently deleted
    tr.add("Money", "Finance")       # тот же набор под другим именем
    tr.add("Crypto", "Bitcoin")      # нового нет ни у кого
    tl.use_backend(backend)
    n, skipped = seed.seed("targets", add_missing=True, dry_run=True)
    assert n == 1 and len(skipped) == 3
    assert "Invest" in tl.fetch("targets")["targets"] and "Crypto" not in tl.fetch("targets")["targets"]
    assert seed.seed("targets", add_missing=True, dry_run=False)[0] == 1
    assert sorted(tl.fetch("targets")["targets"]) == ["Crypto", "Invest"]


# ── GitHub ────────────────────────────────────────────────────────────────

def test_github_errors_are_plain_and_never_carry_the_token(monkeypatch):
    gh = tl.GitHubBackend("adopy/acl-team-library", "ghp_SECRET")
    monkeypatch.setattr(gh, "_call", lambda *a, **k: (401, b'{"message": "Bad credentials"}'))
    with pytest.raises(tl.LibraryError) as exc:
        gh.get("targets.json")
    assert "expired" in str(exc.value) and "SECRET" not in str(exc.value)


def test_github_missing_file_is_an_empty_library_but_missing_repo_is_an_error(monkeypatch):
    gh = tl.GitHubBackend("adopy/acl-team-library", "t")
    answers = {"contents": (404, b"{}"), "repo": (200, b"{}")}
    monkeypatch.setattr(gh, "_call", lambda m, url, *a, **k: answers["contents" if "/contents/" in url else "repo"])
    assert gh.get("targets.json") == tl.Remote(None, None)
    answers["repo"] = (404, b"{}")
    with pytest.raises(tl.LibraryError, match="was not found"):
        gh.get("targets.json")


def test_github_stale_sha_is_a_conflict(monkeypatch):
    gh = tl.GitHubBackend("adopy/acl-team-library", "t")
    monkeypatch.setattr(gh, "_call", lambda *a, **k: (409, b'{"message": "is at abc but expected def"}'))
    with pytest.raises(tl.Conflict):
        gh.put("targets.json", "{}", "def", "msg")
