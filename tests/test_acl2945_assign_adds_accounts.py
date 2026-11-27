"""ACL-2945: в блоке «Assign accounts to…» строка «название, код страны» добавляет аккаунт в карту машины."""
from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from tests.test_acl2920_assign_follows_logged_as import (  # noqa: F401  (фикстура hub и помощники)
    D1, D2, _accounts, _post, _send, hub,
)

D3 = "3" * 16   # номер собираем в тесте: сканер секретов не любит длинные цифры в коде


def _preview(text):
    from src.web import app as app_mod

    return _post(app_mod.api_user_assign_preview, {"text": text})[1]["results"]


def _map_rows():
    from src.web import app as app_mod

    path = Path(app_mod.get_ad_account_map_path())
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _by_name(name):
    return next((r for r in _map_rows() if r["account_name"] == name), None)


# ── Check ────────────────────────────────────────────────────────────────

def test_check_shows_will_be_added_with_the_country_name(hub):
    (res,) = _preview("Gamma 9, PE")
    assert res["state"] == "add" and res["country"] == "Peru" and res["account_name"] == "Gamma 9"
    assert _by_name("Gamma 9") is None                                 # Check ничего не пишет


def test_country_code_is_case_insensitive_and_uses_the_spelling_the_map_already_has(hub):
    path = _map_path()
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("profile_id\n", "profile_id,country\n", 1)
                    .replace("p-1\n", "p-1,Saudi Arabia\n", 1), encoding="utf-8")
    (res,) = _preview("Gamma 9, sa")
    assert res["state"] == "add" and res["country"] == "Saudi Arabia"


def _map_path():
    from src.web import app as app_mod

    return Path(app_mod.get_ad_account_map_path())


# ── Assign ───────────────────────────────────────────────────────────────

def test_assign_adds_the_account_to_the_map_under_its_country_and_assigns_it(hub):
    status, data = _send("Dan Dicker", "Gamma 9, PE\nDelta 4, SA")
    assert data["ok"] and data["added"] == ["Gamma 9", "Delta 4"] and data["assigned"] == ["Gamma 9", "Delta 4"]
    assert _by_name("Gamma 9")["country"] == "Peru" and _by_name("Delta 4")["country"] == "Saudi Arabia"
    (post,) = [c for c in hub.calls if c[0] == "POST"]
    assert [r["account_name"] for r in post[3]["rows"]] == ["Gamma 9", "Delta 4"]
    assert [r["country"] for r in post[3]["rows"]] == ["Peru", "Saudi Arabia"]
    assert [x["account_name"] for x in hub.assigned["dan@adopy.test"] if not x["removed_at"]] == [
        "Alpha 1", "Gamma 9", "Delta 4"]
    assert all(r["sent"] for r in data["results"])


def test_the_number_is_optional_but_stored_when_given(hub):
    status, data = _send("Dan Dicker", f"Gamma 9, PE\nDelta 4, PE, act_{D3}")
    assert data["ok"]
    assert _by_name("Gamma 9")["ad_account_id"] == ""                  # без номера всё равно добавлен
    assert _by_name("Delta 4")["ad_account_id"] == "act_" + D3        # с номером — сохранён


def test_the_second_assign_of_the_same_line_does_not_add_twice(hub):
    assert _send("Dan Dicker", "Gamma 9, PE")[1]["added"] == ["Gamma 9"]
    status, data = _send("Dan Dicker", "Gamma 9, PE")                  # теперь аккаунт в карте: обычная найденная строка
    assert data["ok"] and data["added"] == []
    assert len([r for r in _map_rows() if r["account_name"] == "Gamma 9"]) == 1


# ── что остаётся «Not found» ─────────────────────────────────────────────

def test_unknown_country_code_stays_not_found_and_names_the_code(hub):
    (res,) = _preview("Gamma 9, ZZ")
    assert res["state"] == "not_found" and "ZZ" in res["error"]
    status, data = _send("Dan Dicker", "Gamma 9, ZZ")
    assert data["ok"] is False and _by_name("Gamma 9") is None


def test_no_country_for_an_unknown_account_stays_not_found(hub):
    (res,) = _preview("Gamma 9")
    assert res["state"] == "not_found" and not res.get("error")
    status, data = _send("Dan Dicker", "Gamma 9")
    assert data["ok"] is False and _by_name("Gamma 9") is None
    assert not [c for c in hub.calls if c[0] == "POST"]


def test_a_second_country_code_or_number_clash_is_refused(hub):
    (res,) = _preview(f"Gamma 9, PE, act_{D1}")                        # номер уже у Alpha 1
    assert res["state"] == "clash" and "Alpha 1" in res["error"]
    (res,) = _preview("Gamma 9, PE, SA")                               # два кода — не угадываем
    assert res["state"] == "not_found"


# ── строки, которые уже в карте, работают как раньше ─────────────────────

def test_lines_for_accounts_already_in_the_map_behave_as_before(hub):
    before = _map_rows()
    results = _preview("Alpha 1\nBeta 2, PE\nact_" + D1)
    assert [r["state"] for r in results] == ["found", "found", "duplicate"]
    assert results[1]["account_name"] == "Beta 2"                      # страна у существующего не меняется
    status, data = _send("Dan Dicker", "Alpha 1\nBeta 2, PE")
    assert data["ok"] and data["added"] == [] and data["assigned"] == ["Alpha 1", "Beta 2"]
    assert _map_rows() == before                                       # карта не тронута


