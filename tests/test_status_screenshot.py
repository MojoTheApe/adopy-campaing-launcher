# -*- coding: utf-8 -*-
"""Проблемное состояние кабинета приходит со снимком и кнопкой Copy screenshot (ACL-1430).

01.10 владелец попросил доказательство для поддержки: Doc20 disabled, 6540
restricted. До этой правки снимок страницы оставался только при неудачном
чтении, путём к файлу в подсказке. Теперь чтение, нашедшее что угодно, кроме
`clear`, снимает видимую часть Ads Manager — внутри того же профиля Multilogin,
другой страницы у чтения нет, — а карта даёт кнопку, которая кладёт картинку
в буфер обмена.

Живой кабинет здесь не открывается: страница подставная.
"""
from __future__ import annotations

import base64
import os
import base64
import re
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.analysis_agent import account_spend as sp  # noqa: E402

DAY = "2026-09-30"
ACC = "FacebookDoc20 - Example"
PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\rIDATx\x9cc\xf8\xcf\xc0\xf0\x1f\x00\x05\x00\x01\xff\xa5\xf6E@\x00\x00\x00\x00IEND\xaeB`\x82"
)


@pytest.fixture
def local(tmp_path, monkeypatch):
    """Хранилище и папка снимков — во временной папке, не в %LOCALAPPDATA%."""
    monkeypatch.setattr(sp.paths, "get_local_config_dir", lambda: str(tmp_path))
    monkeypatch.setattr(sp, "_store_path", lambda: tmp_path / "account_spend.json")
    return tmp_path


def _read(monkeypatch, status, *, grid=True, shots_on=True):
    # ACL-1535: снимок состояния выключен по умолчанию; эти тесты проверяют
    # включённый (`ACL_STATUS_SHOT=1`) путь — прежнее поведение.
    if shots_on:
        monkeypatch.setenv("ACL_STATUS_SHOT", "1")
    else:
        monkeypatch.delenv("ACL_STATUS_SHOT", raising=False)
    shots = []

    def screenshot(path, full_page=False):
        assert full_page is False, "снимок — видимая часть, баннер и колонка Delivery"
        shots.append(path)
        Path(path).write_bytes(PNG)

    page = NS(url="https://adsmanager.facebook.com/?act=111", goto=lambda *a, **k: None,
              close=lambda: None, screenshot=screenshot)
    monkeypatch.setattr(sp, "_entry_for", lambda _: NS(ad_account_id="111"))
    monkeypatch.setattr(sp, "_attach_only", lambda *a: NS(page=page, close=lambda **k: None))
    monkeypatch.setattr(sp, "_wait_for_grid", lambda _: grid)
    monkeypatch.setattr(sp, "_read_status", lambda _: status)
    monkeypatch.setattr(sp, "_shown_period", lambda _: DAY)
    monkeypatch.setattr(sp, "_read_total", lambda _: (7.0, "test"))
    monkeypatch.setattr(sp, "_read_tree", lambda *a, **k: ([], "", ""))
    return sp.read_via_rpa(ACC, DAY, DAY), shots


# ── снимок ───────────────────────────────────────────────────────────────

def test_a_disabled_read_saves_a_screenshot(local, monkeypatch):
    out, shots = _read(monkeypatch, ("disabled", "Account disabled"))
    assert out.state == "ok" and out.status == "disabled"
    assert len(shots) == 1
    assert out.status_shot and out.status_shot_at
    assert sp.status_shot_path(out.status_shot) == Path(shots[0]).resolve()
    assert Path(shots[0]).parent == local / "spend_shots"
    assert out.shot == "", "снимок неудачи — другое поле, у удачного чтения его нет"


def test_the_status_entry_keeps_the_shot(local, monkeypatch):
    out, _ = _read(monkeypatch, ("restricted", "Verification required to advertise"))
    sp._put(out)
    got = sp.get_status(ACC)
    assert got["status"] == "restricted"
    assert got["shot_id"] == out.status_shot
    assert got["shot_at"] == out.status_shot_at


def test_a_clear_read_takes_no_screenshot(local, monkeypatch):
    out, shots = _read(monkeypatch, ("clear", ""))
    assert out.state == "ok" and shots == []
    assert out.status_shot == ""
    sp._put(out)
    assert sp.get_status(ACC)["shot_id"] == ""


def test_a_clear_read_replaces_the_old_problem_shot_on_the_map(local, monkeypatch):
    bad, _ = _read(monkeypatch, ("disabled", "Account disabled"))
    sp._put(bad)
    good, _ = _read(monkeypatch, ("clear", ""))
    sp._put(good)
    assert sp.get_status(ACC)["shot_id"] == "", "кнопка у «no warning» доказывала бы прошлое"


def test_a_problem_seen_on_a_stuck_page_is_shot_too(local, monkeypatch):
    out, shots = _read(monkeypatch, ("disabled", "Account disabled"), grid=False)
    assert out.state == "failed"
    assert out.status_shot, "у отключённого кабинета таблицы может не быть — снимок тогда и нужен"
    assert out.shot, "снимок неудачи остаётся как был"


def test_a_failed_screenshot_does_not_break_the_read(local, monkeypatch):
    monkeypatch.setenv("ACL_STATUS_SHOT", "1")

    def boom(**_k):
        raise RuntimeError("page closed")
    page = NS(url="https://adsmanager.facebook.com/?act=111", goto=lambda *a, **k: None,
              close=lambda: None, screenshot=boom)
    monkeypatch.setattr(sp, "_entry_for", lambda _: NS(ad_account_id="111"))
    monkeypatch.setattr(sp, "_attach_only", lambda *a: NS(page=page, close=lambda **k: None))
    monkeypatch.setattr(sp, "_wait_for_grid", lambda _: True)
    monkeypatch.setattr(sp, "_read_status", lambda _: ("disabled", "Account disabled"))
    monkeypatch.setattr(sp, "_shown_period", lambda _: DAY)
    monkeypatch.setattr(sp, "_read_total", lambda _: (7.0, "test"))
    monkeypatch.setattr(sp, "_read_tree", lambda *a, **k: ([], "", ""))
    out = sp.read_via_rpa(ACC, DAY, DAY)
    assert out.state == "ok" and out.status == "disabled"
    assert out.status_shot == ""


# ── ACL-1535: по умолчанию снимка нет ────────────────────────────────────
#
# ACL-1532: `page.screenshot` сбрасывал override вьюпорта профиля Multilogin
# (1919×1079 → 3837×2157), а размер окна — часть отпечатка. Без
# `ACL_STATUS_SHOT=1` чтение страницу не снимает ни Playwright, ни CDP.

def _spy_read(monkeypatch, status, *, env=None, **fakes):
    if env is None:
        monkeypatch.delenv("ACL_STATUS_SHOT", raising=False)
    else:
        monkeypatch.setenv("ACL_STATUS_SHOT", env)
    calls = []

    def screenshot(path=None, **_k):
        calls.append("screenshot")
        if path:
            Path(path).write_bytes(PNG)

    def new_cdp_session(*_a, **_k):
        calls.append("new_cdp_session")
def test_with_the_switch_on_the_unclipped_capture_is_used(local, monkeypatch):
    out, calls = _spy_read(monkeypatch, ("disabled", "Account disabled"), env="1")
    assert calls == ["new_cdp_session", "captureScreenshot", "detach"]
