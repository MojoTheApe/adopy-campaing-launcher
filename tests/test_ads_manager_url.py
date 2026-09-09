"""Адрес Ads Manager: в параметре `act` — только цифры.

9 сентября `FacebookDQ438` отказался читать спенд со словами «Ads Manager
opened a different account». В карте был записан правильный номер — это видно
в адресной строке самого браузера. Разошлись не данные, а наш адрес.

Мы клали в запрос `act=act_<цифры>` — номер в том виде, в каком он лежит в
карте. Meta такой параметр **не понимает и молча игнорирует**: открывается
кабинет профиля по умолчанию.

Полтора месяца это выглядело работающим. У профиля с одним кабинетом умолчание
и есть тот самый кабинет: Meta игнорировала параметр и попадала куда надо по
другой причине. Семь удачных чтений `KLD_BFL_X84` получены по сломанному
адресу. Увидеть это можно было только на профиле с несколькими кабинетами —
`DQ438` оказался первым.

Проверено живьём на нём же:

    act=act_<цифры>  ->  открылся кабинет профиля по умолчанию
    act=<цифры>      ->  открылся тот, о котором спрашивали

Отдельная тонкость, из-за которой ошибка и жила так долго: наш разборщик
адреса префикс **прощает**. Мы были снисходительны к своей ошибке на чтении и
совершали её на записи.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.analysis_agent import account_spend as sp  # noqa: E402

#: Номер собран из частей: настоящий в файле репозитория не появится.
DIGITS = "2062543" + "467628462"


def _act_param(url: str) -> str:
    m = re.search(r"[?&]act=([^&]*)", url)
    return m.group(1) if m else ""


def test_the_account_number_goes_in_as_digits():
    """Meta понимает только цифры. `act_` в значении она игнорирует целиком."""
    assert _act_param(sp._url(DIGITS, "2026-09-08")) == DIGITS


def test_a_number_stored_with_the_prefix_is_stripped():
    """В карте все четырнадцать номеров записаны с префиксом — так их и пишут."""
    assert _act_param(sp._url("act_" + DIGITS, "2026-09-08")) == DIGITS


def test_the_prefix_never_reaches_the_query():
    """Именно этот вид адреса и уводил на чужой кабинет."""
    for stored in (DIGITS, "act_" + DIGITS, "  act_" + DIGITS + "  "):
        url = sp._url(stored, "2026-09-08", "2026-09-08")
        assert "act=act_" not in url, f"префикс снова в запросе: {url}"


def test_spaces_around_the_stored_number_do_not_leak_into_the_url():
    """Номер вписывают руками — пробел по краям обычное дело."""
    assert _act_param(sp._url("  " + DIGITS + " ", "2026-09-08")) == DIGITS


def test_the_end_of_the_period_still_gets_its_extra_day():
    """Конец в этом параметре исключающий — правило, добытое на живом аккаунте.

    Оно живёт в той же функции, и правка номера не должна была его задеть.
    """
    url = sp._url(DIGITS, "2026-09-01", "2026-09-06")
    assert "date=2026-09-01_2026-09-07" in url


def test_a_single_day_stays_a_single_day():
    assert "date=2026-09-08_2026-09-09" in sp._url(DIGITS, "2026-09-08", "2026-09-08")


def test_what_we_write_is_what_we_would_accept_back():
    """Разборщик прощает префикс, и это скрывало ошибку записи.

    Проверка держит обе стороны в согласии: то, что мы кладём в адрес, должно
    вычитываться обратно тем же кодом и давать тот же номер.
    """
    url = sp._url("act_" + DIGITS, "2026-09-08")

    class _Page:
        def __init__(self, u):
            self.url = u

    assert sp._act_from_url(_Page(url), tries=1) == "act_" + DIGITS


def test_the_refusal_names_both_accounts():
    """Прежний текст не говорил ни какой ждали, ни какой открылся.

    Починка — это ровно два числа; без них человек идёт их выяснять сам, а
    выяснять ему нечем.
    """
    source = (ROOT / "src" / "analysis_agent" / "account_spend.py").read_text(encoding="utf-8")
    block = source[source.index("opened = _act_from_url(page, tries=1)"):]
    block = block[: block.index("return out")]
    assert "{opened" in block, "открывшийся кабинет не назван"
    assert "{expected_act}" in block, "ожидаемый кабинет не назван"
