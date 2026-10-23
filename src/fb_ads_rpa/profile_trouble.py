"""Что не так с профилем — прежде чем его перезапускать (ACL-1320).

Живой прогон c00d8c3 (26.09, 15:21–15:57 UTC, новые аккаунты): редирект
Facebook на страницу входа (`business.facebook.com/business/loginpage`) и
мёртвый прокси профиля (`ERR_SOCKS_CONNECTION_FAILED`,
`ERR_SSL_PROTOCOL_ERROR`) робот принимал за «вкладка зависла» и раз за разом
останавливал и поднимал профиль через MLX API. Одна такая остановка кончилась
`connect ECONNREFUSED`, и профиль остался закрытым.

Перезапуск не лечит ни то, ни другое: разлогиненный аккаунт после
перезапуска так же разлогинен, мёртвый прокси так же мёртв, а человек видит
не причину, а закрытое окно. Поэтому сперва — диагноз:

* **страница входа Facebook** (адрес `*.facebook.com/…/login…` или видимая
  форма входа) — `profile_logged_out`: человеку войти в профиль;
* **сеть профиля** (`net::ERR_SOCKS_*`, `ERR_PROXY_*`, `ERR_TUNNEL_*`,
  `ERR_CONNECTION_*`, `ERR_SSL_PROTOCOL_ERROR` и родственные — страница
  ошибки Chrome, упавший переход, недавняя ошибка сети этого профиля) —
  `profile_network`: человеку проверить прокси;
* только **настоящий зависший рендерер на обычной странице Ads Manager**
  получает свежую вкладку или перезапуск профиля.

Остановки этого модуля — на весь аккаунт: следующая кампания того же
задания упрётся в то же самое, поэтому публикатор её уже не начинает
(`ProfileStop`, `ACCOUNT_STOPS`).
"""
from __future__ import annotations

import re
import time
from urllib.parse import urlsplit

LOGGED_OUT = "profile_logged_out"
NETWORK = "profile_network"
RESTART_FAILED = "profile_restart_failed"
CDP_REFUSED = "profile_cdp_refused"
OPEN_BY_HAND = "profile_open_by_hand"
SIMPLE_CREATE = "simple_create_layout"
LEAD_TERMS = "lead_terms_not_accepted"
WRONG_AD_ACCOUNT = "wrong_ad_account"
MLX_NOT_RUNNING = "multilogin_not_running"

