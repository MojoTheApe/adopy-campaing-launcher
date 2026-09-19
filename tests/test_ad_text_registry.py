"""Библиотека рекламных текстов: что считается одним и тем же текстом.

Тексты переехали из вкладки чужой таблицы в реестр на машине — по той же
причине, что и таргеты: 8 сентября таблица закрылась целиком, и на дешборде
это выглядело как «библиотека пуста».

Главный вопрос здесь один: когда два текста — один и тот же. У таргета это
набор, где порядок не значит ничего. У текста порядок значит всё: «купите
сейчас, скидка» и «скидка, купите сейчас» человек различает с первого взгляда.
Проверки ниже держат именно эту разницу, а заодно — что перенос из вкладки
рассказывает, чего не довёз.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.analysis_agent import ad_text_registry as ar  # noqa: E402


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    """Каждой проверке — своя библиотека, не машина разработчика."""
    monkeypatch.setattr(ar.paths, "get_local_config_dir", lambda: str(tmp_path))
    return tmp_path


def _add(name, headline="Заголовок", body="Тело", description="", **kw):
    return ar.add(name, headline, body, description, **kw)


# ── что такое «тот же текст» ──────────────────────────────────────────────

def test_the_same_words_in_a_different_order_are_a_different_text():
    """Ровно то, чем текст отличается от таргета.

    Если сравнивать как набор — а именно так устроен реестр таргетов рядом, —
    перестановка абзацев сделает новое объявление «повтором», и человек не
    сможет завести его вовсе.
    """
    _add("Первый", body="Купите сейчас. Скидка.")
    _add("Второй", body="Скидка. Купите сейчас.")
    assert len(ar.all_texts()) == 2


def test_the_same_text_typed_twice_is_refused_and_the_twin_is_named():
    _add("Оригинал", headline="Зарегистрируйтесь", body="Инвестируйте сегодня")
    with pytest.raises(ar.AdTextError) as exc:
        _add("Копия", headline="Зарегистрируйтесь", body="Инвестируйте сегодня")
    assert "Оригинал" in str(exc.value), "человеку нужно имя, а не просто «повтор»"


def test_case_and_stray_spaces_are_a_way_of_writing_not_another_text():
    """Facebook покажет одно и то же — значит, и здесь это одно и то же."""
    _add("Первый", headline="Купите  СЕЙЧАС", body="Тело   объявления")
    with pytest.raises(ar.AdTextError):
        _add("Второй", headline="купите сейчас", body="тело объявления")


def test_a_line_break_inside_the_body_makes_it_another_text():
    """Переносами разбивают абзацы; объявление читается иначе.

    Именно поэтому пробелы здесь схлопываются, а переносы — нет.
    """
    _add("Сплошняком", body="Первый абзац. Второй абзац.")
    _add("Абзацами", body="Первый абзац.\nВторой абзац.")
    assert len(ar.all_texts()) == 2


def test_a_different_description_makes_it_another_text():
    """Описание — такое же поле объявления, как остальные."""
    _add("Со звёздами", headline="Купите", body="Текст", description="★★★★ 4.6k")
    _add("Без звёзд", headline="Купите", body="Текст", description="")
    assert len(ar.all_texts()) == 2


def test_a_name_taken_by_another_text_is_refused():
    _add("Stable halal")
    with pytest.raises(ar.AdTextError) as exc:
        _add("stable  HALAL", body="Совсем другое тело")
    assert "already taken" in str(exc.value)


def test_an_empty_text_is_not_worth_saving():
    with pytest.raises(ar.AdTextError):
        ar.add("Пустой", "", "", "")


def test_a_text_needs_a_name():
    with pytest.raises(ar.AdTextError):
        ar.add("   ", "Заголовок", "Тело")


def test_a_body_keeps_its_line_breaks_but_not_its_stray_spaces():
    t = _add("Абзацы", body="Первая   строка\n\n\n\nВторая строка  ")
    assert t.body == "Первая строка\n\nВторая строка"


def test_a_control_character_from_a_paste_is_refused_but_a_line_break_is_not():
    """Копипаст из таблицы приносит \\x00 и \\x0b — они ломают разметку.

    Перенос строки при этом законен: без него не будет ни одного абзаца.
    """
    _add("Живой", body="Первая\nВторая")
    with pytest.raises(ar.AdTextError):
        _add("Битый", body="Первая\x0bВторая")


def test_a_name_cannot_span_two_lines():
    """Иначе список текстов разъезжается, а имя не совпадёт ни с чем."""
    with pytest.raises(ar.AdTextError):
        _add("Имя\nв две строки")


# ── правка и удаление ─────────────────────────────────────────────────────

def test_a_text_being_edited_is_not_its_own_duplicate():
    _add("Первый", headline="Купите", body="Текст")
    t = ar.update("Первый", "Первый", "Купите", "Текст", "★★★★")
    assert t.description == "★★★★"


def test_editing_into_someone_elses_text_is_refused():
    _add("Первый", headline="A", body="B")
    _add("Второй", headline="C", body="D")
    with pytest.raises(ar.AdTextError) as exc:
        ar.update("Второй", "Второй", "A", "B")
    assert "Первый" in str(exc.value)


def test_a_removed_text_goes_to_recently_deleted_and_can_be_restored():
    _add("Ненужный", headline="A", body="B")
    ar.remove("Ненужный")
    assert ar.all_texts() == []
    assert [d["name"] for d in ar.recently_deleted()] == ["Ненужный"]
    assert ar.restore("Ненужный").body == "B"
    assert ar.recently_deleted() == []


def test_removing_a_text_that_is_not_there_says_so():
    with pytest.raises(ar.AdTextError):
        ar.remove("Никогда не было")


# ── осмотр библиотеки целиком ─────────────────────────────────────────────

def test_duplicates_are_found_in_a_registry_edited_by_hand(store):
    """Правило при заведении не защищает файл, который правили блокнотом."""
    store.joinpath("ad_text_registry.json").write_text(
        '{"version": 1, "hidden": [], "texts": {'
        '"Первый": {"name": "Первый", "headline": "A", "body": "B", "description": "",'
        ' "asset": "", "language": "", "added_at": "", "source": "sheet"},'
        '"Второй": {"name": "Второй", "headline": "a", "body": " b ", "description": "",'
        ' "asset": "", "language": "", "added_at": "", "source": "sheet"}}}',
        encoding="utf-8")
    assert ar.duplicates()["texts"] == [["Второй", "Первый"]]


def test_a_broken_registry_file_is_reported_and_not_silently_emptied(store):
    """Пустая библиотека и нечитаемый файл требуют разных действий.

    Вернуть пустоту — значит предложить человеку завести всё заново поверх
    файла, который цел и просто не разобран.
    """
    store.joinpath("ad_text_registry.json").write_text("{ не json", encoding="utf-8")
    with pytest.raises(ar.AdTextError):
        ar.all_texts()


def test_a_registry_from_the_future_is_refused_rather_than_guessed(store):
    store.joinpath("ad_text_registry.json").write_text(
        '{"version": 2, "texts": {}, "hidden": []}', encoding="utf-8")
    with pytest.raises(ar.AdTextError):
        ar.all_texts()


def test_texts_are_listed_in_a_stable_order_regardless_of_case():
    for name in ("яблоко", "Ананас", "банан"):
        _add(name, body="тело " + name)
    assert [t.name for t in ar.all_texts()] == ["Ананас", "банан", "яблоко"]


def test_lookup_ignores_case_and_spacing():
    _add("ES how much")
    assert ar.lookup("es  HOW much").name == "ES how much"


def test_lookup_of_a_missing_text_is_nothing_not_an_error():
    assert ar.lookup("нет такого") is None
