"""Токен Ads Manager остаётся во вкладке, и мы НИЧЕГО на странице не оборачиваем.

Решение владельца и координатора (ревью r8): после случая с разлогином на 5265
обёртки `fetch`/XHR убраны вовсе. Патч этих функций — самый правдоподобный
способ потревожить проверки целостности Facebook, поэтому его больше нет.

Как это работает
----------------
1. Помощник живёт в ОТДЕЛЬНОМ мире нашей вкладки (`Page.createIsolatedWorld`
   + `Runtime.evaluate` с `contextId`): скрипты страницы его не видят, в
   главном мире не появляется ни одного свойства. DOM и таймлайн у миров
   общие, поэтому помощник читает то, что страница УЖЕ сделала.
2. Токен берётся без единого запроса и без обёрток:
   - из адресов `performance.getEntriesByType('resource')` — Ads Manager уже
     сходил на Graph, и в адресе такого запроса бывает `access_token=` (имя
     ресурса в Resource Timing содержит строку запроса целиком);
   - из встроенного HTML и текста инлайновых скриптов (`"accessToken":"EAA…"`).
   Токен остаётся в замыкании отдельного мира.
3. Python шлёт помощнику только описание запроса `{method, path, params}`.
   Помощник сам добавляет токен, зовёт `fetch()` ИЗ ОТДЕЛЬНОГО МИРА (у него
   источник страницы, те же куки) и возвращает JSON ответа (токены вырезаны) и
   выбранные заголовки лимитов. `fetch` отдельного мира — родной, мимо любых
   обёрток самой страницы.

Токен НИКОГДА не возвращается в Python: ни значением, ни в исключении, ни в
логе. Python видит только `has_token: true/false` и откуда он взят. Помощник
принимает только GET, без `method`/`_method`/`batch`/`suppress_http_code`/
`access_token` в параметрах, и не идёт по перенаправлениям. Python проверяет то
же до вызова.

Переснять токен (ошибка 190) — перезагрузить вкладку: новый документ, новый
отдельный мир, нового помощника Python создаёт заново (`InPageGraph.reacquire`).
"""
from __future__ import annotations

import json
import re
import secrets
import time
import urllib.parse
from contextlib import contextmanager
from typing import Any, Callable

from src.fb_api.cdp import CdpError, CdpTimeout, PageError, StaleContextError, host_and_path
from src.fb_api.graph import Checkpoint, GraphStop, LoggedOut, PageUnresponsive
from src.fb_api.telemetry import strip_tokens

ADS_MANAGER_URL = "https://adsmanager.facebook.com/adsmanager/manage/campaigns?act={act}"

#: Ключи, которыми Graph подменяет глагол или пакует запросы; токен — только у
#: страницы. Тот же список стоит в помощнике (`FORBIDDEN`).
FORBIDDEN_PARAMS = frozenset({"method", "_method", "batch", "suppress_http_code", "access_token"})
#: Имя параметра — только ASCII-буквы, цифры и `_` (тот же `KEY_RE` в помощнике):
#: пробелы, управляющие символы, `[]`, полноширинные буквы не проходят, и
#: сервер не нормализует их в запретное имя.
PARAM_KEY_RE = re.compile(r"[A-Za-z0-9_]+", re.ASCII)

#: Куда идёт наш вызов: 'captured' — хост Graph, которым ходил сам Ads Manager
#: (например, adsmanager-graph.facebook.com), 'public' — graph.facebook.com.
GRAPH_HOST_MODES = frozenset({"captured", "public"})
#: Режим куки: 'captured' — как у запроса Ads Manager (по адресу его не узнать,
#: поэтому по умолчанию 'include': Graph и Ads Manager на одном facebook.com).
CREDENTIAL_MODES = frozenset({"captured", "include", "omit"})