#: Что сказать человеку — английский, это видно в дешборде.
MESSAGES = {
    LOGGED_OUT: "Logged out of Facebook - the profile window is brought to the front: log in in it, "
                "then press Publish again",
    NETWORK: "The profile's proxy/network is not working — check the proxy in Multilogin",
    RESTART_FAILED: "Restarting the Multilogin profile failed, so it may be closed now — "
                    "open it in Multilogin, check that it loads Ads Manager, close it, "
                    "then publish again",
    # ACL-1403: MLX сообщил порт автоматизации, но на нём никто не слушает
    # (профиль упал при старте или порт устарел), и ожидание порта не помогло.
    CDP_REFUSED: "The Multilogin profile started but its browser did not accept the connection - "
                 "open the profile in Multilogin, check that it loads, close it, then press Publish again",
    # ACL-1320: профиль открыт руками (без порта автоматизации). Остановка
    # через MLX API могла потерять только что созданную сессию Facebook,
    # если Multilogin её ещё не сохранил, — робот такой профиль не трогает.
    OPEN_BY_HAND: "This Multilogin profile is open by hand. Close its window "
                  "(so Multilogin saves the session), then press Publish again.",
    # ACL-1320, решение координатора 26.09: новый одно-экранный редактор Meta
    # включается на весь кабинет — следующая кампания того же аккаунта упрётся
    # в него же и оставит ещё один недособранный черновик. Останавливаем аккаунт.
    # ACL-1327: робот этот редактор проходит; остановка осталась для его
    # разметки, которой робот не знает (нет раздела группы / объявления), и
    # для отката FB_RPA_SIMPLE_CREATE=0.
    SIMPLE_CREATE: "Meta opened its new single-screen campaign editor for this account, and the "
                   "robot could not find its Ad set / Ad sections - check the draft in Ads Manager",
    # ACL-1327: условия Lead Ads страницы не приняты («Please read and accept
    # Meta's Lead Ads Terms…») — принимает человек в Meta, робот «Accept» не
    # жмёт. Страница у кампаний аккаунта одна — следующая упрётся в то же.
    LEAD_TERMS: "Lead Generation terms are not accepted on the Page (Meta's Lead Ads Terms) - "
                "open the Page's lead ads terms in Meta, accept them, then publish again",
    # ACL-1697: Ads Manager открыл не тот рекламный кабинет, что в черновике.
    WRONG_AD_ACCOUNT: "This profile's Facebook user has no access to that ad account, or Meta opened "
                      "another one. Check the ad account id on the Accounts page and that the "
                      "account is visible in this profile",
    # ACL-1401: локальный лаунчер Multilogin (launcher.mlx.yt:45001) отказал
    # в соединении — приложение Multilogin на компьютере не запущено. Ни один
    # профиль этого аккаунта (и любого другого) не поднимется, пока его не
    # запустят; повторять запуск и уходить в простой браузер нельзя.
    MLX_NOT_RUNNING: "Multilogin is not running on this computer - start the Multilogin app, "
                     "then press Publish again",
}

#: Остановки, после которых остальные кампании задания этого аккаунта не
#: начинаются: причина — в профиле, а не в кампании.
ACCOUNT_STOPS = frozenset(MESSAGES)

#: Сколько секунд ошибка сети профиля считается «недавней» — достаточно,
#: чтобы связать её с зависшей следом вкладкой (26.09: ERR_SOCKS в 15:34:45,
#: «вкладка не отвечает» и перезапуск в 15:37:39).
NET_ERROR_RECENT_S = 300.0

#: Ревью PR #323 (F3): сколько неразрешённых ошибок сети (после последнего
#: удачного перехода) нужно, чтобы по одной истории назвать сеть профиля.
#: Одна ошибка, за которой последовал удачный повтор (20.09 S15), — не сеть.
NET_ERRORS_FOR_HISTORY = 2

_NET_CODE = re.compile(
    r"\b(ERR_(?:SOCKS_[A-Z_]+|PROXY_[A-Z_]+|TUNNEL_[A-Z_]+|CONNECTION_[A-Z_]+"
    r"|SSL_PROTOCOL_ERROR|TIMED_OUT|NAME_NOT_RESOLVED|INTERNET_DISCONNECTED"
    r"|EMPTY_RESPONSE|ADDRESS_UNREACHABLE))\b")

#: Путь страницы входа: `/login`, `/login.php`, `/login/`, `/loginpage`,
#: `/business/loginpage/` — отдельным сегментом, не частью слова.
_LOGIN_PATH = re.compile(r"(^|/)(login(page)?(\.php)?)(/|$)", re.I)

#: Адрес страницы входа внутри текста исключения: «Navigation to … is
#: interrupted by another navigation to https://business.facebook.com/business/loginpage/?next=…»
_LOGIN_IN_TEXT = re.compile(r"https?://[a-z0-9.-]*facebook\.com/[^\s\"']*", re.I)

_ATTR = "_acl_net_errors"
_OK_ATTR = "_acl_nav_ok"


