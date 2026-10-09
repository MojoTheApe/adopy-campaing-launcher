"""Токен Ads Manager остаётся во вкладке (решение владельца, ACL-1756).

Как это работает
----------------
1. До загрузки Ads Manager во вкладку ставится помощник
   (`Page.addScriptToEvaluateOnNewDocument`, только верхний документ). Он
   оборачивает `fetch` и `XMLHttpRequest` страницы и смотрит на СОБСТВЕННЫЕ
   запросы Ads Manager к `graph.facebook.com`: первый же из них несёт
   `access_token` — в адресе или в теле (строка, `URLSearchParams`,
   `FormData`). Токен, хост и версия API ложатся в замыкание помощника.
   Как только токен взят, обёртки снимаются — страница дальше работает со
   своими родными `fetch`/XHR.
2. Запасные пути, если за отведённое время запроса с токеном не было — тоже
   внутри страницы: адреса из `performance` (Resource Timing) и встроенные
   скрипты HTML (`"accessToken":"EAA…"`).
3. Python шлёт помощнику только описание запроса `{method, path, params}`.
   Помощник сам добавляет токен, зовёт `fetch()` к Graph API с адреса и
   отпечатка профиля и возвращает JSON ответа (токены из него вырезаны — в
   `paging.next` Graph кладёт `access_token=`) и выбранные заголовки лимитов.

Токен НИКОГДА не возвращается в Python: ни значением, ни в исключении, ни в
логе. Python видит только `has_token: true/false` и откуда он взят.
Помощник принимает только GET, без `method`/`_method`/`batch`/
`suppress_http_code`/`access_token` в параметрах, и не идёт по
перенаправлениям: этот срез только читает. Python проверяет то же до вызова.

Переснять токен (ошибка 190) — перезагрузить вкладку: новый документ, новое
замыкание, старого токена нет нигде (`InPageGraph.reacquire`).
"""
from __future__ import annotations

import json
import re
import secrets
import time
from typing import Any, Callable

from src.fb_api.cdp import CdpError, PageError
from src.fb_api.telemetry import strip_tokens

ADS_MANAGER_URL = "https://adsmanager.facebook.com/adsmanager/manage/campaigns?act={act}"

#: Ключи, которыми Graph подменяет глагол или пакует запросы; токен — только у
#: страницы. Тот же список стоит в помощнике (`FORBIDDEN`).
FORBIDDEN_PARAMS = frozenset({"method", "_method", "batch", "suppress_http_code", "access_token"})
#: Имя параметра — только ASCII-буквы, цифры и `_` (тот же `KEY_RE` в помощнике):
#: пробелы, управляющие символы, `[]`, полноширинные буквы не проходят, и
#: сервер не нормализует их в запретное имя.
PARAM_KEY_RE = re.compile(r"[A-Za-z0-9_]+", re.ASCII)

