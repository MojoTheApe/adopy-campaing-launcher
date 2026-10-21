"""Goals per campaign manager: stored, edited, deleted.

They used to be a JavaScript literal in report.html, so adding a country meant
editing HTML and every machine carried the same numbers regardless of whose they
were. These tests hold what changed and, more importantly, what must not: a
machine that has been showing these numbers for months must not open the new
screen empty.
"""
from __future__ import annotations

import pytest

from src.web import goals


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(goals, "get_local_config_dir", lambda: str(tmp_path))
    monkeypatch.delenv("ACL_TEAM_LEAD", raising=False)
    yield


def test_a_new_manager_starts_with_their_own_countries_at_ten():
    """30.09 установка «от имени Дана» показала цели владельца (ACL-1452)."""
    got = goals.for_cm("Dan", ["Chile", "Kuwait", "Atlantis"])
    assert [(g["country"], g["code"], g["market"], g["ftd"]) for g in got] == [
        ("Kuwait", "KW", "GCC", 10), ("Chile", "CL", "Latam", 10), ("Atlantis", "", "Other", 10)]
    assert all(g["cpa"] is None and g["cr"] is None and g["leads_per_day"] is None for g in got)
    assert not goals.goals_path().exists(), "reading must not create a file"


def test_a_new_manager_without_countries_starts_empty():
    assert goals.for_cm("Dan") == []
    assert goals.for_cm("Dan", []) == []


def test_nobody_inherits_the_owners_numbers():
    got = goals.for_cm("Dan", ["Kuwait", "Mexico"])
    assert {g["ftd"] for g in got} == {10}
    assert not hasattr(goals, "DEFAULT_GOALS"), "чьи-то цели снова вшиты как старт"


def test_stored_goals_are_returned_as_stored():
    goals.save("Evgeni Sedin", [{"country": "Kuwait", "code": "KW", "market": "GCC", "ftd": 15, "cpa": 3000}])
    got = goals.for_cm("Evgeni Sedin", ["Chile", "Mexico"])
    assert [(g["country"], g["ftd"], g["cpa"]) for g in got] == [("Kuwait", 15, 3000.0)]


def test_the_first_edit_keeps_the_starting_countries():
    """Человек видел свои страны по 10 — правка одной не стирает остальные."""
    goals.upsert("Dan", {"country": "Chile", "ftd": 25}, ["Chile", "Peru"])
    got = {g["country"]: g["ftd"] for g in goals.for_cm("Dan")}
    assert got == {"Chile": 25, "Peru": 10}


def test_each_manager_keeps_their_own():
    goals.upsert("Evgeni", {"country": "Peru", "code": "PE", "market": "Latam", "ftd": 10})

    assert [g["country"] for g in goals.for_cm("Evgeni")].count("Peru") == 1
    assert goals.for_cm("Yotam") == [], "untouched, no countries known, nothing inherited"


def test_editing_a_country_replaces_it_rather_than_adding_a_second():
    goals.upsert("Evgeni", {"country": "Kuwait", "code": "KW", "market": "GCC", "ftd": 25})

    kuwait = [g for g in goals.for_cm("Evgeni") if g["country"] == "Kuwait"]
    assert len(kuwait) == 1
    assert kuwait[0]["ftd"] == 25


def test_deleting_a_country_stops_the_target_and_nothing_else():
    goals.delete("Evgeni", "Mexico", ["Chile", "Mexico"])

    countries = [g["country"] for g in goals.for_cm("Evgeni")]
    assert "Mexico" not in countries
    assert "Chile" in countries


def test_the_same_country_twice_is_refused_rather_than_stored():
    with pytest.raises(ValueError, match="listed twice"):
        goals.save("Evgeni", [
            {"country": "Chile", "ftd": 10},
            {"country": "chile", "ftd": 20},
        ])


def test_a_goal_needs_a_country_and_numbers_that_are_numbers():
    with pytest.raises(ValueError, match="country is required"):
        goals.upsert("Evgeni", {"country": "  ", "ftd": 5})
    with pytest.raises(ValueError, match="must be a number"):
        goals.upsert("Evgeni", {"country": "Peru", "ftd": "many"})
    with pytest.raises(ValueError, match="cannot be negative"):
        goals.upsert("Evgeni", {"country": "Peru", "ftd": -3})


