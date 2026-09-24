# -*- coding: utf-8 -*-
"""Первый переход в Ads Manager переживает обрыв соединения (ACL-1284).

20.09 06:50, профиль KLD_BFL_S15: самый первый `goto` упал с
ERR_SSL_PROTOCOL_ERROR, и кампания не начиналась вовсе — задание кончалось,
не открыв ни одной страницы. Обрыв соединения у профиля лечится повтором;
отказ на первой же попытке стоит целого прогона.

Ревью 24.09: у этого цикла не было ни одного теста. Сократить его до одной
попытки, начать повторять вообще любую ошибку или вернуть сон после
последней попытки — всё это проходило молча, а ходит он в живой кабинет.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import ads_manager_flow as amf  # noqa: E402


class Page:
    """Страница, которая падает заданное число раз, а потом открывается."""

    def __init__(self, fails: int, error: str = "net::ERR_SSL_PROTOCOL_ERROR at https://…"):
        self.fails, self.error = fails, error
        self.urls: list[str] = []

    def goto(self, url, **kw):
        self.urls.append(url)
        if len(self.urls) <= self.fails:
            raise RuntimeError(self.error)


@pytest.fixture
def flow(monkeypatch):
    """Переход и ничего вокруг: сон, паузы и снимки выключены."""
    slept: list[float] = []
    monkeypatch.setattr(amf.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(amf, "pause_after_navigation", lambda *a, **k: None)

    def make(fails: int, error: str | None = None):
        page = Page(fails, error) if error else Page(fails)
        obj = amf.AdsManagerFlow.__new__(amf.AdsManagerFlow)
        obj.page = page
        obj.cfg = {"urls": {"campaigns": "https://adsmanager.facebook.com/x?act={act_id}"}}
        obj.nav_timeout = 1000
        return obj, page

    return make, slept


DRAFT = type("Draft", (), {"ad_account_id": "act_1234567890"})()


def _result():
    """Ровно те поля результата, которые трогает переход и снимок после него."""
    r = type("Result", (), {})()
    r.steps_completed = []
    r.screenshot_paths = []
    r.screenshots = []
    return r


def test_a_dropped_connection_is_retried_and_the_run_goes_on(flow):
    """Две неудачи подряд — и всё равно открылись: ради этого цикл и есть."""
    make, slept = flow
    obj, page = make(fails=2)
    result = _result()
    obj._goto_campaigns(DRAFT, result)
    assert len(page.urls) == 3, f"попыток было {len(page.urls)}"
    assert "open_campaigns" in result.steps_completed
    assert slept == [8.0, 8.0], f"ждали между попытками {slept}"


def test_three_failures_give_up_and_say_the_reason(flow):
    """Повтор — не бесконечный: после трёх отказов наверх уходит та самая
    ошибка, а не «что-то пошло не так»."""
    make, slept = flow
    obj, page = make(fails=99)
    with pytest.raises(RuntimeError, match="ERR_SSL_PROTOCOL_ERROR"):
        obj._goto_campaigns(DRAFT, _result())
    assert len(page.urls) == 3
    # После последней попытки спать незачем — с неё уже уходят.
    assert slept == [8.0, 8.0], f"лишний сон перед отказом: {slept}"


def test_only_a_connection_error_is_retried(flow):
    """Всё остальное — не наша беда: повторять чужую ошибку значит трогать
    живой кабинет три раза вместо одного и прятать причину."""
    make, slept = flow
    obj, page = make(fails=99, error="Timeout 30000ms exceeded")
    with pytest.raises(RuntimeError, match="Timeout"):
        obj._goto_campaigns(DRAFT, _result())
    assert len(page.urls) == 1, "повторяли то, что повторять не надо"
    assert slept == []


def test_the_first_hop_needs_an_account(flow):
    make, _ = flow
    obj, _page = make(fails=0)
    empty = type("Draft", (), {"ad_account_id": ""})()
    with pytest.raises(amf.AdsManagerFlowError, match="ad_account_id"):
        obj._goto_campaigns(empty, _result())
