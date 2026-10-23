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
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.analysis_agent import account_spend as sp

TPL = ROOT / "src" / "web" / "templates" / "report.html"


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    """Журнал живёт рядом с показаниями; в тестах — во временной папке."""
    monkeypatch.setattr(sp, "_entry_for", lambda _: SimpleNamespace(
        profile_id="550e8400-e29b-41d4-a716-446655440000"))
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


def test_a_crash_leaves_a_record_too(monkeypatch):
    """Самая неудавшаяся попытка из всех — та, что упала.

    Маршрут в `src/web/app.py` прямо ловит `Exception` и отвечает «Could not
    reach the account»: значит падение здесь предусмотрено. Раньше в этом
    случае в журнале не оставалось ничего — действие на живом кабинете было,
    следа нет. Журнал, который молчит именно тогда, когда что-то пошло совсем
    не так, не отвечает на «почему это выключилось вчера».
    """
    def explode(*a, **kw):
        raise TimeoutError("switch never appeared")

    monkeypatch.setattr(sp, "_turn_off", explode)

    with pytest.raises(TimeoutError):
        sp.turn_off("FacebookDQ438", "ad", "7", requester="Evgeni")

    row = sp.turn_off_history()[0]
    assert row["object_id"] == "7" and row["account"] == "FacebookDQ438"
    assert "TimeoutError" in row["error"] and "switch never appeared" in row["error"]
    assert row["requester"] == "Evgeni" and row["route"] == "rpa"
    assert row["at"], "у записи о падении нет времени"


def test_a_broken_attempt_is_not_called_a_failure(monkeypatch):
    """Замечание ревьюера, и оно точнее моей первой правки.

    `failed` означает «нажали, и тумблер НЕ стал выключенным» — установленный
    факт. Оборванная попытка такого факта не даёт: объявление могло и
    выключиться, просто подтвердить не успели. Записать «не удалось» значило
    бы соврать в ту сторону, из которой делают вывод «значит, оно ещё
    крутится», — и человек оставит выключенное объявление считаться
    работающим.
    """
    def explode(*a, **kw):
        raise TimeoutError("switch never appeared")

    monkeypatch.setattr(sp, "_turn_off", explode)
    with pytest.raises(TimeoutError):
        sp.turn_off("FacebookDQ438", "ad", "7", requester="Evgeni")

    assert sp.turn_off_history()[0]["state"] == "unconfirmed"


def test_the_crash_is_not_swallowed():
    """Записать и промолчать было бы хуже исходного: человек увидел бы
    «ничего не произошло» вместо ошибки. Решать, что ответить, — не здесь."""
    src = (ROOT / "src" / "analysis_agent" / "account_spend.py").read_text(encoding="utf-8")
    body = src[src.index("def turn_off(account"):]
    body = body[: body.index("\ndef _turn_off(")]
    assert "raise" in body, "исключение больше не пробрасывается"
    assert "except Exception" in body


def test_a_journal_failure_does_not_hide_the_crash(monkeypatch):
    """Если сломан сам журнал, наружу всё равно должно уйти исходное падение,
    а не ошибка записи о нём."""
    def explode(*a, **kw):
        raise TimeoutError("switch never appeared")

    monkeypatch.setattr(sp, "_turn_off", explode)
    monkeypatch.setattr(sp, "_turn_off_log", lambda: (_ for _ in ()).throw(OSError("no disk")))

    with pytest.raises(TimeoutError):
        sp.turn_off("FacebookDQ438", "ad", "7", requester="Evgeni")


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


@pytest.mark.parametrize("read_error", [PermissionError("denied"), OSError("disk unavailable"),
                                      UnicodeError("invalid encoding")])
