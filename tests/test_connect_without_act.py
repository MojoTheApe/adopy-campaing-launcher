"""Профиль можно открыть до подключения, а подключить — без номера кабинета.

Круг был замкнутый. Дешборд, подключая найденный в Multilogin профиль, просил
номер рекламного кабинета. Номер он умеет читать сам — но только у профиля,
который **открыт автоматизацией** или хотя бы когда-то открывался на этой
машине. Открыть профиль дешборд соглашался только у аккаунта, который уже в
карте. А чтобы попасть в карту, требовался номер.

Выйти можно было единственным способом: сходить за номером руками. Порядок,
которым человек работает на самом деле, — нашёл, открыл, посмотрел в кабинете,
подключил — был запрещён кодом, а не природой вещей.

Здесь проверяется, что круг разорван в трёх местах: профиль открывается без
карты, отсутствие его папки не считается отказом, и пустой номер подключению
не мешает.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.analysis_agent import account_spend as sp  # noqa: E402

PAGE = ROOT / "src" / "web" / "templates" / "report.html"


class _Provider:
    """Провайдер, который запоминает, чем его просили открыть."""

    def __init__(self):
        self.opened_with = None

    def open(self, entry, label, **options):
        assert options == {"allow_missing_dir": True, "allow_restart": False}
        self.opened_with = SimpleNamespace(
            profile_id=entry.profile_id,
            folder_id=entry.folder_id,
            instance=entry.multilogin_instance,
            label=label,
        )
        return SimpleNamespace(close=lambda keep_browser_open=False: None)


@pytest.fixture
def browser(monkeypatch):
    """Слой браузера, который ничего не открывает по-настоящему."""
    provider = _Provider()

    class _Direct:
        # Публичная пара, как у настоящего провайдера: она делегирует
        # приватным, а strict_profile зафиксирован внутри неё.
        def attach_existing_only(self, profile_id, user_data_dir, *, new_page=False):
            return self._try_attach_existing(
                profile_id, user_data_dir, strict_profile=True, new_page=new_page
            )

        def profile_is_running(self, user_data_dir, profile_id=""):
            return self._profile_process_running(user_data_dir, profile_id)

        def _try_attach_existing(self, *a, **kw):
            return None

        def _profile_process_running(self, *a, **kw):
            return False

    monkeypatch.setattr("src.fb_ads_rpa.browser.MultiloginDirectCDPProvider", _Direct)
    monkeypatch.setattr("src.fb_ads_rpa.browser.get_browser_provider",
                        lambda name=None: provider)
    return provider


# ── открыть до подключения ────────────────────────────────────────────────

def test_a_profile_can_be_opened_without_being_in_the_account_map(browser, monkeypatch):
    """Тот самый выход из круга: карты нет, а открыть надо."""
    monkeypatch.setattr(sp, "_mlx_profile_dir", lambda *a, **kw: None)
    ok, message = sp.open_profile_for_automation("550e8400-e29b-41d4-a716-446655440000", "folder-1", "B", "FacebookDQ438")
    assert ok, message
    assert browser.opened_with.profile_id == "550e8400-e29b-41d4-a716-446655440000"
    assert browser.opened_with.folder_id == "folder-1"
    assert browser.opened_with.instance == "B"


def test_a_missing_profile_folder_is_not_a_refusal(browser, monkeypatch):
    """Папку создаёт сам Multilogin при первом запуске на этой машине.

    Отказывать из-за её отсутствия значило отказываться запускать профиль
    потому, что его ни разу не запускали.
    """
    monkeypatch.setattr(sp, "_mlx_profile_dir", lambda *a, **kw: None)
    ok, _ = sp.open_profile_for_automation("6ba7b810-9dad-11d1-80b4-00c04fd430c8")
    assert ok
    assert browser.opened_with is not None, "до запуска через Multilogin не дошли"


def test_the_instance_is_carried_through(browser, monkeypatch):
    """Профили лежат в двух разных аккаунтах Multilogin.

    Открыть не в том — значит не найти профиль вовсе.
    """
    monkeypatch.setattr(sp, "_mlx_profile_dir", lambda *a, **kw: None)
    sp.open_profile_for_automation("550e8400-e29b-41d4-a716-446655440000", instance="B")
    assert browser.opened_with.instance == "B"


def test_a_profile_opened_by_hand_is_still_refused(browser, monkeypatch, tmp_path):
    """Перезапуск разлогинивает — это правило не смягчается.

    Оно и было единственной причиной, по которой сюда вообще смотрят.
    """
    monkeypatch.setattr(sp, "_mlx_profile_dir", lambda *a, **kw: tmp_path)

    class _Running:
        # Публичная пара, как у настоящего провайдера: она делегирует
        # приватным, а strict_profile зафиксирован внутри неё.
        def attach_existing_only(self, profile_id, user_data_dir, *, new_page=False):
            return self._try_attach_existing(
                profile_id, user_data_dir, strict_profile=True, new_page=new_page
            )

        def profile_is_running(self, user_data_dir, profile_id=""):
            return self._profile_process_running(user_data_dir, profile_id)

        def _try_attach_existing(self, *a, **kw):
            return None

        def _profile_process_running(self, *a, **kw):
            return True

    monkeypatch.setattr("src.fb_ads_rpa.browser.MultiloginDirectCDPProvider", _Running)
    ok, message = sp.open_profile_for_automation("550e8400-e29b-41d4-a716-446655440000", label="DQ438")
    assert not ok
    assert message == sp.HAND_OPEN_MESSAGE
    assert browser.opened_with is None, "профиль, открытый руками, всё-таки трогали"


def test_an_already_automated_profile_is_not_restarted(browser, monkeypatch, tmp_path):
    monkeypatch.setattr(sp, "_mlx_profile_dir", lambda *a, **kw: tmp_path)
    closed = []

    class _Attached:
        # Публичная пара, как у настоящего провайдера: она делегирует
        # приватным, а strict_profile зафиксирован внутри неё.
        def attach_existing_only(self, profile_id, user_data_dir, *, new_page=False):
            return self._try_attach_existing(
                profile_id, user_data_dir, strict_profile=True, new_page=new_page
            )

        def profile_is_running(self, user_data_dir, profile_id=""):
            return self._profile_process_running(user_data_dir, profile_id)

        def _try_attach_existing(self, *a, **kw):
            return SimpleNamespace(close=lambda keep_browser_open=False: closed.append(keep_browser_open))

        def _profile_process_running(self, *a, **kw):
            return True

    monkeypatch.setattr("src.fb_ads_rpa.browser.MultiloginDirectCDPProvider", _Attached)
    ok, message = sp.open_profile_for_automation("550e8400-e29b-41d4-a716-446655440000")
    assert ok and "already open" in message
    assert closed == [True], "окно закрыли за человеком"
    assert browser.opened_with is None, "профиль подняли заново вместо того, чтобы прицепиться"


def test_the_map_based_entry_point_still_works(browser, monkeypatch):
    """Кнопка на строке подключённого аккаунта не должна была сломаться."""
    monkeypatch.setattr(sp, "_mlx_profile_dir", lambda *a, **kw: None)
    monkeypatch.setattr(sp, "_entry_for", lambda account: SimpleNamespace(
        profile_id="550e8400-e29b-41d4-a716-446655440000", folder_id="f-9", multilogin_instance="A"))
    ok, _ = sp.open_for_automation("KLD_BFL_X84")
    assert ok
    assert browser.opened_with.profile_id == "550e8400-e29b-41d4-a716-446655440000"


def test_an_account_missing_from_the_map_says_so(browser, monkeypatch):
    monkeypatch.setattr(sp, "_entry_for", lambda account: None)
    ok, message = sp.open_for_automation("Никогда не подключали")
    assert not ok and "not in the account map" in message


def test_opening_a_profile_goes_through_the_serialized_browser_lock():
    """Один замок на всё, что открывает браузер.

    Дешборд и прогон публикации делят один живой профиль Multilogin; без
    общего замка два потока полезут в одно окно.
    """
    assert getattr(sp.open_profile_for_automation, "__wrapped__", None) is not None, (
        "функция не обёрнута @serialized_browser"
    )


# ── подключение без номера ────────────────────────────────────────────────

def test_an_empty_account_number_no_longer_cancels_the_connect():
    """Пусто — «подключить и вписать позже», а не «передумал».

    Отмена осталась отменой: `prompt` возвращает `null` только по Cancel.
    """
    page = PAGE.read_text(encoding="utf-8")
    block = page[page.index("async function amapConnect"):]
    block = block[: block.index("/account-map-patch")]
    assert "if (typed === null) return;" in block, "Cancel перестал отменять"
    assert "if (!act) return;" not in block, "пустой номер снова отменяет подключение"


def test_the_prompt_says_what_to_do_when_the_profile_did_not_open():
    """Кнопки Open больше нет (ACL-1457): выход — закрыть окно и снова Connect."""
    page = PAGE.read_text(encoding="utf-8")
    assert "close it in Multilogin and press Connect again" in page
    assert "leave this" in page and "empty to connect now" in page


def test_every_found_profile_is_opened_by_its_connect_button():
    """Было две кнопки в строгом порядке: Open, потом Connect. Теперь одна."""
    page = PAGE.read_text(encoding="utf-8")
    block = page[page.index("function amapRenderHits"):]
    block = block[: block.index("function amapHint")]
    assert "btnOpen" not in block, "отдельная кнопка Open вернулась"
    assert "amapOpenAndConnect" in block, "Connect больше не открывает профиль"
    flow = page[page.index("async function amapOpenAndConnect"):]
    flow = flow[: flow.index("\n}\n")]
    assert flow.index("amapOpenFoundProfile") < flow.index("amapConnect("), "подключение раньше открытия"


def test_opening_a_found_profile_rereads_what_it_says_about_itself():
    """Иначе после успешного Open рядом останется «never opened here»."""
    page = PAGE.read_text(encoding="utf-8")
    block = page[page.index("async function amapOpenFoundProfile"):]
    block = block[: block.index("function amapHint")]
    assert "/mlx-profile-open" in block
    assert "/mlx-profile-details" in block, "подсказка о кабинете не обновляется"
    assert "delete p._details" in block, "старый ответ остался в кэше строки"


def test_the_route_exists_and_takes_a_profile_not_an_account():
    source = (ROOT / "src" / "web" / "app.py").read_text(encoding="utf-8")
    assert '@app.post("/mlx-profile-open")' in source
    block = source[source.index('@app.post("/mlx-profile-open")'):]
    block = block[: block.index("@app.post(\"/account-probe")]
    assert "profile_id" in block and "account_spend.open_profile_for_automation" in block
    assert re.search(r'form\.get\("instance"\)', block), "инстанс Multilogin не передаётся"
