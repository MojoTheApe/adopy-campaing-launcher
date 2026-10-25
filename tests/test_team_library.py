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


def initialized(backend):
    for kind, (path, _) in tl.FILES.items():
        backend.files[path] = (tl._dump(tl.empty(kind)), f"seed-{kind}")
    return backend


@pytest.fixture
def team(tmp_path, monkeypatch):
    backend = initialized(tl.MemoryBackend())
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
    tl.use_backend(initialized(Racing(other_machine)))
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
    tl.use_backend(initialized(Racing(other_machine)))
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
    backend = initialized(Down())
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


# Reviewer regressions: preservation and truthful save outcomes.
@pytest.mark.parametrize("kind", ["targets", "ad_texts"])
def test_missing_remote_file_preserves_the_pre_migration_library(team, kind):
    backend, on = team
    on("Anna")
    registry = tr if kind == "targets" else ar
    if kind == "targets":
        tr.add("Keep", "Finance")
    else:
        ar.add("Keep", "Headline", "Body")
    before = registry._store().read_bytes()
    del backend.files[tl.FILES[kind][0]]
    state = registry.pull(force=True)
    assert state["ok"] is False and "missing" in state["error"]
    assert registry._store().read_bytes() == before
    with pytest.raises((tr.TargetingError, ar.AdTextError), match="Seed"):
        registry.remove("Keep")
    assert tl.FILES[kind][0] not in backend.files


@pytest.mark.parametrize("kind,broken", [
    ("targets", {"name": "Keep"}),
    ("targets", {"name": "Keep", "interests": 123}),
    ("ad_texts", {"name": "Keep", "headline": ""}),
    ("ad_texts", {"name": "Keep", "headline": 123}),
])
def test_invalid_remote_rows_do_not_replace_a_good_read_copy(team, kind, broken):
    backend, on = team
    on("Anna")
    registry = tr if kind == "targets" else ar
    if kind == "targets":
        tr.add("Keep", "Finance")
    else:
        ar.add("Keep", "Headline", "Body")
    before = registry._store().read_bytes()
    path, key = tl.FILES[kind]
    data = tl.empty(kind)
    data[key]["Keep"] = broken
    backend.files[path] = (tl._dump(data), "broken")
    state = registry.pull(force=True)
    assert not state["ok"] and "damaged" in state["error"]
    assert registry._store().read_bytes() == before


def test_invalid_deleted_record_does_not_poison_the_read_copy(team):
    backend, on = team
    on("Anna")
    tr.add("Keep", "Finance")
    before = tr._store().read_bytes()
    data = tl.fetch("targets")
    data["deleted"]["Gone"] = None
    backend.files["targets.json"] = (tl._dump(data), "broken")
    assert not tr.pull(force=True)["ok"]
    assert tr._store().read_bytes() == before


def test_migration_keeps_original_data_for_later_add_missing(team):
    backend, on = team
    tl.use_backend(None)
    on("Anna")
    tr.add("Local-only", "Finance")
    before = tr._store().read_bytes()
    data = tl.empty("targets")
    data["targets"]["Team"] = tr.Target("Team", "Travel").to_dict()
    backend.files["targets.json"] = (tl._dump(data), "team")
    tl.use_backend(backend)
    tr.pull(force=True)
    backup = tr._store().with_name("targeting_registry.pre-team-library.json")
    assert backup.read_bytes() == before
    tr.pull(force=True)
    assert backup.read_bytes() == before
    assert names() == ["Team"]


def test_a_repository_without_a_token_is_local_and_not_connected(team, monkeypatch):
    """ACL-2038: репозиторий по умолчанию есть всегда, подключение — это токен."""
    _, on = team
    on("Anna")
    tl.use_backend(None)
    monkeypatch.setenv("ACL_LIBRARY_REPO", "example/library")
    monkeypatch.setenv("ACL_LIBRARY_TOKEN", "")
    assert not tl.configured() and tl.backend() is None
    tr.add("Keep", "Finance")
    assert names() == ["Keep"]
    assert tr.pull(force=True) == {"connected": False, "ok": True, "error": "", "synced_at": ""}


