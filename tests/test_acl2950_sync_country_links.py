"""ACL-2950: страна синхронизированного аккаунта попадает на карту Home (хранилище связей)."""
import pytest

from src.web import account_links, user_accounts as ua
from tests.test_acl2899_user_accounts import (  # noqa: F401  (фикстура env и помощники)
    Hub, acc, env, read_map, run, write_map,
)


@pytest.fixture(autouse=True)
def _buyer(monkeypatch, env):
    monkeypatch.setattr("src.web.buyer.get_selected_cm", lambda: "")
    monkeypatch.setattr(account_links, "get_local_config_dir", lambda: str(env))
    monkeypatch.setattr(account_links, "get_ad_account_map_path", lambda: str(env / "map.csv"))


def forget_handled():
    """Как на машине до выпуска: синхронизация ещё не определила ничьих стран."""
    st = ua.load_state()
    ua._save_state(st["names"], st["etag"], placed=[])


def links():
    return [(r["account_name"], r["value"]) for r in account_links.load() if r["kind"] == "country"]


def groups():
    return sorted({v for _, v in links()}, key=str.casefold)


def raw(env):
    p = env / "account_links.csv"
    return p.read_bytes() if p.exists() else b""


# ── основное ─────────────────────────────────────────────────────────────

def test_each_synced_account_is_placed_under_its_country(env):
    r = run(Hub([acc("Alpha", country="Peru"), acc("Beta", country="Chile"), acc("Gamma", country="Peru")]))
    assert r["status"] == "updated" and r["placed"] == 3
    assert sorted(links()) == [("Alpha", "Peru"), ("Beta", "Chile"), ("Gamma", "Peru")]
    assert account_links.available_accounts(["Alpha", "Beta", "Gamma"]) == []


def test_code_joins_an_existing_full_name_group(env):
    account_links.link("Old", "country", "Saudi Arabia")
    run(Hub([acc("New", country="SA")]))
    assert groups() == ["Saudi Arabia"] and ("New", "Saudi Arabia") in links()


def test_full_name_joins_an_existing_code_group(env):
    account_links.link("Old", "country", "SA")
    run(Hub([acc("New", country="Saudi Arabia")]))
    assert groups() == ["SA"] and ("New", "SA") in links()


def test_code_and_full_name_in_one_sync_make_one_group(env):
    run(Hub([acc("A", country="SA"), acc("B", country="Saudi Arabia"), acc("C", country="saudi arabia")]))
    assert len(groups()) == 1
    assert {n for n, _ in links()} == {"A", "B", "C"}


def test_group_spelling_comes_before_the_map_spelling(env):
    account_links.link("Old", "country", "Kuwait")
    run(Hub([acc("New", country="kw")]))
    assert ("New", "Kuwait") in links() and groups() == ["Kuwait"]


def test_a_hand_placement_is_never_overwritten_or_doubled(env):
    account_links.link("Alpha", "country", "Chile")
    run(Hub([acc("Alpha", country="Peru")]))
    assert links() == [("Alpha", "Chile")]
    assert read_map(env)["Alpha"]["country"] == "Peru"        # карта по-прежнему отдаёт серверную страну


def test_resync_changes_nothing(env):
    hub = Hub([acc("Alpha", country="Peru"), acc("Beta", country="SA")])
    run(hub)
    before = raw(env)
    r = run(hub)                                               # 304: список не менялся
    assert r["status"] == "not_modified" and "placed" not in r
    assert raw(env) == before
    r = run(Hub([acc("Alpha", country="Peru"), acc("Beta", country="SA")], etag='"e2"'))   # новый список, те же данные
    assert r["status"] == "updated" and "placed" not in r and raw(env) == before


def test_an_account_synced_earlier_is_placed_on_the_next_sync(env):
    run(Hub([acc("Early", country="Peru")]))
    (env / "account_links.csv").unlink()                       # как на машине до выпуска: связей нет
    forget_handled()
    r = run(Hub([acc("Early", country="Peru")]))               # 304
    assert r["status"] == "not_modified" and r["placed"] == 1 and r["changed"] is True
    assert links() == [("Early", "Peru")]


def test_a_link_failure_does_not_fail_the_sync_and_is_noted_per_account(env, monkeypatch):
    def broken(pairs, buyer=None):
        raise PermissionError("read-only")

    monkeypatch.setattr(account_links, "link_many", broken)
    r = run(Hub([acc("One", country="Peru"), acc("Two", country="Peru"), acc("NoCountry")]))
    assert r["ok"] and r["status"] == "updated" and "placed" not in r
    assert sorted(r["place_notes"]) == ["One", "Two"]           # заметка у тех, кого не удалось поставить
    assert {"One", "Two", "NoCountry"} <= set(read_map(env))    # карта слита