#: Помощник страницы. `__NAME__` заменяется на имя свойства window (со
#: случайным хвостом). Никаких обращений наружу, кроме `fetch` к Graph API по
#: явному вызову `call()`.
_HELPER_JS = r"""
(() => {
  const NAME = __NAME__;
  if (window !== window.top) return;
  if (Object.prototype.hasOwnProperty.call(window, NAME)) return;

  const TOKEN_RE = /^EAA[A-Za-z0-9]{20,}$/;
  const TOKEN_ANY = /EAA[A-Za-z0-9]{20,}/g;
  const GRAPH_RE = /^https:\/\/([a-z0-9-]*graph\.facebook\.com)\/(v\d+\.\d+)\//i;
  const PARAM_RE = /(access_token(?:=|%3D|\\u003d))[^&"'\s\\]+/gi;
  const HEADERS = ['x-business-use-case-usage', 'x-ad-account-usage', 'x-app-usage',
                   'x-fb-ads-insights-throttle'];

  const realFetch = window.fetch ? window.fetch.bind(window) : null;
  const origFetch = window.fetch;
  const XHR = window.XMLHttpRequest && window.XMLHttpRequest.prototype;
  const origOpen = XHR && XHR.open;
  const origSend = XHR && XHR.send;
  const xhrUrl = new WeakMap();

  let token = null;
  let source = '';
  let host = 'graph.facebook.com';
  let version = '';
  let retired = false;
  // Параметры, которыми Graph позволяет подменить глагол запроса или
  // упаковать несколько запросов в один (`method=post`, `batch`, …): срез
  // только читает, такие ключи отказываются целиком.
  const FORBIDDEN = ['method', '_method', 'batch', 'suppress_http_code', 'access_token'];
  const KEY_RE = /^[A-Za-z0-9_]+$/;
  let hooked = false;

  function take(url, body) {
    if (token || retired) return;
    let u;
    try { u = new URL(String(url), location.href); } catch (e) { return; }
    const m = GRAPH_RE.exec(u.href);
    if (!m) return;
    if (!version) { host = m[1].toLowerCase(); version = m[2]; }
    let t = u.searchParams.get('access_token');
    if (!t && body) {
      try {
        if (typeof body === 'string') t = new URLSearchParams(body).get('access_token');
        else if (body instanceof URLSearchParams || body instanceof FormData) t = body.get('access_token');
      } catch (e) { t = null; }
    }
    if (t && TOKEN_RE.test(String(t))) {
      token = String(t); source = 'network'; host = m[1].toLowerCase(); version = m[2];
      unhook();
    }
  }

  // Дешёвая маскировка обёрток (ревью O1): свой toString отдаёт текст
  // родной функции, `length` и `name` — как у родной. Остаётся видно:
  // `Function.prototype.toString.call(обёртка)` и наличие собственного
  // свойства `toString`; окно — только до захвата токена.
  function disguise(wrapper, original) {
    const props = {
      toString: { value: function toString() { return Function.prototype.toString.call(original); },
                  configurable: true, writable: true, enumerable: false },
      length: { value: original.length, configurable: true },
      name: { value: original.name, configurable: true },
    };
    for (const k of Object.keys(props)) {
      try { Object.defineProperty(wrapper, k, props[k]); } catch (e) {}
    }
    return wrapper;
  }

  function hook() {
    if (origFetch) {
      window.fetch = disguise(function (input, init) {
        try { take(typeof input === 'string' ? input : (input && input.url), init && init.body); } catch (e) {}
        return origFetch.apply(this, arguments);
      }, origFetch);
    }
    if (XHR) {
      XHR.open = disguise(function (method, url) {
        try { xhrUrl.set(this, String(url)); } catch (e) {}
        return origOpen.apply(this, arguments);
      }, origOpen);
      XHR.send = disguise(function (body) {
        try { const u = xhrUrl.get(this); if (u) take(u, body); } catch (e) {}
        return origSend.apply(this, arguments);
      }, origSend);
    }
    hooked = true;
  }

  function unhook() {
    if (!hooked) return;
    hooked = false;
    try { if (origFetch) window.fetch = origFetch; } catch (e) {}
    try { if (XHR) { XHR.open = origOpen; XHR.send = origSend; } } catch (e) {}
  }

  function fallbacks() {
    if (token || retired) return;
    try {
      for (const e of performance.getEntriesByType('resource')) {
        take(e.name, null);
        if (token) { source = 'resource_timing'; return; }
      }
    } catch (e) {}
    try {
      for (const s of document.scripts) {
        if (s.src) continue;
        const text = s.textContent || '';
        const m = /"accessToken"\s*:\s*"(EAA[A-Za-z0-9]{20,})"/.exec(text) || /(EAA[A-Za-z0-9]{20,})/.exec(text);
        if (m && TOKEN_RE.test(m[1])) { token = m[1]; source = 'html'; unhook(); return; }
      }
    } catch (e) {}
  }

  function state() {
    const h = location.hostname || '';
    const p = location.pathname || '';
    let kind = 'other';
    if (/\/checkpoint(\/|$)/i.test(p) || /\/(two_step_verification|two_factor)(\/|$)/i.test(p)) kind = 'checkpoint';
    else if (/\/login(\.php)?(\/|$)/i.test(p) || /^\/recover(\/|$)/i.test(p)
             || !!document.querySelector('input[name="pass"]')) kind = 'login';
    else if (/(^|\.)adsmanager\.facebook\.com$/i.test(h) || p.indexOf('/adsmanager') === 0) kind = 'ads_manager';
    return { kind: kind, host: h, path: p.slice(0, 120), ready_state: document.readyState,
             has_token: !!token, source: source, version: version, retired: retired };
  }

  function clean(text) {
    let out = String(text);
    if (token) out = out.split(token).join('<token>');
    return out.replace(PARAM_RE, '$1<hidden>').replace(TOKEN_ANY, '<token>');
  }

  async function call(spec) {
    if (retired) return { error: 'retired' };
    if (!spec || spec.method !== 'GET') return { error: 'read_only' };
    const path = String(spec.path || '');
    if (!/^\/[A-Za-z0-9_\/.\-]*$/.test(path) || path.indexOf('..') >= 0) return { error: 'bad_path' };
    const params = spec.params || {};
    for (const k of Object.keys(params)) {
      // Сначала белый список имён (ревью раунд 2): ' method', 'method[]',
      // 'method\u0000', полноширинная ｍ и т. п. сервер мог бы нормализовать
      // в `method`. Потом — запретный список поверх.
      if (!KEY_RE.test(String(k))) return { error: 'forbidden_param' };
      if (FORBIDDEN.indexOf(String(k).toLowerCase()) >= 0) return { error: 'forbidden_param' };
    }
    if (!token) return { error: 'no_token', state: state() };
    if (!realFetch) return { error: 'no_fetch' };
    const q = new URLSearchParams();
    for (const k of Object.keys(params)) {
      const v = params[k];
      q.set(k, typeof v === 'string' ? v : JSON.stringify(v));
    }
    q.set('access_token', token);
    const url = 'https://' + host + (version ? '/' + version : '') + path + '?' + q.toString();
    // Один запрос, без повтора: сбой сети — отчёт и стоп (ревью O4).
    // Перенаправления не идём (`redirect: 'error'`): ответ — только сам Graph.
    let resp = null;
    try {
      resp = await realFetch(url, { method: 'GET', credentials: 'omit', mode: 'cors',
                                    cache: 'no-store', redirect: 'error' });
    } catch (e) {
      return { error: 'network', state: state() };
    }
    let text = '';
    try { text = await resp.text(); } catch (e) { return { error: 'network', status: resp.status }; }
    text = clean(text);
    let body;
    try { body = JSON.parse(text); } catch (e) { body = { non_json: text.slice(0, 300) }; }
    const headers = {};
    for (const name of HEADERS) {
      try { const v = resp.headers.get(name); if (v) headers[name] = clean(v); } catch (e) {}
    }
    return { status: resp.status, body: body, headers: headers };
  }

  function ready(ms) {
    return new Promise((resolve) => {
      const t0 = Date.now();
      const tick = () => {
        const s = state();
        if (token || s.kind === 'login' || s.kind === 'checkpoint') return resolve(s);
        if (Date.now() - t0 >= ms) { fallbacks(); return resolve(state()); }
        setTimeout(tick, 250);
      };
      tick();
    });
  }

  function retire() { retired = true; token = null; unhook(); return true; }

  hook();
  Object.defineProperty(window, NAME, {
    value: Object.freeze({ call: call, ready: ready, state: state, retire: retire }),
    enumerable: false, configurable: false, writable: false,
  });
})();
"""


