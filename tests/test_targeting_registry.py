"""Реестр таргетов: уникальность набора, а не строки.

Правило, ради которого он написан: таргет — это набор интересов. Перестановка
значений не делает его новым, а добавленное, убранное или иначе написанное
значение — делает.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.analysis_agent import targeting_registry as tr  # noqa: E402


@pytest.fixture(autouse=True)
def _own_store(tmp_path, monkeypatch):
    """Каждый тест — свой файл: реестр машины трогать нельзя."""
    monkeypatch.setattr(tr, "_store", lambda: tmp_path / "targeting_registry.json")


def test_order_does_not_make_a_new_target():
    tr.add("A", "один, два, три, четыре")
    twin = tr.find_duplicate("один, три, четыре, два")
    assert twin is not None and twin.name == "A"


def test_a_missing_value_makes_it_different():
    tr.add("A", "один, два, три, четыре")
    assert tr.find_duplicate("один, два, три") is None


def test_an_extra_value_makes_it_different():
    tr.add("A", "один, два, три")
    assert tr.find_duplicate("один, два, три, четыре") is None


def test_a_different_spelling_is_a_different_target():
    """`investment` и `investing` — разные интересы, а не одно слово."""
    tr.add("Fin", "Investment, Trading")
    assert tr.find_duplicate("Investing, Trading") is None


def test_case_and_spacing_are_only_ways_of_writing():
    tr.add("Fin", "Investment, Trading")
    twin = tr.find_duplicate("  investment ,   TRADING ")
    assert twin is not None and twin.name == "Fin"


def test_a_repeat_inside_one_target_collapses():
    """«один, два, один» — тот же набор, что «один, два»."""
    assert tr.fingerprint("один, два, один") == tr.fingerprint("два, один")


def test_narrowing_is_part_of_what_a_target_is():
    """Те же интересы с разным сужением — разные аудитории."""
    tr.add("A", "Investment", "Business owners")
    assert tr.find_duplicate("Investment", "Frequent travellers") is None
    twin = tr.find_duplicate("Investment", "business owners")
    assert twin is not None and twin.name == "A"


def test_adding_a_duplicate_is_refused_and_names_the_twin():
    tr.add("Первый", "один, два")
    with pytest.raises(tr.TargetingError) as e:
        tr.add("Второй", "два, один")
    assert "Первый" in str(e.value), "отказ не назвал, с чем именно совпало"


def test_a_taken_name_is_refused_separately():
    """Занятое имя и повтор набора — разные беды, и говорить о них надо разное."""
    tr.add("A", "один, два")
    with pytest.raises(tr.TargetingError) as e:
        tr.add("A", "три, четыре")
    assert "занято" in str(e.value).lower()


def test_a_target_is_not_its_own_duplicate_when_edited():
    tr.add("A", "один, два")
    tr.update("A", "A renamed", "два, один")
    # пробел в имени превращается в подчёркивание — см. campaign_safe_name
    assert [t.name for t in tr.all_targets()] == ["A_renamed"]


def test_editing_into_someone_elses_set_is_refused():
    tr.add("A", "один, два")
    tr.add("B", "три, четыре")
    with pytest.raises(tr.TargetingError):
        tr.update("B", "B", "два, один")


def test_values_with_a_comma_cannot_merge_two_targets():
    """Разделитель отпечатка не должен встречаться в названии интереса."""
    a = tr.fingerprint("один два, три")
    b = tr.fingerprint("один, два три")
    assert a != b, "два разных набора дали один отпечаток"


def test_removing_and_listing():
    tr.add("A", "один")
    tr.add("B", "два")
    tr.remove("A")
    assert [t.name for t in tr.all_targets()] == ["B"]
    with pytest.raises(tr.TargetingError):
        tr.remove("A")


def test_an_empty_target_is_refused():
    with pytest.raises(tr.TargetingError):
        tr.add("", "один")
    with pytest.raises(tr.TargetingError):
        tr.add("A", "   ")


def test_sharing_reports_failure_instead_of_swallowing_it(monkeypatch):
    """«Сохранено здесь» и «увидят все» — разные вещи.

    Молчаливый отказ общей таблицы выглядел бы как успех, и таргет, которого
    не увидит никто, кроме автора, считался бы розданным.
    """
    t = tr.Target(name="A", interests="один, два")

    import src.analysis_agent.gsheet_targeting as g

    def boom(*a, **k):
        raise RuntimeError("Google отказал")

    monkeypatch.setattr(g, "add_targeting_profile", boom)
    assert "Google отказал" in tr.share(t)


def test_sharing_says_nothing_when_it_worked(monkeypatch):
    import src.analysis_agent.gsheet_targeting as g

    monkeypatch.setattr(g, "add_targeting_profile", lambda *a, **k: None)
    assert tr.share(tr.Target(name="A", interests="один")) == ""


def test_a_space_in_the_name_would_break_the_campaign_name():
    """Название кампании — это части, склеенные подчёркиванием.

    `cbUpdateName` собирает `страна_ассет_ОС_тип_ТАРГЕТ_возраст_дата`. Пробел
    внутри имени таргета попадает внутрь части, и название перестаёт
    разбираться обратно: `KW_Ait_All_Image_Investment Plus_35_0809` — это уже
    не семь частей.
    """
    assert tr.campaign_safe_name("Investment Plus") == "Investment_Plus"
    assert tr.campaign_safe_name("  Main   1  ") == "Main_1"
    assert tr.campaign_safe_name("Well - being") == "Well_-_being"


def test_the_safe_name_does_not_pile_up_underscores():
    assert tr.campaign_safe_name("a  b") == "a_b"
    assert tr.campaign_safe_name("_a_") == "a"


def test_a_name_of_only_spaces_is_refused():
    with pytest.raises(tr.TargetingError):
        tr.add("   ", "один")


def test_the_rule_applies_when_adding():
    t = tr.add("Investment Plus", "один, два")
    assert t.name == "Investment_Plus"
    assert [x.name for x in tr.all_targets()] == ["Investment_Plus"]


def test_two_names_differing_only_by_a_space_collide():
    """`Main 1` и `Main_1` — после правила одно и то же имя."""
    tr.add("Main_1", "один")
    with pytest.raises(tr.TargetingError) as e:
        tr.add("Main 1", "два, три")
    assert "занято" in str(e.value).lower()


def test_the_registry_reports_a_repeated_name():
    """Осмотр целиком, а не только правило при заведении.

    Файл можно поправить руками, а прежние записи могли лечь до того, как
    правило появилось. Молчать о повторе — значит дать двум одинаковым
    аудиториям жить под разными именами.
    """
    tr._save_raw({
        "A": {"name": "A", "interests": "один"},
        "a": {"name": "a", "interests": "два"},
    })
    assert tr.duplicates()["names"] == ["a"]


def test_the_registry_reports_a_repeated_set():
    tr._save_raw({
        "A": {"name": "A", "interests": "один, два"},
        "B": {"name": "B", "interests": "два, один"},
    })
    sets = tr.duplicates()["sets"]
    assert len(sets) == 1 and sorted(sets[0]) == ["A", "B"]


def test_a_clean_registry_reports_nothing():
    tr.add("A", "один")
    tr.add("B", "два")
    assert tr.duplicates() == {"names": [], "sets": []}


def test_the_same_set_in_a_different_order_cannot_be_added():
    """Главное правило, названное пользователем дважды."""
    tr.add("Первый", "один, два, три, четыре")
    with pytest.raises(tr.TargetingError):
        tr.add("Второй", "четыре, два, один, три")


def test_the_same_name_cannot_be_added():
    tr.add("Первый", "один, два")
    with pytest.raises(tr.TargetingError):
        tr.add("первый", "три, четыре")
