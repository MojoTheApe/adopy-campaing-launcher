"""Предохранитель: не перезапускать профиль Multilogin своими руками.

Почему он вообще есть. `MultiloginDirectCDPProvider` умеет два разных дела, и
они выглядят одинаково снаружи:

* **прицепиться** к профилю, который открыл сам MLX. Тот стартует его с
  `--proxy-server` и с работающим MLX Agent — кабинет виден Facebook из своего
  прокси, как всегда;
* **перезапустить** профиль самим провайдером. Команда, которую он для этого
  собирает, прокси не несёт вовсе и отключает расширения, то есть и MLX Agent.
  Кабинет выйдет на Facebook **с настоящего IP этой машины**, без маскировки.

Разница между ними — это разница между обычным прогоном и той подписью, из-за
которой 30 июля отключили запасной путь через обычный браузер. Снаружи же оба
случая выглядят как «профиль открылся».

Поэтому вторая ветка закрыта отказом, а открыть её можно только осознанно —
переменной окружения. Тесты ниже проверяют сам отказ и то, что он объясняет
причину, а не просто падает.

Настоящий браузер здесь не запускается: подменяются ровно те три места, что
трогают систему.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa.browser import MultiloginDirectCDPProvider  # noqa: E402


class _Entry:
    """Строка карты аккаунтов — ровно то, что читает провайдер."""

    def __init__(self, profile_id="profile-under-test"):
        self.profile_id = profile_id


def _provider(tmp_path):
    p = MultiloginDirectCDPProvider()
    # Профиль и браузер «есть»: проверяем предохранитель, а не поиск файлов.
    p._find_profile_user_data_dir = lambda pid: tmp_path / "profile"      # type: ignore[method-assign]
    p._find_mlx_chrome = lambda udd: str(tmp_path / "chrome.exe")         # type: ignore[method-assign]
    return p


def test_a_closed_profile_is_not_relaunched(tmp_path, monkeypatch):
    """Профиль закрыт — человек открывает его в MLX Desktop, а не мы.

    Перезапуск отсюда вывел бы кабинет на Facebook с настоящего IP машины.
    """
    monkeypatch.delenv("FB_RPA_ALLOW_UNPROXIED_RELAUNCH", raising=False)
    p = _provider(tmp_path)
    with patch.object(p, "_try_attach_existing", return_value=None), \
         patch.object(p, "_profile_process_running", return_value=False), \
         patch.object(p, "_kill_profile_browser") as kill:
        with pytest.raises(RuntimeError) as e:
            p.open(_Entry(), "KLD_BFL_X84")

    text = str(e.value)
    assert "refusing to relaunch" in text, "отказ перестал быть отказом"
    assert "not running" in text, "не сказано, почему прицепиться не вышло"
    assert "real IP" in text, "не сказано, чем это грозит аккаунту"
    kill.assert_not_called(), "профиль трогать нельзя — мы отказались"


def test_a_profile_without_a_debug_port_is_not_relaunched_either(tmp_path, monkeypatch):
    """Открыт, но без порта отладки — тоже не наш случай.

    Причина другая, и человеку она нужна другая: профиль запущен, но не так,
    чтобы к нему можно было прицепиться.
    """
    monkeypatch.delenv("FB_RPA_ALLOW_UNPROXIED_RELAUNCH", raising=False)
    p = _provider(tmp_path)
    with patch.object(p, "_try_attach_existing", return_value=None), \
         patch.object(p, "_profile_process_running", return_value=True), \
         patch.object(p, "_kill_profile_browser"):
        with pytest.raises(RuntimeError) as e:
            p.open(_Entry(), "KLD_BFL_X84")

    assert "no CDP debug port" in str(e.value), (
        "две разные причины отказа слились в одну — человеку не понять, что делать"
    )


def test_attaching_to_an_open_profile_needs_no_override(tmp_path, monkeypatch):
    """Обычный случай: MLX уже открыл профиль, мы просто подключаемся.

    Ничего не убивается и не запускается.
    """
    monkeypatch.delenv("FB_RPA_ALLOW_UNPROXIED_RELAUNCH", raising=False)
    p = _provider(tmp_path)
    marker = object()
    with patch.object(p, "_try_attach_existing", return_value=marker), \
         patch.object(p, "_kill_profile_browser") as kill:
        assert p.open(_Entry(), "KLD_BFL_X84") is marker
    kill.assert_not_called()
    assert p._proc is None, "прицепились к чужому процессу — своего у нас нет"


@pytest.mark.parametrize("value", ["1", "true", "YES", "on"])
def test_the_override_is_the_only_way_past_the_gate(tmp_path, monkeypatch, value):
    """Открыть ветку перезапуска можно только осознанно.

    Проверяется, что предохранитель пропустил: дальше идёт остановка профиля,
    и до неё дело доходит именно после снятия запрета.
    """
    monkeypatch.setenv("FB_RPA_ALLOW_UNPROXIED_RELAUNCH", value)
    p = _provider(tmp_path)
    with patch.object(p, "_try_attach_existing", return_value=None), \
         patch.object(p, "_profile_process_running", return_value=False), \
         patch.object(p, "_kill_profile_browser") as kill, \
         patch("time.sleep"), \
         patch("subprocess.Popen", side_effect=RuntimeError("дальше не идём")):
        with pytest.raises(RuntimeError) as e:
            p.open(_Entry(), "KLD_BFL_X84")

    assert "refusing to relaunch" not in str(e.value), "запрет не снялся"
    kill.assert_called_once(), "до остановки профиля дело не дошло"


@pytest.mark.parametrize("value", ["", "0", "no", "maybe", " "])
def test_a_half_hearted_value_does_not_open_the_gate(tmp_path, monkeypatch, value):
    """`FB_RPA_ALLOW_UNPROXIED_RELAUNCH=0` — это «нельзя», а не «переменная задана»."""
    monkeypatch.setenv("FB_RPA_ALLOW_UNPROXIED_RELAUNCH", value)
    p = _provider(tmp_path)
    with patch.object(p, "_try_attach_existing", return_value=None), \
         patch.object(p, "_profile_process_running", return_value=False), \
         patch.object(p, "_kill_profile_browser"):
        with pytest.raises(RuntimeError) as e:
            p.open(_Entry(), "KLD_BFL_X84")
    assert "refusing to relaunch" in str(e.value)


def test_a_row_without_a_profile_id_is_refused_early(tmp_path):
    """Без профиля открывать нечего, и гадать тут не о чем."""
    p = _provider(tmp_path)
    with pytest.raises(RuntimeError) as e:
        p.open(_Entry(profile_id=""), "KLD_BFL_X84")
    assert "profile_id required" in str(e.value)
