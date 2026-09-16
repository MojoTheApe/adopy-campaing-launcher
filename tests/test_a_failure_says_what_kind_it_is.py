# -*- coding: utf-8 -*-
"""Вид отказа должен быть виден из заголовка, а не из недр тикета (ACL-354).

15.09 пришёл первый живой тикет: `ad_creative_reuse: other`. Настоящая
причина — `library_pick_left_selection` — лежала внутри, в контексте. В списке
тикетов такой заголовок не говорит ничего, и чтобы понять, про что он, надо
открыть каждый. Ровно то, от чего группировка должна была избавить.

Вид причины — это ещё и ключ группировки: одинаковые отказы складываются в
один тикет по паре «шаг + вид». Значит ошибка в определении вида стоит не
только непонятного заголовка, но и перемешанных куч.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.acl_sentry import _reason_kind  # noqa: E402


def test_a_machine_label_becomes_the_kind_itself():
    """Метки шагов устойчивы от прогона к прогону и не несут имён кампаний.

    Значит таблицу под каждый новый отказ дописывать не нужно — метка сама
    годится как вид. Это и есть починка сегодняшнего `other`.
    """
    assert _reason_kind("library_pick_left_selection") == "library-pick-left-selection"
    assert _reason_kind("same_file_already_failed_this_run") == "same-file-already-failed-this-run"
    assert _reason_kind("reload_and_retry") == "reload-and-retry"


def test_silence_is_its_own_kind():
    """Шаг, не сказавший ничего, — отдельная беда, а не разновидность «прочего».

    Три отказа из четырёх 15.09 пришли с пустой причиной. Сложить их вместе с
    неузнанными значило бы спрятать самое частое, что у нас есть (ACL-1172).
    """
    assert _reason_kind("") == "silent"
    assert _reason_kind("   ") == "silent"
    assert _reason_kind(None) == "silent"


def test_the_file_chooser_failure_is_not_just_a_timeout():
    """«FileChooser.set_files: Timeout 30000ms exceeded» содержит слово timeout,
    но чинится иначе: окно выбора не дожило до передачи файла. Частное должно
    проверяться раньше общего, иначе вид теряется."""
    why = "TimeoutError: FileChooser.set_files: Timeout 30000ms exceeded."
    assert _reason_kind(why) == "file-chooser-lost"


def test_a_plain_timeout_is_still_a_timeout():
    assert _reason_kind("шаг не дождался таблицы") == "timeout"
    assert _reason_kind("Timeout 25000ms exceeded while waiting") == "timeout"


@pytest.mark.parametrize("why, kind", [
    ("таблица campaign: table-loading", "still-loading"),
    ("не найден блок Detailed targeting", "not-found"),
    ("refused: report not loaded", "refused"),
    ("открылся не тот кабинет", "wrong-account"),
])
def test_the_known_kinds_still_work(why, kind):
    assert _reason_kind(why) == kind


@pytest.mark.parametrize("why", [
    "что-то невнятное про кампанию GT_ait_2292",
    "ad3 failed on account 12345",
    "Ошибка",
])
def test_free_text_stays_other(why):
    """Свободный текст видом быть не может: в нём имена кампаний и номера, и
    каждый такой отказ дал бы свой тикет вместо общего."""
    assert _reason_kind(why) == "other"


def test_a_label_with_digits_or_spaces_is_not_a_kind():
    """Похожее на метку, но с номером внутри, — уже не метка: номер меняется."""
    assert _reason_kind("ad2 creative upload") == "other"
    assert _reason_kind("row_not_found_2292_es_text_5") != "row-not-found-2292-es-text-5"


def test_the_kind_never_carries_a_campaign_name():
    """Ключ группировки не должен зависеть от кампании — иначе каждая даст свой
    тикет, и складывать станет нечего."""
    a = _reason_kind("destination_sweep:2292 - ES Text 5 Ait:row_not_found")
    b = _reason_kind("destination_sweep:1808 - AR Text 2 Ait:row_not_found")
    assert a == b
