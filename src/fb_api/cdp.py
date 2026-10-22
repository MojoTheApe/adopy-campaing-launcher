"""Своя вкладка профиля поверх сырого CDP-клиента ACL-1702, без Playwright.

Websocket-клиент не пишем второй раз: берём `RawCdp` из
`src.fb_ads_rpa.tab_watchdog` (стандартная библиотека, одно соединение к
браузеру, свой срок на каждый вызов). Здесь — только то, что нужно Graph-вызовам
изнутри профиля:

- вкладка всегда СВОЯ (`Target.createTarget`, как у Playwright — без
  `background`) и закрывается за собой:
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
import time
import urllib.parse
import urllib.request
from typing import Any, Callable

from src.fb_ads_rpa.tab_watchdog import CdpError, RawCdp

__all__ = ["CdpError", "CdpTimeout", "PageError", "StaleContextError", "PageTab"]

#: Срок одного служебного вызова CDP (создать вкладку, подключиться, …).
CALL_S = 10.0
#: Срок вопроса браузеру об URL вкладки после таймаута (рендерер мог зависнуть).
TARGET_INFO_S = 5.0


def host_and_path(url: str) -> str:
    """`host/path` без query, фрагмента и логина: в текст ошибки токены не попадают."""
    try:
        parts = urllib.parse.urlsplit(url or "")
        return f"{parts.hostname or ''}{parts.path or ''}"
    except ValueError:
        return ""


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
        #: Первая навигация после нашего холодного старта ещё впереди (ACL-2095 п. 5/7).
        self._first_nav_cold = False
        self._sleep: Callable[[float], None] = time.sleep

    @classmethod
    def open(cls, cdp_url: str, *, connect: Callable[..., Any] | None = None,
             call_s: float | None = None) -> "PageTab":
        """Подключиться к браузеру профиля и открыть в нём свою пустую вкладку.

        Холодный старт (TEST9095y, 9403A: `Page.enable` не ответил за 10 с):
        профиль, который мы только что подняли (`launch.mark_started`), на первые
        вызовы — подключение, вкладка, `Page.enable`, снятие троттлинга —
        получает `ACL_CDP_COLD_START_S` (40 с). Дальше у вкладки обычные сроки
        (`CALL_S`), а отметка порта снимается после первой удачной вкладки.
        """
        from src.fb_api import launch as _launch

        since = _launch.cold_since(cdp_url) if call_s is None else None
        cold = since is not None
        if call_s is None:
            call_s = _launch.cold_start_s() if cold else CALL_S
        timings: dict = {}
        step = "connect"
        began = time.monotonic()
        try:
            if cold and connect is None:
                # П. 8a: порт от MLX — ещё не готовый браузер (B97 13:27: порт отдан за
                # 11,6 с, браузер поднялся на 10–30 с позже). Ждём `/json/version`.
                step = "ready"
                ready, took = _launch.wait_ready(cdp_url, budget_s=call_s)
                timings["ready_s"] = took
                if not ready:
                    raise CdpTimeout(f"ready: the browser did not answer /json/version within {call_s:.0f} s")
                step = "connect"
            try:
                cdp = (connect or RawCdp.connect)(cdp_url, timeout=call_s)
            except TimeoutError as exc:
                raise CdpTimeout(f"connect: the browser did not answer within {call_s:.0f} s") from exc
            timings["connect_s"] = time.monotonic() - began
            if cold:
                _launch.mark_ready(cdp_url)
            step = "tab"
            tab = cls._open_on(cdp, cdp_url, call_s, timings, began)
            # П. 5/7: первая навигация после холодного старта — сначала проба прокси.
            tab._first_nav_cold = cold
        except BaseException as exc:
            if cold:
                failed = str(exc).split(":", 1)[0] if isinstance(exc, CdpTimeout) else step
                _launch.record_cold_open(ok=False, since_start_s=since, budget_s=call_s,
                                         timings={**timings, "total_s": time.monotonic() - began},
                                         step=failed)
            raise
        if cold:
            _launch.record_cold_open(ok=True, since_start_s=since, budget_s=call_s,
                                     timings={**timings, "total_s": time.monotonic() - began})
            _launch.forget(cdp_url)
        return tab

    @classmethod
    def _open_on(cls, cdp: Any, cdp_url: str, call_s: float, timings: dict, began: float) -> "PageTab":
        target_id = ""
        try:
            # Ровно как Playwright (`crBrowser`: {url:"about:blank", browserContextId}),
            # без `background`. Проба ревьюера на настоящем Chrome: вкладка с
            # background:true НИКОГДА не отвечает на Page.enable (живой 5414/5265).
            # Свёрнутое окно остаётся свёрнутым и на экран не выходит; наша вкладка
            # лишь становится активной в нём, а при закрытии браузер сам
            # активирует соседнюю.
            created = _call(cdp, "Target.createTarget", {"url": "about:blank"}, timeout=call_s)
            target_id = str(created.get("targetId") or "")
            if not target_id:
                raise CdpError("the browser did not open a tab")
            attached = _call(cdp, "Target.attachToTarget",
                             {"targetId": target_id, "flatten": True}, timeout=call_s)
            session_id = str(attached.get("sessionId") or "")
            if not session_id:
                raise CdpError("could not attach to the new tab")
            tab = cls(cdp, target_id, session_id, cdp_url=cdp_url)
            # Живой пилот 5414: «Page.enable: the browser did not answer». Мы
            # уже на ОДНОМ websocket браузера с flatten-сессией (как Playwright),
            # отдельный websocket страницы не открываем. Но если в браузере кто-то
            # держит Target.setAutoAttach{waitForDebuggerOnStart:true} (агент
            # Multilogin/Mimic, сессия робота), новая цель стартует НА ПАУЗЕ, и
            # рендерер не отвечает на Page.enable, пока её не отпустят. Playwright
            # шлёт Runtime.runIfWaitingForDebugger после подключения — делаем так
            # же, на НАШЕЙ сессии. Если цель не на паузе, команда — пустая. Это не
            # Runtime.enable: домен Runtime не включается.
            try:
                tab.call("Runtime.runIfWaitingForDebugger", timeout=call_s)
            except CdpTimeout:
                raise
            except CdpError:
                pass
            # Runtime.enable не зовём: Runtime.evaluate работает и без него, а
            # включённый домен Runtime — известный признак автоматизации.
            tab.call("Page.enable", timeout=call_s)
            timings["enable_s"] = time.monotonic() - began
            tab.throttle = tab.keep_active(timeout=call_s)
            return tab
        except BaseException:
            if target_id:
                # F4: на пути ошибки тоже не закрываем последнюю вкладку браузера
                # (Chrome и профиль Multilogin ушли бы вместе с ней).
                try:
                    cls._close_unless_last(cdp, target_id, call_s)
                except Exception:
                    pass
            try:
                cdp.close()
            except Exception:
                pass
            raise

    @staticmethod
    def _close_unless_last(cdp: Any, target_id: str, call_s: float) -> None:
        """Закрыть цель, если в браузере есть другие вкладки; единственную не трогаем.

        Список вкладок не получен — тоже не закрываем: лучше оставить
        пустую about:blank, чем уронить браузер.
        """
        infos = (cdp.call("Target.getTargets", timeout=call_s) or {}).get("targetInfos") or []
        pages = [t for t in infos if t.get("type") == "page" and not t.get("subtype")]
        if len(pages) > 1:
            cdp.call("Target.closeTarget", {"targetId": target_id}, timeout=call_s)

    def browser_url(self, timeout: float = TARGET_INFO_S) -> str:
        """URL нашей цели от БРАУЗЕРНОГО процесса (`Target.getTargetInfo`, без sessionId).

        Браузер отвечает, даже когда рендерер вкладки завис. Не ответил — пустая
        строка (вызывающий оставляет исходный `CdpTimeout`).
        """
        try:
            got = self._cdp.call("Target.getTargetInfo", {"targetId": self.target_id}, timeout=timeout)
            return str(((got or {}).get("targetInfo") or {}).get("url") or "")
        except Exception:
            return ""

    # -- вызовы во вкладку
    def call(self, method: str, params: dict | None = None, *, timeout: float = CALL_S) -> dict:
        if self.closed:
            raise CdpError("the tab is closed")
        return _call(self._cdp, method, params, session_id=self.session_id, timeout=timeout)

    def keep_active(self, *, timeout: float = CALL_S) -> dict:
        """Снять троттлинг фона на НАШЕЙ сессии, не показывая окно (как робот, ACL-1402)."""
        status = {"focus": False, "lifecycle": False}
        try:
            self.call("Emulation.setFocusEmulationEnabled", {"enabled": True}, timeout=timeout)
            status["focus"] = True
        except CdpTimeout:
            raise
        except Exception:
            pass
        try:
            self.call("Page.setWebLifecycleState", {"state": "active"}, timeout=timeout)
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
        try:
            got = self.call(
                "Runtime.evaluate",
                {"expression": expression, "awaitPromise": True, "returnByValue": True,
                 "contextId": context_id},
                timeout=timeout + 5.0,
            )
        except CdpTimeout:
            raise
        except CdpError as exc:
            # Отдельный мир исчез (документ сменился) — отдельная ошибка, чтобы
            # ТОЛЬКО не-Meta вызовы (state/probe) можно было повторить в новом
            # мире; Graph-вызов не повторяется никогда (ревью r9-1).
            if _looks_stale(str(exc)):
                raise StaleContextError(str(exc)) from exc
            raise
        if got.get("exceptionDetails"):
            details = got["exceptionDetails"]
            text = (details.get("exception") or {}).get("description") or details.get("text") or ""
            raise PageError(f"page script failed: {str(text).splitlines()[0][:200] if text else ''}")
        return (got.get("result") or {}).get("value")

    def navigate(self, url: str, *, timeout: float = 30.0) -> None:
        if self._first_nav_cold:
            self._first_nav_cold = False
            self._navigate_after_cold_start(url, timeout)
            return
        self._navigate_once(url, timeout)

    def _navigate_once(self, url: str, timeout: float) -> None:
        got = self.call("Page.navigate", {"url": url}, timeout=timeout)
        if got.get("errorText"):
            raise PageError(f"navigation failed: {got['errorText']}")

    def _navigate_after_cold_start(self, url: str, timeout: float) -> None:
        """Первая навигация после нашего старта (ACL-2095 п. 5/7).

        MLX включает вход в прокси через несколько секунд после готовности порта
        (B97 22.10: api.ipify.org → ERR_INVALID_AUTH_CREDENTIALS, та же вкладка через
        15 с — загрузилась). Поэтому сначала нейтральная проба (`ACL_PROXY_PROBE_URL`),
        на ошибку входа — один повтор через `PROXY_RETRY_S`; только потом Facebook.
        Второй отказ входа — окончательный. Проба упала иначе (сеть, DNS) — не держим.
        """
        from src.fb_api import launch as _launch

        began = time.monotonic()
        probe = _launch.proxy_probe_url()
        retried = False
        if probe:
            try:
                self._navigate_once(probe, min(timeout, 30.0))
            except PageError as exc:
                if not _launch.is_proxy_auth_error(str(exc)):
                    probe = ""            # проба не про вход в прокси — не решает
                else:
                    retried = True
                    self._sleep(_launch.PROXY_RETRY_S)
                    try:
                        self._navigate_once(probe, min(timeout, 30.0))
                    except PageError as again:
                        _launch._record({"event": "fb_api_proxy_probe", "ok": False, "retried": True,
                                         "took_s": round(time.monotonic() - began, 1)})
                        if _launch.is_proxy_auth_error(str(again)):
                            raise
                        probe = ""
        if probe:
            _launch._record({"event": "fb_api_proxy_probe", "ok": True, "retried": retried,
                             "took_s": round(time.monotonic() - began, 1)})
        try:
            self._navigate_once(url, timeout)
        except PageError as exc:
            # Пробы не было (выключена/не решила) — тот же один повтор на самой первой навигации.
            if retried or not _launch.is_proxy_auth_error(str(exc)) or probe:
                raise
            self._sleep(_launch.PROXY_RETRY_S)
            self._navigate_once(url, timeout)

    def reload(self, *, timeout: float = 30.0) -> None:
        self.call("Page.reload", {"ignoreCache": False}, timeout=timeout)

    def close_target(self, target_id: str) -> bool:
        """Закрыть ЧУЖУЮ нашу прежнюю вкладку (осталась от прошлого запуска дешборда).

        Только если в браузере есть другие вкладки (наша живая — одна из них);
        единственную не закрываем. Любая ошибка — False, ничего не ломаем.
        """
        if self.closed or not target_id or target_id == self.target_id:
            return False
        try:
            infos = (self._cdp.call("Target.getTargets", timeout=CALL_S) or {}).get("targetInfos") or []
            pages = [t for t in infos if t.get("type") == "page" and not t.get("subtype")]
            if len(pages) < 2 or not any(t.get("targetId") == target_id for t in pages):
                return False
            self._cdp.call("Target.closeTarget", {"targetId": target_id}, timeout=CALL_S)
            return True
        except Exception:
            return False

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
        # Без системного прокси: порт отладки — это 127.0.0.1 профиля.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        req = urllib.request.Request(f"http://{host}:{port}{path}")
        with opener.open(req, timeout=timeout) as resp:
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