def test_confirmed_remote_save_survives_a_local_disk_error(team, monkeypatch):
    backend, on = team
    on("Anna")
    def fail(_):
        raise OSError("disk full")
    monkeypatch.setattr(tr, "_save_raw", fail)
    result = tr.add("Keep", "Finance")
    assert result.name == "Keep"
    assert "Keep" in json.loads(backend.files["targets.json"][0])["targets"]
    assert not tl.status("targets")["ok"]
    assert "Saved to the team library" in tl.status("targets")["error"]


def test_an_uncertain_put_is_confirmed_by_readback_without_repeating(team, monkeypatch):
    backend, on = team
    on("Anna")
    put = backend.put
    def lose_response(*args):
        put(*args)
        raise tl.WriteUncertain("response lost")
    monkeypatch.setattr(backend, "put", lose_response)
    assert tr.add("Keep", "Finance").name == "Keep"
    assert len(backend.commits) == 1 and names() == ["Keep"]


def test_unconfirmed_save_does_not_claim_nothing_was_saved(team, monkeypatch):
    backend, on = team
    on("Anna")
    def fail(*args):
        raise tl.WriteUncertain("response lost")
    monkeypatch.setattr(backend, "put", fail)
    with pytest.raises(tr.TargetingError) as error:
        tr.add("Keep", "Finance")
    assert "Nothing was saved" not in str(error.value)
    assert "Refresh" in str(error.value)


