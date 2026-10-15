# -*- coding: utf-8 -*-
r"""Профиль, поднятый самим роботом, — «свой», даже когда Windows показывает
его папку короткими именами 8.3 (ACL-1337).

27.09 на stage 47152af 6540 и 9943 были открыты роботом с портами 59860 и
53503, но в командной строке процессов папка профиля стояла как
C:\Users\EVGENI~1\mlx\profiles\B18D03~1\…, а робот ждал длинный путь. Пути не
совпали, и аккаунт остановился с «профиль открыт руками».
"""
from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

# ACL-1582: файл идёт только в Windows-задание CI; причина — в маркере.
pytestmark = pytest.mark.windows_only(reason="8.3 short names (kernel32.GetShortPathNameW) exist only on Windows")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import browser as br  # noqa: E402


def _process(udd, port=19222, listening=True):
    return NS(info={"exe": "C:/Users/x/mlx/mimic/chrome.exe",
                    "cmdline": [f"--user-data-dir={udd}", f"--remote-debugging-port={port}"]},
              net_connections=lambda **kw: [NS(status="LISTEN", laddr=NS(port=port))] if listening else [])


def _provider(monkeypatch, procs):
    import psutil
    monkeypatch.setattr(psutil, "process_iter", lambda _: procs)
    provider = br.MultiloginDirectCDPProvider()
    monkeypatch.setattr(provider, "_cdp_accepting", lambda *a: True)
    return provider


def test_a_short_path_in_the_command_line_names_the_same_profile(monkeypatch, tmp_path):
    long_dir = tmp_path / "b18d0399-profiles-long-name" / "5d508a9c-82ac-profile"
    long_dir.mkdir(parents=True)
    short_form = r"C:\Users\EVGENI~1\mlx\profiles\B18D03~1\5D508A~1"
    real = os.path.realpath

    def fake_realpath(value, *a, **kw):
        # На машине владельца Windows раскрывает короткое имя в длинное.
        return str(long_dir) if value == short_form else real(value, *a, **kw)

    monkeypatch.setattr(br.os.path, "realpath", fake_realpath)
    provider = _provider(monkeypatch, [_process(short_form, 59860)])
    assert provider._discover_owned_debug_port(str(long_dir)) == 59860


@pytest.mark.skipif(os.name != "nt", reason="8.3 short names exist only on Windows")
def test_a_real_windows_short_name_resolves(monkeypatch, tmp_path):
    long_dir = tmp_path / "a-directory-with-a-long-name" / "profile-with-long-name"
    long_dir.mkdir(parents=True)
    buf = ctypes.create_unicode_buffer(1024)
    n = ctypes.windll.kernel32.GetShortPathNameW(str(long_dir), buf, 1024)
    short = buf.value if n else ""
    if not short or short.lower() == str(long_dir).lower():
        pytest.skip("8.3 short names are disabled on this volume")
    provider = _provider(monkeypatch, [_process(short, 53503)])
    assert provider._discover_owned_debug_port(str(long_dir)) == 53503


def test_another_profile_is_still_not_owned(monkeypatch, tmp_path):
    mine = tmp_path / "profile-a"
    other = tmp_path / "profile-b"
    mine.mkdir()
    other.mkdir()
    provider = _provider(monkeypatch, [_process(str(other), 19222)])
    assert provider._discover_owned_debug_port(str(mine)) is None


def test_a_hand_opened_profile_without_a_port_is_still_not_owned(monkeypatch, tmp_path):
    mine = tmp_path / "profile-a"
    mine.mkdir()
    provider = _provider(monkeypatch, [_process(str(mine), 0, listening=False)])
    assert provider._discover_owned_debug_port(str(mine)) is None
