"""Браузер RPA: подбор Mimic под профиль и разбор путей.

Большая часть `browser.py` без живого CDP не проверяется, но решения, которые
он принимает **до** запуска, проверяются полностью — и ошибаются они молча.

Главное здесь — подбор версии Mimic. Chromium отказывается открывать профиль,
созданный более новой сборкой: «Chrome version downgrade». На экране это
выглядит как «профиль не открывается», хотя дело в том, что взяли не тот
исполняемый файл из нескольких лежащих рядом.

Дерево `mlx/deps` и папки профилей делаются настоящими, во временной папке:
логика тут про файлы на диске.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import browser as br  # noqa: E402

UUID_LIKE = "-".join(["f" * 8, "f" * 4, "f" * 4, "f" * 4, "f" * 12])


# ─────────────── папка профиля из пути ───────────────


def test_the_folder_is_inferred_from_the_path():
    """…/profiles/{ws}/{folder}/{profile} — один идентификатор меньше руками."""
    assert br._folder_id_from_profile_dir(f"C:/mlx/profiles/ws/{UUID_LIKE}/prof") == UUID_LIKE


@pytest.mark.parametrize("path", [
    None, "", "C:/mlx/profiles/ws/Downloads/prof",
])
def test_a_folder_that_is_not_a_uuid_is_not_inferred(path):
    """На той же глубине может оказаться обычный каталог."""
    assert br._folder_id_from_profile_dir(path) is None


# ─────────────── версия профиля ───────────────


def test_the_version_is_read_from_last_version(tmp_path):
    (tmp_path / "Last Version").write_text("149.0.7827.55\n", encoding="utf-8")
    assert br._profile_chrome_version(tmp_path) == "149.0.7827.55"


def test_the_newest_snapshot_is_used_when_there_is_no_file(tmp_path):
    """Сравнение по числам, а не по строкам: иначе «9» окажется больше «149»."""
    for v in ("9.0.1", "149.0.7827.55", "132.0.1"):
        (tmp_path / "Snapshots" / v).mkdir(parents=True)
    assert br._profile_chrome_version(tmp_path) == "149.0.7827.55"


def test_an_empty_profile_has_no_version(tmp_path):
    assert br._profile_chrome_version(tmp_path) is None


@pytest.mark.parametrize("text,expected", [
    ("149.0.7827.55", (149, 0)),
    ("150.4", (150, 4)),
    ("150", (150, 0)),
    ("не версия", None),
    ("", None),
    (None, None),
])
def test_the_version_parses_or_admits_it_cannot(text, expected):
    assert br._parse_chrome_major_minor(text) == expected


# ─────────────── подбор Mimic ───────────────


@pytest.fixture()
def mlx(tmp_path, monkeypatch):
    monkeypatch.setenv("MLX_HOME", str(tmp_path))
    return tmp_path


def _mimic(root: Path, *versions: str) -> None:
    for v in versions:
        d = root / "deps" / f"mimic_{v}"
        d.mkdir(parents=True)
        (d / "chrome.exe").write_text("", encoding="utf-8")


def test_without_a_profile_the_newest_mimic_wins(mlx):
    _mimic(mlx, "132.1", "150.4", "149.2")
    assert br._find_mlx_chrome_for_profile().parent.name == "mimic_150.4"


def test_a_profile_gets_a_mimic_at_least_as_new_as_itself(mlx, tmp_path):
    """Chromium не открывает профиль более новой сборки — «version downgrade».

    Взять просто самый новый нельзя: он может оказаться новее нужного и
    отработать, а вот более старый сломает профиль. Проверяется, что берётся
    подходящий, а не первый попавшийся.
    """
    _mimic(mlx, "150.4", "149.2", "132.1")
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "Last Version").write_text("149.0.7827.55", encoding="utf-8")
    chosen = br._find_mlx_chrome_for_profile(profile)
    assert chosen.parent.name in ("mimic_150.4", "mimic_149.2"), (
        "выбран Mimic старее профиля — он его не откроет"
    )


def test_when_nothing_is_new_enough_the_newest_is_still_offered(mlx, tmp_path):
    """Отказ здесь ничем не лучше попытки: пусть человек увидит настоящую ошибку."""
    _mimic(mlx, "132.1")
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "Last Version").write_text("149.0.7827.55", encoding="utf-8")
    assert br._find_mlx_chrome_for_profile(profile).parent.name == "mimic_132.1"


def test_no_mimic_installed_means_no_answer(mlx):
    assert br._find_mlx_chrome_for_profile() is None
    (mlx / "deps").mkdir()
    assert br._find_mlx_chrome_for_profile() is None, "пустой deps — тоже нет ответа"


def test_a_broken_mimic_name_does_not_win_by_accident(mlx):
    """`mimic_старьё` разбирается в (0, 0) и не должен обойти настоящую версию."""
    _mimic(mlx, "150.4", "старьё")
    assert br._find_mlx_chrome_for_profile().parent.name == "mimic_150.4"


# ─────────────── распознавание отказа из-за версии ───────────────


@pytest.mark.parametrize("message", [
    "Chrome version downgrade detected",
    "User data migration failed",
    "Access is denied. (os error 5) chrome_Delete",
])
def test_a_version_failure_is_recognised(message):
    """Эту ошибку лечит другой Mimic, а не повтор той же попытки."""
    assert br._downgrade_launch_error(Exception(message)) is True


@pytest.mark.parametrize("message", [
    "Timeout 30000ms exceeded",
    "ECONNREFUSED",
    "Access is denied.",
    "",
])
def test_an_ordinary_failure_is_not_a_version_failure(message):
    """Иначе на любую ошибку доступа начнётся подбор версий вместо разбора."""
    assert br._downgrade_launch_error(Exception(message)) is False


# ─────────────── закрытие соединения ───────────────


class _Browser:
    def __init__(self, kind: str):
        self.closed = False
        self.disconnected = False
        if kind == "cdp":
            self.disconnect = self._disconnect

    def close(self):
        self.closed = True

    def _disconnect(self):
        self.disconnected = True


class _Playwright:
    def __init__(self):
        self.stopped = False

    def stop(self):
        self.stopped = True


def _conn(kind: str, remote: bool):
    return br.BrowserConnection(
        provider="test", page=object(), browser=_Browser(kind),
        playwright=_Playwright(), remote=remote,
    )


def test_handing_off_a_cdp_session_leaves_the_window_open():
    """Передача человеку: отцепляемся, окно остаётся.

    Закрыть его значило бы отобрать у человека то, ради чего передавали.
    """
    c = _conn("cdp", remote=True)
    c.close(keep_browser_open=True)
    assert c.browser.disconnected is True
    assert c.browser.closed is False


def test_handing_off_a_persistent_context_does_not_stop_playwright():
    """Для постоянного контекста `stop()` закрывает само окно.

    Это и есть разница между «отцепиться» и «закрыть», незаметная снаружи.
    """
    c = _conn("persistent", remote=False)
    c.close(keep_browser_open=True)
    assert c.browser.closed is False
    assert c.playwright.stopped is False, "окно человека закрыли бы этим вызовом"


def test_an_ordinary_close_closes_the_browser():
    c = _conn("persistent", remote=False)
    c.close()
    assert c.browser.closed is True
    assert c.playwright.stopped is True


def test_a_failing_browser_does_not_break_the_caller():
    """Закрытие — уборка. Исключение отсюда уронило бы прогон на выходе."""
    c = _conn("persistent", remote=False)
    c.browser.close = lambda: (_ for _ in ()).throw(RuntimeError("уже мертво"))
    c.close()
    assert c.playwright.stopped is True, "уборка не доведена до конца"
