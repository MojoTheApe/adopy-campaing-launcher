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

from src.fb_api.cdp import (CdpError, CdpTimeout, PageError, StaleContextError,
                            SessionLostError, PageIdentityError, host_and_path)
from src.fb_api.graph import (Checkpoint, GraphStop, LoggedOut, PageFailed,
                              PageUnresponsive)
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

  // ACL-1775: единственное место, где разрешён lead_gen_form_id (зеркало
  // write_policy._without_lead_form). Возвращает копию без ключа, null — отказ,
  // исходный объект — формы нет. Ключ в другом месте остаётся и ловится statusOk.
  function withoutLeadForm(edge, checked, onAd) {
    const LF = WRITE.lead_form;
    if (!LF || edge !== LF.edge) return checked;
    const spec = checked.object_story_spec;
    if (!spec || typeof spec !== 'object' || Array.isArray(spec)) return checked;
    for (const fmt of LF.formats) {
      const data = spec[fmt];
      const cta = data && typeof data === 'object' ? data.call_to_action : null;
      const value = cta && typeof cta === 'object' ? cta.value : null;
      if (!value || typeof value !== 'object' || Array.isArray(value) || !(LF.key in value)) continue;
      if (!onAd) return null;
      const form = value[LF.key];
      if (typeof form !== 'string' || !new RegExp(LF.id_re).test(form)) return null;
      if (Object.keys(value).length !== 1) return null;
      const copy = JSON.parse(JSON.stringify(checked));
      delete copy.object_story_spec[fmt].call_to_action.value[LF.key];
      return copy;
    }
    return checked;
  }

  // ACL-1776: отдельная цепочка создания формы лидов (зеркало
  // write_policy.check_form_create). Возвращает null (можно) или короткую причину.
  function checkForm(spec) {
    const F = WRITE && WRITE.form_create;
    if (!F) return 'form_policy';
    const m = new RegExp('^/([0-9]{1,25})/' + F.edge + '$').exec(String(spec.path || ''));
    if (!m) return 'form_path';
    // Страница закреплена на сессию (WRITE.form_create.page), а не берётся у вызывающего.
    if (!F.page || !/^[0-9]{1,25}$/.test(String(F.page))) return 'form_no_page';
    if (m[1] !== String(F.page)) return 'form_page';
    if (String(spec.form_page || '') !== m[1]) return 'form_page';
    if (spec.files && Object.keys(spec.files).length) return 'form_file';
    const params = spec.params || {};
    const txt = (x) => typeof x === 'string' && x.trim().length > 0 && x.length <= F.max_text;
    const url = (x) => typeof x === 'string' && new RegExp(F.url_re, 'i').test(x.trim());
    const bad = (value) => {
      if (Array.isArray(value)) return value.some(bad);
      if (value && typeof value === 'object') {
        return Object.keys(value).some((k) => {
          const low = String(k).toLowerCase();
          const parts = low.split(/[^a-z0-9]+/);
          return F.forbidden.indexOf(low) >= 0 || parts.indexOf('tos') >= 0
            || parts.indexOf('terms') >= 0 || low.indexOf('tos_accepted') >= 0 || bad(value[k]);
        });
      }
      return false;
    };
    if (bad(params)) return 'form_forbidden';
    const keys = Object.keys(params);
    for (const k of keys) if (!KEY_RE.test(k) || F.keys.indexOf(k) < 0) return 'form_key';
    for (const k of ['name', 'questions', 'locale']) if (!(k in params)) return 'form_key';
    const c = Object.assign({}, params);
    for (const k of F.json_keys) {
      if (!(k in params)) continue;
      let v = params[k];
      if (typeof v === 'string') { try { v = JSON.parse(v); } catch (e) { return 'form_json'; } }
      c[k] = v;
    }
    if (bad(c)) return 'form_forbidden';
    const only = (obj, kind) => obj && typeof obj === 'object' && !Array.isArray(obj)
      && Object.keys(obj).every((k) => F.nested[kind].indexOf(k) >= 0);
    if (!txt(c.name) || c.name.length > 200 || !new RegExp(F.name_re).test(c.name)) return 'form_name';
    if (!new RegExp(F.locale_re).test(String(c.locale))) return 'form_locale';
    const qs = c.questions;
    if (!Array.isArray(qs) || qs.length < 1 || qs.length > F.max_questions) return 'form_questions';
    for (const q of qs) {
      if (!only(q, 'question') || F.question_types.indexOf(q.type) < 0) return 'form_questions';
      if (q.type === 'CUSTOM') { if (!txt(q.key) || !txt(q.label)) return 'form_questions'; }
      else if (Object.keys(q).length !== 1) return 'form_questions';
      if ('options' in q) {
        if (q.type !== 'CUSTOM' || !Array.isArray(q.options) || q.options.length < 1
            || q.options.length > F.max_options) return 'form_questions';
        for (const o of q.options) if (!only(o, 'option') || !txt(o.key) || !txt(o.value)) return 'form_questions';
      }
    }
    if ('question_page_custom_headline' in c && !txt(c.question_page_custom_headline)) return 'form_headline';
    if ('follow_up_action_url' in c && !url(c.follow_up_action_url)) return 'form_url';
    if ('privacy_policy' in c) {
      const p = c.privacy_policy;
      if (!only(p, 'privacy_policy') || !url(p.url) || !txt(p.link_text)) return 'form_privacy';
    }
    if ('thank_you_page' in c) {
      const t = c.thank_you_page;
      if (!only(t, 'thank_you_page') || !txt(t.title)) return 'form_thanks';
      if ('body' in t && !txt(t.body)) return 'form_thanks';
      if ('button_text' in t && !txt(t.button_text)) return 'form_thanks';
      if ('button_type' in t && F.button_types.indexOf(t.button_type) < 0) return 'form_thanks';
      if ('website_url' in t && !url(t.website_url)) return 'form_thanks';
    }
    if ('context_card' in c) {
      const x = c.context_card;
      if (!only(x, 'context_card') || !txt(x.title) || F.card_styles.indexOf(x.style) < 0) return 'form_card';
      if (!Array.isArray(x.content) || !x.content.length || !x.content.every(txt)) return 'form_card';
    }
    return null;
  }

  async function write(spec, job) {
    const refused = (why) => ({ error: 'write_refused', why: why });
    if (!WRITE) return { error: 'read_only' };
    if (spec.method !== 'POST') return { error: 'read_only' };
    const path = String(spec.path || '');
    // ACL-1776: путь Страницы идёт ТОЛЬКО по цепочке форм; путь act_X - только по своей.
    if (/\/leadgen_forms$/.test(path)) {
      const why = checkForm(spec);
      if (why) return refused(why);
      return post(path, spec.params || {}, undefined, null, spec, {}, null);
    }
    // ACL-1800: правка существующего объекта — отдельная узкая политика. Помощник
    // создателя (без `object_update`) пути объекта не принимает; помощник правок
    // (без рёбер) пути создания не принимает.
    const objMatch = /^\/[1-9][0-9]{4,}$/.exec(path);
    if (objMatch && !WRITE.object_update) return refused('path');
    if (!objMatch && WRITE.object_update) return refused('path');
    const params = spec.params || {};
    let want = null;
    let files = {};
    let checked = null;
    if (objMatch) {
      const ou = WRITE.object_update;
      if (spec.files && Object.keys(spec.files).length) return refused('file');
      const pk = Object.keys(params);
      if (pk.length !== 1) return refused('key');
      const k = pk[0];
      if (!KEY_RE.test(k) || ou.fields.indexOf(k) < 0 || WRITE.forbidden.indexOf(k.toLowerCase()) >= 0) return refused('key');
      const v = params[k];
      if (k === 'status') {
        if (typeof v !== 'string' || ou.statuses.indexOf(v) < 0) return refused('status');
      } else {
        const okNum = typeof v === 'number' && Number.isSafeInteger(v) && v > 0 && v < 1e15;
        const okStr = typeof v === 'string' && /^[1-9][0-9]{0,14}$/.test(v);
        if (!okNum && !okStr) return refused('amount');
      }
    } else {
    const m = /^\/(act_[0-9]{5,})\/([a-z]+)$/.exec(path);
    if (!m || m[1] !== WRITE.act) return refused('path');
    const edge = m[2];
    const keys = WRITE.edges[edge];
    if (!keys) return refused('edge');
    for (const k of Object.keys(params)) {
      if (!KEY_RE.test(String(k)) || keys.indexOf(String(k)) < 0) return refused('key');
    }
    if (WRITE.paused_required.indexOf(edge) >= 0 && params.status !== 'PAUSED') return refused('status');
    checked = Object.assign({}, params);
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
    const walked = withoutLeadForm(edge, checked, spec.on_ad === true);
    if (walked === null) return refused('lead_form');
    if (!statusOk(walked)) return refused('status');
    want = WRITE.files[edge] || null;
    files = spec.files || {};
    const fk = Object.keys(files);
    if (want) { if (fk.length !== 1 || fk[0] !== want) return refused('file'); }
    else if (fk.length) return refused('file');
    }
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
    return post(path, params, want, blob, spec, files, job || null);
  }

  // ACL-1899: отправка файла (XHR: abort и проверка перенаправления). Без собственного таймаута: ждём ответ,
  // сетевую ошибку или `abort()` (Stop у покупателя).
  function sendXhr(url, body, cred, job) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      job.xhr = xhr;
      xhr.open('POST', url, true);
      xhr.withCredentials = (cred === 'include');
      // ВАЖНО: на `xhr.upload` НЕ вешаем ни одного слушателя (ни onprogress, ни addEventListener).
      // Любой слушатель загрузки по спецификации включает CORS-preflight; Graph отвечает на
      // OPTIONS с `Access-Control-Allow-Origin: *` без `Allow-Credentials`, и запрос с
      // куками (`withCredentials`) браузер режет: тело не уходит, приходит `network`.
      // Поэтому байтового прогресса нет - Python показывает прошедшее время.
      // Остаточный риск: XHR сам идёт по 307/308 и мог УЖЕ переотправить тело (с токеном)
      // по новому адресу до этой проверки. Рвём запрос, как только заголовки пришли не с
      // нашего адреса, и считаем результат НЕИЗВЕСТНЫМ. Graph-хосты не перенаправляют.
      const wrongUrl = () => !!xhr.responseURL && xhr.responseURL !== url;
      xhr.onreadystatechange = () => {
        if (xhr.readyState === 2 && wrongUrl()) {
          reject(new Error('redirect'));
          try { xhr.abort(); } catch (e) {}
        }
      };
      xhr.onload = () => {
        if (wrongUrl()) { reject(new Error('redirect')); return; }
        resolve({ status: xhr.status, text: String(xhr.responseText || ''), url: xhr.responseURL || '' });
      };
      xhr.onerror = () => reject(new Error('network'));
      xhr.onabort = () => reject(new Error('aborted'));
      xhr.send(body);
    });
  }

  // Задания записи с файлом: `startCall` сразу отдаёт id, `poll` читает прогресс
  // (локально, к Meta не ходит), `abort` рвёт отправку. Проверки политики — те же
  // (`write(spec, job)`), здесь ничего не ослаблено.
  const jobs = {};
  let jobSeq = 0;
  function startCall(spec) {
    if (retired) return { error: 'retired' };
    if (!WRITE || !spec || spec.method !== 'POST') return { error: 'read_only' };
    if (!spec.files || !Object.keys(spec.files).length) return { error: 'bad_option' };
    for (const k of Object.keys(jobs)) {
      if (jobs[k].state !== 'running' && Object.keys(jobs).length > 8) delete jobs[k];
    }
    for (const k of Object.keys(jobs)) if (jobs[k].state === 'running') return { error: 'bad_option' };
    const id = 'j' + (++jobSeq);
    const job = { state: 'running', xhr: null, aborted: false, result: null };
    jobs[id] = job;
    write(spec, job).then(
      (r) => { job.result = r; job.state = (r && r.error) ? 'error' : 'done'; },
      () => { job.result = { error: 'network' }; job.state = 'error'; });
    return { job: id };
  }
  function poll(id) {
    const j = jobs[String(id)];
    if (!j) return { state: 'error', result: { error: 'no_job' } };
    if (j.state === 'running') return { state: 'running' };
    return { state: j.state, result: j.result };
  }
  function abort(id) {
    const j = jobs[String(id)];
    if (!j) return { ok: false };
    j.aborted = true;
    try { if (j.xhr) j.xhr.abort(); } catch (e) {}
    return { ok: true };
  }

  // Общая отправка записи: политика уже проверена (цепочка act_X или цепочка форм).
  async function post(path, params, want, blob, spec, files, job) {
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
    let text = '';
    if (job && blob) {
      // ACL-1899: файл уходит через XMLHttpRequest — он отдаёт `upload.onprogress`.
      // Те же правила, что у fetch: один запрос, без повтора, без перенаправлений.
      if (typeof XMLHttpRequest !== 'function') { unstage(files[want]); return { error: 'no_fetch' }; }
      if (job.aborted) { unstage(files[want]); return { error: 'aborted' }; }
      let got = null;
      try { got = await sendXhr(url, body, cred, job); }
      catch (e) {
        unstage(files[want]);
        const why = job.aborted ? 'aborted' : (e && e.message === 'redirect' ? 'redirected' : 'network');
        return { error: why, state: state(), endpoint: endpoint };
      }
      unstage(files[want]);
      resp = { status: got.status };
      text = got.text;
    } else {
      try {
        resp = await fetch(url, { method: 'POST', body: body, credentials: cred, mode: 'cors',
                                  cache: 'no-store', redirect: 'error' });
      } catch (e) {
        if (want) unstage(files[want]);
        return { error: 'network', state: state(), endpoint: endpoint };
      }
      if (want) unstage(files[want]);
      try { text = await resp.text(); } catch (e) { return { error: 'network', status: resp.status }; }
    }
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
                          stage: stage, unstage: unstage,
                          startCall: startCall, poll: poll, abort: abort });
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
                 write_page: str | None = None,
                 allow_object_updates: bool = False,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic):
        # ACL-1761: запись — только явно и только в один проверенный кабинет.
        # ACL-1800: `allow_object_updates` — другой режим записи: только правка
        # существующих объектов (статус/бюджет/ставка), создание невозможно.
        if allow_writes and allow_object_updates:
            raise ValueError("creation writes and object updates are separate modes")
        self.object_updates = bool(allow_object_updates)
        self.allow_writes = bool(allow_writes) or self.object_updates
        self.write_act = None
        self.write_page: str | None = None
        self._write_policy: dict | None = None
        if self.allow_writes:
            from src.fb_api import write_policy as _wp

            m = re.fullmatch(r"act_\d{5,}", str(write_act or ""))
            if not m:
                raise ValueError("allow_writes needs the verified act_<id>")
            self.write_act = str(write_act)
            if write_page and not re.fullmatch(r"[0-9]{1,25}", str(write_page)):
                raise ValueError("write_page must be the numeric Page id of the plan")
            self.write_page = str(write_page) if write_page else None
            self._write_policy = (_wp.policy_for_commands(self.write_act) if self.object_updates
                                  else _wp.policy_for_page(self.write_act, self.write_page or ""))
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
        # Warm reuse can replace the helper mode without navigating the tab.
        # Inherit only ACL's prior navigation intent, never the current page URL.
        self._expected_url = getattr(tab, "navigation_url", "")
        self._session_recovered = bool(getattr(tab, "recovery_attempted", False))
        self._connection_stop: GraphStop | None = None

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
    def pin_page(self, page: str, *, timeout: float = 30.0) -> dict:
        """Закрепить Страницу плана на сессию (ACL-1776), как `write_act` закреплён на кабинет.

        Один раз: повторно - только то же значение. Помощник переустанавливается
        в новом мире уже с этой Страницей в политике форм. После сброса мира ждём токен
        (`wait_ready`): свежий мир мог не найти его в буфере Resource Timing. Возвращает
        состояние страницы; без токена вызывающий не пишет.
        """
        if not self.allow_writes:
            raise ValueError("read-only: writes are not enabled for this page")
        page = str(page or "")
        if not re.fullmatch(r"[0-9]{1,25}", page):
            raise ValueError("the Page to pin must be a numeric Page id")
        if self.write_page:
            if self.write_page != page:
                raise ValueError("a different Page is already pinned for this session")
            return {"kind": "ads_manager", "has_token": True, "pinned": True}
        from src.fb_api import write_policy as _wp

        self.write_page = page
        self._write_policy = _wp.policy_for_page(self.write_act, page)
        self._reset_world()
        return self.wait_ready(timeout=timeout)

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

        Retry local work once after a stale world or a detached session. A
        detached session must reattach to the same verified owned target first.
        Graph calls never pass through this retry boundary.
        """
        expr = f"globalThis[{json.dumps(self.name)}]{tail}"
        if self._connection_stop is not None:
            raise self._connection_stop
        for attempt in (0, 1):
            try:
                with self._timeouts_as_stops():
                    if self._session_recovered:
                        self.tab.verify_url(self._expected_url)
                ctx = self._ensure()
                with self._timeouts_as_stops():
                    return strip_tokens(self.tab.evaluate_in(ctx, expr, timeout=timeout))
            except SessionLostError as exc:
                self._reset_world()
                if attempt:
                    raise self._stop_connection(exc) from exc
                try:
                    self.tab.recover_session(self._expected_url)
                    self._session_recovered = True
                except (CdpError, OSError) as recovery:
                    raise self._stop_connection(recovery) from recovery
            except PageIdentityError as exc:
                raise self._stop_connection(exc) from exc
            except StaleContextError:
                self._reset_world()
                if attempt:
                    raise
        raise CdpError("isolated world evaluation failed")  # недостижимо

    def _stop_connection(self, exc: BaseException) -> GraphStop:
        kind = getattr(exc, "kind", "")
        if kind == "checkpoint":
            stop = Checkpoint("the profile is on a Facebook checkpoint")
        elif kind == "login":
            stop = LoggedOut("the profile is logged out of Facebook")
        else:
            stop = PageFailed("connection to the owned Ads Manager tab was lost or its account changed; "
                              "recovery stopped without replaying the interrupted request")
        self._connection_stop = stop
        return stop

    def _ready_for_call(self, *, timeout: float = 10.0) -> None:
        """Приготовить отдельный мир к Graph-вызову: помощник на месте, мир живой.

        Повторы допустимы — сюда ничего к Meta не уходит. `.state()` заодно
        подтверждает, что контекст отвечает (иначе `_eval_helper` его пересоздаст).
        """
        try:
            state = self._eval_helper(".state()", timeout=timeout)
            if self._session_recovered and isinstance(state, dict) and not state.get("has_token"):
                state = self._eval_helper(".probe()", timeout=timeout)
        except (CdpError, OSError) as exc:
            raise self._stop_connection(exc) from exc
        if isinstance(state, dict) and state.get("kind") in ("login", "checkpoint"):
            raise self._stop_connection(PageIdentityError(state["kind"]))
        if self._session_recovered and (not isinstance(state, dict) or
                state.get("kind") != "ads_manager" or not state.get("has_token")):
            raise self._stop_connection(PageIdentityError())

    # -- жизненный цикл
    def install(self) -> None:
        """Совместимость с пилотом: помощник ставится лениво в отдельном мире."""
        self._reset_world()

    def navigate(self, act_id: str, *, timeout: float = 60.0) -> None:
        """Открыть Ads Manager кабинета в своей вкладке. Одна загрузка, ни одного клика."""
        with self._timeouts_as_stops():
            self.tab.navigate(ads_manager_url(act_id), timeout=timeout)
        self._expected_url = ads_manager_url(act_id)
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

    def live_state(self, *, timeout: float = 10.0) -> dict:
        """Дешёвая проверка прогретой вкладки перед действием (ACL-1812).

        Один `.state()` помощника; токена в нём нет, а вкладка на Ads Manager —
        один полный `.probe()` (Resource Timing мог переполниться за часы, токен
        остаётся в HTML). К Meta ничего не уходит. Зависшая вкладка поднимает
        `GraphStop` (вход, checkpoint, не отвечает), мёртвая — `CdpError`: решает
        вызывающий (заменить вкладку один раз).
        """
        got = self._eval_helper(".state()", timeout=timeout)
        if isinstance(got, dict) and got.get("kind") == "ads_manager" and not got.get("has_token"):
            got = self._eval_helper(".probe()", timeout=timeout)
        return got if isinstance(got, dict) else {"kind": "unknown", "has_token": False}

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
        if not self.allow_writes or self.object_updates:
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
              filename: str = "", timeout: float = 120.0, on_ad: bool = False,
              form_page: str = "", progress: Callable[[float], None] | None = None,
              cancelled: Callable[[], bool] | None = None) -> dict:
        """POST в разрешённое ребро. РОВНО один раз; сбой после отправки — `call_unknown`.

        `files` — {поле: bytes}. Файл складывается в помощник прямо перед
        вызовом, в том же мире; помощник сверяет размер и SHA-256 перед
        отправкой. Политика проверяется здесь (`write_policy.check`) и ещё раз
        в помощнике.

        ACL-1899: запись С ФАЙЛОМ не имеет общего срока (`timeout` к ней не
        относится): задание стартует в помощнике, а Python раз в `UPLOAD_POLL_S`
        спрашивает состояние (только локальный CDP, к Meta не ходит) и отдаёт
        прошедшие секунды в `progress` (байтового прогресса нет: слушатель
        `xhr.upload` включил бы CORS-preflight, который Graph не пропускает). Кончается ответом, ошибкой сети или
        пропажей страницы (`call_unknown`); на `cancelled()` рвём отправку.
        Запись без файла — прежний короткий `timeout`.
        """
        if not self.allow_writes:
            raise ValueError("read-only: writes are not enabled for this page")
        import hashlib

        from src.fb_api import write_policy as _wp

        if form_page:
            # ACL-1776: цепочка форм - Страница плана; цепочка act_X для такого пути не годится.
            _wp.check_form_create(form_page, "POST", path, params, files, self.write_page or "")
        elif self.object_updates:
            _wp.check_object_update("POST", path, params, files)
        else:
            _wp.check(self.write_act, "POST", path, params, files, on_ad=on_ad)
        spec: dict[str, Any] = {"method": "POST", "path": path, "params": params or {}, "files": {},
                                "filename": filename, "host": self.graph_host,
                                "credentials": self.credentials}
        if form_page:
            spec["form_page"] = str(form_page)
        if on_ad:
            # ACL-1775: набор плана ON_AD — вторая линия политики пропустит форму в креативе.
            spec["on_ad"] = True
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
        if files:
            return self._write_file_job(ctx, spec, progress, cancelled)
        expr = f"globalThis[{json.dumps(self.name)}].call({json.dumps(spec)})"
        try:
            got = strip_tokens(self.tab.evaluate_in(ctx, expr, timeout=timeout))
        except (CdpTimeout, StaleContextError, PageError, CdpError, TimeoutError):
            # Запрос мог уйти: результат неизвестен, повтора нет — сверка чтением.
            return {"error": "call_unknown"}
        return got if isinstance(got, dict) else {"error": "call_unknown"}

    #: Как часто спрашивать у помощника прогресс отправки файла (ACL-1899).
    UPLOAD_POLL_S = 1.0
    #: Срок ОДНОГО локального опроса/старта/обрыва; общего срока у загрузки нет.
    UPLOAD_CDP_TIMEOUT_S = 15.0
    #: Сколько таймаутов ОПРОСА подряд терпим (повторно файл не шлём никогда).
    MAX_POLL_TIMEOUTS = 3

    def _write_file_job(self, ctx: int, spec: dict, progress, cancelled) -> dict:
        """Запись с файлом без общего срока: старт, опрос раз в секунду, обрыв по Stop."""
        def run(method: str, arg: str = "") -> Any:
            expr = f"globalThis[{json.dumps(self.name)}].{method}({arg})"
            return strip_tokens(self.tab.evaluate_in(ctx, expr, timeout=self.UPLOAD_CDP_TIMEOUT_S))

        try:
            started = run("startCall", json.dumps(spec))
        except (CdpTimeout, StaleContextError, PageError, CdpError, TimeoutError):
            return {"error": "call_unknown"}
        if not isinstance(started, dict):
            return {"error": "call_unknown"}
        if started.get("error"):
            return started                              # отказ ДО отправки (политика/состояние)
        job = started.get("job")
        if not isinstance(job, str) or not re.fullmatch(r"j[0-9]{1,6}", job):
            return {"error": "call_unknown"}
        poll_timeouts = 0
        began = self._clock()
        while True:
            if cancelled is not None and cancelled():
                try:
                    run("abort", json.dumps(job))
                except (CdpTimeout, StaleContextError, PageError, CdpError, TimeoutError):
                    pass
                # Результат неизвестен: дальше обычная сверка чтением, повторной загрузки нет.
                return {"error": "call_unknown", "why": "cancelled"}
            try:
                got = run("poll", json.dumps(job))
            except (CdpTimeout, TimeoutError):
                # Один опрос не ответил - не конец загрузки: терпим подряд до MAX_POLL_TIMEOUTS.
                poll_timeouts += 1
                if poll_timeouts > self.MAX_POLL_TIMEOUTS:
                    return {"error": "call_unknown"}
                self._sleep(self.UPLOAD_POLL_S)
                continue
            except (StaleContextError, PageError, CdpError):
                return {"error": "call_unknown"}
            poll_timeouts = 0
            if not isinstance(got, dict):
                return {"error": "call_unknown"}
            state = got.get("state")
            if state == "running":
                if progress is not None:
                    try:
                        progress(max(0.0, self._clock() - began))
                    except Exception:                    # noqa: BLE001 — показ не должен ронять запись
                        pass
                self._sleep(self.UPLOAD_POLL_S)
                continue
            result = got.get("result")
            if state in ("done", "error") and isinstance(result, dict):
                return result
            return {"error": "call_unknown"}

    def reacquire(self, *, timeout: float = 90.0) -> dict:
        """Переснять токен: гасим помощника, перезагружаем вкладку, новый мир."""
        try:
            self._eval_helper(".retire()", timeout=10.0)
        except (PageError, CdpError, TimeoutError):
            pass
        self.tab.reload()
        self._reset_world()
        return self.wait_ready(timeout=timeout)
