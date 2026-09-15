"""Поле для файла берётся у браузера, а не ищется в дереве.

14.09, живой прогон: обход всего дерева — с теневыми корнями и содержимым
iframe — нашёл НОЛЬ полей `input[type=file]` при открытом окне выбора
(`cdp_inputs: 0`, три попытки подряд). Искать было нечего: окно медиа живёт в
кросс-доменном iframe, вынесенном в отдельный процесс, а `DOM.getDocument`
границу процесса не переходит, сколько ни ставь `pierce`.

Событие `Page.fileChooserOpened` несёт `backendNodeId`; он применяется в
сессии фрейма, который открыл диалог. Проверки настоящих OOPIF находятся
в test_upload_chooser_review.py.

Все проверки здесь ИСПОЛНЯЮТ код. Предыдущий обход был покрыт двенадцатью
текстовыми проверками, все зелёные — а он падал на первой строке с NameError,
потому что ни одна его не запускала.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class _Session:
    """Поддельная CDP-сессия: помнит команды и отдаёт событие по требованию."""

    def __init__(self, *, backend=555, fail_on=None, emit=True):
        self.sent, self.handlers, self.detached = [], {}, False
        self.backend, self.fail_on, self.emit = backend, fail_on, emit

    def on(self, event, handler):
        self.handlers[event] = handler

    def send(self, method, params=None):
        self.sent.append((method, params))
        if self.fail_on and method == self.fail_on:
            raise RuntimeError("нельзя " + method)
        if method == "Page.setInterceptFileChooserDialog" and (params or {}).get("enabled"):
            self._armed = True
        return {}

    def fire(self):
        h = self.handlers.get("Page.fileChooserOpened")
        if h and self.emit:
            h({"backendNodeId": self.backend, "mode": "selectSingle"})

    def detach(self):
        self.detached = True


class _Page:
    def wait_for_timeout(self, ms):
        import time
        time.sleep(ms / 1000)

    def __init__(self, session):
        self.context = type("Ctx", (), {"new_cdp_session": lambda _s, _p: session})()


def _run(session, *, click_ok=True):
    from src.fb_ads_rpa import meta_dom

    diag: dict = {}

    def click():
        session.fire()          # нажатие открывает окно — событие приходит
        return click_ok

    ok = meta_dom._upload_heavy_via_chooser_event(
        _Page(session), __file__, diag, click, wait_s=0.5
    )
    return ok, diag


# ── ради чего всё ─────────────────────────────────────────────────────

def test_the_file_is_set_by_backend_node_id():
    s = _Session(backend=777)
    ok, diag = _run(s)
    assert ok is True, diag
    # Последней идёт не она, а снятие перехвата из `finally` — и это
    # правильно: снимать надо в любом случае. Ищем по имени.
    sets = [p for m, p in s.sent if m == "DOM.setFileInputFiles"]
    assert sets, "файл не отдавался вовсе"
    params = sets[-1]
    assert params["backendNodeId"] == 777, "поле взято не из события"
    assert params["files"][0].endswith(".py"), "браузеру ушёл не путь"
    assert diag["heavy_backend_node"] == 777


def test_interception_is_armed_before_the_click():
    """Иначе событие уйдёт мимо нас, и ждать его будет некому."""
    s = _Session()
    _run(s)
    methods = [m for m, _ in s.sent]
    assert "Page.setInterceptFileChooserDialog" in methods
    i = methods.index("Page.setInterceptFileChooserDialog")
    assert methods.index("DOM.setFileInputFiles") > i


def test_interception_is_always_disarmed():
    """Оставленный перехват проглотит окно выбора у следующего шага, и тот
    будет ждать его вечно."""
    s = _Session()
    _run(s)
    offs = [p for m, p in s.sent
            if m == "Page.setInterceptFileChooserDialog" and not (p or {}).get("enabled")]
    assert offs, "перехват не снят"
    assert s.detached, "сессия не отцеплена"


def test_it_is_disarmed_even_when_the_event_never_comes():
    s = _Session(emit=False)
    ok, diag = _run(s)
    assert ok is False
    assert "не пришло" in diag["heavy_error"]
    offs = [p for m, p in s.sent
            if m == "Page.setInterceptFileChooserDialog" and not (p or {}).get("enabled")]
    assert offs, "после неудачи перехват остался включённым"
    assert s.detached


def test_a_refusal_to_set_files_is_reported():
    s = _Session(fail_on="DOM.setFileInputFiles")
    ok, diag = _run(s)
    assert ok is False
    assert "setFileInputFiles" in diag["heavy_error"]


def test_a_refusal_to_arm_is_reported():
    s = _Session(fail_on="Page.setInterceptFileChooserDialog")
    ok, diag = _run(s)
    assert ok is False
    assert "intercept" in diag["heavy_error"]
    assert s.detached, "сессия не отцеплена после неудачи перехвата"


def test_the_click_is_recorded():
    s = _Session()
    _, diag = _run(s)
    assert diag["heavy_clicked"] is True


def test_playwright_does_not_intercept_the_same_window():
    """Два перехватчика на одно событие — заведомая драка. Для тяжёлого файла
    нажатие делает наш путь сам, а `expect_file_chooser` в этой ветке не
    участвует."""
    src = (ROOT / "src" / "fb_ads_rpa" / "meta_dom.py").read_text(encoding="utf-8")
    start = src.index('_step["heavy"] = _upload_heavy_via_chooser_event')
    branch = src[start: src.index("else:", start)]
    before_fallback = branch[: branch.index("if not _step[\"heavy\"]")]
    assert "expect_file_chooser" not in before_fallback, (
        "перехватчик Playwright снова открыт одновременно с нашим"
    )
