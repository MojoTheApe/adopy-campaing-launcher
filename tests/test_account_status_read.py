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


# ══ настоящие слова Meta ══════════════════════════════════════════════════
#
# 12.09, DQ438 (act_2062543467628462): кабинет ограничен, реклама не крутится,
# а дешборд показывал «no warning». Ни одна из формулировок выше не совпала —
# все они были придуманы. Meta пишет о себе действительным залогом.

RESTRICTED = (
    "Ads Manager Billing & payments We restricted your ad account You can’t run "
    "ads. We restricted your account because some of your activity looks like it "
    "doesn’t follow our rules. You can request a review if you think this is a "
    "mistake. Request review Campaigns Ad sets Ads"
)


def test_the_words_meta_actually_used():
    state, text = sp._read_status(_Page(RESTRICTED))
    assert state == "restricted", "ограниченный кабинет снова прошёл как чистый"
    assert "We restricted your ad account" in text, "человеку не с чем идти разбираться"


def test_this_check_would_have_caught_the_old_miss():
    """Обратная проверка: сторож, повторяющий код, ничего не стоит.

    Прежний список искал «account is restricted» — страдательный залог,
    которого на странице нет.
    """
    old = ("account is restricted", "restricted from advertising",
           "advertising access is restricted")
    assert not any(p in RESTRICTED.lower() for p in old), (
        "прежние формулировки совпадают с настоящей страницей — проверка ничего не ловит"
    )


# ── банер есть, а формулировка новая ──────────────────────────────────────

def test_an_unrecognised_banner_is_not_called_quiet():
    """Список формулировок — перечень **известных**, а Meta их меняет.

    Пока банер не опознан, честный ответ — «что-то не так, вот её слова». Под
    надписью «no warning» аккаунт уже стоял неспособным крутить рекламу.
    """
    state, text = sp._read_status(_Page(
        "Ads Manager Something new happened. You can’t run ads. Request review Campaigns"
    ))
    assert state == "blocked"
    assert "can’t run ads" in text


def test_the_unclassified_answer_is_not_the_unreadable_one():
    """`unknown` значит «страница ничего не дала прочитать», и дешборд так его
    и подписывает. Прочитанный, но неопознанный банер — другое."""
    assert sp._read_status(_Page("", boom=True))[0] == "unknown"
    assert sp._read_status(_Page("You cannot run ads right now."))[0] == "blocked"


def test_a_known_phrase_still_wins_over_the_catch_all():
    """Иначе «restricted» потерялось бы в общем «blocked» — а это разные
    сообщения человеку."""
    state, _ = sp._read_status(_Page(RESTRICTED))
    assert state == "restricted"


def test_an_ordinary_page_is_still_clear():
    """Ловушка на ловушку: слишком жадный признак объявил бы больным всё."""
    state, _ = sp._read_status(_Page(
        "Campaigns Ad sets Ads Results from 5 campaigns Amount spent Reach Impressions"
    ))
    assert state == "clear"
