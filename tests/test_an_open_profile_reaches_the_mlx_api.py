# -*- coding: utf-8 -*-
"""Открытый профиль доходит до MLX automation API (acl-1200).

16.09, живой прогон FacebookDQ501 - Duan Guyen:

    10:52:44 DirectCDP on running profile failed (…refusing to relaunch…);
             trying normal chain.
    10:52:49 Profile 2a5d0c87-… is running but no CDP debug port found
    ошибка задания: …refusing to relaunch this Multilogin profile…

Один и тот же отказ дважды и ни строчки от MLX API. Причина — `self._provider`:
`_open_multilogin_direct` подменял его на DirectCDP на входе, и «нормальная
цепочка» следующей строкой запускала DirectCDP повторно. Настроенный провайдер
не спрашивали вовсе.

Из-за этого `MultiloginAttachOnlyProvider._start_via_mlx_api` — который сам
останавливает этот один профиль через MLX API и поднимает его с automation,
сохраняя прокси и куки, — не вызывался никогда, пока профиль открыт. Ровно тот
случай, ради которого он написан. Байеру вместо этого говорили закрыть окно
руками.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import rpa_publisher  # noqa: E402
from src.fb_ads_rpa.rpa_publisher import RpaPublisher  # noqa: E402


class _Entry:
    profile_id = "550e8400-e29b-41d4-a716-446655440000"
    folder_id = "6ba7b810-9dad-11d1-80b4-00c04fd430c8"
    multilogin_instance = "A"


class _Configured:
    """Настроенный провайдер — тот, что умеет поднять профиль через MLX API."""

    def __init__(self, outcome):
        self.outcome = outcome
        self.asked = 0

    def open(self, entry, account_label):
        self.asked += 1
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome

    def close(self, entry, *, keep_browser_open=False):
        pass


class _Direct:
    """DirectCDP: отказывается поднимать профиль без прокси — и правильно."""

    REFUSAL = (
        "FacebookDQ501 - Duan Guyen: refusing to relaunch this Multilogin profile. "
        "DirectCDP can only attach to a profile MLX itself started, and the profile "
        "is open but exposes no CDP debug port."
    )

    def __init__(self):
        self.asked = 0

    def open(self, entry, account_label):
        self.asked += 1
        raise RuntimeError(self.REFUSAL)

    def close(self, entry, *, keep_browser_open=False):
        pass


@pytest.fixture
def publisher(monkeypatch):
    """Издатель с подставленными провайдерами и открытым профилем."""
    configured = _Configured(RuntimeError("MLX API: profile lock not released"))
    direct = _Direct()

    monkeypatch.setattr(rpa_publisher, "get_browser_provider", lambda name: configured)
    monkeypatch.setattr(
        rpa_publisher.RpaPublisher, "_resolve_entry", lambda self, label: _Entry()
    )
    monkeypatch.setattr(
        rpa_publisher.RpaPublisher, "_profile_already_running", staticmethod(lambda e: True)
    )
    import src.fb_ads_rpa.browser as browser_mod

    monkeypatch.setattr(browser_mod, "MultiloginDirectCDPProvider", lambda: direct)

    pub = RpaPublisher(browser_provider="multilogin_attach")
    return {"pub": pub, "configured": configured, "direct": direct}


def _attach_only(monkeypatch, value: bool) -> None:
    monkeypatch.setattr(rpa_publisher, "get_mlx_attach_only", lambda: value)


# ── то, что было сломано ──────────────────────────────────────────────────

def test_the_configured_provider_is_asked_when_the_profile_is_open(publisher, monkeypatch):
    """Ровно случай DQ501: профиль открыт, и спросить надо MLX API."""
    _attach_only(monkeypatch, True)
    with pytest.raises(RuntimeError):
        publisher["pub"]._ensure_browser("FacebookDQ501 - Duan Guyen")
    assert publisher["configured"].asked == 1


def test_directcdp_is_not_even_tried_under_attach_only(publisher, monkeypatch):
    """Ярлык через DirectCDP там давал только отказ и потерянное время."""
    _attach_only(monkeypatch, True)
    with pytest.raises(RuntimeError):
        publisher["pub"]._ensure_browser("FacebookDQ501 - Duan Guyen")
    assert publisher["direct"].asked == 0


def test_the_buyer_reads_the_mlx_reason_not_directcdps(publisher, monkeypatch):
    """Настоящую причину скрывал отказ от пути, который даже не настроен."""
    _attach_only(monkeypatch, True)
    with pytest.raises(RuntimeError) as err:
        publisher["pub"]._ensure_browser("FacebookDQ501 - Duan Guyen")
    text = str(err.value)
    assert "profile lock not released" in text
    assert "refusing to relaunch" not in text


def test_nobody_is_told_to_close_a_window_any_more(publisher, monkeypatch):
    """RPA поднимает профиль сам — просить об этом байера больше не за что."""
    _attach_only(monkeypatch, True)
    with pytest.raises(RuntimeError) as err:
        publisher["pub"]._ensure_browser("FacebookDQ501 - Duan Guyen")
    assert "close" not in str(err.value).lower()


# ── подмена провайдера ────────────────────────────────────────────────────

def test_a_failed_directcdp_attempt_leaves_the_provider_alone(publisher, monkeypatch):
    """Из-за подмены «нормальная цепочка» вторично запускала DirectCDP."""
    _attach_only(monkeypatch, False)
    pub, configured = publisher["pub"], publisher["configured"]
    assert pub._provider is configured
    with pytest.raises(RuntimeError):
        pub._open_multilogin_direct(_Entry(), "FacebookDQ501 - Duan Guyen")
    assert pub._provider is configured


def test_a_working_directcdp_attempt_does_move_the_provider(publisher, monkeypatch):
    """При успехе дальше работать должен он же — соединение уже его."""
    pub = publisher["pub"]
    opened = object()

    class _Works(_Direct):
        def open(self, entry, account_label):
            self.asked += 1
            return opened

    works = _Works()
    import src.fb_ads_rpa.browser as browser_mod

    monkeypatch.setattr(browser_mod, "MultiloginDirectCDPProvider", lambda: works)
    assert pub._open_multilogin_direct(_Entry(), "acc") is opened
    assert pub._provider is works


# ── остальные провайдеры не тронуты ───────────────────────────────────────

def test_without_attach_only_the_shortcut_still_runs_first(publisher, monkeypatch):
    """Там DirectCDP — по-прежнему верный первый ход, и он идёт первым."""
    _attach_only(monkeypatch, False)
    with pytest.raises(RuntimeError):
        publisher["pub"]._ensure_browser("FacebookDQ501 - Duan Guyen")
    assert publisher["direct"].asked == 1
    assert publisher["configured"].asked == 1


def test_a_closed_profile_goes_straight_to_the_configured_provider(publisher, monkeypatch):
    _attach_only(monkeypatch, False)
    monkeypatch.setattr(
        rpa_publisher.RpaPublisher, "_profile_already_running", staticmethod(lambda e: False)
    )
    with pytest.raises(RuntimeError):
        publisher["pub"]._ensure_browser("FacebookDQ501 - Duan Guyen")
    assert publisher["direct"].asked == 0
    assert publisher["configured"].asked == 1


def test_an_open_profile_that_connects_is_used_as_is(publisher, monkeypatch):
    """Успешный путь трогать нельзя: соединение возвращается и запоминается."""
    _attach_only(monkeypatch, True)
    opened = object()
    publisher["configured"].outcome = opened
    got = publisher["pub"]._ensure_browser("FacebookDQ501 - Duan Guyen")
    assert got is opened
    assert publisher["pub"]._conn is opened


@pytest.fixture(autouse=True)
def physical_ownership():
    from src.fb_ads_rpa.operations import profile_lock
    with profile_lock(_Entry.profile_id):
        yield
