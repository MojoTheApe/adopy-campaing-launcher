"""Публикатор RPA: два запрета и выбор пути к браузеру.

Здесь два предохранителя, и оба поставлены после того, как беда случилась.

**Только черновики.** Система собирает кампанию и оставляет её DRAFT; финальный
Publish жмёт человек. Публикатор отказывается принимать что-либо кроме DRAFT —
это граница между «собрали и показали» и «потратили чужие деньги».

**Никакого обычного браузера.** Открыть куки аккаунта в ванильном Chromium —
без отпечатка Mimic и без прокси профиля — это подпись, за которую кабинеты и
сжигают. 30 июля это случилось на живом аккаунте: браузер DQ204 завис, DirectCDP
не смог, и цепочка молча упала на обычный браузер с настоящей сессией. С тех
пор путь закрыт, и открыть его можно только осознанно.

Тесты подменяют провайдеров и `AdsManagerFlow`: настоящий браузер не
открывается, проверяются решения, а не Meta.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch
from types import SimpleNamespace
from src.fb_ads_rpa.operations import profile_lock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa.rpa_publisher import RpaPublisher  # noqa: E402


class _Draft:
    """Черновик кампании — ровно те поля, что читает публикатор."""

    def __init__(self, status="DRAFT", errors=None, name="KW_Ait_All_Image_X_35_0809"):
        self.status = status
        self.errors = errors or []
        self.name = name
        self.account_label = "TEST_ACCOUNT"


def _publisher() -> RpaPublisher:
    p = RpaPublisher.__new__(RpaPublisher)   # без __init__: он тянет провайдера
    p._conn = None
    p._entry = SimpleNamespace(profile_id="550e8400-e29b-41d4-a716-446655440000")
    p.browser_provider_name = "multilogin_attach"
    p._provider = MagicMock()
    p.hand_off = False
    p.dry_run = False
    p.selectors_path = None
    return p


# ─────────────── только черновики ───────────────


@pytest.mark.parametrize("status", ["ACTIVE", "active", " Active ", "PAUSED"])
def test_anything_but_a_draft_is_refused(status):
    """Граница между «собрали и показали» и «потратили чужие деньги»."""
    with pytest.raises(ValueError) as e:
        _publisher().publish_draft(_Draft(status=status))
    assert "draft-only" in str(e.value)


def test_a_draft_passes_the_status_check():
    """Регистр и пробелы — способ записи, а не другой статус."""
    p = _publisher()
    with patch.object(p, "_ensure_browser", side_effect=RuntimeError("дальше не идём")):
        with pytest.raises(RuntimeError) as e:
            p.publish_draft(_Draft(status="  draft  "))
    assert "draft-only" not in str(e.value), "черновик не пропустили"


def test_a_draft_with_errors_is_reported_not_published():
    """Ошибки сборки — это отчёт, а не исключение и не попытка публиковать."""
    p = _publisher()
    with patch.object(p, "_ensure_browser") as opened:
        res = p.publish_draft(_Draft(errors=["нет креатива", "нет ссылки"]))
    opened.assert_not_called(), "браузер открывать не за чем"
    assert res.success is False
    assert "нет креатива; нет ссылки" == res.error


# ─────────────── запрет обычного браузера ───────────────


def test_the_plain_browser_path_is_closed(monkeypatch):
    """Тот самый путь, что сжёг аккаунт 30 июля."""
    monkeypatch.delenv("FB_RPA_ALLOW_PLAIN_BROWSER", raising=False)
    with pytest.raises(RuntimeError) as e:
        _publisher()._open_playwright_profile(object(), "TEST_ACCOUNT")
    text = str(e.value)
    assert "DISABLED" in text
    assert "non-proxied" in text, "не сказано, чем именно опасен этот путь"
    assert "Multilogin Desktop" in text, "не сказано, что делать вместо этого"


@pytest.mark.parametrize("value", ["", "0", "no", "off", "почти"])
def test_a_half_hearted_value_does_not_open_the_plain_browser(monkeypatch, value):
    """`=0` значит «нельзя», а не «переменная задана»."""
    monkeypatch.setenv("FB_RPA_ALLOW_PLAIN_BROWSER", value)
    with pytest.raises(RuntimeError) as e:
        _publisher()._open_playwright_profile(object(), "TEST_ACCOUNT")
    assert "DISABLED" in str(e.value)


@pytest.mark.parametrize("value", ["1", "true", "YES", "on"])
def test_the_override_opens_it_and_nothing_else_does(monkeypatch, value):
    monkeypatch.setenv("FB_RPA_ALLOW_PLAIN_BROWSER", value)
    p = _publisher()
    with patch("src.fb_ads_rpa.rpa_publisher.PlaywrightProfileProvider") as prov:
        prov.return_value.open.return_value = "соединение"
        assert p._open_playwright_profile(object(), "TEST_ACCOUNT") == "соединение"


# ─────────────── распознавание умершей вкладки ───────────────


@pytest.mark.parametrize("message", [
    "Target page, context or browser has been closed",
    "Browser has been closed",
    "browser closed",
])
def test_a_dead_tab_is_recognised(message):
    """Умершая вкладка обычно не значит умерший браузер.

    Проверено живьём 2026-07-31: вкладка упала посреди прогона, а Mimic
    продолжал работать со своим портом. Распознав это, публикатор
    прицепляется заново вместо того, чтобы уронить прогон.
    """
    assert RpaPublisher._browser_closed_error(Exception(message)) is True


@pytest.mark.parametrize("message", [
    "Timeout 30000ms exceeded",
    "license check failed",
    "",
])
def test_an_ordinary_failure_is_not_a_dead_tab(message):
    """Иначе публикатор молча переоткрывал бы браузер на любой ошибке."""
    assert RpaPublisher._browser_closed_error(Exception(message)) is False


# ─────────────── что сказать человеку, когда не вышло ───────────────


def test_the_failure_message_says_what_to_do():
    """Отказ без следующего шага — это тупик, а не сообщение."""
    text = str(RpaPublisher._mlx_fallback_error(Exception("CDP отказал")))
    assert "Open this profile in the Multilogin" in text
    assert "CDP отказал" in text, "потеряна настоящая причина"


def test_both_causes_are_kept_when_both_paths_failed():
    text = str(RpaPublisher._mlx_fallback_error(Exception("первая"), Exception("вторая")))
    assert "первая" in text and "вторая" in text


# ─────────────── выбор пути ───────────────


def test_an_unknown_account_is_named_plainly():
    p = _publisher()
    with patch.object(p, "_resolve_entry", return_value=None):
        with pytest.raises(RuntimeError) as e:
            p._ensure_browser("НЕТ_ТАКОГО")
    assert "not in ad_account_map.csv" in str(e.value)


def test_an_open_profile_goes_straight_to_direct_cdp(monkeypatch):
    """Без attach-only MLX API отказывается стартовать уже открытый профиль.

    Пробовать его первым — значит потратить время и получить 401. Работа с
    уже открытым аккаунтом — обычный случай, а не исключение: человек не
    должен закрывать браузер, чтобы прогон начался.

    Только **без** attach-only: там настроенный провайдер сам подключается к
    открытому профилю, а не сумев — перезапускает его через MLX API с прокси.
    Ярлык через DirectCDP там давал один отказ и скрывал причину (ACL-1200,
    см. `test_an_open_profile_reaches_the_mlx_api.py`).
    """
    monkeypatch.setenv("FB_MLX_ATTACH_ONLY", "0")
    p = _publisher()
    with patch.object(p, "_resolve_entry", return_value=SimpleNamespace(profile_id="550e8400-e29b-41d4-a716-446655440000")), \
         patch.object(p, "_profile_already_running", return_value=True), \
         patch.object(p, "_open_multilogin_direct", return_value="прямое") as direct:
        assert p._ensure_browser("TEST_ACCOUNT") == "прямое"
    direct.assert_called_once()
    p._provider.open.assert_not_called(), "API дёрнули впустую"


def test_under_attach_only_an_open_profile_is_handed_to_the_provider():
    """Умолчание: открытый профиль — дело настроенного провайдера, не ярлыка."""
    p = _publisher()
    p._provider.open.return_value = "через API"
    with patch.object(p, "_resolve_entry", return_value=SimpleNamespace(profile_id="550e8400-e29b-41d4-a716-446655440000")), \
         patch.object(p, "_profile_already_running", return_value=True), \
         patch.object(p, "_open_multilogin_direct") as direct:
        assert p._ensure_browser("TEST_ACCOUNT") == "через API"
    direct.assert_not_called()
    p._provider.open.assert_called_once()


def test_attach_only_refuses_to_fall_back(monkeypatch):
    """При attach-only запасных путей нет вовсе — и это умолчание.

    Ошибка объясняет, что делать: закрыть профиль или повторить, чтобы RPA
    подняла его с автоматизацией.
    """
    monkeypatch.setenv("FB_MLX_ATTACH_ONLY", "1")
    p = _publisher()
    p._provider.open.side_effect = RuntimeError("HTTP 401: Profile Authorization error")
    with patch.object(p, "_resolve_entry", return_value=SimpleNamespace(profile_id="550e8400-e29b-41d4-a716-446655440000")), \
         patch.object(p, "_profile_already_running", return_value=False), \
         patch.object(p, "_reset_browser"), \
         patch.object(p, "_open_multilogin_direct") as direct, \
         patch.object(p, "_open_playwright_profile") as plain:
        with pytest.raises(RuntimeError) as e:
            p._ensure_browser("TEST_ACCOUNT")
    assert "attach-only" in str(e.value)
    direct.assert_not_called()
    plain.assert_not_called(), "при attach-only обычный браузер недопустим"


def test_an_expired_token_falls_through_to_direct_cdp(monkeypatch):
    monkeypatch.setenv("FB_MLX_ATTACH_ONLY", "0")
    p = _publisher()
    p._provider.open.side_effect = RuntimeError("HTTP 401: Authorization error")
    with patch.object(p, "_resolve_entry", return_value=SimpleNamespace(profile_id="550e8400-e29b-41d4-a716-446655440000")), \
         patch.object(p, "_profile_already_running", return_value=False), \
         patch.object(p, "_reset_browser"), \
         patch.object(p, "_open_multilogin_direct", return_value="прямое") as direct:
        assert p._ensure_browser("TEST_ACCOUNT") == "прямое"
    direct.assert_called_once()


def test_an_unrelated_failure_is_not_dressed_up_as_a_fallback(monkeypatch):
    """Незнакомая ошибка пробрасывается как есть.

    Подставлять на неё запасной путь значило бы лечить симптом и открывать
    браузер там, где причина совсем в другом.
    """
    monkeypatch.setenv("FB_MLX_ATTACH_ONLY", "0")
    p = _publisher()
    p._provider.open.side_effect = ValueError("что-то своё")
    with patch.object(p, "_resolve_entry", return_value=SimpleNamespace(profile_id="550e8400-e29b-41d4-a716-446655440000")), \
         patch.object(p, "_profile_already_running", return_value=False), \
         patch.object(p, "_reset_browser"), \
         patch.object(p, "_open_multilogin_direct") as direct:
        with pytest.raises(ValueError):
            p._ensure_browser("TEST_ACCOUNT")
    direct.assert_not_called()


def test_an_open_connection_is_reused():
    """Второй раз браузер не открывается — иначе профиль трогают лишний раз."""
    p = _publisher()
    p._conn = "уже открыто"
    with patch.object(p, "_resolve_entry") as resolve:
        assert p._ensure_browser("TEST_ACCOUNT") == "уже открыто"
    resolve.assert_not_called()


@pytest.fixture(autouse=True)
def mapped_profile_lease(monkeypatch):
    from src.fb_ads_rpa import rpa_publisher as rp
    monkeypatch.setattr(rp, "load_ad_account_map", lambda *_: {})
    monkeypatch.setattr(rp, "resolve_account_entry", lambda *_: SimpleNamespace(profile_id="550e8400-e29b-41d4-a716-446655440000"))
    with profile_lock("550e8400-e29b-41d4-a716-446655440000"):
        yield