def test_owner_key_still_never_reaches_the_page(hub):
    from tests.test_acl2920_assign_follows_logged_as import OWNER_KEY

    answer = _send("Dan Dicker", "Gamma 9, PE")[1]
    assert OWNER_KEY not in json.dumps(answer)


# ── Fix round 1 (ревью) ──────────────────────────────────────────────────

def test_a_shared_list_name_with_a_country_is_still_added_to_the_map(hub):
    from unittest.mock import patch

    with patch("src.analysis_agent.gsheet_accounts.fetch_account_rows",
               return_value=[{"account_label": "Sheet Only"}]):
        (res,) = _preview("Sheet Only, PE")
        assert res["state"] == "add" and res["country"] == "Peru"
        status, data = _send("Dan Dicker", "Sheet Only, PE")
    assert data["ok"] and data["added"] == ["Sheet Only"]
    assert _by_name("Sheet Only")["country"] == "Peru"


def test_a_partial_name_does_not_beat_an_explicit_add(hub):
    (res,) = _preview("Alpha, PE")                                     # в карте есть «Alpha 1»
    assert res["state"] == "add" and res["account_name"] == "Alpha"
    (res,) = _preview("alpha 1, PE")                                   # точное имя без учёта регистра — существующий
    assert res["state"] == "found" and res["account_name"] == "Alpha 1"


def test_added_but_not_assigned_is_reported_as_added(hub):
    from unittest.mock import patch

    real_call = type(hub).__call__

    def failing(self, method, url, body, headers):
        if method == "POST" and url.endswith("/api/assignments"):
            return 500, {"error": "hub is down"}
        return real_call(self, method, url, body, headers)

    with patch.object(type(hub), "__call__", failing):
        status, data = _send("Dan Dicker", "Gamma 9, PE")
    assert data["ok"] is False and data["added"] == ["Gamma 9"]
    assert data["results"][0]["added"] is True and not data["results"][0].get("sent")
    assert _by_name("Gamma 9")["country"] == "Peru"                    # в карте осталась


def test_a_number_instead_of_a_country_says_no_country_code(hub):
    (res,) = _preview("Gamma 9, 123")
    assert res["state"] == "not_found" and "No country code" in res["error"] and '""' not in res["error"]


def test_a_column_pasted_from_a_spreadsheet_with_tabs_works_like_commas(hub):
    results = _preview("Gamma 9\tPE\nDelta 4\tSA\tact_" + D3)
    assert [(r["state"], r["country"]) for r in results] == [("add", "Peru"), ("add", "Saudi Arabia")]
    assert results[1]["row"]["ad_account_id"] == "act_" + D3
    status, data = _send("Dan Dicker", "Gamma 9\tPE")
    assert data["ok"] and _by_name("Gamma 9")["country"] == "Peru"


def test_added_accounts_are_placed_in_their_country_on_the_home_map(hub):
    from src.web import account_links

    status, data = _send("Dan Dicker", "Gamma 9, PE" + chr(10) + "Delta 4" + chr(9) + "SA")
    assert data["ok"]
    assert account_links.values_for("Gamma 9", "country") == ["Peru"]      # иначе аккаунт висит в Available accounts
    assert account_links.values_for("Delta 4", "country") == ["Saudi Arabia"]
    assert "Gamma 9" not in account_links.available_accounts(["Gamma 9", "Delta 4"])


# ── Fix round 4 (ревью) ──────────────────────────────────────────────────

def test_the_existing_group_spelling_wins_so_one_country_is_one_group(hub):
    from src.web import account_links

    account_links.link("Old 1", "country", "SA")                       # группа записана кодом
    account_links.link("Old 2", "country", "Peru")                     # другая — названием
    (res,) = _preview("New 1, SA")
    assert res["country"] == "SA"
    status, data = _send("Dan Dicker", "New 1, SA" + chr(10) + "New 2, PE")
    assert data["ok"]
    assert account_links.values_for("New 1", "country") == ["SA"]
    assert account_links.values_for("New 2", "country") == ["Peru"]
    assert sorted(account_links.all_values("country")) == ["Peru", "SA"]   # новых групп не появилось
    assert _by_name("New 1")["country"] == "SA"                       # то же написание в строке карты


def test_a_failed_link_keeps_the_assign_and_marks_that_line_only(hub):
    from unittest.mock import patch

    from src.web import account_links

    with patch.object(account_links, "link", side_effect=OSError("disk")):
        status, data = _send("Dan Dicker", "Gamma 9, PE" + chr(10) + "Alpha 1")
    assert data["ok"] and data["assigned"] == ["Gamma 9", "Alpha 1"]
    by = {r["input"]: r for r in data["results"]}
    assert by["Gamma 9, PE"]["note"] == "added to the map, not placed under Peru"
    assert "note" not in by["Alpha 1"]


def test_an_account_linked_to_another_country_gets_a_second_one_not_moved(hub):
    from src.web import account_links

    account_links.link("Alpha 1", "country", "Chile")
    assert _send("Dan Dicker", "Alpha 1, PE")[1]["ok"]                 # уже в карте: строка как раньше, страна не меняется
    assert account_links.values_for("Alpha 1", "country") == ["Chile"]