def test_empty_country_stays_in_available_accounts(env):
    r = run(Hub([acc("NoCountry", country=""), acc("Spaces", country="   ")]))
    assert r["status"] == "updated" and "placed" not in r and "place_notes" not in r
    assert links() == []
    assert account_links.available_accounts(["NoCountry", "Spaces"]) == ["NoCountry", "Spaces"]


# ── разбор входов ────────────────────────────────────────────────────────

def test_name_variants_match_an_existing_placement(env):
    account_links.link("alpha 1", "country", "Chile")
    run(Hub([acc("Alpha   1", country="Peru")]))               # другие пробелы и регистр: тот же аккаунт
    assert groups() == ["Chile"] and len(links()) == 1
    account_links.link("Beta 2", "country", "Chile")
    run(Hub([acc("Beta" + chr(0x200B) + " 2", country="Peru")], etag='"e2"'))   # невидимый символ
    assert len(links()) == 2


def test_account_removed_from_the_hub_is_not_placed(env):
    run(Hub([acc("Keep", country="Peru"), acc("Gone", country="Peru")]))
    account_links.unlink("Gone", "country", "Peru")
    forget_handled()
    run(Hub([acc("Keep", country="Peru"), acc("Gone", country="Peru", removed_at="2026-11-26T10:00:00Z")], etag='"e2"'))
    assert links() == [("Keep", "Peru")]
    run(Hub([acc("Keep", country="Peru"), acc("Gone", country="Peru", removed_at="2026-11-26T10:00:00Z")], etag='"e2"'))
    assert links() == [("Keep", "Peru")]                        # и на 304 тоже


def test_account_hidden_by_hand_is_not_placed(env):
    from src.web import hidden_accounts as ha
    ha.set_hidden("Hid", True)
    run(Hub([acc("Hid", country="Peru"), acc("Shown", country="Peru")]))
    assert links() == [("Shown", "Peru")]


def test_unknown_country_is_kept_as_written(env):
    run(Hub([acc("A", country="Atlantis")]))
    assert links() == [("A", "Atlantis")]


@pytest.mark.parametrize("bad", ["=SUM(1)", "+1", "-x", "@a", "x" * 61, "Pe" + chr(0x9B) + "ru"])
def test_an_unusable_country_is_noted_and_not_written(env, bad):
    r = run(Hub([acc("A", country=bad), acc("B", country="Peru")]))
    assert r["status"] == "updated" and links() == [("B", "Peru")]
    assert list(r["place_notes"]) == ["A"]


def test_invisible_characters_are_stripped_from_the_country(env):
    run(Hub([acc("A", country="Pe" + chr(0xFEFF) + "ru  ")]))
    assert links() == [("A", "Peru")]


def test_duplicate_names_in_the_map_make_one_link(env):
    write_map(env, [{"account_name": "Dup", "country": "Peru"}, {"account_name": "dup", "country": "Chile"}])
    ua.place_countries(["Dup"], [])
    assert len(links()) == 1


def test_manual_accounts_not_from_the_hub_are_not_touched(env):
    write_map(env, [{"account_name": "Mine", "country": "Peru"}])
    run(Hub([acc("Server", country="Chile")]))
    assert ("Server", "Chile") in links() and read_map(env)["Mine"]["country"] == "Peru"
    run(Hub([acc("Server", country="Chile"), acc("Other", country="Chile")], etag='"e2"'))
    assert ("Mine", "Peru") in links() and len(links()) == 3


def test_missing_links_file_is_created(env):
    assert not (env / "account_links.csv").exists()
    run(Hub([acc("A", country="Peru")]))
    assert (env / "account_links.csv").exists() and links() == [("A", "Peru")]


def test_read_only_links_file_does_not_fail_the_sync(env, monkeypatch):
    def deny(rows):
        raise PermissionError("read-only")

    monkeypatch.setattr(account_links, "_write", deny)
    r = run(Hub([acc("A", country="Peru"), acc("B", country="Peru")]))
    assert r["ok"] and r["status"] == "updated"
    assert sorted(r["place_notes"]) == ["A", "B"] and "A" in read_map(env)


def test_unreadable_links_file_does_not_fail_the_sync(env, monkeypatch):
    def boom():
        raise OSError("locked")

    monkeypatch.setattr(account_links, "load", boom)
    r = run(Hub([acc("A", country="Peru")]))
    assert r["ok"] and r["status"] == "updated" and "A" in read_map(env)


