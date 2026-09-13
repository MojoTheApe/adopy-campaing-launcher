"""Аккаунт можно убрать с глаз, когда удалить его нельзя.

Во вкладке Accounts двенадцать строк, а работать байер может с двумя.
Остальные пришли из общей таблицы: там аккаунт есть, на этой машине про
него не записано ничего. Кнопки «удалить» у таких строк не было — и это
не забытая кнопка, а правило: удалять нечего, локальной записи нет, а
вычеркнуть строку из общей таблицы дешборд не вправе, она вернётся на
следующем обновлении.

Причина верная, но на экране об этом не говорилось ни слова, и владелец
прочитал отсутствие кнопки как поломку. Прочитал правильно: пустое место
на месте действия — это и есть поломка, даже если код прав.

Теперь такую строку можно спрятать. Это настройка вида и только она:
аккаунт остаётся в таблице, сохраняет цифры и работает везде, где
работал. Спрятанное видно счётчиком и возвращается одним нажатием.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.web import hidden_accounts as ha

TPL = ROOT / "src" / "web" / "templates" / "report.html"


@pytest.fixture(autouse=True)
def local_dir(tmp_path, monkeypatch):
    """Список живёт в машинной папке; в тестах — во временной."""
    monkeypatch.setenv("ACL_CONFIG_DIR", str(tmp_path))
    return tmp_path


def test_hidden_survives_a_reload(local_dir):
    ha.set_hidden("FacebookMKTOBFLV14 GT Ait", True)
    assert ha.is_hidden("FacebookMKTOBFLV14 GT Ait")
    assert (local_dir / "hidden_accounts.json").is_file()


def test_unhiding_brings_it_back(local_dir):
    ha.set_hidden("Acc", True)
    ha.set_hidden("Acc", False)
    assert not ha.is_hidden("Acc")
    assert ha.load() == set()


def test_the_match_forgives_case_and_spacing():
    """Одна и та же подпись приходит из таблицы и из карты по-разному.
    Спрятанное не должно выныривать из-за лишнего пробела."""
    ha.set_hidden("Facebook  DQ438", True)
    assert ha.is_hidden("facebook dq438")
    assert ha.is_hidden("  Facebook DQ438  ")


def test_an_empty_label_changes_nothing():
    ha.set_hidden("   ", True)
    assert ha.load() == set()


def test_a_broken_file_shows_everything(local_dir):
    """Не показать список из-за испорченной настройки вида хуже, чем
    показать его целиком."""
    path = local_dir / "hidden_accounts.json"
    for junk in ("не json", "[]", "{}", '{"hidden": "всё"}', '{"hidden": null}'):
        path.write_text(junk, encoding="utf-8")
        assert ha.load() == set(), f"на {junk!r} список спрятанных не пуст"


def test_the_file_is_written_whole_or_not_at_all(local_dir):
    """Половина записи — это список, который при следующем чтении
    рассыплется в пустоту и вернёт все строки разом."""
    ha.set_hidden("A", True)
    ha.set_hidden("B", True)
    data = json.loads((local_dir / "hidden_accounts.json").read_text(encoding="utf-8"))
    assert sorted(data["hidden"]) == ["a", "b"]
    assert not list(local_dir.glob("*.tmp")), "временный файл остался рядом"


def test_a_write_that_fails_does_not_take_the_page_down(monkeypatch):
    """Настройка вида не стоит того, чтобы ронять страницу аккаунтов."""
    def angry(*a, **kw):
        raise OSError("диск только для чтения")

    monkeypatch.setattr(ha, "_save", angry)
    ha.set_hidden("Acc", True)          # не бросает


# ── и это видно на экране ─────────────────────────────────────────────

def _accounts_js() -> str:
    page = TPL.read_text(encoding="utf-8")
    start = page.index("var loaded = false, openProfiles = []")
    return page[start: page.index("// ── Таргеты: общая библиотека", start)]


def test_a_row_that_cannot_be_removed_can_be_hidden():
    js = _accounts_js()
    assert "ac-hide" in js, "у неудаляемой строки снова нечего нажать"
    assert "Comes from the shared sheet" in js, (
        "не сказано, почему удалить нельзя — пустое место читается как поломка"
    )


def test_hidden_rows_are_counted_and_can_be_shown():
    js = _accounts_js()
    assert "acc-hidden-bar" in js and "ac-show-hidden" in js
    assert "accounts hidden" in js, "счётчик спрятанных не назван словами"


def test_hiding_never_touches_the_account_itself():
    """Прячется вид. Ни карта, ни общая таблица при этом не меняются."""
    js = _accounts_js()
    hide = js[js.index("function hide(btn)"):]
    hide = hide[: hide.index("\n  function ")]
    assert "/api/accounts-hidden" in hide
    for forbidden in ("/account-map-patch", "/account-map-remove", "/account-links"):
        assert forbidden not in hide, f"пряча строку, дешборд лезет в {forbidden}"
