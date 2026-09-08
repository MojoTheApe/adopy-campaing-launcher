"""Состояние кабинета читается его же словами — и не выдумывается.

Главное правило здесь: отсутствие предупреждения не значит «активен». Баннер
мог не успеть появиться, вёрстка Meta меняется, формулировку могли поменять
вчера. Чистая страница даёт `clear` — «предупреждений не видно», — и ничего
больше. Написать в этом месте `active` значило бы выдать незнание за проверку.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.analysis_agent import account_spend as sp  # noqa: E402


class _Page:
    def __init__(self, text: str, boom: bool = False):
        self._text, self._boom = text, boom

    def inner_text(self, _sel):
        if self._boom:
            raise RuntimeError("страница закрылась")
        return self._text


def test_a_disabled_account_is_named():
    state, text = sp._read_status(_Page("Some header. Your ad account is disabled. Learn more."))
    assert state == "disabled"
    assert "disabled" in text.lower()


def test_a_restricted_account_is_named():
    state, _ = sp._read_status(_Page("This account is restricted from advertising."))
    assert state == "restricted"


def test_a_quiet_page_is_clear_not_active():
    """Самое важное: «не видно предупреждений» — это не «активен»."""
    state, text = sp._read_status(_Page("Campaigns Ad sets Ads Results from 5 campaigns"))
    assert state == "clear"
    assert text == ""


def test_an_unreadable_page_says_so():
    assert sp._read_status(_Page("", boom=True))[0] == "unknown"
    assert sp._read_status(_Page("   "))[0] == "unknown"


def test_disabled_wins_over_review():
    """Порядок признаков важен: отключён весомее, чем «на проверке»."""
    page = _Page("Your account is under review. Also: this ad account is disabled.")
    assert sp._read_status(page)[0] == "disabled"


def test_status_is_stored_without_a_period():
    """Спенд отвечает «сколько за эти дни», состояние — «что сейчас».

    В одном ключе колонка статуса пустела бы при каждом другом отрезке, хотя
    кабинет как был отключён, так и остался.
    """
    assert sp._STATUS_PREFIX.endswith("|")
    assert "date" not in sp._STATUS_PREFIX
