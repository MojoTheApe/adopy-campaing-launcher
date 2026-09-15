"""Список профилей переживает разовый сбой Multilogin.

Профили тянутся страницами по сто, и в рабочем пространстве владельца их
1760 — восемнадцать запросов подряд на каждую первичную загрузку. Повторов не
было: единственный сбой на любой из восемнадцати страниц обрушивал всё, и под
полем поиска появлялось красное «Multilogin refused the profile list».

Воспроизведено через сам дешборд: три полные загрузки подряд дали две по 1759
профилей и один отказ 501. То есть речь не о сломанном доступе, а о разовом
спотыкании — и человеку об этом говорилось словом «refused», которое читается
как «отказано».

Повторяем только то, что имеет смысл повторять. 401 и 403 — про токен: он не
станет живым оттого, что мы спросим ещё раз.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.web import app as web_app


class _Resp:
    def __init__(self, status: int, payload: dict | None = None, text: str = ""):
        self.status_code = status
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def _page(n: int) -> dict:
    """Ответ Multilogin с `n` профилями."""
    return {"data": {"profiles": [
        {"profile_id": f"p{i}", "folder_id": "f", "name": f"Profile {i}"} for i in range(n)
    ]}}


class _Fake:
    """Отдаёт заранее назначенные ответы по порядку и считает запросы."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append(json.get("offset"))
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _search(script, monkeypatch):
    monkeypatch.setattr(web_app, "_MLX_RETRY_PAUSE_S", 0)   # тест не ждёт впустую
    fake = _Fake(script)
    profiles, error = web_app._mlx_search_all(fake, {}, "")
    return profiles, error, fake


# ── ради чего всё ─────────────────────────────────────────────────────

def test_a_single_hiccup_no_longer_loses_everything(monkeypatch):
    """Та самая третья попытка из трёх."""
    profiles, error, fake = _search([
        _Resp(200, _page(100)),
        _Resp(501, None, "<h1>Error response</h1><p>Error code: 501</p>"),
        _Resp(200, _page(60)),
    ], monkeypatch)

    assert error == "", f"разовый сбой всё ещё обрушивает загрузку: {error}"
    assert len(profiles) == 160
    assert fake.calls == [0, 100, 100], "сбойная страница повторена не та"


def test_a_broken_connection_is_retried_too(monkeypatch):
    """Обрыв связи — из того же рода, что и 5xx."""
    profiles, error, _ = _search([
        ConnectionError("connection reset"),
        _Resp(200, _page(10)),
    ], monkeypatch)

    assert error == ""
    assert len(profiles) == 10


def test_giving_up_says_the_tries_happened(monkeypatch):
    """«Refused» без продолжения читается как «отказано в доступе». Если
    попытки были — это надо сказать, иначе человек снова решит, что сломан
    доступ, и пойдёт разбираться не туда."""
    profiles, error, fake = _search([_Resp(501, None, "boom")] * 3, monkeypatch)

    assert "501" in error and "3 tries" in error
    assert fake.calls == [0, 0, 0], "повторов не три"
    assert profiles == []


def test_what_was_already_read_is_not_thrown_away(monkeypatch):
    """Сбой на четвёртой странице не повод забыть три прочитанные."""
    profiles, error, _ = _search([
        _Resp(200, _page(100)), _Resp(200, _page(100)),
        _Resp(500, None, "boom"), _Resp(500, None, "boom"), _Resp(500, None, "boom"),
    ], monkeypatch)

    assert error, "отказ не назван"
    assert len(profiles) == 200, "прочитанное до сбоя потеряно"


# ── чего повторять нельзя ─────────────────────────────────────────────

def test_a_dead_token_is_not_retried(monkeypatch):
    """401 не станет двухсотым оттого, что мы спросим ещё раз. Повтор лишь
    оттянет честный ответ, а человек всё это время смотрит на «saving…»."""
    profiles, error, fake = _search([
        _Resp(401, {"status": {"message": "token expired"}}),
    ], monkeypatch)

    assert error.startswith("HTTP 401")
    assert "tries" not in error, "про попытки сказано там, где их не было"
    assert fake.calls == [0], "мёртвый токен спрошен повторно"


def test_a_forbidden_request_is_not_retried(monkeypatch):
    profiles, error, fake = _search([
        _Resp(403, {"status": {"message": "Request is not allowed"}}),
    ], monkeypatch)

    assert error.startswith("HTTP 403")
    assert fake.calls == [0]


def test_only_transient_statuses_are_worth_a_second_ask():
    assert web_app._mlx_worth_retrying(500)
    assert web_app._mlx_worth_retrying(501)
    assert web_app._mlx_worth_retrying(429)
    assert not web_app._mlx_worth_retrying(401)
    assert not web_app._mlx_worth_retrying(403)
    assert not web_app._mlx_worth_retrying(404)


# ── обычная работа не изменилась ──────────────────────────────────────

def test_a_clean_run_asks_each_page_once(monkeypatch):
    profiles, error, fake = _search([
        _Resp(200, _page(100)), _Resp(200, _page(100)), _Resp(200, _page(7)),
    ], monkeypatch)

    assert error == ""
    assert len(profiles) == 207
    assert fake.calls == [0, 100, 200], "страницы спрошены не по одному разу"
