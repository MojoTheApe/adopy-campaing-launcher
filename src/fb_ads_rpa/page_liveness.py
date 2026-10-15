"""Жива ли вкладка профиля — и что делать, когда её уже нет (ACL-1320).

Живой прогон c00d8c3 (26.09, 15:21–15:57 UTC): профиль Facebook_1443
остановили, пока робот ждал окно цели, и поток публикации крутился в
`_page_responsive` → `wait_for_function` с GIL в руках — дешборд перестал
отвечать (/health по таймауту, py-spy: `active+gil` в `_sync` Playwright).

Замер на настоящем Chromium (connect_over_cdp, браузер убит):

* первые ~45 с соединение ещё «живо» (`is_connected()` True): любой вызов
  страницы висит до своего таймаута, `wait_for_function` честно отдаёт
  TimeoutError — это «вкладка не отвечает», и ждём мы его во сне (ввод-вывод,
  GIL отпущен);
* потом Playwright узнаёт о разрыве, и КАЖДЫЙ вызов падает за ~5 мс с
  TargetClosedError. Проверка живости отвечала на это None («спросить не
  удалось» — «не зависла»), а циклы ожидания без паузы шли на следующий круг:
  тысячи вызовов в секунду, и каждый — это `inspect.stack()` синхронной
  обёртки Playwright, то есть чистый Python под GIL.

Здесь одно место, которое отвечает «вкладки больше нет»: закрыта сама
(`is_closed()`), отвалился браузер (`browser.is_connected()` — локальное
поле, без обращения к драйверу), или вызов уже упал с «закрыто / соединение
оборвано». Вкладка, однажды признанная ушедшей, помечается, и следующие
вопросы о ней не идут к драйверу вовсе.
"""
from __future__ import annotations

#: Тексты исключений Playwright, которые значат «вкладки/браузера/драйвера
#: больше нет». TargetClosedError — по имени класса; остальное — по тексту:
#: «Connection closed» (упал драйвер), «Browser closed», «Event loop is
#: closed! Is Playwright already stopped?».
_GONE_MARKERS = (
    "target page, context or browser has been closed",
    "browser has been closed",
    "context has been closed",
    "page has been closed",
    "target closed",
    "connection closed",
    "browser closed",
    "event loop is closed",
    "playwright already stopped",
    "websocket closed",
)

_MARK = "_acl_gone"


def is_gone_error(exc: BaseException | None) -> bool:
    """Исключение значит «вкладки, браузера или драйвера больше нет»."""
    if exc is None:
        return False
    if type(exc).__name__ == "TargetClosedError":
        return True
    text = str(exc).lower()
    return any(marker in text for marker in _GONE_MARKERS)


def mark_gone(page) -> None:
    """Запомнить, что вкладки больше нет: дальше — без вопросов драйверу."""
    try:
        setattr(page, _MARK, True)
    except Exception:
        pass


def _marked(page) -> bool:
    # Только собственный атрибут: у MagicMock-заглушек любой `getattr`
    # «истинен», и заглушка иначе считалась бы ушедшей.
    try:
        return vars(page).get(_MARK) is True
    except TypeError:
        return False


def tab_gone(page) -> bool:
    """Вкладки больше нет: закрыта, браузер отсоединён или уже падала «закрыто».

    Ничего не спрашивает у страницы: `is_closed()` и `is_connected()` —
    локальные поля Playwright, поэтому ответ мгновенный и на мёртвом
    драйвере. Заглушка без этих методов — «не ушла».
    """
    if page is None:
        return True
    if _marked(page):
        return True
    # ACL-1702: вкладку разблокировал сторож (зависла в одном вызове дольше
    # порога) — для работы кампании её больше нет, итог `tab_hung:<шаг>`.
    # Метку `_acl_gone` не ставим: вкладка могла выжить (terminate/reload), и
    # следующая кампания задания снимет отметку и пойдёт по ней.
    from src.fb_ads_rpa import tab_watchdog

    if tab_watchdog.page_marked_hung(page):
        return True
    try:
        if page.is_closed() is True:
            mark_gone(page)
            return True
    except Exception as exc:
        if is_gone_error(exc):
            mark_gone(page)
            return True
    try:
        browser = page.context.browser
    except Exception:
        browser = None
    if browser is not None:
        try:
            if browser.is_connected() is False:
                mark_gone(page)
                return True
        except Exception as exc:
            if is_gone_error(exc):
                mark_gone(page)
                return True
    return False


def browser_gone(page) -> bool:
    """Браузер вкладки отсоединён (профиль остановлен, драйвер оборвался).

    В отличие от `tab_gone`, не смотрит на саму вкладку: её мы могли закрыть
    сами (`fresh_tab` закрывает зависшую), а профиль при этом жив.
    """
    if page is None:
        return True
    try:
        browser = page.context.browser
    except Exception as exc:
        return is_gone_error(exc)
    if browser is None:
        return False
    try:
        return browser.is_connected() is False
    except Exception as exc:
        return is_gone_error(exc)


def answers(page, timeout_s: float = 5.0) -> bool | None:
    """Отвечает ли вкладка: True / False (не за `timeout_s`) / None (её нет
    или спросить не удалось).

    `page.wait_for_function` — единственный вызов sync Playwright с
    таймаутом, который соблюдает сам драйвер, и ждёт он во сне (ввод-вывод
    отпускает GIL). Ушедшую вкладку не спрашиваем вовсе, а «закрыто» в ответе
    помечает её ушедшей — следующий вопрос не пойдёт к драйверу.
    """
    # ACL-1614: после Stop вкладку не спрашиваем — проверка живости тоже ожидание.
    from src.fb_ads_rpa import job_cancel

    job_cancel.check()
    if tab_gone(page):
        return None
    try:
        page.wait_for_function("() => true", timeout=int(timeout_s * 1000))
        return True
    except Exception as exc:
        if is_gone_error(exc) or tab_gone(page):
            mark_gone(page)
            return None
        if "timeout" in type(exc).__name__.lower() or "Timeout" in str(exc):
            return False
        return None
