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

Помощник живёт в ОТДЕЛЬНОМ мире вкладки (`Page.createIsolatedWorld` +
`Runtime.evaluate` с `contextId`): скрипты страницы его не видят, в главном
мире не остаётся ни одного свойства, и мы НИЧЕГО не оборачиваем на странице
(ревью r8: обёртки `fetch`/XHR убраны вовсе). Поэтому и `Network.*` не слушаем:
токен Ads Manager в Python не попадает (см. `token_capture`).

Троттлинг фона (живой пилот 5265, «CDP read timed out»): вкладка открыта
фоновой в окне, которое может быть свёрнуто, и Chrome душит её таймеры и
рендер. Снимаем так же, как робот (`browser._disable_background_throttling`):
`Emulation.setFocusEmulationEnabled` + `Page.setWebLifecycleState: active` —
на НАШЕЙ сессии, окно не показывается и фокус не забирается. Скринкаст робота
(`_keep_renderer_active`) не берём: ему нужен ответ на каждый кадр, а сырой
клиент события не читает.

Каждый вызов CDP, не дождавшийся ответа, — `CdpTimeout` с именем метода и
сроком: по сообщению видно, на каком шаге встали.
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Any, Callable

from src.fb_ads_rpa.tab_watchdog import CdpError, RawCdp

__all__ = ["CdpError", "CdpTimeout", "PageError", "StaleContextError", "PageTab"]

#: Срок одного служебного вызова CDP (создать вкладку, подключиться, …).
CALL_S = 10.0


class PageError(CdpError):
    """Скрипт во вкладке бросил исключение или вернул не то."""


class CdpTimeout(CdpError, TimeoutError):
    """Браузер не ответил на вызов CDP в срок; в тексте — метод и срок."""


class StaleContextError(CdpError):
    """Отдельный мир исчез (документ сменился): создать заново и повторить."""


def _call(cdp: Any, method: str, params: dict | None = None, *, session_id: str = "",
          timeout: float = CALL_S) -> dict:
    try:
        return cdp.call(method, params or {}, session_id=session_id, timeout=timeout)
    except TimeoutError as exc:
        if isinstance(exc, CdpTimeout):
            raise
        raise CdpTimeout(f"{method}: the browser did not answer within {timeout:.0f} s") from exc


def _looks_stale(message: str) -> bool:
    low = (message or "").lower()
    return ("cannot find context" in low or "context with specified id" in low
            or "execution context was destroyed" in low or "no execution context" in low
            or "unique context id" in low)


class PageTab:
    """Одна своя вкладка в браузере профиля."""

    def __init__(self, cdp: Any, target_id: str, session_id: str, *, cdp_url: str = ""):
        self._cdp = cdp
        self._cdp_url = cdp_url
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
            tab = cls(cdp, target_id, session_id, cdp_url=cdp_url)
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
        """Снять троттлинг фона на НАШЕЙ сессии, не показывая окно (как робот, ACL-1402)."""
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

    def main_frame_id(self) -> str:
        tree = self.call("Page.getFrameTree").get("frameTree") or {}
        return str((tree.get("frame") or {}).get("id") or "")

    def create_isolated_world(self, world_name: str) -> int:
        """Отдельный мир верхнего фрейма: свой контекст JS, общий DOM/timeline.

        Страница его не видит, глобалей в главном мире не появляется.
        """
        frame_id = self.main_frame_id()
        if not frame_id:
            raise CdpError("the tab has no main frame yet")
        got = self.call("Page.createIsolatedWorld",
                        {"frameId": frame_id, "worldName": world_name,
                         "grantUniveralAccess": False})
        ctx = got.get("executionContextId")
        if not isinstance(ctx, int):
            raise CdpError("the browser did not return an isolated world context")
        return ctx

    def evaluate_in(self, context_id: int, expression: str, *, timeout: float = 60.0) -> Any:
        """Выполнить JS в отдельном мире (`contextId`); промис дожидается."""
        got = self.call(
            "Runtime.evaluate",
            {"expression": expression, "awaitPromise": True, "returnByValue": True,
             "contextId": context_id},
            timeout=timeout + 5.0,
        )
        if got.get("exceptionDetails"):
            details = got["exceptionDetails"]
            text = (details.get("exception") or {}).get("description") or details.get("text") or ""
            raise PageError(f"page script failed: {str(text).splitlines()[0][:200] if text else ''}")
        return (got.get("result") or {}).get("value")

    def navigate(self, url: str, *, timeout: float = 30.0) -> None:
        got = self.call("Page.navigate", {"url": url}, timeout=timeout)
        if got.get("errorText"):
            raise PageError(f"navigation failed: {got['errorText']}")

    def reload(self, *, timeout: float = 30.0) -> None:
        self.call("Page.reload", {"ignoreCache": False}, timeout=timeout)

    # -- закрытие
    def close(self) -> None:
        """Закрыть СВОЮ вкладку. Сокет не ответил — через HTTP отладочного порта.

        Последнюю вкладку браузера не закрываем (Chrome и профиль ушли бы).
        Ctrl-C во время закрытия не отменяет попытку: ловим `KeyboardInterrupt`,
        пробуем HTTP-путь и только потом поднимаем прерывание.
        """
        if self.closed:
            return
        self.closed = True
        try:
            ok = self._close_over_socket()
        except KeyboardInterrupt:
            try:
                self._close_over_http()
            finally:
                self._safe_cdp_close()
            raise
        except Exception:
            ok = False
        if not ok:
            try:
                self._close_over_http()
            except Exception:
                pass
        self._safe_cdp_close()

    def _close_over_socket(self) -> bool:
        """True — наша вкладка закрыта или (единственная) уведена на about:blank."""
        infos = (self._cdp.call("Target.getTargets", timeout=CALL_S) or {}).get("targetInfos") or []
        pages = [t for t in infos if t.get("type") == "page" and not t.get("subtype")]
        if len(pages) > 1 or not any(t.get("targetId") == self.target_id for t in pages):
            self._cdp.call("Target.closeTarget", {"targetId": self.target_id}, timeout=CALL_S)
        else:
            # Единственная вкладка: уводим её на пустую страницу, браузер живёт.
            self._cdp.call("Page.navigate", {"url": "about:blank"},
                           session_id=self.session_id, timeout=CALL_S)
        return True

    def _close_over_http(self) -> None:
        """Запасной путь: `/json/list` + `/json/close/<id>` на порту отладки.

        Только наша цель и только если вкладок больше одной — последнюю не трогаем.
        """
        host, port = self._http_host_port()
        if not port:
            return
        targets = self._http(host, port, "/json/list")
        if not isinstance(targets, list):
            return
        pages = [t for t in targets if t.get("type") == "page"]
        mine = [t for t in pages if t.get("id") == self.target_id]
        if mine and len(pages) > 1:
            self._http(host, port, f"/json/close/{self.target_id}")

    def _http_host_port(self) -> tuple[str, int]:
        url = self._cdp_url if "://" in (self._cdp_url or "") else f"http://{self._cdp_url}"
        parts = urllib.parse.urlsplit(url)
        return parts.hostname or "127.0.0.1", int(parts.port or 0)

    @staticmethod
    def _http(host: str, port: int, path: str, *, timeout: float = CALL_S) -> Any:
        req = urllib.request.Request(f"http://{host}:{port}{path}")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
        try:
            return json.loads(body)
        except ValueError:
            return body

    def _safe_cdp_close(self) -> None:
        try:
            self._cdp.close()
        except Exception:
            pass