def test_an_unreadable_record_is_not_reported_as_an_empty_one(monkeypatch, read_error):
    """Замечание ревьюера. Пустой список значит «здесь ничего не меняли» —
    самый успокаивающий вывод из возможных. Отдавать его вместо «прочитать не
    удалось» хуже, чем промолчать: человек уходит уверенным в том, чего никто
    не проверял."""
    import asyncio

    import httpx

    from src.web.app import app

    class UnreadableJournal:
        def read_text(self, **kwargs):
            raise read_error

    monkeypatch.setattr(sp, "_turn_off_log", lambda: UnreadableJournal())

    async def go():
        t = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=t, base_url="http://127.0.0.1") as c:
            return await c.get("/account-spend/turn-off-history",
                               params={"account": "FacebookDQ438"})

    r = asyncio.run(go())
    assert r.status_code == 500, "поломка чтения отвечает успехом"
    body = r.json()
    assert body["ok"] is False
    assert "entries" not in body, "к отказу приложен пустой список записей"


@pytest.mark.parametrize("contents", [None, ""])
def test_a_missing_or_empty_journal_is_empty(store, contents):
    if contents is not None:
        sp._turn_off_log().write_text(contents, encoding="utf-8")
    assert sp.turn_off_history(account="FacebookDQ438") == []


@pytest.mark.parametrize("bad_account", [42, True, None, [], {}, ""])
def test_a_malformed_account_does_not_hide_valid_history(store, bad_account):
    import asyncio
    import httpx
    from src.web.app import app

    _write(object_id="good")
    with sp._turn_off_log().open("a", encoding="utf-8") as journal:
        journal.write(json.dumps({"account": bad_account, "object_id": "bad"}) + "\n")

    async def read():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://127.0.0.1") as client:
            return await client.get("/account-spend/turn-off-history",
                                    params={"account": "FacebookDQ438", "limit": 1})

    response = asyncio.run(read())
    assert response.status_code == 200
    assert [row["object_id"] for row in response.json()["entries"]] == ["good"]
    assert [row["object_id"] for row in sp.turn_off_history(limit=1)] == ["good"]


def test_the_screen_checks_the_answer_before_believing_it():
    js = _tree_js()
    load = js[js.index("function amapLogLoad"):]
    load = load[: load.index("\nfunction ")]
    assert "!j || !j.ok" in load, "ответ снова принимается на веру"
    bad = load.index("!j || !j.ok")
    nothing = load.index("Nothing has been changed")
    assert bad < nothing, (
        "проверка ответа должна стоять раньше вывода «ничего не меняли», "
        "иначе отказ чтения снова прочитается как пустой журнал"
    )


def test_the_open_record_is_refreshed_after_a_failed_action():
    """Замечание ревьюера: именно упавшая попытка пишет строку «результат не
    подтверждён», и именно она нужнее всего на глазах. Обновление только по
    успешному ответу прятало бы ровно её."""
    page = TPL.read_text(encoding="utf-8")
    do = page[page.index("function amapTurnOffDo"):]
    do = do[: do.index("function amapTurnOffSaid")]
    assert do.count("amapLogRefresh(row, account)") == 2, (
        "журнал обновляется не на обоих исходах"
    )
    assert "amapLogRefresh(row, account)" in do[do.index(".catch("):], (
        "после отказа журнал не перечитывается"
    )


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


def test_the_screen_shows_three_outcomes_not_two():
    """Неподтверждённое нельзя называть тем же словом, что установленный
    отказ: из «did not turn off» человек делает вывод «значит, оно ещё
    крутится», тогда как объявление могло и выключиться."""
    js = _tree_js()
    said = js[js.index("var said = document.createElement"):]
    said = said[: said.index("line.appendChild(said)")]
    assert "unconfirmed" in said
    assert "result not confirmed" in said
    assert "' warn'" in said, "третий исход красится как отказ"
    assert "Ads Manager" in said, "не сказано, где посмотреть, чем всё кончилось"


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
    assert "amapLogRefresh(row, account)" in do

    # Перечитывается только раскрытый журнал: свёрнутый и так перечитается
    # при раскрытии, а лишний запрос на каждое нажатие не нужен никому.
    helper = page[page.index("function amapLogRefresh"):]
    helper = helper[: helper.index("\nfunction ")]
    assert "body.hidden" in helper
    assert "amapLogLoad(block, account)" in helper


def test_the_record_stays_open_across_a_reread():
    """Иначе каждое нажатие Read закрывало бы журнал, открытый именно затем,
    чтобы увидеть последствия нажатия."""
    js = _tree_js()
    assert "row.dataset.logOpen" in js