class ProfileStop(RuntimeError):
    """Остановка на весь аккаунт: профиль разлогинен, без сети, открыт руками
    или не поднялся после перезапуска. `code` — имя отказа для политик."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = (detail or "")[:200]
        text = f"{code}: {MESSAGES.get(code, code)}"
        if self.detail:
            text += f" ({self.detail})"
        super().__init__(text)


def message(code: str) -> str:
    return MESSAGES.get(code, "")


def login_url(url: str) -> bool:
    """Адрес — страница входа Facebook (любой поддомен facebook.com)."""
    if not isinstance(url, str) or not url:
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    if not (host == "facebook.com" or host.endswith(".facebook.com")):
        return False
    return bool(_LOGIN_PATH.search(parts.path or ""))


def net_code(text: str) -> str:
    """Код ошибки сети профиля в тексте (исключения, страницы ошибки) или ""."""
    found = _NET_CODE.search(text or "")
    return found.group(1) if found else ""


def login_in_text(text: str) -> str:
    """Адрес страницы входа Facebook внутри текста исключения, или ""."""
    for url in _LOGIN_IN_TEXT.findall(text or ""):
        if login_url(url):
            return url
    return ""


#: Локальный лаунчер Multilogin: `launcher.mlx.yt` указывает на 127.0.0.1,
#: порт 45001 (`MULTILOGIN_LAUNCHER_*`, multilogin_config).
_LAUNCHER = re.compile(r"launcher\.mlx\.yt|(?:port=|:)45001\b", re.I)


def launcher_unreachable(exc: BaseException | None) -> str:
    """Лаунчер Multilogin не принял соединение (ACL-1401) — деталь или "".

    Ищет по цепочке причин (`__cause__` / `__context__`) ошибку соединения
    `requests` (отказ, таймаут соединения) к локальному лаунчеру. Таймаут
    чтения — не она: лаунчер ответил на соединение и просто долго поднимает
    профиль. Обёртки (`RuntimeError(f"{exc} (MLX attach-only…)")`) не мешают:
    смотрим всю цепочку, а не только верхнее исключение.
    """
    try:
        import requests
    except Exception:  # pragma: no cover - requests есть всегда
        return ""
    seen: set[int] = set()
    cur = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if (isinstance(cur, requests.exceptions.ConnectionError)
                and not isinstance(cur, requests.exceptions.ReadTimeout)):
            url = str(getattr(getattr(cur, "request", None), "url", "") or "")
            if _LAUNCHER.search(url) or _LAUNCHER.search(str(cur)):
                kind = ("connect_timeout"
                        if isinstance(cur, requests.exceptions.ConnectTimeout)
                        else "connection_refused")
                return f"{kind}: launcher.mlx.yt:45001"
        cur = cur.__cause__ or cur.__context__
    return ""


def classify_error(exc: BaseException | str | None) -> tuple[str, str]:
    """Что значит упавший переход: (LOGGED_OUT, адрес) / (NETWORK, код) / ("", "")."""
    text = str(exc or "")
    url = login_in_text(text)
    if url:
        return LOGGED_OUT, _short_url(url)
    code = net_code(text)
    if code:
        return NETWORK, code
    return "", ""


def _short_url(url: str) -> str:
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    return f"{parts.hostname or ''}{parts.path or ''}"[:120]


def _holder(page):
    try:
        return page.context
    except Exception:
        return page


def note_net_error(page, code: str) -> None:
    """Запомнить ошибку сети профиля (на его контексте — свежие вкладки того
    же профиля её видят)."""
    if not code or page is None:
        return
    holder = _holder(page)
    try:
        seen = list(vars(holder).get(_ATTR) or [])
    except TypeError:
        return
    seen.append((time.monotonic(), code))
    try:
        setattr(holder, _ATTR, seen[-20:])
    except Exception:
        pass


def note_nav_ok(page) -> None:
    """Удачный переход профиля: ошибки сети до него больше не в счёт
    (ревью PR #323, F3 — повтор, который прошёл, «лечит» разовый разрыв)."""
    if page is None:
        return
    try:
        setattr(_holder(page), _OK_ATTR, time.monotonic())
    except Exception:
        pass


def recent_net_errors(page, within_s: float = NET_ERROR_RECENT_S) -> list[str]:
    """Коды неразрешённых ошибок сети этого профиля: за последние `within_s`
    секунд и позже последнего удачного перехода (`note_nav_ok`)."""
    if page is None:
        return []
    try:
        state = vars(_holder(page))
    except TypeError:
        return []
    seen = state.get(_ATTR) or []
    ok_at = state.get(_OK_ATTR)
    ok_at = ok_at if isinstance(ok_at, (int, float)) else float("-inf")
    now = time.monotonic()
    return [code for ts, code in seen if now - ts <= within_s and ts > ok_at]


def _meta_page(url: str) -> bool:
    """Обычная страница Meta (Ads Manager / Business) — не вход, не ошибка."""
    if not isinstance(url, str) or not url.startswith("https://"):
        return False
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    return (host == "facebook.com" or host.endswith(".facebook.com")) and not login_url(url)


#: Разметка вкладки: форма входа Facebook или страница ошибки Chrome с кодом.
_PAGE_JS = r"""() => {
  const err = document.querySelector('#main-frame-error');
  let code = '';
  if (err) {
    const el = err.querySelector('.error-code');
    code = ((el ? el.textContent : err.textContent) || '').trim();
  }
  const visible = (e) => { if (!e) return false; const r = e.getBoundingClientRect();
    return r.width > 0 && r.height > 0; };
  const pass = document.querySelector('input[name="pass"], input[type="password"]');
  const email = document.querySelector('input[name="email"], input[type="email"], input[name="login"]');
  const login = visible(pass) && visible(email);
  return { v: { code: code.slice(0, 80), login: login } };
}"""


def _read_page(page, timeout_s: float) -> dict:
    """Ограниченное чтение разметки: `wait_for_function` (таймаут соблюдает
    драйвер). Не ответила — пусто."""
    try:
        handle = page.wait_for_function(_PAGE_JS, timeout=int(timeout_s * 1000))
    except Exception:
        return {}
    try:
        got = handle.json_value()
    except Exception:
        got = None
    finally:
        try:
            handle.dispose()
        except Exception:
            pass
    if not isinstance(got, dict):
        return {}
    return got.get("v") or {}


def classify_page(page, *, history: bool = True, read_dom: bool = True,
                  timeout_s: float = 3.0) -> tuple[str, str]:
    """Диагноз вкладки перед свежей вкладкой/перезапуском: (код, подробность).

    По порядку, от дешёвого к дорогому:
    1. адрес (`page.url` — локальное поле, отвечает и зависшая вкладка):
       страница входа Facebook → LOGGED_OUT; `chrome-error://` → NETWORK;
    2. `read_dom`: ограниченное чтение разметки — видимая форма входа →
       LOGGED_OUT, страница ошибки Chrome с кодом сети → NETWORK;
    3. `history` — только вспомогательный признак (ревью PR #323, F3): не
       меньше `NET_ERRORS_FOR_HISTORY` ошибок сети после последнего удачного
       перехода, и только если на вкладке сейчас НЕ обычная страница Meta.
       Живая страница Ads Manager по одной истории сетью не называется.

    ("", "") — ни то ни другое: вкладка просто не отвечает.
    """
    try:
        url = page.url
    except Exception:
        url = ""
    if not isinstance(url, str):
        url = ""
    if login_url(url):
        return LOGGED_OUT, _short_url(url)
    if url.startswith("chrome-error://"):
        recent = recent_net_errors(page)
        return NETWORK, (recent[-1] if recent else "chrome_error_page")
    if read_dom:
        seen = _read_page(page, timeout_s)
        if seen.get("login"):
            return LOGGED_OUT, "login_form"
        code = net_code(seen.get("code") or "")
        if code:
            note_net_error(page, code)
            return NETWORK, code
    if history and not _meta_page(url):
        recent = recent_net_errors(page)
        if len(recent) >= NET_ERRORS_FOR_HISTORY:
            return NETWORK, recent[-1]
    return "", ""