def helper_source(name: str) -> str:
    return _HELPER_JS.replace("__NAME__", json.dumps(name))


def ads_manager_url(act_id: str) -> str:
    return ADS_MANAGER_URL.format(act=act_digits(act_id))


def act_digits(act_id: str) -> str:
    text = (act_id or "").strip()
    return text[4:] if text.startswith("act_") else text


class InPageGraph:
    """Транспорт Graph API: `fetch()` во вкладке, токен — только там.

    Всё, что возвращается в Python, проходит `strip_tokens` (вторая линия:
    помощник вырезает токены сам).
    """

    def __init__(self, tab: Any, *, name: str | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic):
        self.tab = tab
        self.name = name or f"__acl_graph_{secrets.token_hex(6)}"
        self._sleep = sleep
        self._clock = clock
        self._installed = False

    def _js(self, tail: str) -> str:
        return f"window[{json.dumps(self.name)}]{tail}"

    def _eval(self, expression: str, *, timeout: float = 60.0) -> Any:
        return strip_tokens(self.tab.evaluate(expression, timeout=timeout))

    def start(self, act_id: str, *, timeout: float = 60.0) -> dict:
        """Поставить помощника, открыть Ads Manager кабинета, дождаться токена.

        Одна загрузка страницы, ни одного клика. Возвращает состояние страницы
        (`kind`, `has_token`, `source`, …) — без токена.
        """
        if not self._installed:
            self.tab.add_init_script(helper_source(self.name))
            self._installed = True
        self.tab.navigate(ads_manager_url(act_id))
        return self.wait_ready(timeout=timeout)

    def wait_ready(self, *, timeout: float = 60.0) -> dict:
        """Дождаться помощника в новом документе, затем — токена (внутри страницы).

        Цикл здесь — ожидание загрузки своей вкладки, к Meta он не ходит.
        """
        deadline = self._clock() + timeout
        probe = (f"(() => {{ const h = {self._js('')}; "
                 "return h && !h.state().retired && location.href !== 'about:blank'; })()")
        while True:
            try:
                if self._eval(probe, timeout=10.0):
                    break
            except (PageError, CdpError, TimeoutError):
                pass  # документ меняется под нами — пробуем ещё
            if self._clock() >= deadline:
                return {"kind": "not_loaded", "has_token": False}
            self._sleep(0.5)
        left_ms = max(1000, int((deadline - self._clock()) * 1000))
        state = self._eval(self._js(f".ready({left_ms})"), timeout=left_ms / 1000 + 10.0)
        return state if isinstance(state, dict) else {"kind": "other", "has_token": False}

    def state(self) -> dict:
        try:
            got = self._eval(f"(() => {{ const h = {self._js('')}; return h ? h.state() : null; }})()",
                             timeout=10.0)
        except (PageError, CdpError, TimeoutError):
            return {"kind": "unknown", "has_token": False}
        return got if isinstance(got, dict) else {"kind": "unknown", "has_token": False}

    def request(self, method: str, path: str, params: dict | None = None, *,
                timeout: float = 60.0) -> dict:
        if method != "GET":
            raise ValueError("read-only: only GET requests are allowed")
        bad = [k for k in (params or {})
               if not isinstance(k, str) or not PARAM_KEY_RE.fullmatch(k) or k.lower() in FORBIDDEN_PARAMS]
        if bad:
            raise ValueError(f"read-only: parameter {bad[0]!r} is not allowed")
        spec = {"method": method, "path": path, "params": params or {}}
        got = self._eval(self._js(f".call({json.dumps(spec)})"), timeout=timeout)
        return got if isinstance(got, dict) else {"error": "bad_reply"}

    def reacquire(self, *, timeout: float = 60.0) -> dict:
        """Переснять токен: старый документ гасит свой, вкладка перезагружается."""
        try:
            self._eval(self._js(".retire()"), timeout=10.0)
        except (PageError, CdpError, TimeoutError):
            pass
        self.tab.reload()
        return self.wait_ready(timeout=timeout)