def test_a_missing_map_places_nothing(env):
    assert ua.place_countries(["A"], []) == {"placed": [], "failed": {}, "handled": []}


def test_empty_placeholder_country_is_replaced_by_the_first_account(env):
    account_links.add_value("country", "Peru")
    run(Hub([acc("A", country="Peru")]))
    assert links() == [("A", "Peru")]


def test_no_extra_files_or_state_fields(env):
    run(Hub([acc("A", country="Peru")]))
    state = ua.load_state()
    assert set(state) <= {"names", "etag", "sync_hidden", "seeded_for", "signin_prompted", "placed"}


# ── Fix round 1: один раз на аккаунт, одна запись, скрытые, испорченный файл, посев ──

def test_a_hand_unlink_is_not_undone_by_the_next_sync(env):
    hub = Hub([acc("Alpha", country="Peru"), acc("Beta", country="Peru")])
    run(hub)
    account_links.unlink("Alpha", "country", "Peru")
    for _ in range(3):
        r = run(hub)                                           # 304 при каждом открытии Home
        assert "placed" not in r
    run(Hub([acc("Alpha", country="Peru"), acc("Beta", country="Peru")], etag='"e2"'))   # и новый список
    assert links() == [("Beta", "Peru")]


def test_close_country_is_not_undone_by_the_next_sync(env):
    hub = Hub([acc("A", country="Peru"), acc("B", country="Peru")])
    run(hub)
    account_links.close_country("country", "Peru")
    r = run(hub)
    assert "placed" not in r and links() == [] and account_links.all_values("country") == []


def test_a_newly_assigned_account_is_still_placed_after_a_hand_removal(env):
    run(Hub([acc("A", country="Peru")]))
    account_links.close_country("country", "Peru")
    r = run(Hub([acc("A", country="Peru"), acc("New", country="Chile")], etag='"e2"'))
    assert r["placed"] == 1 and links() == [("New", "Chile")]


def test_the_handled_keys_live_in_the_state_file_and_start_empty(env):
    assert ua.load_state()["placed"] == []
    run(Hub([acc("Alpha  1", country="Peru")]))
    assert ua.load_state()["placed"] == ["alpha 1"]


def test_a_failed_write_is_retried_on_the_next_sync(env, monkeypatch):
    real = account_links._write
    monkeypatch.setattr(account_links, "_write", lambda rows: (_ for _ in ()).throw(PermissionError("ro")))
    hub = Hub([acc("A", country="Peru")])
    assert "place_notes" in run(hub)
    monkeypatch.setattr(account_links, "_write", real)
    assert run(hub)["placed"] == 1 and links() == [("A", "Peru")]


def test_all_new_links_are_written_once(env, monkeypatch):
    calls = []
    real = account_links._write
    monkeypatch.setattr(account_links, "_write", lambda rows: (calls.append(len(rows)), real(rows))[1])
    run(Hub([acc(f"Acc {i}", country="Peru") for i in range(40)]))
    assert calls == [40] and len(links()) == 40


def test_writes_to_the_links_file_take_turns(env):
    import threading

    assert isinstance(account_links._links_lock, type(threading.RLock()))
    held = []
    with account_links._links_lock:                            # Home держит замок: связи не пишутся параллельно
        t = threading.Thread(target=lambda: (account_links.link("X", "country", "Peru"), held.append(1)))
        t.start()
        t.join(0.3)
        assert held == [] and t.is_alive()
    t.join(5)
    assert held == [1] and links() == [("X", "Peru")]


def test_accounts_removed_from_available_accounts_are_not_placed(env):
    account_links.hide_accounts(["Quiet"])
    run(Hub([acc("Quiet", country="Peru"), acc("Loud", country="Peru")]))
    assert links() == [("Loud", "Peru")]


def test_an_undecodable_links_file_gives_a_note(env):
    (env / "account_links.csv").write_bytes(b"account_name,kind,value\n" + b"\xff\xfe\xfa,country,Peru\n")
    r = run(Hub([acc("A", country="Peru")]))
    assert r["ok"] and r["status"] == "updated" and "A" in read_map(env)
    assert list(r["place_notes"]) == ["(links file)"]


def test_creating_the_links_file_still_seeds_manual_map_countries(env):
    write_map(env, [{"account_name": "Manual", "country": "Chile"}])
    assert not (env / "account_links.csv").exists()
    run(Hub([acc("Server", country="Peru")]))
    assert sorted(links()) == [("Manual", "Chile"), ("Server", "Peru")]
