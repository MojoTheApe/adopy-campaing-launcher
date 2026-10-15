"""Пока идёт сборка кампании, экран не гаснет.

Заблокированный экран останавливает прогон: Windows уходит на защищённый
рабочий стол, браузер перестаёт получать время, и креатив, который
загружается за 74 секунды, грузился 42 минуты, после чего прогон сдался.
Признак этого состояния (`LogonUI.exe`) давно пишется в телеметрию — то
есть машина знала, что произошло, и молчала. Здесь убирается сама причина.

Мы просим систему не гасить экран на время работы и отпускаем просьбу,
когда работа кончилась. Ровно так делает проигрыватель видео. Это не
изменение настроек безопасности машины: блокировку, включённую человеком
или политикой домена, отменить нельзя и не нужно.

Просьба делается через `SetThreadExecutionState`, и она живёт на потоке.
Работа публикации и так идёт в своём потоке — соседние потоки дешборда
ничего не меняют.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.web import keep_awake as ka


class _FakeKernel:
    """Считает, о чём просили, в том порядке, в каком просили."""

    def __init__(self, fail: bool = False) -> None:
        self.calls: list[int] = []
        self.fail = fail

    def SetThreadExecutionState(self, flags: int) -> int:  # noqa: N802 — имя из WinAPI
        self.calls.append(flags)
        return 0 if self.fail else 0x80000000


@pytest.fixture
def kernel(monkeypatch) -> _FakeKernel:
    fake = _FakeKernel()
    monkeypatch.setattr(ka, "_kernel32", lambda: fake)
    return fake


def test_the_run_asks_for_the_display_and_the_machine(kernel):
    """Одного «не гасить экран» мало: уснувшая машина останавливает так же."""
    with ka.keep_display_awake("DQ438") as held:
        assert held is True
    asked = kernel.calls[0]
    assert asked & ka.ES_DISPLAY_REQUIRED, "экран может погаснуть"
    assert asked & ka.ES_SYSTEM_REQUIRED, "машина может уснуть"
    assert asked & ka.ES_CONTINUOUS, "просьба разовая — она должна держаться"


def test_the_request_is_released_when_the_run_ends(kernel):
    """Оставленная просьба пережила бы работу и держала бы экран
    включённым неизвестно сколько — это уже изменение поведения машины."""
    with ka.keep_display_awake():
        pass
    assert len(kernel.calls) == 2, "просьба не снята"
    assert kernel.calls[-1] == ka.ES_CONTINUOUS, "снято не тем флагом"


def test_it_is_released_even_when_the_run_fails(kernel):
    """Прогон кончается и падением, и отменой — чаще, чем успехом."""
    with pytest.raises(RuntimeError):
        with ka.keep_display_awake():
            raise RuntimeError("сборка упала")
    assert kernel.calls[-1] == ka.ES_CONTINUOUS, "после падения экран остался удержан"


def test_a_refusal_is_not_a_reason_to_stop(monkeypatch):
    """Права может не хватить, системы может не быть. Публикация важнее
    страховки: без неё прогон идёт как шёл."""
    fake = _FakeKernel(fail=True)
    monkeypatch.setattr(ka, "_kernel32", lambda: fake)
    with ka.keep_display_awake() as held:
        assert held is False
    assert len(fake.calls) == 1, "снимали то, чего не держали"


def test_no_windows_no_crash(monkeypatch):
    """Тесты и разработка идут не только на Windows."""
    monkeypatch.setattr(ka, "_kernel32", lambda: None)
    with ka.keep_display_awake() as held:
        assert held is False


def test_an_api_that_throws_does_not_take_the_run_down(monkeypatch):
    class _Angry:
        def SetThreadExecutionState(self, flags):  # noqa: N802
            raise OSError("нет доступа")

    monkeypatch.setattr(ka, "_kernel32", lambda: _Angry())
    with ka.keep_display_awake() as held:
        assert held is False


# ── и это применено к настоящей работе ────────────────────────────────

def test_the_publish_job_holds_it_around_the_whole_run():
    """Вокруг ВСЕЙ работы, а не вокруг шага: прерывать нельзя всю.

    Проверяется исходник: обернуть надо именно вызов сборки, а вызвать его
    из теста — значит поднять браузер.
    """
    src = (ROOT / "src" / "web" / "publish_jobs.py").read_text(encoding="utf-8")
    body = src[src.index("def _run_job_inner("):]
    body = body[: body.index("\ndef ")]
    assert "with keep_display_awake(" in body, "работа идёт без удержания экрана"
    hold = body.index("with keep_display_awake(")
    run = body.index("_run_rpa(")
    assert hold < run, "удержание взято после начала сборки"


def test_the_queue_does_not_hold_the_screen_while_it_waits():
    """Второй аккаунт ждёт снаружи замка. Держать экран ради ожидания
    незачем — иначе очередь из пяти аккаунтов не даёт машине уснуть весь
    вечер, даже когда работает один."""
    src = (ROOT / "src" / "web" / "publish_jobs.py").read_text(encoding="utf-8")
    body = src[src.index("def _run_job_inner("):]
    body = body[: body.index("\ndef ")]
    assert body.index("with _publish_serial(") < body.index("with keep_display_awake("), (
        "экран удерживается ещё до входа в очередь"
    )