def test_team_lead_is_an_agreement_and_says_so(monkeypatch):
    """The dashboard has no sign-in; the flag unlocks a UI, not a door."""
    assert goals.is_team_lead() is False
    monkeypatch.setenv("ACL_TEAM_LEAD", "1")
    assert goals.is_team_lead() is True

    module = (goals.__file__)
    text = open(module, encoding="utf-8").read()
    assert "an agreement, not a mechanism" in text


# ── заголовок и страны человека (ACL-1452) ──────────────────────────────

def test_the_panel_title_names_whose_goals_they_are():
    from pathlib import Path
    page = (Path(__file__).resolve().parent.parent / "src" / "web" / "templates" / "report.html").read_text(encoding="utf-8")
    assert "Evgeni — Goals Progress" not in page, "имя владельца снова вписано в заголовок"
    assert "{{ (gv.cm or '') | e | tojson }}" in page


def test_the_persons_countries_come_from_their_numbers_and_their_map(monkeypatch):
    from src.web import account_links, app as web
    monkeypatch.setenv("MY_CM_NAME", "Dan")
    monkeypatch.setattr(account_links, "all_values", lambda kind: ["Peru", "Empty"])
    monkeypatch.setattr(account_links, "accounts_for", lambda kind, v: ["Acc"] if v == "Peru" else [])
    report = {"countries": [
        {"country": "Chile", "my_totals": {"spend": 12.0}},
        {"country": "Kuwait", "my_totals": {}},          # чужая страна: у него там ничего
        {"country": "Mexico", "my_totals": {"leads": 3}},
    ]}
    assert web._my_goal_countries(report) == ["Chile", "Mexico", "Peru"]


def _js_title(cm):
    """Отрисовать строку заголовка из настоящего шаблона и выполнить её в node."""
    import json
    import shutil
    import subprocess
    from pathlib import Path

    from src.web import app as web

    page = (Path(__file__).resolve().parent.parent / "src" / "web" / "templates" / "report.html").read_text(encoding="utf-8")
    start = page.index("  var GOALS_WHO = ")
    end = page.index("\n", page.index("goals-title", start))
    snippet = page[start:end]
    js = web.templates.env.from_string(snippet).render(gv={"cm": cm})
    node = shutil.which("node")
    if node is None:
        pytest.skip("нет node — выполнить заголовок нечем")
    out = subprocess.run([node, "-e", "var h='';" + js + ";process.stdout.write(JSON.stringify(h))"],
                         capture_output=True, text=True, encoding="utf-8", timeout=30, check=True).stdout
    return json.loads(out)


def test_the_rendered_title_names_the_manager_or_nobody():
    """Отрисованный заголовок, а не только исходник шаблона (ревью #453, п.4)."""
    assert ">Dan — Goals Progress" in _js_title("Dan")
    assert ">Goals Progress · FTDs" in _js_title(""), "без имени — без тире и без чужого имени"
    tricky = _js_title('O"Neil</script><b>')
    assert 'O"Neil' not in tricky and "<b>" not in tricky, "имя вставлено без экранирования"


def _post_goals(web, body):
    """POST /goals без TestClient: обработчик вызывается с готовым телом."""
    import asyncio

    class _Req:
        async def json(self):
            return body

    return asyncio.run(web.goals_write(_Req()))


def test_the_panel_and_the_save_seed_from_the_same_countries(monkeypatch, tmp_path):
    """Панель за период и POST /goals берут одни и те же страны (ревью #453, п.1)."""
    from src.web import account_links, app as web

    monkeypatch.setenv("MY_CM_NAME", "Dan")
    monkeypatch.setattr(account_links, "all_values", lambda kind: [])
    full = {"my_cm": "Dan", "countries": [
        {"country": "Chile", "my_totals": {"spend": 5.0}},
        {"country": "Mexico", "my_totals": {"spend": 5.0}},
        {"country": "Peru", "my_totals": {"leads": 2}},       # только в старых днях книги
    ]}
    period = {"my_cm": "Dan", "countries": [
        {"country": "Chile", "my_totals": {"spend": 1.0}},
        {"country": "Mexico", "my_totals": {}},              # в этот день ничего
        {"country": "Peru", "my_totals": {}},
    ]}
    calls = []
    monkeypatch.setattr(web, "get_cached_report", lambda **kw: (calls.append(kw) or full, None, []))

    seed = web._my_goal_countries(full)
    view = web._build_goals_view(period, seed)
    shown = sorted(r["country"] for r in view["rows"])
    assert shown == ["Chile", "Mexico", "Peru"], "панель за период сузила стартовые страны"

    r = _post_goals(web, {"cm": "Dan", "goal": {"country": "Chile", "ftd": 25}})
    assert r.status_code == 200, r.body
    saved = {g["country"]: g["ftd"] for g in goals.for_cm("Dan")}
    assert saved == {"Chile": 25, "Mexico": 10, "Peru": 10}, "сохранено не то, что было на экране"
    assert calls == [{"my_cm": "Dan"}]

    # цели уже свои — отчёт для стартового списка больше не собирается
    _post_goals(web, {"cm": "Dan", "goal": {"country": "Peru", "ftd": 3}})
    assert len(calls) == 1, "сохранение пересобирает книгу, хотя стартовый список не нужен"


