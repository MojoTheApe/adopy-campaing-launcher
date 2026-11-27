"""ACL-2950: страна синхронизированного аккаунта попадает на карту Home (хранилище связей)."""
import pytest

from src.web import account_links, user_accounts as ua
from tests.test_acl2899_user_accounts import (  # noqa: F401  (фикстура env и помощники)
    Hub, acc, env, read_map, run, write_map,
)


@pytest.fixture(autouse=True)
def _buyer(monkeypatch):
    monkeypatch.setattr("src.web.buyer.get_selected_cm", lambda: "")


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
    r = run(Hub([acc("Early", country="Peru")]))               # 304
    assert r["status"] == "not_modified" and r["placed"] == 1 and r["changed"] is True
    assert links() == [("Early", "Peru")]


def test_a_link_failure_does_not_fail_the_sync_and_is_noted_for_that_account(env, monkeypatch):
    real = account_links.link

    def flaky(account, kind, value, buyer=None):
        if account == "Bad":
            raise PermissionError("read-only")
        return real(account, kind, value, buyer=buyer)

    monkeypatch.setattr(account_links, "link", flaky)
    r = run(Hub([acc("Good", country="Peru"), acc("Bad", country="Peru")]))
    assert r["ok"] and r["status"] == "updated" and r["placed"] == 1
    assert list(r["place_notes"]) == ["Bad"]
    assert "Bad" in read_map(env) and links() == [("Good", "Peru")]   # карта слита, остальное не пострадало


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
    for n in ("Keep", "Gone"):
        account_links.unlink(n, "country", "Peru")
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
    assert links() == [("Server", "Chile")]


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
    assert ua.place_countries(["A"], []) == {"placed": [], "failed": {}}


def test_empty_placeholder_country_is_replaced_by_the_first_account(env):
    account_links.add_value("country", "Peru")
    run(Hub([acc("A", country="Peru")]))
    assert links() == [("A", "Peru")]


def test_no_extra_files_or_state_fields(env):
    run(Hub([acc("A", country="Peru")]))
    state = ua.load_state()
    assert set(state) <= {"names", "etag", "sync_hidden", "seeded_for", "signin_prompted"}