#: Помощник ОТДЕЛЬНОГО мира. `__NAME__` — имя свойства `globalThis` (со
#: случайным хвостом). Ничего на странице не оборачивает; `fetch` зовёт только
#: по явному `call()`.
_HELPER_JS = r"""
(() => {
  const NAME = __NAME__;
  // ACL-1761: политика записи (null — только чтение). Вшита при установке,
  // страница её не видит и не меняет.
  const WRITE = __WRITE__;
  const g = (typeof globalThis !== 'undefined') ? globalThis : self;
  if (g[NAME]) return;

  const TOKEN_RE = /^EAA[A-Za-z0-9]{20,}$/;
  const TOKEN_ANY = /EAA[A-Za-z0-9]{20,}/g;
  // Хост Graph — только graph.facebook.com или <слово>-graph.facebook.com
  // (adsmanager-graph); якорь на начале нормализованного адреса, сразу за
  // хостом — `/` (ни порта, ни userinfo, ни поддоменов, ни http:). Ревью r5.
  const GRAPH_RE = /^https:\/\/((?:[a-z0-9]+-)?graph\.facebook\.com)(?:\/(v\d+\.\d+))?(?:\/|$)/i;
  const PARAM_RE = /(access_token(?:=|%3D|\\u003d))[^&"'\s\\]+/gi;
  const HEADERS = ['x-business-use-case-usage', 'x-ad-account-usage', 'x-app-usage',
                   'x-fb-ads-insights-throttle'];
  const FORBIDDEN = ['method', '_method', 'batch', 'suppress_http_code', 'access_token'];
  const KEY_RE = /^[A-Za-z0-9_]+$/;

  let token = null;
  let source = '';
  let host = 'graph.facebook.com';
  let version = '';
  let retired = false;
  let amRequest = null;

  // Разобрать адрес Graph: если в строке запроса есть access_token — это он.
  function consider(url) {
    if (token || retired) return;
    let u;
    try { u = new URL(String(url), location.href); } catch (e) { return; }
    const m = GRAPH_RE.exec(u.href);
    if (!m) return;
    let t = null;
    try { t = u.searchParams.get('access_token'); } catch (e) { t = null; }
    if (t && TOKEN_RE.test(t)) {
      token = t; host = m[1].toLowerCase(); version = m[2] || ''; source = 'resource_timing';
      let keys = [];
      try {
        u.searchParams.forEach((v, k) => { if (k !== 'access_token' && KEY_RE.test(k)) keys.push(k); });
      } catch (e) { keys = []; }
      amRequest = { via: 'resource_timing', method: 'GET', credentials: 'include',
                    path: u.pathname.slice(0, 120), params: keys.sort().slice(0, 40) };
    }
  }

  // Дешёвый опрос (ревью r9-2): только Resource Timing, без сериализации DOM.
  function scanFast() {
    if (token || retired) return;
    try {
      const entries = performance.getEntriesByType('resource');
      for (let i = 0; i < entries.length; i++) { consider(entries[i].name); if (token) return; }
    } catch (e) {}
  }

  // Полный поиск (только в финальном probe): HTML и инлайновые скрипты. Токен
  // берём лишь из контекста ключа `"accessToken":"EAA…"` — голый /(EAA…)/
  // убран (ревью r9-2, O5): он мог подхватить не тот токен и стоил перезагрузки.
  function scanHtml() {
    if (token || retired) return;
    const KEY = /"accessToken"\s*:\s*"(EAA[A-Za-z0-9]{20,})"/;
    try {
      const html = document.documentElement ? document.documentElement.innerHTML : '';
      const m = KEY.exec(html);
      if (m && TOKEN_RE.test(m[1])) { token = m[1]; source = 'html'; return; }
    } catch (e) {}
    try {
      for (const s of document.scripts) {
        if (s.src) continue;
        const m = KEY.exec(s.textContent || '');
        if (m && TOKEN_RE.test(m[1])) { token = m[1]; source = 'html'; return; }
      }
    } catch (e) {}
  }

  function state() {
    scanFast();
    const h = location.hostname || '';
    const p = location.pathname || '';
    let kind = 'other';
    if (/\/checkpoint(\/|$)/i.test(p) || /\/(two_step_verification|two_factor)(\/|$)/i.test(p)) kind = 'checkpoint';
    else if (/\/login(\.php)?(\/|$)/i.test(p) || /^\/recover(\/|$)/i.test(p)
             || !!document.querySelector('input[name="pass"]')) kind = 'login';
    else if (/(^|\.)adsmanager\.facebook\.com$/i.test(h) || p.indexOf('/adsmanager') === 0) kind = 'ads_manager';
    return { kind: kind, host: h, path: p.slice(0, 120), ready_state: document.readyState,
             has_token: !!token, source: source, version: version, retired: retired,
             graph_host: host, credentials: credentialsFor('captured'), am_request: amRequest };
  }

  function clean(text) {
    let out = String(text);
    if (token) out = out.split(token).join('<token>');
    return out.replace(PARAM_RE, '$1<hidden>').replace(TOKEN_ANY, '<token>');
  }

  function credentialsFor(mode) {
    if (mode === 'include' || mode === 'omit') return mode;
    return 'include';  // адрес не говорит о куки; Graph и Ads Manager — один сайт
  }

  // ── ACL-1761: запись (только при вшитой политике) ──
  // Файл копится здесь кусками (без сети, можно повторять), потом уходит ОДНИМ
  // POST. Ни токен, ни файл наружу не возвращаются.
  const staged = {};
  const STAGE_MAX = 200 * 1024 * 1024;
  function stagedBytes() {
    let n = 0;
    for (const k of Object.keys(staged)) for (const c of staged[k]) n += c.length;
    return n;
  }
  function stage(id, b64) {
    if (!WRITE || retired) return { error: 'read_only' };
    if (!/^[a-z0-9_]{1,40}$/.test(String(id))) return { error: 'bad_stage' };
    let bin;
    try { bin = atob(String(b64)); } catch (e) { return { error: 'bad_stage' }; }
    if (stagedBytes() + bin.length > STAGE_MAX) return { error: 'too_large' };
    const arr = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
    (staged[id] = staged[id] || []).push(arr);
    let total = 0;
    for (const c of staged[id]) total += c.length;
    return { ok: true, bytes: total };
  }
  function unstage(id) { delete staged[String(id)]; return true; }

  // Вложенные строки — обычный текст (ревью R2-B1): разбираются только
  // JSON-ключи верхнего уровня, в `write()` до проверок.
  function statusOk(value, depth) {
    depth = depth || 0;
    if (Array.isArray(value)) { for (const v of value) if (!statusOk(v, depth + 1)) return false; return true; }
    if (value && typeof value === 'object') {
      for (const k of Object.keys(value)) {
        const low = String(k).toLowerCase();
        if (WRITE.forbidden.indexOf(low) >= 0) return false;
        // Ревью R6: degrees_of_freedom_spec — только на верхнем уровне креатива.
        if (low === 'degrees_of_freedom_spec' && depth > 0) return false;
        if (WRITE.opt_out_keys.indexOf(low) >= 0) { if (value[k] !== 'OPT_OUT') return false; }
        else if (low.indexOf('status') >= 0 && value[k] !== 'PAUSED') return false;
        if (!statusOk(value[k], depth + 1)) return false;
      }
    }
    return true;
  }

  async function write(spec) {
    const refused = (why) => ({ error: 'write_refused', why: why });
    if (!WRITE) return { error: 'read_only' };
    if (spec.method !== 'POST') return { error: 'read_only' };
    const path = String(spec.path || '');
    const m = /^\/(act_[0-9]{5,})\/([a-z]+)$/.exec(path);
    if (!m || m[1] !== WRITE.act) return refused('path');
    const edge = m[2];
    const keys = WRITE.edges[edge];
    if (!keys) return refused('edge');
    const params = spec.params || {};
    for (const k of Object.keys(params)) {
      if (!KEY_RE.test(String(k)) || keys.indexOf(String(k)) < 0) return refused('key');
    }
    if (WRITE.paused_required.indexOf(edge) >= 0 && params.status !== 'PAUSED') return refused('status');
    const checked = Object.assign({}, params);
    for (const k of WRITE.json_keys) {
      if (!(k in params)) continue;
      let v = params[k];
      if (typeof v === 'string') { try { v = JSON.parse(v); } catch (e) { return refused('json'); } }
      if (v === null || typeof v !== 'object') return refused('json');
      checked[k] = v;
    }
    // Ревью R5-M1: degrees_of_freedom_spec — только выключение функций из списка.
    if ('degrees_of_freedom_spec' in checked) {
      const d = checked.degrees_of_freedom_spec;
      if (!d || typeof d !== 'object' || Array.isArray(d)) return refused('dof');
      const dk = Object.keys(d);
      if (dk.length !== 1 || dk[0] !== 'creative_features_spec') return refused('dof');
      const spec = d.creative_features_spec;
      if (!spec || typeof spec !== 'object' || Array.isArray(spec) || !Object.keys(spec).length) return refused('dof');
      for (const f of Object.keys(spec)) {
        if (WRITE.dof_features.indexOf(f) < 0) return refused('dof');
        const it = spec[f];
        if (!it || typeof it !== 'object' || Array.isArray(it)) return refused('dof');
        const ik = Object.keys(it);
        if (ik.length !== 1 || ik[0] !== 'enroll_status' || it.enroll_status !== 'OPT_OUT') return refused('dof');
      }
    }
    // Объявление — только ссылка на готовый креатив.
    if (edge === 'ads' && 'creative' in checked) {
      const c = checked.creative;
      if (!c || typeof c !== 'object' || Array.isArray(c)) return refused('creative');
      const ck = Object.keys(c);
      if (ck.length !== 1 || ck[0] !== 'creative_id'
          || (typeof c.creative_id !== 'string' && typeof c.creative_id !== 'number')) return refused('creative');
    }
    if (!statusOk(checked)) return refused('status');
    const want = WRITE.files[edge];
    const files = spec.files || {};
    const fk = Object.keys(files);
    if (want) { if (fk.length !== 1 || fk[0] !== want) return refused('file'); }
    else if (fk.length) return refused('file');
    let blob = null;
    if (want) {
      const parts = staged[String(files[want])];
      if (!parts || !parts.length) return { error: 'no_file' };
      blob = new Blob(parts);
      // Ревью M1: ушло бы не то, что положил Python — отказ ДО отправки.
      const expect = spec.expect || {};
      if (typeof expect.bytes !== 'number' || blob.size !== expect.bytes) return refused('file_size');
      try {
        const digest = await crypto.subtle.digest('SHA-256', await blob.arrayBuffer());
        const hex = Array.from(new Uint8Array(digest)).map((b) => b.toString(16).padStart(2, '0')).join('');
        if (hex !== String(expect.sha256 || '')) return refused('file_hash');
      } catch (e) { return refused('file_hash'); }
    }
    scanFast(); scanHtml();
    if (!token) return { error: 'no_token', state: state() };
    if (typeof fetch !== 'function') return { error: 'no_fetch' };
    const hostMode = spec.host === undefined ? 'captured' : spec.host;
    const credMode = spec.credentials === undefined ? 'captured' : spec.credentials;
    if (['captured', 'public'].indexOf(hostMode) < 0) return { error: 'bad_option' };
    if (['captured', 'include', 'omit'].indexOf(credMode) < 0) return { error: 'bad_option' };
    const useHost = hostMode === 'public' ? 'graph.facebook.com' : host;
    const cred = credentialsFor(credMode);
    let body;
    if (blob) {
      body = new FormData();
      for (const k of Object.keys(params)) {
        const v = params[k];
        body.append(k, typeof v === 'string' ? v : JSON.stringify(v));
      }
      body.append('access_token', token);
      body.append(want, blob, String(spec.filename || 'upload').replace(/[^A-Za-z0-9_.\-]/g, '_').slice(0, 80));
    } else {
      body = new URLSearchParams();
      for (const k of Object.keys(params)) {
        const v = params[k];
        body.set(k, typeof v === 'string' ? v : JSON.stringify(v));
      }
      body.set('access_token', token);
    }
    // Токен — в теле, не в адресе. Один запрос, без повтора и перенаправлений.
    const url = 'https://' + useHost + (version ? '/' + version : '') + path;
    const endpoint = { host: useHost, version: version, credentials: cred };
    let resp = null;
    try {
      resp = await fetch(url, { method: 'POST', body: body, credentials: cred, mode: 'cors',
                                cache: 'no-store', redirect: 'error' });
    } catch (e) {
      if (want) unstage(files[want]);
      return { error: 'network', state: state(), endpoint: endpoint };
    }
    if (want) unstage(files[want]);
    let text = '';
    try { text = await resp.text(); } catch (e) { return { error: 'network', status: resp.status }; }
    text = clean(text);
    let parsed;
    try { parsed = JSON.parse(text); } catch (e) { parsed = { non_json: text.slice(0, 300) }; }
    return { status: resp.status, body: parsed, headers: {}, endpoint: endpoint };
  }

  async function call(spec) {
    if (retired) return { error: 'retired' };
    if (spec && spec.method === 'POST') return write(spec);
    if (!spec || spec.method !== 'GET') return { error: 'read_only' };
    const path = String(spec.path || '');
    if (!/^\/[A-Za-z0-9_\/.\-]*$/.test(path) || path.indexOf('..') >= 0) return { error: 'bad_path' };
    const params = spec.params || {};
    for (const k of Object.keys(params)) {
      if (!KEY_RE.test(String(k))) return { error: 'forbidden_param' };
      if (FORBIDDEN.indexOf(String(k).toLowerCase()) >= 0) return { error: 'forbidden_param' };
    }
    const hostMode = spec.host === undefined ? 'captured' : spec.host;
    const credMode = spec.credentials === undefined ? 'captured' : spec.credentials;
    if (['captured', 'public'].indexOf(hostMode) < 0) return { error: 'bad_option' };
    if (['captured', 'include', 'omit'].indexOf(credMode) < 0) return { error: 'bad_option' };
    scanFast(); scanHtml();
    if (!token) return { error: 'no_token', state: state() };
    if (typeof fetch !== 'function') return { error: 'no_fetch' };
    const useHost = hostMode === 'public' ? 'graph.facebook.com' : host;
    const cred = credentialsFor(credMode);
    const q = new URLSearchParams();
    for (const k of Object.keys(params)) {
      const v = params[k];
      q.set(k, typeof v === 'string' ? v : JSON.stringify(v));
    }
    q.set('access_token', token);
    const url = 'https://' + useHost + (version ? '/' + version : '') + path + '?' + q.toString();
    const endpoint = { host: useHost, version: version, credentials: cred };
    // Один запрос, без повтора; перенаправления не идём (`redirect: 'error'`):
    // ответ — только сам Graph. `fetch` отдельного мира — родной, мимо обёрток
    // страницы.
    let resp = null;
    try {
      resp = await fetch(url, { method: 'GET', credentials: cred, mode: 'cors',
                                cache: 'no-store', redirect: 'error' });
    } catch (e) {
      return { error: 'network', state: state(), endpoint: endpoint };
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
    return { status: resp.status, body: body, headers: headers, endpoint: endpoint };
  }

  function probe() { scanFast(); scanHtml(); return state(); }
  function retire() { retired = true; token = null; return true; }

  g[NAME] = Object.freeze({ call: call, state: state, probe: probe, retire: retire,
                          stage: stage, unstage: unstage });
})();
"""