def test_a_lead_editing_someone_else_does_not_give_them_this_machines_map(monkeypatch):
    """Карта связей этой машины — страны её хозяина, не того, чьи цели правят (п.3)."""
    from src.web import account_links, app as web

    monkeypatch.setenv("MY_CM_NAME", "Lead")
    monkeypatch.setattr(account_links, "all_values", lambda kind: ["Kuwait"])
    monkeypatch.setattr(account_links, "accounts_for", lambda kind, v: ["Acc"])
    lead_report = {"my_cm": "Lead", "countries": [{"country": "Chile", "my_totals": {"spend": 1.0}}]}
    assert web._my_goal_countries(lead_report, "Dan") == [], "страны тимлида ушли в цели Дана"
    dan_report = {"my_cm": "Dan", "countries": [{"country": "Peru", "my_totals": {"spend": 1.0}}]}
    assert web._my_goal_countries(dan_report, "Dan") == ["Peru"]
    assert web._my_goal_countries(lead_report) == ["Chile", "Kuwait"]

def test_asset_goals_survive_a_save():
    got = goals.save("Dan", [{"country": "Peru", "cpa": 55, "assets": {"1121": {"cpa": "30"}, "": {"cpa": 1}, "x": {}}}])
    assert got[0]["assets"] == {"1121": {"cpa": 30.0, "cr": None}}
    assert goals.for_cm("Dan")[0]["assets"] == {"1121": {"cpa": 30.0, "cr": None}}
    assert "assets" not in goals.save("Dan", [{"country": "Peru", "cpa": 55}])[0]


# ── ACL-602: the lead sees everyone's goals, a buyer only their own ──────
# Эндпоинты зовутся напрямую: TestClient в этой сборке несовместим с httpx.

def _call(monkeypatch, fn, me="Dan", **kw):
    import asyncio, json
    from src.web import app as web
    monkeypatch.setenv("MY_CM_NAME", me)
    monkeypatch.setattr(web, "_goal_countries_now", lambda who: [])
    resp = asyncio.run(getattr(web, fn)(**kw))
    return resp.status_code, json.loads(resp.body)


def test_overview_lists_every_stored_manager():
    goals.save("Dan", [{"country": "Chile", "ftd": 5}, {"country": "Peru", "ftd": 7}])
    goals.save("Ana", [{"country": "Kuwait", "ftd": 3}])
    got = goals.overview()
    assert [m["cm"] for m in got] == ["Ana", "Dan"]
    assert got[1]["countries"] == 2 and got[1]["ftd_total"] == 12


def test_lead_gets_the_overview_endpoint(monkeypatch):
    goals.save("Ana", [{"country": "Kuwait", "ftd": 3}])
    monkeypatch.setenv("ACL_TEAM_LEAD", "1")
    code, body = _call(monkeypatch, "goals_overview")
    assert code == 200 and [m["cm"] for m in body["managers"]] == ["Ana"]


def test_non_lead_cannot_see_other_peoples_goals(monkeypatch):
    goals.save("Ana", [{"country": "Kuwait", "ftd": 3}])
    code, body = _call(monkeypatch, "goals_overview")
    assert code == 403 and "managers" not in body
    code, body = _call(monkeypatch, "goals_read", cm="Ana")
    assert code == 403 and "Kuwait" not in str(body)
    code, _ = _call(monkeypatch, "goals_read", cm="")
    assert code == 200  # own goals stay readable


def test_lead_can_still_read_one_persons_goals(monkeypatch):
    goals.save("Ana", [{"country": "Kuwait", "ftd": 3}])
    monkeypatch.setenv("ACL_TEAM_LEAD", "1")
    code, body = _call(monkeypatch, "goals_read", cm="Ana")
    assert code == 200 and body["goals"][0]["country"] == "Kuwait"