def test_a_delayed_pull_cannot_overwrite_a_newer_successful_save(team, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    backend, on = team
    on("Anna")
    tr.add("Old", "Finance")
    fetched, release, writing = threading.Event(), threading.Event(), threading.Event()
    get = backend.get
    def delayed(path):
        result = get(path)
        if threading.current_thread().name == "slow-pull":
            fetched.set()
            assert release.wait(5)
        return result
    monkeypatch.setattr(backend, "get", delayed)
    def pull():
        threading.current_thread().name = "slow-pull"
        return tr.pull(force=True)
    def add():
        writing.set()
        return tr.add("New", "Travel")
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(pull)
        assert fetched.wait(5)
        second = pool.submit(add)
        assert writing.wait(5)
        # Before the fix the add finishes while the older read is still held.
        try:
            second.result(timeout=0.2)
        except TimeoutError:
            pass
        finally:
            release.set()
        first.result(timeout=5)
        second.result(timeout=5)
    assert names() == ["New", "Old"]


def test_default_seed_does_not_resurrect_a_previously_emptied_library():
    from scripts.seed_team_library import plan
    remote = tl.empty("targets")
    remote["deleted"]["Removed"] = {"data": tr.Target("Removed", "Travel").to_dict(),
                                     "deleted_by": "Anna", "deleted_at": "earlier"}
    local = tl.empty("targets")
    local["targets"]["Old alias"] = tr.Target("Old alias", "Travel").to_dict()
    items, skipped = plan("targets", remote, local, False)
    assert not items and skipped


@pytest.mark.parametrize("kind", ["targets", "ad_texts"])
def test_cache_age_survives_restart_during_an_outage(team, monkeypatch, kind):
    backend, on = team
    on("Anna")
    registry = tr if kind == "targets" else ar
    registry.pull(force=True)
    stamp = tl.status(kind)["synced_at"]
    tl._pulled.clear()
    tl._status.clear()
    def unavailable(_):
        raise tl.LibraryError("offline")
    monkeypatch.setattr(backend, "get", unavailable)
    assert registry.pull(force=True)["synced_at"] == stamp


def test_unknown_team_target_cannot_silently_become_broad_targeting(team):
    _, on = team
    on("Anna")
    with pytest.raises(tr.TargetingError, match="not available"):
        tr.resolve_interests("Missing")


def test_publish_refreshes_recent_builder_data_and_reports_offline_copy(team, monkeypatch, tmp_path):
    from types import SimpleNamespace
    from src.fb_ads_rpa import enrich_draft as en
    from src.fb_ads_rpa.draft import CampaignDraft, AdSetDraft
    backend, on = team
    # Память «стянуто меньше минуты назад» — на процесс: другой тест мог
    # стянуть targets только что, и тогда первый запуск здесь не тянет.
    monkeypatch.setattr(en, "_forced_pull_at", {})
    on("Anna")
    tr.add("Keep", "Old interests")
    data = tl.fetch("targets")
    data["targets"]["Keep"]["interests"] = "Latest interests"
    remote = backend.get("targets.json")
    backend.put("targets.json", tl._dump(data), remote.sha, "other manager")
    monkeypatch.setattr(en, "load_ad_account_map", lambda p: {})
    monkeypatch.setattr(en, "resolve_account_entry", lambda *a: SimpleNamespace(
        ad_account_id="act_test", page_id="page", profile_id="profile"))
    def enrich():
        draft = CampaignDraft("Test", "CL", "Test", ad_sets=[AdSetDraft("Set", targeting_label="Keep")])
        en.enrich_campaign_drafts([draft], account_label="Test", upload_fb_path=str(tmp_path/"none"),
                                 ad_account_map_path=str(tmp_path/"map"), creatives_root=str(tmp_path))
        return draft
    assert enrich().ad_sets[0].targeting_label == "Latest interests"
    def unavailable(_):
        raise tl.LibraryError("offline")
    monkeypatch.setattr(backend, "get", unavailable)
    # ACL-1313: стянутое меньше минуты назад запуск повторно не тянет
    # (enrich_draft.LIBRARY_FRESH_S) — «прошла минута».
    monkeypatch.setitem(en._forced_pull_at, "targets", en._forced_pull_at["targets"] - en.LIBRARY_FRESH_S - 1)
    draft = enrich()
    assert draft.ad_sets[0].targeting_label == "Latest interests"
    assert any("saved copy from" in w for w in draft.warnings)


def test_github_lost_put_response_is_uncertain_and_never_leaks_exception_text(monkeypatch):
    import urllib.error
    gh = tl.GitHubBackend("example/library", "test-secret")
    def timeout(*a, **kw):
        raise urllib.error.URLError("test-secret")
    monkeypatch.setattr(tl.urllib.request, "urlopen", timeout)
    with pytest.raises(tl.WriteUncertain) as error:
        gh.put("targets.json", "{}", "old", "test")
    assert "test-secret" not in str(error.value)
    assert "Nothing was saved" not in str(error.value)


def test_unreadable_github_response_is_a_named_library_error(monkeypatch):
    gh = tl.GitHubBackend("example/library", "synthetic")
    monkeypatch.setattr(gh, "_call", lambda *a, **k: (200, b"not json"))
    with pytest.raises(tl.LibraryError, match="unreadable"):
        gh.get("targets.json")


def test_api_save_reports_its_actual_scope(team):
    from src.web import app as web
    backend, on = team
    on("Anna")
    target = json.loads(web._edit_targeting_profile({"name": "Team", "interests": "Finance"}).body)
    text = json.loads(web._edit_ad_text({"name": "Team", "headline": "Headline"}).body)
    assert target["scope"] == text["scope"] == "team"
    tl.use_backend(None)
    target = json.loads(web._edit_targeting_profile({"name": "Local", "interests": "Travel"}).body)
    text = json.loads(web._edit_ad_text({"name": "Local", "headline": "Other headline"}).body)
    assert target["scope"] == text["scope"] == "local"


def test_offline_publish_retries_are_throttled_across_ads(team, monkeypatch):
    backend, on = team
    on('Anna')
    tr.add('Keep', 'Finance')
    tl._pulled.clear()
    calls = []
    def offline(path):
        calls.append(path)
        raise tl.LibraryError('offline')
    monkeypatch.setattr(backend, 'get', offline)
    for _ in range(3):
        assert tr.resolve_interests('Keep') == 'Finance'
    assert calls == ['targets.json']
    assert not tl.status('targets')['ok']
