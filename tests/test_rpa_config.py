"""Настройки RPA: какой путь выбирается и что значит «выключено».

Эти функции решают, каким браузером открыть кабинет и каким движком вести
сборку. Ошибка здесь не падает — она уводит прогон по другой дороге, и на
экране это выглядит как «RPA стала себя вести иначе».

Два свойства проверяются во всех: **умолчание** и **как читается отказ**.
`FB_MLX_ATTACH_ONLY=0` должно означать «нельзя», а не «переменная задана» —
на этой разнице однажды уже открыли ветку, которую держали закрытой.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import config as cfg  # noqa: E402

#: переменные, которые здесь читаются: снимаем все, чтобы окружение машины
#: не подсказывало тесту ответ
_ENV = (
    "FB_PUBLISH_BACKEND", "FB_MLX_ATTACH_ONLY", "FB_BROWSER_PROVIDER",
    "MULTILOGIN_AUTOMATION_TOKEN_A", "DOLPHIN_API_BASE", "DOLPHIN_API_TOKEN",
    "FB_RPA_ENGINE", "FB_RPA_RECIPE_STOP_AFTER",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)


# ─────────────── умолчания ───────────────


def test_publishing_goes_through_the_browser_by_default():
    """`rpa`, а не старый Marketing API."""
    assert cfg.get_publish_backend() == "rpa"


def test_attach_only_is_on_by_default():
    """Умолчание — только прицепиться, без запасных путей.

    Именно это удерживает RPA от того, чтобы открыть кабинет чем-то, кроме
    профиля Multilogin.
    """
    assert cfg.get_mlx_attach_only() is True


def test_the_default_provider_is_the_attaching_one():
    assert cfg.get_browser_provider() == "multilogin_attach"


def test_the_recipe_runs_to_the_end_unless_told_otherwise():
    assert cfg.get_recipe_stop_after() == ""


def test_the_recorded_flow_is_off_unless_asked_for():
    assert cfg.get_use_recorded_flow() is False


# ─────────────── как читается отказ ───────────────


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "OFF", " False "])
def test_switching_attach_only_off_takes_a_real_no(monkeypatch, value):
    monkeypatch.setenv("FB_MLX_ATTACH_ONLY", value)
    assert cfg.get_mlx_attach_only() is False


@pytest.mark.parametrize("value", ["1", "yes", "on", "true", "", "что угодно"])
def test_anything_that_is_not_a_no_leaves_attach_only_on(monkeypatch, value):
    """Пустое значение и мусор — это не «выключено».

    Иначе опечатка в `.env` тихо открывала бы запасные пути.
    """
    monkeypatch.setenv("FB_MLX_ATTACH_ONLY", value)
    assert cfg.get_mlx_attach_only() is True


# ─────────────── выбор провайдера ───────────────


def test_an_explicit_provider_wins(monkeypatch):
    monkeypatch.setenv("FB_BROWSER_PROVIDER", "  Dolphin  ")
    assert cfg.get_browser_provider() == "dolphin"


def test_without_attach_only_and_without_a_token_it_falls_to_direct(monkeypatch):
    """Ветка `multilogin_direct` — та самая, что умеет перезапустить профиль.

    Попасть в неё можно только сняв attach-only и не имея токена: тест
    закрепляет, что случайно туда не попадают.
    """
    monkeypatch.setenv("FB_MLX_ATTACH_ONLY", "0")
    assert cfg.get_browser_provider() == "multilogin_direct"


def test_with_a_token_it_uses_the_multilogin_api(monkeypatch):
    monkeypatch.setenv("FB_MLX_ATTACH_ONLY", "0")
    monkeypatch.setenv("MULTILOGIN_AUTOMATION_TOKEN_A", "токен")
    assert cfg.get_browser_provider() == "multilogin"


def test_a_blank_token_does_not_count_as_a_token(monkeypatch):
    monkeypatch.setenv("FB_MLX_ATTACH_ONLY", "0")
    monkeypatch.setenv("MULTILOGIN_AUTOMATION_TOKEN_A", "   ")
    assert cfg.get_browser_provider() == "multilogin_direct"


# ─────────────── прочее ───────────────


def test_the_recorded_flow_needs_its_exact_name(monkeypatch):
    monkeypatch.setenv("FB_RPA_ENGINE", "recorded_flow")
    assert cfg.get_use_recorded_flow() is True
    monkeypatch.setenv("FB_RPA_ENGINE", "recorded")
    assert cfg.get_use_recorded_flow() is False, "похожее имя не должно включать движок"


def test_the_stop_gate_is_trimmed(monkeypatch):
    """Пробел в `.env` не должен превращаться в несуществующий шаг."""
    monkeypatch.setenv("FB_RPA_RECIPE_STOP_AFTER", "  s12_conversion_location  ")
    assert cfg.get_recipe_stop_after() == "s12_conversion_location"


def test_the_dolphin_address_loses_its_trailing_slash(monkeypatch):
    monkeypatch.setenv("DOLPHIN_API_BASE", "http://127.0.0.1:3001/")
    assert cfg.get_dolphin_api_base() == "http://127.0.0.1:3001"
    monkeypatch.delenv("DOLPHIN_API_BASE")
    assert cfg.get_dolphin_api_base() == "http://127.0.0.1:3001"
