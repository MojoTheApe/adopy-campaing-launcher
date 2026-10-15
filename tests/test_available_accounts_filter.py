"""ACL-1319: Available accounts shows only accounts worth placing.

Решение владельца 26.09: строка отчёта с нулевым спендом не делает аккаунт
«известным». Список показывает активных (спенд за последние 30 дней книги) и
только что подключённых на этой машине; остальные — за «Show inactive»;
убранные руками — в «Hidden accounts». Всё, кроме этого списка, не меняется.
"""
from __future__ import annotations

import asyncio
import csv
import json

import httpx
import pandas as pd
import pytest

from src.web import account_links as links
from src.web import available_accounts as avail
from src.web.app import _build_account_map_view, app


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("ACL_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(links, "get_local_config_dir", lambda: str(tmp_path))
    map_path = str(tmp_path / "ad_account_map.csv")
    monkeypatch.setattr(links, "get_ad_account_map_path", lambda: map_path)
    monkeypatch.setattr("src.web.app.get_ad_account_map_path", lambda: map_path)
    yield


def _workbook(rows: list[tuple[str, str, float]]) -> pd.DataFrame:
    """(alias, date, spend) → книга с теми колонками, что читает фильтр."""
    return pd.DataFrame(
        [{"Account Alias": a, "Date": d, "$ Spend": s, "CM": "Evgeni Sedin",
          "Country Final": "Mexico"} for a, d, s in rows]
    )


def _map(tmp_path, names: list[str]) -> None:
    with open(tmp_path / "ad_account_map.csv", "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["account_name", "ad_account_id", "profile_id"])
        w.writeheader()
        for n in names:
            w.writerow({"account_name": n, "ad_account_id": "act_1", "profile_id": "p-" + n})


def _report(labels: list[str]) -> dict:
    return {"countries": [{
        "country": "Mexico",
        "account_rows": [{"account_label": l, "account_excel": {}} for l in labels],
    }]}


# ── окно активности ─────────────────────────────────────────────────────

def test_zero_spend_rows_do_not_make_an_account_active():
    df = _workbook([
        ("FacebookZero", "2026-09-20", 0.0),
        ("FacebookZero", "2026-09-21", 0.0),
        ("FacebookLive", "2026-09-21", 12.5),
    ])
    act = avail.workbook_activity(df)
    assert act.recent == {"facebooklive": 12.5}
    assert "facebookzero" not in act.ever


def test_the_window_is_thirty_days_back_from_the_workbooks_last_date():
    """Меряется от последнего дня книги, а не от сегодня: отставшая книга не
    должна гасить работающие аккаунты."""
    df = _workbook([
        ("FacebookEdge", "2026-08-25", 5.0),   # last-29 → в окне
        ("FacebookOld", "2026-08-24", 5.0),    # last-30 → уже нет
        ("FacebookLast", "2026-09-23", 1.0),
    ])
    act = avail.workbook_activity(df)
    assert (act.window_from, act.window_to) == ("2026-08-25", "2026-09-23")
    assert set(act.recent) == {"facebookedge", "facebooklast"}
    assert "facebookold" in act.ever


def test_no_workbook_means_no_filter():
    assert avail.workbook_activity(None) is None
    assert avail.workbook_activity(pd.DataFrame({"Account Alias": ["X"]})) is None
    out = avail.split_available(["A", "B"], connected=[], placed=[], activity=None, hidden=set())
    assert [r["account"] for r in out["shown"]] == ["A", "B"]


# ── раскладка ───────────────────────────────────────────────────────────

def _act():
    return avail.workbook_activity(_workbook([
        ("FacebookDQ500", "2026-09-20", 47.8),      # активный
        ("FacebookUL216", "2026-09-20", 0.0),       # в книге, спенда нет
        ("FacebookElf87", "2026-07-01", 30.0),      # тратил, но давно
        ("FacebookElf87", "2026-09-23", 0.0),
        ("FacebookMKTOBFLX84", "2026-09-22", 9.0),  # книжное имя KLD_BFL_X84
    ]))


def test_sheet_labels_are_matched_by_their_first_token():
    out = avail.split_available(
        ["FacebookDQ500 EC Ait", "FacebookUL216 CL", "FacebookElf87 MX"],
        connected=[], placed=[], activity=_act(), hidden=set(),
    )
    assert [(r["account"], r["status"]) for r in out["shown"]] == [("FacebookDQ500 EC Ait", "active")]
    assert [r["account"] for r in out["inactive"]] == ["FacebookUL216 CL", "FacebookElf87 MX"]


def test_a_just_connected_account_is_shown_until_it_has_ever_spent():
    """Подключённый на машине и ни разу не тративший — «новый», его надо
    видеть, чтобы разместить. Тративший когда-то, но не в окне, — неактивен."""
    out = avail.split_available(
        ["KLD_BFL_NEW1", "FacebookElf87", "KLD_BFL_X84"],
        connected=["KLD_BFL_NEW1", "FacebookElf87", "KLD_BFL_X84"],
        placed=[], activity=_act(), hidden=set(),
    )
    status = {r["account"]: r["status"] for r in out["shown"] + out["inactive"]}
    assert status == {"KLD_BFL_NEW1": "new", "FacebookElf87": "inactive", "KLD_BFL_X84": "active"}


def test_tail_guessing_is_only_for_connected_accounts():
    """Подпись из отчёта книжная — угадать ей чужой спенд по хвосту нельзя."""
    out = avail.split_available(["FacebookZZZX84"], connected=[], placed=[], activity=_act(), hidden=set())
    assert out["shown"] == [] and [r["account"] for r in out["inactive"]] == ["FacebookZZZX84"]


def test_the_same_account_placed_under_another_label_is_placed():
    out = avail.split_available(
        ["FacebookDQ500 EC"], connected=[], placed=["FacebookDQ500 - Juzy"],
        activity=_act(), hidden=set(),
    )
    assert out == {"shown": [], "inactive": [], "hidden": []}


def test_hidden_accounts_leave_both_lists():
    out = avail.split_available(
        ["FacebookDQ500 EC", "FacebookUL216"], connected=[], placed=[], activity=_act(),
        hidden={"facebookdq500 ec", "facebookul216"},
    )
    assert out["shown"] == [] and out["inactive"] == []
    assert [r["account"] for r in out["hidden"]] == ["FacebookDQ500 EC", "FacebookUL216"]


# ── вид карты целиком ───────────────────────────────────────────────────

def test_the_view_narrows_only_the_available_list(tmp_path):
    _map(tmp_path, ["KLD_BFL_NEW1", "FacebookPlaced"])
    links.link("FacebookPlaced", "country", "Mexico")
    df = _workbook([
        ("FacebookDQ500", "2026-09-20", 47.8),
        ("FacebookUL216", "2026-09-20", 0.0),
        ("FacebookPlaced", "2026-09-20", 0.0),
    ])
    report = _report(["FacebookDQ500 MX", "FacebookUL216 MX", "FacebookPlaced"])
    view = _build_account_map_view(report, df, span="all")

    assert [r["account"] for r in view["available"]] == ["FacebookDQ500 MX", "KLD_BFL_NEW1"]
    assert [r["account"] for r in view["available_inactive"]] == ["FacebookUL216 MX"]
    # Выпадающий список привязки и карточка страны — как были.
    assert set(view["all_accounts"]) == {
        "FacebookDQ500 MX", "FacebookUL216 MX", "FacebookPlaced", "KLD_BFL_NEW1"}
    mexico = [c for c in view["countries"] if c["country"] == "Mexico"][0]
    assert [r["account"] for r in mexico["rows"]] == ["FacebookPlaced"]
    assert view["activity_window"] == {"from": "2026-08-22", "to": "2026-09-20", "days": 30}

    links.hide_accounts(["FacebookDQ500 MX"])
    view = _build_account_map_view(report, df, span="all")
    assert [r["account"] for r in view["available"]] == ["KLD_BFL_NEW1"]
    assert view["available_hidden"] == [{"account": "FacebookDQ500 MX"}]


# ── хранилище ───────────────────────────────────────────────────────────

def test_hide_restore_and_list(tmp_path):
    assert links.hidden_accounts() == []
    assert links.hide_accounts(["FacebookB", "facebookb", "  FacebookA  "]) == ["FacebookB", "FacebookA"]
    assert links.hide_accounts(["FACEBOOKA"]) == []
    assert links.hidden_accounts() == ["FacebookA", "FacebookB"]
    assert links.hidden_keys() == {"facebooka", "facebookb"}
    assert (tmp_path / "available_hidden.csv").is_file()
    assert links.restore_accounts(["facebookA", "Nobody"]) == ["FacebookA"]
    assert links.hidden_accounts() == ["FacebookB"]


def test_hiding_touches_nothing_else(tmp_path):
    _map(tmp_path, ["FacebookA"])
    links.link("FacebookC", "country", "Peru")
    before_map = (tmp_path / "ad_account_map.csv").read_bytes()
    before_links = (tmp_path / "account_links.csv").read_bytes()
    links.hide_accounts(["FacebookA", "FacebookB"])
    links.restore_accounts(["FacebookB"])
    assert (tmp_path / "ad_account_map.csv").read_bytes() == before_map
    assert (tmp_path / "account_links.csv").read_bytes() == before_links


def test_a_failed_write_keeps_the_previous_file(tmp_path, monkeypatch):
    """Запись атомарная: сорвавшаяся замена оставляет прежний файл целым и
    не мусорит временными."""
    links.hide_accounts(["FacebookA"])
    before = (tmp_path / "available_hidden.csv").read_bytes()

    def _busy(temp, target):
        raise PermissionError("locked")

    monkeypatch.setattr(links, "_replace_when_free", _busy)
    with pytest.raises(PermissionError):
        links.hide_accounts(["FacebookB"])
    assert (tmp_path / "available_hidden.csv").read_bytes() == before
    assert not list(tmp_path.glob(".*.tmp"))
    assert links.hidden_accounts() == ["FacebookA"]


def test_a_broken_store_hides_nothing(tmp_path):
    (tmp_path / "available_hidden.csv").write_bytes(b"\xff\xfe\x00garbage")
    assert links.hidden_accounts() == []


# ── эндпоинт ────────────────────────────────────────────────────────────

def _post(body) -> tuple[int, dict]:
    async def _go():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            r = await client.post(
                "/api/available-accounts/hidden",
                content=json.dumps(body), headers={"Content-Type": "application/json"},
            )
            return r.status_code, r.json()
    return asyncio.run(_go())


def test_endpoint_hides_and_restores_a_list():
    status, data = _post({"accounts": ["FacebookA", "FacebookB"], "hidden": True})
    assert status == 200 and data["changed"] == ["FacebookA", "FacebookB"] and data["hidden_count"] == 2
    status, data = _post({"accounts": ["FacebookA"], "hidden": False})
    assert status == 200 and data["changed"] == ["FacebookA"] and data["hidden_count"] == 1
    assert links.hidden_accounts() == ["FacebookB"]


def test_endpoint_refuses_accounts_placed_in_a_country():
    links.link("FacebookDQ500 - Juzy", "country", "Peru")
    status, data = _post({"accounts": ["FacebookDQ500 EC", "FacebookA"], "hidden": True})
    assert status == 200
    assert data["skipped"] == ["FacebookDQ500 EC"] and data["changed"] == ["FacebookA"]
    assert links.accounts_for("country", "Peru") == ["FacebookDQ500 - Juzy"]


@pytest.mark.parametrize("body", [
    {"accounts": "FacebookA", "hidden": True},
    {"accounts": [1, 2], "hidden": True},
    {"accounts": [], "hidden": True},
    {"accounts": ["FacebookA"], "hidden": "yes"},
    ["FacebookA"],
])
def test_endpoint_rejects_malformed_bodies(body):
    status, data = _post(body)
    assert status == 400 and data["ok"] is False
    assert links.hidden_accounts() == []
