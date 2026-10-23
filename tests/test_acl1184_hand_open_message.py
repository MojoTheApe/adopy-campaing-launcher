# -*- coding: utf-8 -*-
"""Профиль открыт руками — человек видит выход, а не трассировку (ACL-1184).

Поймано 15.09 при подключении аккаунтов:

    RuntimeError: FacebookDoc19 - Eros Andersson: profile is already running;
    no restart was performed

Проверок «профиль запущен» было две, с разными аргументами: дешборд
(`account_spend`) проверял написания найденной папки и пропускал проверку,
если папку не нашёл; `MultiloginAttachOnlyProvider.open` проверял
`udd or profile_id` и при `allow_restart=False` бросал голый `RuntimeError`.
Теперь проверка одна (`MultiloginDirectCDPProvider.profile_is_running`), а
отказ слоя RPA — остановка `profile_open_by_hand`, которую дешборд
показывает текстом `hand_message()` с кодом `open_by_hand`.

Тесты «папка не найдена» и «проверки разошлись» падают на коде до ACL-1184.
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.analysis_agent import account_spend as sp  # noqa: E402
from src.fb_ads_rpa import browser as br  # noqa: E402
from src.fb_ads_rpa import profile_dir  # noqa: E402
from src.fb_ads_rpa import profile_trouble as pt  # noqa: E402

PID = str(uuid.UUID(int=1184))
UDD = "C:/mlx/profiles/ws/folder/" + PID


def _entry():
    return SimpleNamespace(profile_id=PID, folder_id="folder", multilogin_instance="A")


@pytest.fixture
def world(monkeypatch):
    """Настоящие провайдеры; процессы Mimic — по сценарию теста.

    `running` — ответы проверки процессов по очереди (последний повторяется),
    `asked` — с какими аргументами её звали. Запуск и остановка профиля
    записываются в `ev` — в этих сценариях их быть не должно.
    """
    w = SimpleNamespace(running=[True], asked=[], ev=[])

    def process_running(self, user_data_dir, profile_id=""):
        w.asked.append((user_data_dir, profile_id))
        return w.running.pop(0) if len(w.running) > 1 else w.running[0]

    monkeypatch.setattr(br.MultiloginDirectCDPProvider, "_profile_process_running", process_running)
    monkeypatch.setattr(br.MultiloginDirectCDPProvider, "_try_attach_existing", lambda *a, **kw: None)
    monkeypatch.setattr(br.MultiloginDirectCDPProvider, "_discover_owned_debug_port", lambda *a, **kw: None)
    monkeypatch.setattr(br, "_ROBOT_PROFILES", {})
    monkeypatch.setattr(br, "_find_mlx_profile_dir", lambda pid: UDD)
    monkeypatch.setattr(br.time, "sleep", lambda s: None)
    monkeypatch.setattr(br, "_kill_chrome_for_user_data_dir", lambda *a, **k: w.ev.append("kill"))
    monkeypatch.setattr(profile_dir, "short_path", lambda p: "")

    prov = br.MultiloginAttachOnlyProvider()
    monkeypatch.setattr(prov, "_stop_profile", lambda *a, **k: w.ev.append("stop"))
    monkeypatch.setattr(prov, "_start_via_mlx_api", lambda *a, **k: w.ev.append("start"))
    monkeypatch.setattr(br, "get_browser_provider", lambda name=None: prov)
    w.prov = prov

    # Дешборд: папка профиля найдена (тесты «не найдена» её убирают).
    monkeypatch.setattr(sp, "_mlx_profile_dir", lambda *a, **kw: Path(UDD))
    monkeypatch.setattr(sp, "_set_wait_note", lambda *a: None)
    monkeypatch.setattr(sp, "_record", lambda *a, **k: None)
    from src.fb_api import stop_log
    monkeypatch.setattr(stop_log, "refusal", lambda pid, *a, **k: "")
    monkeypatch.setattr(sp, "_slot_queue", lambda: SimpleNamespace(
        acquire=lambda *a, **k: {}, release=lambda *a, **k: None))
    return w


# ── слой RPA ────────────────────────────────────────────────────────────────

def test_open_without_restart_stops_with_open_by_hand_instead_of_a_bare_runtime_error(world):
    with pytest.raises(pt.ProfileStop) as stop:
        world.prov.open(_entry(), "FacebookDoc19", allow_missing_dir=True, allow_restart=False)
    assert stop.value.code == pt.OPEN_BY_HAND
    assert world.ev == [], "a hand-opened window was stopped or started"


def test_profile_folder_not_found_is_checked_by_profile_id(world, monkeypatch):
    monkeypatch.setattr(br, "_find_mlx_profile_dir", lambda pid: None)
    with pytest.raises(pt.ProfileStop) as stop:
        world.prov.open(_entry(), "FacebookDoc19", allow_missing_dir=True, allow_restart=False)
    assert stop.value.code == pt.OPEN_BY_HAND
    assert world.asked == [(PID, PID)]


def test_the_one_check_never_asks_with_an_empty_folder():
    """Пустая папка совпала бы с командной строкой ЛЮБОГО Mimic."""
    asked = []
    probe = SimpleNamespace(_profile_process_running=lambda d, p="": asked.append((d, p)) or False)
    assert br.MultiloginDirectCDPProvider.profile_is_running(probe, "", PID) is False
    assert br.MultiloginDirectCDPProvider.profile_is_running(probe, "", "") is False
    assert asked == [(PID, PID)]


def test_the_one_check_covers_the_short_8_3_folder_name(monkeypatch):
    monkeypatch.setattr(profile_dir, "short_path", lambda p: "C:/MLX~1/PROFIL~1" if p == UDD else "")
    probe = SimpleNamespace(_profile_process_running=lambda d, p="": d == "C:/MLX~1/PROFIL~1")
    assert br.MultiloginDirectCDPProvider.profile_is_running(probe, UDD, PID) is True


# ── дешборд: Connect / «open profile» ───────────────────────────────────────

def _assert_hand_message(ok, message):
    assert ok is False
    assert message == sp.hand_message(), message
    assert getattr(message, "code", "") == sp.HAND_OPEN_CODE


def test_connect_when_the_profile_folder_is_not_found_shows_the_way_out(world, monkeypatch):
    monkeypatch.setattr(sp, "_mlx_profile_dir", lambda *a, **kw: None)
    _assert_hand_message(*sp.open_profile_for_automation(PID, "folder", "A", "FacebookDoc19"))
    assert world.ev == []


def test_connect_when_the_checks_disagree_shows_the_way_out(world):
    """Проверка дешборда сказала «закрыт», а к запуску окно уже открыто руками."""
    world.running = [False, True]
    _assert_hand_message(*sp.open_profile_for_automation(PID, "folder", "A", "FacebookDoc19"))
    assert world.ev == []


# ── дешборд: чтение спенда (запуск закрытого профиля) ───────────────────────

def test_read_when_the_profile_folder_is_not_found_shows_the_way_out(world, monkeypatch):
    monkeypatch.setattr(sp, "_mlx_profile_dir", lambda *a, **kw: None)
    with pytest.raises(sp.ProfileNotAttachable) as refused:
        with sp.opening_closed_profile(sp._ReadOpen("FacebookDoc19")):
            with sp.profile_lock(PID):  # ACL-1853: reads need the held profile lease
                sp._attach_only(_entry(), "FacebookDoc19")
    assert str(refused.value) == sp.hand_message()
    assert refused.value.code == sp.HAND_OPEN_CODE
    assert world.ev == []


def test_read_when_the_checks_disagree_shows_the_way_out(world):
    world.running = [False, True]
    with pytest.raises(sp.ProfileNotAttachable) as refused:
        with sp.opening_closed_profile(sp._ReadOpen("FacebookDoc19")):
            with sp.profile_lock(PID):  # ACL-1853: reads need the held profile lease
                sp._attach_only(_entry(), "FacebookDoc19")
    assert str(refused.value) == sp.hand_message()
    assert refused.value.code == sp.HAND_OPEN_CODE
    assert world.ev == []


# ── Multilogin держит профиль, а проверка процессов его не видит ─────────────
#
# Ревью ACL-1184 (HIGH): Mimic с папкой 8.3 может не нести id профиля в
# командной строке, и без найденной папки проверка процессов промолчит. Тогда
# профиль находит отказ MLX «can't lock» при запуске — и это тот же отказ
# «открыт руками», а не голый RuntimeError.

@pytest.fixture
def mlx_holds_it(world, monkeypatch):
    import types

    import src.meta_campaign_agent.multilogin_config as cfg_mod
    import src.meta_campaign_agent.multilogin_launcher as launcher_mod

    class Launcher:
        def __init__(self, ml):
            pass

        def start_profile(self, folder_id, profile_id, headless=False):
            world.ev.append("start")
            raise launcher_mod.MultiloginLauncherError("Multilogin: can't lock profile")

        def stop_profile(self, *a, **k):
            world.ev.append("stop")

    monkeypatch.setattr(launcher_mod, "MultiloginLauncher", Launcher)
    monkeypatch.setattr(cfg_mod, "get_multilogin_instance", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr(world.prov, "_start_via_mlx_api",
                        types.MethodType(br.MultiloginAttachOnlyProvider._start_via_mlx_api, world.prov))
    monkeypatch.setattr(sp, "_mlx_profile_dir", lambda *a, **kw: None)
    world.running = [False]
    return world


def test_connect_when_multilogin_holds_the_profile_shows_the_way_out(mlx_holds_it):
    _assert_hand_message(*sp.open_profile_for_automation(PID, "folder", "A", "FacebookDoc19"))
    assert mlx_holds_it.ev == ["start"], "the profile was stopped after MLX refused the start"


def test_read_when_multilogin_holds_the_profile_shows_the_way_out(mlx_holds_it):
    with pytest.raises(sp.ProfileNotAttachable) as refused:
        with sp.opening_closed_profile(sp._ReadOpen("FacebookDoc19")):
            with sp.profile_lock(PID):  # ACL-1853: reads need the held profile lease
                sp._attach_only(_entry(), "FacebookDoc19")
    assert str(refused.value) == sp.hand_message()
    assert refused.value.code == sp.HAND_OPEN_CODE
    assert mlx_holds_it.ev == ["start"]


def test_the_reopen_retry_still_recognises_multilogin_holding_the_profile():
    """ACL-1606: повтор запуска после мягкой остановки узнаёт новую остановку."""
    held = br._open_by_hand(PID, "FacebookDoc19", br.MLX_LOCK_NO_RESTART)
    assert br._start_still_held(held) is True
    assert br._start_still_held(br._open_by_hand(PID, "FacebookDoc19", "no_restart")) is False
