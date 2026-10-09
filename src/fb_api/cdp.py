"""Своя вкладка профиля поверх сырого CDP-клиента ACL-1702, без Playwright.

Websocket-клиент не пишем второй раз: берём `RawCdp` из
`src.fb_ads_rpa.tab_watchdog` (стандартная библиотека, одно соединение к
браузеру, свой срок на каждый вызов). Здесь — только то, что нужно Graph-вызовам
изнутри профиля:

- вкладка всегда СВОЯ (`Target.createTarget`, фоновая) и закрывается за собой:
  если робот или человек в этот момент работает в том же профиле, их вкладки
  мы не трогаем;
- подключение к ней — `Target.attachToTarget` с `flatten`, команды идут с
  `sessionId` по тому же соединению;
- последнюю вкладку браузера не закрываем: Chrome закрылся бы целиком, а с
  ним и профиль Multilogin (то же правило, что у сторожа ACL-1702).

События CDP (Network.*) не слушаем вовсе: токен Ads Manager в Python не
попадает (см. `token_capture`).

Троттлинг фона (живой пилот 5265, «CDP read timed out»): вкладка открыта
фоновой в окне, которое может быть свёрнуто, и Chrome душит её таймеры и
рендер. Снимаем так же, как робот (`browser._disable_background_throttling`):
`Emulation.setFocusEmulationEnabled` + `Page.setWebLifecycleState: active` —
окно не показывается и фокус не забирается. Скринкаст робота
(`_keep_renderer_active`) не берём: ему нужен ответ на каждый кадр, а сырой
клиент события не читает.

Каждый вызов CDP, не дождавшийся ответа, — `CdpTimeout` с именем метода и
сроком: по сообщению видно, на каком шаге встали.
"""
from __future__ import annotations

from typing import Any, Callable

from src.fb_ads_rpa.tab_watchdog import CdpError, RawCdp

__all__ = ["CdpError", "CdpTimeout", "PageError", "PageTab"]

#: Срок одного служебного вызова CDP (создать вкладку, подключиться, …).
CALL_S = 10.0


class PageError(CdpError):
    """Скрипт во вкладке бросил исключение или вернул не то."""


class CdpTimeout(CdpError, TimeoutError):
    """Браузер не ответил на вызов CDP в срок; в тексте — метод и срок."""


def _call(cdp: Any, method: str, params: dict | None = None, *, session_id: str = "",
          timeout: float = CALL_S) -> dict:
    try:
        return cdp.call(method, params or {}, session_id=session_id, timeout=timeout)
    except TimeoutError as exc:
        if isinstance(exc, CdpTimeout):
            raise
        raise CdpTimeout(f"{method}: the browser did not answer within {timeout:.0f} s") from exc


class PageTab:
    """Одна своя вкладка в браузере профиля."""

    def __init__(self, cdp: Any, target_id: str, session_id: str):
        self._cdp = cdp
        self.target_id = target_id
        self.session_id = session_id
        self.closed = False
        #: Удалось ли снять троттлинг фона: {"focus": bool, "lifecycle": bool}.
        self.throttle: dict = {}

    @classmethod
    def open(cls, cdp_url: str, *, connect: Callable[..., Any] | None = None,
             call_s: float = CALL_S) -> "PageTab":
        """Подключиться к браузеру профиля и открыть в нём свою пустую вкладку."""
        try:
            cdp = (connect or RawCdp.connect)(cdp_url, timeout=call_s)
        except TimeoutError as exc:
            raise CdpTimeout(f"connect: the browser did not answer within {call_s:.0f} s") from exc
        target_id = ""
        try:
            created = _call(cdp, "Target.createTarget",
                            {"url": "about:blank", "background": True}, timeout=call_s)
            target_id = str(created.get("targetId") or "")
            if not target_id:
                raise CdpError("the browser did not open a tab")
            attached = _call(cdp, "Target.attachToTarget",
                             {"targetId": target_id, "flatten": True}, timeout=call_s)
            session_id = str(attached.get("sessionId") or "")
            if not session_id:
                raise CdpError("could not attach to the new tab")
            tab = cls(cdp, target_id, session_id)
            # Runtime.enable не зовём: Runtime.evaluate работает и без него, а
            # включённый домен Runtime — известный признак автоматизации.
            tab.call("Page.enable")
            tab.throttle = tab.keep_active()
            return tab
        except BaseException:
            if target_id:
                try:
                    cdp.call("Target.closeTarget", {"targetId": target_id}, timeout=call_s)
                except Exception:
                    pass
            try:
                cdp.close()
            except Exception:
                pass
            raise

    # -- вызовы во вкладку
    def call(self, method: str, params: dict | None = None, *, timeout: float = CALL_S) -> dict:
        if self.closed:
            raise CdpError("the tab is closed")
        return _call(self._cdp, method, params, session_id=self.session_id, timeout=timeout)

    def keep_active(self) -> dict:
        """Снять троттлинг фона, не показывая окно (как робот, ACL-1402)."""
        status = {"focus": False, "lifecycle": False}
        try:
            self.call("Emulation.setFocusEmulationEnabled", {"enabled": True})
            status["focus"] = True
        except CdpTimeout:
            raise
        except Exception:
            pass
        try:
            self.call("Page.setWebLifecycleState", {"state": "active"})
            status["lifecycle"] = True
        except CdpTimeout:
            raise
        except Exception:
            pass
        return status

    def add_init_script(self, source: str) -> None:
        """Скрипт, который выполняется в каждом новом документе вкладки ДО скриптов страницы."""
        self.call("Page.addScriptToEvaluateOnNewDocument", {"source": source})

    def navigate(self, url: str, *, timeout: float = 30.0) -> None:
        got = self.call("Page.navigate", {"url": url}, timeout=timeout)
        if got.get("errorText"):
            raise PageError(f"navigation failed: {got['errorText']}")

    def reload(self, *, timeout: float = 30.0) -> None:
        self.call("Page.reload", {"ignoreCache": False}, timeout=timeout)

    def evaluate(self, expression: str, *, timeout: float = 60.0) -> Any:
        """Выполнить JS во вкладке; промис дожидается, значение приходит по значению."""
        got = self.call(
            "Runtime.evaluate",
            {"expression": expression, "awaitPromise": True, "returnByValue": True},
            timeout=timeout + 5.0,
        )
        if got.get("exceptionDetails"):
            details = got["exceptionDetails"]
            text = (details.get("exception") or {}).get("description") or details.get("text") or ""
            # Текст исключения страницы может нести что угодно — в сообщение
            # идёт только первая строка, и вызывающий всё равно её вычищает.
            raise PageError(f"page script failed: {str(text).splitlines()[0][:200] if text else ''}")
        return (got.get("result") or {}).get("value")

    # -- закрытие
    def close(self) -> None:
        """Закрыть СВОЮ вкладку и соединение. Последнюю вкладку браузера не закрываем."""
        if self.closed:
            return
        self.closed = True
        try:
            infos = (self._cdp.call("Target.getTargets", timeout=CALL_S) or {}).get("targetInfos") or []
            pages = [t for t in infos if t.get("type") == "page" and not t.get("subtype")]
            if len(pages) > 1 or not any(t.get("targetId") == self.target_id for t in pages):
                self._cdp.call("Target.closeTarget", {"targetId": self.target_id}, timeout=CALL_S)
            else:
                # Единственная вкладка: уводим её на пустую страницу, браузер живёт.
                self._cdp.call("Page.navigate", {"url": "about:blank"},
                               session_id=self.session_id, timeout=CALL_S)
        except Exception:
            pass
        finally:
            try:
                self._cdp.close()
            except Exception:
                pass
