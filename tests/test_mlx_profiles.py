"""Профили Multilogin: какой профиль чей, и почему тут нельзя ошибиться.

Подключить рекламный кабинет — значит связать его с профилем Multilogin. Если
связать не с тем, кабинет откроется из чужой личности: с другим отпечатком,
другим прокси и другим IP. Это ровно та подпись, за которую аккаунты и
получают чекпоинт.

Поэтому модуль полон мест, где правильный ответ — «не знаю». Тесты ниже
проверяют в первую очередь их, а не удачные случаи.

Дерево профилей делается настоящее, во временной папке, через `MLX_HOME`:
логика тут — про файлы на диске, и заглушка проверяла бы заглушку.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import mlx_profiles as mp  # noqa: E402

WS = "11111111-1111-1111-1111-111111111111"
FOLDER = "22222222-2222-2222-2222-222222222222"
PROFILE = "359d3e44-aaaa-bbbb-cccc-1234567890ab"


@pytest.fixture()
def mlx_home(tmp_path, monkeypatch):
    """Настоящее дерево …/profiles/<workspace>/<folder>/<profile>."""
    monkeypatch.setenv("MLX_HOME", str(tmp_path))
    (tmp_path / "profiles" / WS / FOLDER / PROFILE).mkdir(parents=True)
    return tmp_path


# ─────────────── где искать профили ───────────────


def test_the_root_follows_mlx_home(tmp_path, monkeypatch):
    monkeypatch.setenv("MLX_HOME", str(tmp_path))
    assert mp._profiles_root() == tmp_path / "profiles"


def test_a_missing_root_is_empty_not_an_error(tmp_path, monkeypatch):
    """Машина без Multilogin — обычное дело, а не отказ дешборда."""
    monkeypatch.setenv("MLX_HOME", str(tmp_path / "нет-такой"))
    assert mp.all_profile_ids() == []
    assert mp.folder_id_for(PROFILE) == ""


def test_profiles_come_newest_first(mlx_home):
    """Тот, которым только что пользовались, и есть подключаемый."""
    root = mlx_home / "profiles" / WS / FOLDER
    old = root / "aaaaaaaa-0000-0000-0000-000000000000"
    old.mkdir()
    os.utime(old, (1, 1))                      # заведомо старее
    ids = mp.all_profile_ids()
    assert ids[0] == PROFILE, "первым должен идти профиль с самым свежим временем"
    assert old.name in ids


# ─────────────── короткое имя Windows ───────────────


def test_a_short_name_expands_to_its_only_match():
    """Windows сокращает путь до `359D3E~1` — по нему и находим профиль."""
    assert mp._expand_short_name("359D3E~1", [PROFILE]) == PROFILE


def test_an_ambiguous_short_name_returns_nothing():
    """Два профиля с общим началом — это не «возьмём первый».

    Взять первый значит открыть кабинет из чужой личности: другой отпечаток,
    другой прокси, другой IP. «Не знаю» здесь — единственный верный ответ.
    """
    twin = "359d3e99-0000-0000-0000-000000000000"
    assert mp._expand_short_name("359D3E~1", [PROFILE, twin]) is None


def test_an_empty_short_name_returns_nothing():
    assert mp._expand_short_name("", [PROFILE]) is None
    assert mp._expand_short_name("~1", [PROFILE]) is None


# ─────────────── папка профиля ───────────────


def test_the_folder_is_read_off_the_path(mlx_home):
    """Одним идентификатором меньше, который человек копирует руками."""
    assert mp.folder_id_for(PROFILE) == FOLDER


def test_a_folder_that_is_not_a_uuid_is_not_a_folder(tmp_path, monkeypatch):
    """На той же глубине может оказаться обычный каталог."""
    monkeypatch.setenv("MLX_HOME", str(tmp_path))
    (tmp_path / "profiles" / WS / "Downloads" / PROFILE).mkdir(parents=True)
    assert mp.folder_id_for(PROFILE) == ""


def test_an_empty_profile_id_asks_nothing(mlx_home):
    assert mp.folder_id_for("") == ""
    assert mp.folder_id_for("   ") == ""


# ─────────────── имя из заголовка окна ───────────────


def test_a_profile_name_is_taken_from_the_window_title():
    assert mp._name_from_title("KLD_BFL_X84: Ads Manager") == "KLD_BFL_X84"


@pytest.mark.parametrize("title", [
    "",                                   # пусто
    "Ads Manager",                        # без двоеточия
    "Kuwait forms: Ads Manager",          # пробел — это не имя профиля
    "x" * 61 + ": что-то",                # слишком длинно для имени
])
def test_an_ordinary_page_title_is_not_a_profile_name(title):
    """Обычная страница с двоеточием не должна выдаваться за имя профиля."""
    assert mp._name_from_title(title) == ""


# ─────────────── хвост имени ───────────────


def test_the_name_key_is_the_trailing_token():
    """`KLD_BFL_W27` и `FacebookMKTOBFLW27 SV Ait` — один аккаунт."""
    assert mp.profile_name_key("KLD_BFL_W27") == "W27"
    assert mp.profile_name_key("kld_bfl_x84") == "X84"


def test_a_name_without_a_tail_has_no_key():
    assert mp.profile_name_key("Facebook") == ""
    assert mp.profile_name_key("") == ""
    assert mp.profile_name_key(None) == ""


# ─────────────── номер кабинета из истории ───────────────


def _history(profile_dir: Path, urls: list[tuple[str, int]]) -> None:
    """Синтетическая история браузера: (url, время последнего визита)."""
    default = profile_dir / "Default"
    default.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(default / "History")
    con.execute("CREATE TABLE urls (url TEXT, last_visit_time INTEGER)")
    con.executemany("INSERT INTO urls VALUES (?, ?)", urls)
    con.commit()
    con.close()


def test_the_ad_account_is_read_from_the_profiles_own_history(mlx_home):
    """Ничего не открывает и никуда не ходит — читает файл на диске.

    Опрос живого кабинета через его браузер предшествовал чекпоинту
    2026-08-25, и это место намеренно так не делает.
    """
    _history(mlx_home / "profiles" / WS / FOLDER / PROFILE, [
        ("https://adsmanager.facebook.com/adsmanager/manage/campaigns?act=1482029437017540", 30),
    ])
    assert mp.ad_account_id_for(PROFILE) == "act_1482029437017540"


def test_one_stray_click_does_not_rename_the_owner(mlx_home):
    """Берётся тот номер, что встречается чаще, а не самый свежий."""
    _history(mlx_home / "profiles" / WS / FOLDER / PROFILE, [
        ("https://adsmanager.facebook.com/adsmanager/manage/campaigns?act=999888777666", 99),
        ("https://adsmanager.facebook.com/adsmanager/manage/campaigns?act=1482029437017540", 30),
        ("https://adsmanager.facebook.com/adsmanager/manage/campaigns?act=1482029437017540", 20),
    ])
    assert mp.ad_account_id_for(PROFILE) == "act_1482029437017540"


def test_no_history_means_no_answer(mlx_home):
    assert mp.ad_account_id_for(PROFILE) == ""
    assert mp.ad_account_id_for("") == ""


def test_history_without_ads_manager_means_no_answer(mlx_home):
    _history(mlx_home / "profiles" / WS / FOLDER / PROFILE, [
        ("https://facebook.com/", 10),
    ])
    assert mp.ad_account_id_for(PROFILE) == ""
