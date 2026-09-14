"""Выключение живого объявления оставляет след, который можно открыть.

Выключение — единственное, чем ACL сам меняет чужой кабинет, а не читает его.
Журнал этих действий писался с первого живого нажатия 12.09, и писался
правильно: отказ отличим от подтверждённого изменения, доставка записана
словами Meta, а не нашими.

Чего не было — возможности его прочитать. `turn_off_history()` не вызывал
никто, кроме тестов: файл рос, на экране от него не было ничего. Запись,
которую некому открыть, не отвечает на «почему это выключилось вчера» — она
лишь позволяет сказать, что ответ где-то есть.

Не было и двух полей, без которых журнал не отвечает на вопрос целиком: кто
нажал и чем сделано.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.analysis_agent import account_spend as sp

TPL = ROOT / "src" / "web" / "templates" / "report.html"


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    """Журнал живёт рядом с показаниями; в тестах — во временной папке."""
    monkeypatch.setattr(sp, "_store_path", lambda: str(tmp_path / "spend.json"))
    return tmp_path


def _write(**kw) -> None:
    row = {"account": "FacebookDQ438", "level": "ad", "object_id": "1",
           "name": "", "state": "off", "delivery": "", "error": "", "how": "",
           "at": "2026-09-14 10:00:00", "requester": "", "route": ""}
    row.update(kw)
    sp._record_turn_off(sp.TurnOffResult(**row))


# ── что записывается ──────────────────────────────────────────────────

def test_the_record_says_who_asked_and_by_which_route(monkeypatch):
    """«Кто-то с этой машины» — не ответ на «кто остановил кампанию»."""
    monkeypatch.setattr(sp, "_turn_off", lambda *a, **kw: sp.TurnOffResult(
        account="FacebookDQ438", level="ad", object_id="7", state="off",
        at="2026-09-14 11:00:00"))
    monkeypatch.setattr(sp, "_mark_object_off", lambda *a, **kw: 0)

    out = sp.turn_off("FacebookDQ438", "ad", "7", requester="Evgeni")

    assert out.requester == "Evgeni"
    assert out.route == "rpa"
    assert sp.turn_off_history()[0]["requester"] == "Evgeni"


def test_an_attempt_that_failed_is_recorded_too(monkeypatch):
    """Журнал, в котором всё всегда получалось, хуже отсутствующего."""
    monkeypatch.setattr(sp, "_turn_off", lambda *a, **kw: sp.TurnOffResult(
        account="FacebookDQ438", level="campaign", object_id="9",
        state="failed", error="switch stayed on", at="2026-09-14 11:05:00"))

    sp.turn_off("FacebookDQ438", "campaign", "9", requester="Evgeni")

    row = sp.turn_off_history()[0]
    assert row["state"] == "failed" and row["error"] == "switch stayed on"
    assert row["route"] == "rpa", "отказ тоже должен говорить, чем его делали"


def test_the_route_is_never_guessed():
    """Поле маршрута пустое по умолчанию — намеренно.

    Если однажды появится второй путь и забудет представиться, журнал
    промолчит, а не соврёт, что это был RPA.
    """
    assert sp.TurnOffResult(account="A", level="ad", object_id="1").route == ""


# ── что читается ──────────────────────────────────────────────────────

def test_the_tree_gets_its_own_account_and_not_the_neighbours():
    """Журнал общий на машину, а дерево показывает один кабинет."""
    _write(account="FacebookDQ438", object_id="mine")
    _write(account="FacebookUL921", object_id="theirs")

    ids = [r["object_id"] for r in sp.turn_off_history(account="FacebookDQ438")]
    assert ids == ["mine"]


def test_a_busy_neighbour_does_not_push_my_action_out_of_sight():
    """Отбор идёт ДО счёта. Иначе десять действий на соседнем аккаунте
    вытеснят единственное нужное, и человек решит, что не записалось."""
    _write(account="FacebookDQ438", object_id="mine")
    for i in range(30):
        _write(account="FacebookUL921", object_id=f"n{i}")

    rows = sp.turn_off_history(limit=5, account="FacebookDQ438")
    assert [r["object_id"] for r in rows] == ["mine"]


def test_the_same_account_written_differently_still_matches():
    """В журнал попадает то, что прислала страница; спрашивают по подписи из
    карты. Лишний пробел не должен прятать от человека его же действие."""
    _write(account="Facebook  DQ438", object_id="mine")

    assert sp.turn_off_history(account="facebook dq438")
    assert sp.turn_off_history(account="  FacebookDQ438  ") == []


def test_newest_first():
    _write(object_id="first", at="2026-09-14 10:00:00")
    _write(object_id="second", at="2026-09-14 10:01:00")

    assert [r["object_id"] for r in sp.turn_off_history()] == ["second", "first"]


def test_a_damaged_line_does_not_hide_the_rest(store):
    _write(object_id="good")
    path = Path(sp._turn_off_log())
    with path.open("a", encoding="utf-8") as fh:
        fh.write("не json\n")
        fh.write(json.dumps(["список, а не запись"]) + "\n")

    assert [r["object_id"] for r in sp.turn_off_history()] == ["good"]


# ── и это видно на экране ─────────────────────────────────────────────

def _tree_js() -> str:
    page = TPL.read_text(encoding="utf-8")
    start = page.index("function amapRowAccount(row)")
    return page[start: page.index("function amapTurnOffSaid", start)]


def test_the_record_can_be_opened_from_the_tree():
    js = _tree_js()
    assert "/account-spend/turn-off-history?account=" in js
    assert "amapLogAttach" in TPL.read_text(encoding="utf-8")


def test_yesterday_evening_is_not_shown_as_today():
    """«Сегодня» считается по МЕСТНОЙ дате, а не по UTC.

    Запись ставит время `datetime.now()` — местное. Если сравнивать его с
    датой по UTC, то с полуночи до трёх часов ночи (у нас UTC+3) вчерашний
    вечер совпадёт с «сегодня» по UTC и покажется без даты — как будто
    сделан сегодня. В журнале, вся задача которого ответить «когда это
    случилось», такая строка прямо врёт.
    """
    js = _tree_js()
    when = js[js.index("function amapLogWhen"):]
    when = when[: when.index("\nfunction ")]
    assert "toISOString" not in when, "«сегодня» снова считается по UTC"
    assert "amapIso(new Date())" in when, (
        "местную дату считает amapIso — она уже есть в файле; своя похожая "
        "формула рядом и есть то расхождение, которое потом ищут полдня"
    )


def test_a_failed_attempt_is_not_painted_as_success():
    """Той же рукой, что и кнопка: «нажали, но не выключилось» — не успех."""
    js = _tree_js()
    said = js[js.index("var said = document.createElement"):]
    said = said[: said.index("line.appendChild(said)")]
    assert "'off' || e.state === 'already_off'" in said
    assert "did not turn off" in said and "bad" in said


def test_nothing_is_invented_when_the_name_is_missing():
    """Журнал без имени честнее журнала с подставленным."""
    js = _tree_js()
    tail = js[js.index("var tail ="):]
    tail = tail[: tail.index("return line")]
    assert "filter(Boolean)" in tail
    assert "if (tail)" in tail, "пустой хвост не должен рисоваться"


def test_an_open_record_is_refreshed_after_an_action():
    """Смотреть в раскрытый журнал, где нет только что сделанного, хуже,
    чем не открывать его вовсе."""
    page = TPL.read_text(encoding="utf-8")
    do = page[page.index("function amapTurnOffDo"):]
    do = do[: do.index("function amapTurnOffSaid")]
    assert "amapLogLoad(block, account)" in do
    assert ".amap-tree-log-body').hidden" in do, "перечитываем только раскрытый"


def test_the_record_stays_open_across_a_reread():
    """Иначе каждое нажатие Read закрывало бы журнал, открытый именно затем,
    чтобы увидеть последствия нажатия."""
    js = _tree_js()
    assert "row.dataset.logOpen" in js
