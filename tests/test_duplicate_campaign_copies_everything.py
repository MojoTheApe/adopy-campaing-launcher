"""Дублирование кампании должно переносить всё, что человек заполнил.

Список копируемых полей был задан руками — `['age', 'budget', 'asset']`. Язык
добавили в форму позже, а сюда не дописали: дублированная кампания уходила без
языка, и на экране это выглядело как забывчивость человека.

Такой список отстаёт молча и всегда. Поэтому копируются все поля слота, а
исключения перечислены явно — их всего четыре, и у каждого своя причина.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TPL = ROOT / "src" / "web" / "templates" / "report.html"


def _fields_of_a_new_slot() -> set[str]:
    """Имена полей, которые строит `buildBlankSlotHTML`, — источник правды.

    Берём из того же места, где форма и создаётся: разойтись им негде.
    """
    src = TPL.read_text(encoding="utf-8")
    start = src.index("function buildBlankSlotHTML")
    body = src[start : src.index("\n  function ", start + 10)]
    names = set(re.findall(r"name=\?'camp_' \+ idx \+ '_([a-z_]+)\?'", body))
    names |= set(re.findall(r'name="camp_\' \+ idx \+ \'_([a-z_]+)"', body))
    return {n for n in names if not n.startswith("ad_")}


def _duplicate_body() -> str:
    src = TPL.read_text(encoding="utf-8")
    start = src.index("window.cbDuplicateSlot = function")
    return src[start : src.index("\n  function cbRenumberAds", start)]


def test_the_slot_really_has_a_language_field():
    """Если поля нет, вся проверка ниже проверяет пустоту."""
    assert "language" in _fields_of_a_new_slot()


def test_every_field_of_a_slot_is_carried_over():
    """Ни одно поле формы не должно теряться при дублировании."""
    body = _duplicate_body()
    skipped = set(re.findall(r"(\w+): 1", body[body.index("SKIP_FIELDS"):body.index("var prefix")]))
    for field in _fields_of_a_new_slot():
        if field in skipped:
            continue
        assert "querySelectorAll('[name^=\"' + prefix + '\"]')" in body, (
            "поля копируются перечислением — новое поле снова потеряется"
        )


def test_the_skipped_fields_are_few_and_named():
    """Исключения должны быть перечислены, а не подразумеваться."""
    body = _duplicate_body()
    block = body[body.index("SKIP_FIELDS"):body.index("var prefix")]
    skipped = set(re.findall(r"(\w+): 1", block))
    assert skipped == {"name", "ad_count", "targeting", "os", "page_id"}, skipped  # ACL-1880: a duplicate resets the Page to Automatic


def test_the_hand_written_list_is_gone():
    """Сторож: список, который отстаёт молча, не должен вернуться."""
    body = _duplicate_body()
    assert "['age', 'budget', 'asset']" not in body


def test_the_empty_language_is_highlighted_at_once():
    """Иначе пустое поле выглядит заполненным, пока его не тронут."""
    assert "cbLanguageChanged(dupLang)" in _duplicate_body()


def test_the_targeting_is_still_copied_with_its_visible_text():
    """Оно в исключениях — значит скопировано отдельно, выше по функции."""
    body = _duplicate_body()
    assert "newTgtHidden.value = srcTgtHidden.value" in body
    assert "newTgtSearch.value = srcTgtSearch.value" in body


# ── предупреждение про блокировку экрана ──────────────────────────────────

def test_the_publish_dialog_warns_about_locking_the_screen():
    """Сказать надо до запуска, а не в разборе через сорок минут.

    10 сентября заблокированный экран стоил 42 минут на файле, который
    соседнее объявление приняло за 74 секунды. Снять блокировку нельзя — это
    настройка машины байера; предупредить вовремя можно.
    """
    tpl = TPL.read_text(encoding="utf-8")
    block = tpl[tpl.index("window.cbPublish = function"):]
    block = block[: block.index("function _doCbPublish")]
    assert "Do not lock the screen" in block, "о блокировке экрана не предупреждают"