def helper_source(name: str, write_policy: dict | None = None) -> str:
    """Исходник помощника. `write_policy` — только у создателя кампаний (ACL-1761)."""
    return (_HELPER_JS.replace("__NAME__", json.dumps(name))
            .replace("__WRITE__", json.dumps(write_policy) if write_policy else "null"))


def ads_manager_url(act_id: str) -> str:
    return ADS_MANAGER_URL.format(act=act_digits(act_id))


def act_digits(act_id: str) -> str:
    text = (act_id or "").strip()
    return text[4:] if text.startswith("act_") else text


class InPageGraph:
    """Транспорт Graph API: `fetch()` из отдельного мира, токен — только там.

    Всё, что возвращается в Python, проходит `strip_tokens` (вторая линия:
    помощник вырезает токены сам).
    """

    def __init__(self, tab: Any, *, name: str | None = None,
                 graph_host: str = "captured", credentials: str = "captured",
                 allow_writes: bool = False, write_act: str | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic):
        # ACL-1761: запись — только явно и только в один проверенный кабинет.
        self.allow_writes = bool(allow_writes)
        self.write_act = None
        self._write_policy: dict | None = None
        if self.allow_writes:
            from src.fb_api import write_policy as _wp

            m = re.fullmatch(r"act_\d{5,}", str(write_act or ""))
            if not m:
                raise ValueError("allow_writes needs the verified act_<id>")
            self.write_act = str(write_act)
            self._write_policy = _wp.policy_for_page(self.write_act)
        if graph_host not in GRAPH_HOST_MODES:
            raise ValueError(f"graph_host must be one of {sorted(GRAPH_HOST_MODES)}")
        if credentials not in CREDENTIAL_MODES:
            raise ValueError(f"credentials must be one of {sorted(CREDENTIAL_MODES)}")
        self.graph_host = graph_host
        self.credentials = credentials
        self.tab = tab
        self.name = name or f"__acl_graph_{secrets.token_hex(6)}"
        self.world_name = f"acl_{secrets.token_hex(6)}"
        self._sleep = sleep
        self._clock = clock
        self._ctx: int | None = None
        self._injected = False

    # -- таймаут вкладки: что на самом деле с ней (F1, ACL-1761)
    def _timeout_stop(self, exc: BaseException) -> GraphStop | None:
        """Вкладка не ответила в срок: спросить у БРАУЗЕРА, где она сейчас.

        Логин → `LoggedOut`, checkpoint → `Checkpoint`, иначе `PageUnresponsive`.
        Браузер тоже молчит (или у вкладки нет такого метода) — `None`, и
        вызывающий поднимает исходный `CdpTimeout`. Повторов нет.
        """
        ask = getattr(self.tab, "browser_url", None)
        if not callable(ask):
            return None
        try:
            url = str(ask() or "")
        except Exception:
            return None
        if not url:
            return None
        path = urllib.parse.urlsplit(url).path.lower()
        where = host_and_path(url) or "an unknown page"
        if path.startswith(("/login", "/recover")):
            return LoggedOut("the profile was logged out while the job ran; log in inside "
                             "Multilogin, then retry. Nothing more was sent.")
        if "/checkpoint" in path or "two_step" in path:
            return Checkpoint("the profile is on a Facebook checkpoint")
        return PageUnresponsive(f"the Ads Manager tab stopped answering (now at {where}); "
                                "nothing more was sent")

    @contextmanager
    def _timeouts_as_stops(self):
        """`CdpTimeout` вкладки → осмысленный стоп; не удалось выяснить — как был."""
        try:
            yield
        except CdpTimeout as exc:
            stop = self._timeout_stop(exc)
            if stop is None:
                raise
            raise stop from exc

    # -- отдельный мир и помощник
    def _reset_world(self) -> None:
        self._ctx = None
        self._injected = False

    def _ensure(self) -> int:
        with self._timeouts_as_stops():
            if self._ctx is None:
                self._ctx = self.tab.create_isolated_world(self.world_name)
                self._injected = False
            if not self._injected:
                # Повторный вызов безвреден: помощник сам выходит, если уже стоит.
                self.tab.evaluate_in(self._ctx, helper_source(self.name, self._write_policy), timeout=10.0)
                self._injected = True
            return self._ctx

    def _eval_helper(self, tail: str, *, timeout: float = 60.0) -> Any:
        """Не-Meta вызов помощника (`state`/`probe`/`retire`): можно повторить.

        Повтор ТОЛЬКО если отдельный мир исчез (`StaleContextError`): это не
        запрос к Meta, пересоздать мир и попробовать ещё раз безопасно. Прочие
        ошибки (`PageError`/`CdpError`/`CdpTimeout`) поднимаются как есть — к
        Graph они не относятся, повторять нечего.
        """
        expr = f"globalThis[{json.dumps(self.name)}]{tail}"
        for attempt in (0, 1):
            ctx = self._ensure()
            try:
                with self._timeouts_as_stops():
                    return strip_tokens(self.tab.evaluate_in(ctx, expr, timeout=timeout))
            except StaleContextError:
                self._reset_world()
                if attempt:
                    raise
        raise CdpError("isolated world evaluation failed")  # недостижимо

    def _ready_for_call(self, *, timeout: float = 10.0) -> None:
        """Приготовить отдельный мир к Graph-вызову: помощник на месте, мир живой.

        Повторы допустимы — сюда ничего к Meta не уходит. `.state()` заодно
        подтверждает, что контекст отвечает (иначе `_eval_helper` его пересоздаст).
        """
        self._eval_helper(".state()", timeout=timeout)

    # -- жизненный цикл
    def install(self) -> None:
        """Совместимость с пилотом: помощник ставится лениво в отдельном мире."""
        self._reset_world()

    def navigate(self, act_id: str, *, timeout: float = 60.0) -> None:
        """Открыть Ads Manager кабинета в своей вкладке. Одна загрузка, ни одного клика."""
        with self._timeouts_as_stops():
            self.tab.navigate(ads_manager_url(act_id), timeout=timeout)
        self._reset_world()

    def start(self, act_id: str, *, timeout: float = 90.0) -> dict:
        """Помощник → Ads Manager → дождаться токена. Состояние страницы — без токена."""
        self.install()
        self.navigate(act_id, timeout=min(60.0, timeout))
        return self.wait_ready(timeout=timeout)

    def wait_ready(self, *, timeout: float = 90.0, poll_s: float = 1.0) -> dict:
        """Дождаться токена опросом `state()` из Python — без обещаний внутри страницы.

        Ждать обещание внутри страницы нельзя: в фоновой вкладке свёрнутого окна
        таймеры душатся, и обещание висит дольше срока чтения (живой пилот 5265:
        «CDP read timed out»). Здесь каждые `poll_s` — короткий `state()`
        (он сам сканирует Resource Timing и HTML); к Meta цикл не ходит. Мир
        пересоздаётся при смене документа. Браузер не ответил в срок
        (`CdpTimeout`) — стоп с именем шага, без повторов. Не дождались токена —
        один раз `probe()` и ответ.
        """
        deadline = self._clock() + timeout
        loaded = False
        while True:
            try:
                got = self._eval_helper(".state()", timeout=10.0)
            except CdpTimeout:
                raise
            except (PageError, CdpError):
                got = None  # документ ещё меняется
            if isinstance(got, dict):
                loaded = True
                if got.get("has_token") or got.get("kind") in ("login", "checkpoint"):
                    return got
            if self._clock() >= deadline:
                break
            self._sleep(poll_s)
        if not loaded:
            return {"kind": "not_loaded", "has_token": False}
        try:
            got = self._eval_helper(".probe()", timeout=10.0)
        except CdpTimeout:
            raise
        except (PageError, CdpError):
            got = None
        return got if isinstance(got, dict) else {"kind": "other", "has_token": False}

    def state(self) -> dict:
        try:
            got = self._eval_helper(".state()", timeout=10.0)
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
        spec = {"method": method, "path": path, "params": params or {},
                "host": self.graph_host, "credentials": self.credentials}
        # Сначала готовим мир (повторы без обращения к Meta)…
        self._ready_for_call()
        ctx = self._ensure()
        expr = f"globalThis[{json.dumps(self.name)}].call({json.dumps(spec)})"
        # …потом Graph-вызов РОВНО ОДИН раз. Любая ошибка после отправки —
        # результат неизвестен и НЕ повторяется (ревью r9-1): это стоп.
        try:
            with self._timeouts_as_stops():
                got = strip_tokens(self.tab.evaluate_in(ctx, expr, timeout=timeout))
        except GraphStop:
            raise
        except CdpTimeout:
            raise
        except (StaleContextError, PageError, CdpError):
            return {"error": "call_unknown"}
        return got if isinstance(got, dict) else {"error": "bad_reply"}

    # -- запись (ACL-1761)
    #: Размер куска файла, который кладётся в помощник за один вызов CDP.
    STAGE_CHUNK = 768 * 1024

    def _stage_once(self, ctx: int, data: bytes, timeout: float) -> str:
        """Один проход складывания файла в помощник. Устаревший мир — исключение."""
        import base64

        stage_id = "f" + secrets.token_hex(8)
        total = 0
        for start in range(0, max(1, len(data)), self.STAGE_CHUNK):
            chunk = base64.b64encode(data[start:start + self.STAGE_CHUNK]).decode("ascii")
            expr = (f"globalThis[{json.dumps(self.name)}].stage("
                    f"{json.dumps(stage_id)}, {json.dumps(chunk)})")
            got = self.tab.evaluate_in(ctx, expr, timeout=timeout)
            if not isinstance(got, dict) or not got.get("ok"):
                err = got.get("error") if isinstance(got, dict) else "bad_reply"
                raise PageError(f"could not stage the file in the page ({err})")
            total = int(got.get("bytes") or 0)
        if total != len(data):
            raise PageError(f"staged {total} bytes instead of {len(data)}")
        return stage_id

    def stage_file(self, data: bytes, *, timeout: float = 30.0) -> tuple[int, str]:
        """Положить файл в помощник кусками. К Meta ничего не уходит (ревью M1).

        Мир исчез посреди складывания — начинаем С НУЛЯ в новом мире (один
        раз): кусок не переотправляется в пустой новый мир под старым id.
        Итоговое число байт сверяется с `len(data)`. Возвращает (мир, id).
        """
        if not self.allow_writes:
            raise ValueError("read-only: staging a file needs allow_writes")
        for attempt in (0, 1):
            self._ready_for_call()
            ctx = self._ensure()
            try:
                return ctx, self._stage_once(ctx, data, timeout)
            except StaleContextError:
                self._reset_world()
                if attempt:
                    raise
        raise CdpError("staging failed")  # недостижимо

    def write(self, path: str, params: dict | None = None, *, files: dict | None = None,
              filename: str = "", timeout: float = 120.0) -> dict:
        """POST в разрешённое ребро. РОВНО один раз; сбой после отправки — `call_unknown`.

        `files` — {поле: bytes}. Файл складывается в помощник прямо перед
        вызовом, в том же мире; помощник сверяет размер и SHA-256 перед
        отправкой. Политика проверяется здесь (`write_policy.check`) и ещё раз
        в помощнике.
        """
        if not self.allow_writes:
            raise ValueError("read-only: writes are not enabled for this page")
        import hashlib

        from src.fb_api import write_policy as _wp

        _wp.check(self.write_act, "POST", path, params, files)
        spec: dict[str, Any] = {"method": "POST", "path": path, "params": params or {}, "files": {},
                                "filename": filename, "host": self.graph_host,
                                "credentials": self.credentials}
        if files:
            (field_name, data), = files.items()
            if not isinstance(data, (bytes, bytearray)):
                raise ValueError("file content must be bytes")
            try:
                ctx, stage_id = self.stage_file(bytes(data))
            except (PageError, CdpError) as exc:
                # Ничего не отправлено: отказ страницы до записи.
                return {"error": "no_file", "why": str(exc)[:120]}
            spec["files"] = {field_name: stage_id}
            spec["expect"] = {"bytes": len(data), "sha256": hashlib.sha256(bytes(data)).hexdigest()}
        else:
            self._ready_for_call()
            ctx = self._ensure()
        expr = f"globalThis[{json.dumps(self.name)}].call({json.dumps(spec)})"
        try:
            got = strip_tokens(self.tab.evaluate_in(ctx, expr, timeout=timeout))
        except (CdpTimeout, StaleContextError, PageError, CdpError, TimeoutError):
            # Запрос мог уйти: результат неизвестен, повтора нет — сверка чтением.
            return {"error": "call_unknown"}
        return got if isinstance(got, dict) else {"error": "call_unknown"}

    def reacquire(self, *, timeout: float = 90.0) -> dict:
        """Переснять токен: гасим помощника, перезагружаем вкладку, новый мир."""
        try:
            self._eval_helper(".retire()", timeout=10.0)
        except (PageError, CdpError, TimeoutError):
            pass
        self.tab.reload()
        self._reset_world()
        return self.wait_ready(timeout=timeout)
