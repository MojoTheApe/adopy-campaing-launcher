"""Загрузка креатива оставляет след того, что видела и делала.

14.09: шесть отказов `ad_creative_upload` подряд на двух аккаунтах, и по
телеметрии о них известно ровно ничего — шаг возвращал False без единого
слова, а исключение внутри глоталось молча:

    except Exception:
        _sleep_h(1.5)
        continue

Разбор свёлся к догадкам по посмертным снимкам, и три версии подряд оказались
неверны: переименованные категории, включённый Advantage+, вес файла. Снимок
снят ПОСЛЕ всех трёх попыток — в нём уже нет ни окна выбора медиа, ни
индикатора загрузки, и отличить «кнопки не нашлось» от «окно выбора файла не
открылось» по нему нельзя.

По снимкам установлено твёрдо только то, что загрузка НЕ НАЧИНАЛАСЬ:
`input[type=file]` — ноль, «Uploading» — ноль, индикатора нет. Почему окно
медиа оказалось не в том состоянии, снимок не говорит.

Эта проверка не о том, что загрузка работает. Она о том, что шаг перестал
молчать: следующий отказ обязан принести причину, а не повод для четвёртой
версии.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SRC = ROOT / "src" / "fb_ads_rpa" / "meta_dom.py"


def _upload_block() -> str:
    """Тело цикла попыток загрузки."""
    text = SRC.read_text(encoding="utf-8")
    start = text.index("    landed = False\n    for _attempt in range(3):")
    return text[start: text.index("    if not landed:", start)]


def test_a_swallowed_exception_now_leaves_a_trace():
    """Это и была слепота: `except Exception: continue` без единой записи."""
    block = _upload_block()
    # Обработчиков в блоке два: внутренний — про одно поле для файла, внешний
    # — про всю попытку. Слепым был внешний, тот, что заканчивается `continue`.
    # `_sleep_h(1.5)` есть только во внешнем — по нему и находим.
    pause = block.index("_sleep_h(1.5)")
    outer = block[block.rindex("except Exception as exc:", 0, pause): pause]
    assert '_step["error"]' in outer, "исключение снова глотается молча"
    assert "_remember(" in outer, "след не записывается"


def test_the_trail_says_whether_there_was_anywhere_to_put_the_file():
    """Ключевое различение: поля для файла не было — или было, но не взяло."""
    block = _upload_block()
    assert '_step["file_inputs"]' in block, "число полей для файла не записано"
    assert '_step["fed"]' in block, "не записано, отдан ли файл"


def test_the_trail_says_whether_the_chooser_opened():
    """«Кнопку не нашёл» и «окно выбора файла не открылось» — разные исходы,
    и лечатся они по-разному."""
    block = _upload_block()
    assert '_step["upload_xpath_clicked"]' in block
    assert '_step["chooser"]' in block


def test_the_trail_separates_slow_from_never_started():
    """10.09 доказано: те же 25.8 МБ уходили за 62 секунды и не уходили вовсе
    с `aria-valuenow="0"`. По отсутствию плитки эти два исхода не различить,
    поэтому рядом записывается состояние индикатора."""
    block = _upload_block()
    assert '_step["waited_s"]' in block
    assert '_step["uploading"]' in block
    assert '_step["progress"]' in block


def test_the_file_and_its_size_are_named():
    """Вес сам по себе причиной не оказался, но без него нельзя даже
    поставить вопрос."""
    text = SRC.read_text(encoding="utf-8")
    diag = text[text.index("    _diag: dict = {"):]
    diag = diag[: diag.index("\n    landed = False")]
    assert '"file"' in diag and '"size_mb"' in diag


def test_the_reason_reaches_the_telemetry_and_not_only_a_file():
    """Файл на диске читает тот, кто знает, что он есть. Телеметрию читают
    всегда — значит причина должна быть там."""
    text = SRC.read_text(encoding="utf-8")
    # С acl-1210 запись своя у каждого потока — читается через
    # `last_upload_diag()`, а не из модульной переменной. С ACL-1348 туда же
    # едет и машинная причина (`note`) — её читает Sentry.
    assert ('_shot("fail_ad_creative_upload", last_upload_diag(), note=last_upload_why())'
            in text), "отказ снова уходит в телеметрию пустым"


def test_nothing_about_the_behaviour_changed():
    """Правка обязана быть безобидной: три попытки, тот же порядок действий,
    те же тридцать секунд ожидания плитки. Меняется только молчание."""
    block = _upload_block()
    assert "for _attempt in range(3):" in block, "число попыток изменилось"
    assert "for _ in range(30):" in block, "окно ожидания плитки изменилось"
    assert "expect_file_chooser(timeout=25000)" in block, "таймаут выбора файла изменился"
    # Порядок прежний: сперва отдать файл полю, потом кликать Upload.
    assert block.index("input[type=file]") < block.index("expect_file_chooser")


def test_the_trail_is_written_as_it_goes_not_at_the_end():
    """Прогон может оборваться на второй попытке — тогда файл, записанный
    только в конце, не расскажет ничего."""
    text = SRC.read_text(encoding="utf-8")
    remember = text[text.index("    def _remember("):]
    remember = remember[: remember.index("\n    landed = False")]
    assert "write_text" in remember, "след пишется не по ходу"
    assert "debug_creative_upload" in remember
