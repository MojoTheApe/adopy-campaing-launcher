"""ACL-1613 (PR-1): последняя ступень лестницы отказов — спросить Claude, что нажать.

Робот застрял на шаге, которого не знает (чужое окно, свёрнутая карточка,
переехавшая кнопка). Модуль берёт ТЕКСТОВУЮ сводку страницы (без скриншотов),
вычищает из неё всё, по чему опознаётся аккаунт, спрашивает модель и получает
ровно одно действие из белого списка. Безопасность — в коде, не в подсказке:
запрещённая кнопка, неизвестный индекс, битый ответ — всё это `give_up`.
Принцип: сомневаешься — отказывай. `give_up` стоит одной красной кампании,
неверный клик может опубликовать, удалить или выключить живую кампанию.

    res = rescue(page, step="further_limit_row", goal="open the Audience section",
                 error="row not found", budget=budget, telemetry=run_telemetry)

Режим `ACL_AI_RESCUE=off|suggest|act` (по умолчанию `off`):
`suggest` пишет ответ в журнал и не трогает страницу; `act` выполняет одно
действие и возвращается — проверку шага повторяет вызывающий. Ничего не
бросает наружу: нет ключа/SDK, ошибка сети, любое исключение → `skipped`.

PR-1: нигде в потоке не вызывается. Точки подключения — в PR-2.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

import yaml

from . import foreign_windows as _fw
from . import run_telemetry as _run_telemetry
from . import wizard_guard as _wg

log = logging.getLogger(__name__)

STEP = "ai_rescue"
MODES = ("off", "suggest", "act")
DEFAULT_MODEL = "claude-sonnet-5-5"
#: Потолок ожидания ответа модели, секунд (общий, а не «на фазу», как у httpx).
TIMEOUT_S = 20.0
MAX_TOKENS = 300
#: Потолки сводки: что уходит модели (на проверку запрещённого они не влияют).
MAX_ELEMENTS = 150
MAX_CHARS = 12000
MAX_LABEL = 80
MAX_SCAN = 600  # сколько кандидатов собирает JS на огромной странице
ANCHOR_SCAN_MS = 2000  # потолок поиска якоря шага

ACTIONS = ("click", "expand", "close_dialog", "press_escape", "scroll_to", "give_up")
_NEEDS_INDEX = ("click", "expand", "close_dialog", "scroll_to")
MARK = "data-acl-ai"
#: Роли, по которым вообще можно кликнуть. Переключатели, чекбоксы, радио, поля
#: ввода и ссылки (уводят со страницы) — никогда.
CLICKABLE_ROLES = ("button", "tab")


# ── запрещённое: одна таблица в yaml (EN/ES/PT), проверка в коде ────────────

class _Table:
    def __init__(self, raw: dict[str, Any]):
        def comp(rows):
            out = []
            for r in rows or []:
                pat = str(r.get("pattern") or "")
                if pat:
                    # границы слов — нашими lookaround'ами по нормализованному тексту
                    out.append((str(r.get("reason") or pat), re.compile(rf"(?<![a-z0-9])(?:{pat})(?![a-z0-9])")))
            return out

        allow = raw.get("allow") or {}
        self.context = comp(raw.get("context"))
        self.target_only = comp(raw.get("target_only"))
        self.close_blockers = comp(raw.get("close_blockers"))
        self.allow_close = {norm_text(x) for x in allow.get("close_dialog") or []}
        self.allow_click = {norm_text(x) for x in allow.get("click") or []}
        self.allow_expand = {norm_text(x) for x in allow.get("expand") or []}
        self.allow_tab = {norm_text(x) for x in allow.get("tab") or []}


@lru_cache(maxsize=1)
def _table() -> _Table | None:
    """Таблица из config/rpa_forbidden_zones.yaml; нет файла/секции/allowlist — None (отказываем всё)."""
    try:
        raw = yaml.safe_load(_wg._ZONES_PATH.read_text(encoding="utf-8")) or {}
        t = _Table(raw.get("ai_rescue") or {})
        ok = (t.context and t.target_only and t.close_blockers and t.allow_close and t.allow_click and t.allow_tab
              and t.allow_expand)
        return t if ok else None
    except Exception as exc:
        log.warning("ai_rescue: forbidden table unreadable (%s) - every pick will be refused", type(exc).__name__)
        return None


_GLYPH_X = str.maketrans({"×": " x ", "✕": " x ", "✖": " x ", "✗": " x ", "╳": " x "})


def norm_text(text: str) -> str:
    """Нижний регистр, без диакритики и невидимых символов, всё не-буквенное — один пробел.

    Мягкий перенос и символы нулевой ширины (категория Cf: U+00AD, U+200B-200D, U+2060, U+FEFF)
    выбрасываются ДО разбора: «Pub­lish» на экране — «Publish». «hereDelete» → «here delete».
    """
    t = "".join(ch for ch in str(text or "") if unicodedata.category(ch) != "Cf").translate(_GLYPH_X)
    t = re.sub(r"([a-zà-ÿ])([A-ZÀ-Ý])", r"\1 \2", t)
    t = unicodedata.normalize("NFKD", t)
    t = "".join(ch for ch in t if not unicodedata.combining(ch)).lower()
    return re.sub(r"[\W_]+", " ", t).strip()


def _scan(rows, text: str) -> str | None:
    n = norm_text(text)
    for reason, rx in rows:
        if rx.search(n):
            return reason
    return None


def forbidden_reason(label_parts, nearby: str = "", dialog_texts=(), *, close_dialog: bool = False) -> str | None:
    """Причина запрета для цели с такими источниками подписи, окружением и текстом окон; иначе None.

    Тексты не усекаются. Нет таблицы — запрещено всё.
    """
    t = _table()
    if t is None:
        return "forbidden_table_missing"
    parts = [label_parts] if isinstance(label_parts, str) else list(label_parts or [])
    whole = " | ".join(str(x) for x in parts)
    for rows, text, where in ((t.context, whole, "label"), (t.target_only, whole, "label"),
                              (t.context, nearby, "nearby")):
        why = _scan(rows, text)
        if why:
            return f"forbidden_{where}:{why}"
    for d in dialog_texts or ():
        why = _scan(t.context, str(d))
        if why:
            return f"forbidden_dialog:{why}"
    for part in parts:
        try:
            # без scope «Close» из yaml (wizard_chrome) действует; для close_dialog — снят
            why = _wg.forbidden_click_reason(str(part), scope="ai_rescue" if close_dialog else None)
        except Exception:
            why = None
        if why:
            return f"forbidden_zone:{str(why)[:40]}"
    return None


def _norm_parts(parts) -> list[str]:
    return [n for n in (norm_text(p) for p in parts or []) if n]


def is_close_label(parts) -> bool:
    """Все источники подписи — слова из allowlist close_dialog (Close/Cerrar/Cancel/×…)."""
    t = _table()
    ns = _norm_parts(parts)
    return bool(t) and bool(ns) and all(n in t.allow_close for n in ns)


def click_allowed(parts, role: str) -> bool:
    """Подпись кнопки в коротком списке click (вкладки — в списке tab)."""
    t = _table()
    ns = _norm_parts(parts)
    if not t or not ns:
        return False
    names = t.allow_tab if role == "tab" else t.allow_click
    return all(n in names for n in ns)


def expand_allowed(label: str) -> bool:
    """Подпись начинается с названия раздела из allowlist expand (целое слово/слова с начала)."""
    t = _table()
    n = norm_text(label)
    return bool(t) and bool(n) and any(n == x or n.startswith(x + " ") for x in t.allow_expand)


def close_blocked_by(text: str) -> str | None:
    """Окно, которое закрывать нельзя ни под какой подписью (отмена может выбросить работу)."""
    t = _table()
    return _scan(t.close_blockers, text) if t else "forbidden_table_missing"


# ── чистка того, что уходит наружу ──────────────────────────────────────────

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+|\S+@\S+")
_URL_RE = _fw._URL_RE
_ACT_RE = re.compile(r"\bact[=_]\S+", re.I)
_UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
_JWT_RE = re.compile(r"\beyJ[\w-]{5,}\.[\w-]{5,}\.[\w-]*")
_FBTOKEN_RE = re.compile(r"\bEA[A-Za-z0-9]{8,}\b")
#: Длинная «каша» из букв и цифр (токены, хэши, render-id), обязательно с цифрой.
_BLOB_RE = re.compile(r"\b(?=[A-Za-z_-]*\d)[A-Za-z0-9_-]{20,}\b")
_PHONE_RE = re.compile(r"(?<!\w)\+?\d[\d\s().-]{7,}\d(?!\w)")
#: «<номер кабинета> - Имя кабинета»: всё после номера — имя.
_ACCOUNT_HEAD_RE = re.compile(r"\d{3,}\s*[-–—]\s*[^|]+")
#: Любой токен с «_»: имена кампаний байера (CO_bnc_All_Video_GMBL), id, snake_case.
_UNDERSCORE_RE = re.compile(r"[A-Za-z0-9.\-]*_[A-Za-z0-9_.\-]*")
#: «Имя кабинета - <номер>» (имя перед номером).
_ACCOUNT_TAIL_RE = re.compile(r"[^|]{0,60}?\s[-–—]\s*\d{3,}")
#: Метка аккаунта в любом регистре: Facebook_9403A, FacebookDoc120, UL921, «Account 1234».
_ACCOUNT_RE = re.compile(r"\b(?:facebook|fb|account|acct)[A-Za-z_\s-]{0,20}\d+[A-Za-z]?\b|\b[A-Z]{2,3}\d{3,}[A-Za-z]?\b", re.I)
_PIXEL_RE = re.compile(r"\bpixel\s+\S+", re.I)
#: «Evgeni's account», «Evgeni's page».
_POSSESSIVE_RE = re.compile(r"\b[\w.-]+['’]s (?:account|profile|page|business)\b", re.I)
#: Любой токен с цифрой: B96, Story14, Doc120, Investment_35_06.10, 06.10.
_DIGIT_TOKEN_RE = re.compile(r"[A-Za-z0-9_.+\-]*\d[A-Za-z0-9_.+\-]*")


def _digit_token(m: re.Match) -> str:
    tok = m.group(0).strip(".")
    return "<n>" if tok.isdigit() and len(tok) <= 3 else "<id>"


def scrub_text(text: str, names: tuple[str, ...] | list[str] = ()) -> str:
    """Убрать из строки всё, что опознаёт аккаунт, кампанию или человека.

    Порядок важен: сначала адреса и ссылки (в них цифры), потом имена, потом
    каждый токен с цифрой. Хвост после `|` (имя кампании в заголовке) — имя.

    `names` — свободные имена, которые регуляркой не угадать: их подаёт вызывающий
    (PR-2): имя Facebook-страницы и Instagram, имя рекламного кабинета, имена
    пикселей, имя кампании. Пустой `names` оставляет в тексте имена без цифр и
    без подчёркиваний («The Self Made Method - CSI»). Имя кампании прогона берётся
    из телеметрии.
    """
    out = str(text or "")
    try:
        camp = _run_telemetry.current_campaign()
    except Exception:
        camp = ""
    for name in sorted({str(n) for n in [camp, *names] if n and len(str(n)) >= 3}, key=len, reverse=True):
        out = re.sub(re.escape(name), "<name>", out, flags=re.I)
    out = _EMAIL_RE.sub("<email>", out)
    out = _URL_RE.sub("<url>", out)
    out = _ACT_RE.sub("act=<id>", out)
    out = _JWT_RE.sub("<token>", out)
    out = _FBTOKEN_RE.sub("<token>", out)
    out = _UUID_RE.sub("<id>", out)
    out = _UNDERSCORE_RE.sub("<name>", out)
    out = re.sub(r"\s*\|.*$", " | <name>", out, flags=re.S) if "|" in out else out
    out = _ACCOUNT_HEAD_RE.sub("<account>", out)
    out = _ACCOUNT_TAIL_RE.sub("<account>", out)
    out = _ACCOUNT_RE.sub("<account>", out)
    out = _PIXEL_RE.sub("Pixel <name>", out)
    out = _POSSESSIVE_RE.sub("<name>", out)
    out = _BLOB_RE.sub("<token>", out)
    out = _PHONE_RE.sub("<phone>", out)
    out = _DIGIT_TOKEN_RE.sub(_digit_token, out)
    try:
        out = _run_telemetry.scrub(out)
    except Exception:
        pass
    return re.sub(r"\s+", " ", out).strip()


# ── страница: общий JS-пролог ───────────────────────────────────────────────

_PRE = r"""
const norm = (s) => (s || '').replace(/[​-‍﻿]/g, '').replace(/\s+/g, ' ').trim();
const shown = (e) => {
  if (!e || !e.getBoundingClientRect) return false;
  const r = e.getBoundingClientRect();
  if (r.width <= 1 || r.height <= 1) return false;
  for (let n = e; n && n.nodeType === 1; n = n.parentElement) {
    const s = getComputedStyle(n);
    if (s.display === 'none' || s.visibility === 'hidden' || s.opacity === '0') return false;
    if (n.hasAttribute('hidden') || n.hasAttribute('inert')) return false;
  }
  return true;
};
const DLG = '[role=dialog], [role=alertdialog], [aria-modal="true"]';
const HEAD = '[role=heading], h1, h2, h3, h4';
// Окно-редактор: ТОЛЬКО если внутри само поле имени кампании/группы/объявления и окно занимает
// не меньше половины экрана. Метки data-surface не используются (их несёт любой потомок футера).
const EDITOR = 'input[placeholder^="Enter your campaign name"], input[placeholder^="Enter your ad set name"],'
  + ' input[placeholder^="Enter your ad name"]';
const INTER = 'button, a[href], input, select, textarea, [role=button], [role=link], [role=switch],'
  + ' [role=checkbox], [role=radio], [role=menuitem], [role=tab], [tabindex], [aria-expanded],'
  + ' [contenteditable="true"]';
const SKIP = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE']);
const ROLES = new Set(['button','link','checkbox','radio','combobox','textbox','tab','switch','menuitem','searchbox']);
const FIELD = new Set(['combobox', 'textbox', 'searchbox']);
// текст узлов через пробел; attrs — ещё aria-label/title/alt/value всех потомков
const textOf = (root, attrs, skipRoot) => {
  const out = [];
  const walk = (n) => {
    if (n.nodeType === 3) { const t = norm(n.data); if (t) out.push(t); return; }
    if (n.nodeType !== 1 || SKIP.has(n.tagName)) return;
    if (attrs && !(skipRoot && n === root)) {
      for (const a of ['aria-label', 'title', 'alt']) { const v = norm(n.getAttribute(a)); if (v) out.push(v); }
      if (n.tagName === 'INPUT') { const v = norm(n.value || n.getAttribute('value')); if (v) out.push(v); }
    }
    for (const c of n.childNodes) walk(c);
  };
  walk(root);
  return norm(out.join(' '));
};
// ВСЕ источники подписи цели и потомков, по одному на элемент массива
const parts = (el) => {
  const p = [];
  for (const a of ['aria-label', 'title', 'alt']) { const v = norm(el.getAttribute(a)); if (v) p.push(v); }
  if (typeof el.value === 'string') { const v = norm(el.value); if (v) p.push(v); }
  const lb = el.getAttribute('aria-labelledby');
  if (lb) for (const id of lb.split(/\s+/)) { const x = document.getElementById(id); if (x) { const v = textOf(x, true); if (v) p.push(v); } }
  const t = textOf(el, true, true); if (t) p.push(t);   // своё aria-label уже в списке выше
  return p;
};
const roleOf = (e) => {
  const r = (e.getAttribute('role') || '').toLowerCase();
  if (ROLES.has(r)) return r;
  const t = e.tagName.toLowerCase();
  if (t === 'button') return 'button';
  if (t === 'a' && e.hasAttribute('href')) return 'link';
  if (t === 'select') return 'combobox';
  if (t === 'textarea') return 'textbox';
  if (t === 'input') {
    const ty = (e.getAttribute('type') || 'text').toLowerCase();
    if (ty === 'checkbox' || ty === 'radio') return ty;
    if (ty === 'button' || ty === 'submit') return 'button';
    if (ty === 'hidden' || ty === 'file') return '';
    return 'textbox';
  }
  if (e.getAttribute('contenteditable') === 'true') return 'textbox';
  if (e.hasAttribute('aria-expanded')) return r || 'button';
  return '';
};
// короткая подпись для модели; у полей ввода значение (имя страницы, пикселя) не берём
const labelOf = (e, role) => {
  // поле ввода/выбора (combobox, textbox, select): ТОЛЬКО placeholder, иначе <field> —
  // aria-label, aria-labelledby, value и подписи могут нести имя страницы или кабинета
  if (FIELD.has(role) || e.tagName === 'SELECT') {
    return (norm(e.getAttribute('placeholder')) || '<field>').slice(0, 80);
  }
  let l = norm(e.getAttribute('aria-label'));
  const lb = e.getAttribute('aria-labelledby');
  if (!l && lb) l = norm(lb.split(/\s+/).map((id) => document.getElementById(id)).filter(Boolean)
    .map((x) => textOf(x, false)).join(' '));
  if (!l && e.labels && e.labels.length) l = norm([...e.labels].map((x) => textOf(x, false)).join(' '));
  if (!l) l = textOf(e, false);
  if (!l && e.tagName === 'INPUT' && typeof e.value === 'string') l = norm(e.value);  // input type=button
  if (!l) l = norm(e.getAttribute('title') || e.getAttribute('placeholder') || e.getAttribute('alt'));
  return l.slice(0, 80);
};
const stateOf = (e) => {
  const s = [];
  const ex = e.getAttribute('aria-expanded');
  if (ex === 'true' || ex === 'false') s.push(ex === 'true' ? 'expanded' : 'collapsed');
  if (e.disabled || e.getAttribute('aria-disabled') === 'true') s.push('disabled');
  const ch = e.getAttribute('aria-checked');
  if (e.checked === true || ch === 'true') s.push('checked');
  const pr = e.getAttribute('aria-pressed');
  if (pr === 'true') s.push('pressed'); else if (pr === 'false') s.push('not-pressed');
  return s;
};
const isEditor = (d) => {
  if (!d.querySelector(EDITOR)) return false;
  const r = d.getBoundingClientRect();
  return r.width * r.height >= 0.5 * innerWidth * innerHeight;
};
const realDialogs = () => [...document.querySelectorAll(DLG)].filter((d) => shown(d) && !isEditor(d));
const dialogOf = (el) => {
  let d = el.parentElement ? el.parentElement.closest(DLG) : null;
  while (d && isEditor(d)) d = d.parentElement ? d.parentElement.closest(DLG) : null;
  return d;
};
const dirtyIn = (d) => [...d.querySelectorAll(
  'input:not([type=checkbox]):not([type=radio]):not([type=hidden]):not([type=button]):not([type=submit]), textarea, [contenteditable="true"]')]
  .some((x) => norm(typeof x.value === 'string' ? x.value : x.textContent) !== '');
const inRowOf = (el) => !!el.closest(
  'tr, table, [role=row], [role=gridcell], [role=columnheader], [role=table], [role=grid], [role=treegrid]');
const isLink = (el) => !!el.closest('a[href]');
const hasShadow = (el) => [el, ...el.querySelectorAll('*')].some((x) => !!x.shadowRoot);
"""

_SUMMARY_JS = "(args) => {" + _PRE + r"""
  const {anchor, maxScan} = args;
  for (const e of document.querySelectorAll('[data-acl-ai]')) e.removeAttribute('data-acl-ai');
  const dlgTitle = (d) => {
    const h = [...d.querySelectorAll(HEAD)].filter(shown).map((x) => norm(x.textContent)).find(Boolean);
    return (h || norm(d.getAttribute('aria-label')) || textOf(d, false)).slice(0, 80);
  };
  const sectionOf = (e) => {
    let n = e.parentElement;
    for (let i = 0; n && i < 7; i++, n = n.parentElement) {
      const h = [...n.querySelectorAll(HEAD)].find((x) => !e.contains(x) && shown(x));
      if (h) return norm(h.textContent).slice(0, 60);
    }
    return '';
  };
  const dialogs = realDialogs();
  const dlgOut = dialogs.slice(0, 5).map((d) => ({title: dlgTitle(d), text: textOf(d, false).slice(0, 300)}));
  const headings = [...new Set([...document.querySelectorAll(HEAD)].filter(shown)
    .map((h) => norm(h.textContent).slice(0, 80)).filter(Boolean))].slice(0, 25);
  // страница-список кампаний/групп/объявлений: широкая таблица/грид (у Meta это один
  // div[role=table] с одной строкой-шапкой), окон нет
  const vp = innerWidth * innerHeight;
  const grids = [...document.querySelectorAll('table, [role=grid], [role=treegrid], [role=table]')].filter((g) => {
    if (!shown(g)) return false;
    const r = g.getBoundingClientRect();
    return r.width * r.height >= 0.35 * vp;
  });
  const listPage = grids.length > 0 && dialogs.length === 0;
  // якорь шага: первый видимый текстовый узел с этим текстом, поиск не дольше 2 с
  let ay = null, ax = 0;
  if (anchor) {
    const a = anchor.toLowerCase(), t0 = performance.now();
    const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    let n;
    while ((n = w.nextNode())) {
      if (performance.now() - t0 > args.anchorMs) break;
      if (n.data.toLowerCase().includes(a) && n.parentElement && shown(n.parentElement)) {
        const r = n.parentElement.getBoundingClientRect();
        ay = r.top + r.height / 2; ax = r.left + r.width / 2;
        break;
      }
    }
  }
  const out = [];
  let scanned = 0;
  const sel = 'button, a[href], input, select, textarea, [role], [aria-expanded], [contenteditable="true"]';
  for (const e of document.querySelectorAll(sel)) {
    if (scanned >= maxScan) break;
    const role = roleOf(e);
    // строки списков и ссылки (по ним мы не кликаем, а подписи — имена кампаний) модели не показываем
    if (!role || role === 'link' || !shown(e) || inRowOf(e)) continue;
    scanned++;
    const r = e.getBoundingClientRect();
    const d = dialogOf(e);
    const inDialog = !!(d && dialogs.includes(d));
    const dist = ay === null ? r.top : Math.abs(r.top + r.height / 2 - ay) + 0.25 * Math.abs(r.left + r.width / 2 - ax);
    out.push({el: e, role, label: labelOf(e, role), state: stateOf(e), inDialog, dist,
              dialog: inDialog ? dlgTitle(d) : ''});
  }
  out.sort((p, q) => (q.inDialog - p.inDialog) || (p.dist - q.dist));
  const res = out.map((o, i) => {
    o.el.setAttribute('data-acl-ai', String(i));
    return {i, role: o.role, label: o.label, state: o.state, inDialog: o.inDialog,
            dialog: o.dialog, section: o.inDialog ? o.dialog : sectionOf(o.el)};
  });
  return {dialogs: dlgOut, headings, elements: res, scanned, listPage};
}
"""

#: Всё, что нужно для решения «можно ли»: полные источники подписи цели и потомков,
#: окружение, полный текст окна, и (act) точка клика, проверенная elementFromPoint.
_LOCATE_JS = "async (args) => {" + _PRE + r"""
  const hits = document.querySelectorAll('[data-acl-ai="' + args.i + '"]');
  if (hits.length !== 1) return {found: hits.length};
  const el = hits[0];
  const frame = () => new Promise((r) => requestAnimationFrame(() => r()));
  const rectOf = () => { const r = el.getBoundingClientRect(); return [r.left, r.top, r.width, r.height]; };
  let stable = true;
  if (args.prepare) {
    // мгновенная прокрутка (CSS scroll-behavior: smooth игнорируется), затем два кадра:
    // прямоугольник цели должен стоять на месте
    el.scrollIntoView({block: 'center', inline: 'center', behavior: 'instant'});
    await frame();
    const r1 = rectOf();
    await frame();
    const r2 = rectOf();
    stable = r1.every((v, k) => Math.abs(v - r2[k]) < 0.5);
  }
  const role = roleOf(el);
  const dlgs = [];
  const d = dialogOf(el);
  if (d) dlgs.push(d);
  for (const x of el.querySelectorAll(DLG)) if (shown(x) && !isEditor(x)) dlgs.push(x);
  const p = el.parentElement;
  const pt = p && !d ? textOf(p, true) : '';
  const info = {
    found: 1, visible: shown(el), role, label: labelOf(el, role), parts: parts(el),
    nearby: pt.length <= 400 ? pt : '',
    dialogs: dlgs.map((x) => ({text: norm(textOf(x, true) + ' ' + norm(x.getAttribute('aria-label'))), dirty: dirtyIn(x),
                               editorLike: !!x.querySelector(EDITOR)})),
    dialogOpen: realDialogs().length > 0, inDialog: !!d, inRow: inRowOf(el), shadow: hasShadow(el), link: isLink(el),
    desc: [...el.querySelectorAll(INTER)].slice(0, 80).map((x) => ({role: roleOf(x), parts: parts(x)})),
    expanded: el.getAttribute('aria-expanded'), pressed: el.getAttribute('aria-pressed'),
    stable, hit: null,
  };
  if (args.prepare) {
    const rr = el.getBoundingClientRect();
    let res = {ok: false, reason: 'no_point'};
    for (const [fx, fy] of [[.5, .5], [.25, .5], [.75, .5], [.5, .25], [.5, .75], [.08, .5], [.5, .08]]) {
      const x = rr.left + rr.width * fx, y = rr.top + rr.height * fy;
      if (x < 0 || y < 0 || x >= innerWidth || y >= innerHeight) { res = {ok: false, reason: 'offscreen'}; continue; }
      const h = document.elementFromPoint(x, y);
      if (!h) { res = {ok: false, reason: 'no_hit'}; continue; }
      if (h !== el && !el.contains(h)) { res = {ok: false, reason: 'covered'}; continue; }
      const ia = h.closest(INTER);
      if (ia && ia !== el && el.contains(ia)) { res = {ok: false, reason: 'interactive_descendant'}; continue; }
      // позиция внутри элемента — для locator.click(position=...), он ещё раз проверит цель
      res = {ok: true, x: x - rr.left, y: y - rr.top, parts: parts(h)};
      break;
    }
    info.hit = res;
  }
  return info;
}
"""

_DIALOGS_JS = "() => {" + _PRE + r"""
  return realDialogs().map((d) => ({text: norm(textOf(d, true) + ' ' + norm(d.getAttribute('aria-label'))), dirty: dirtyIn(d),
                editorLike: !!d.querySelector(EDITOR)}));
}
"""

_CLEAR_JS = "() => document.querySelectorAll('[data-acl-ai]').forEach((e) => e.removeAttribute('data-acl-ai'))"


@dataclass
class PageSummary:
    text: str
    elements: dict[int, dict[str, Any]] = field(default_factory=dict)  # только попавшие в сводку
    dialogs: list[dict[str, str]] = field(default_factory=list)
    headings: list[str] = field(default_factory=list)
    truncated: int = 0
    digest: str = ""
    list_page: bool = False


def clear_marks(page) -> None:
    try:
        page.evaluate(_CLEAR_JS)
    except Exception:
        pass


def build_summary(page, *, anchor: str = "", names: tuple[str, ...] | list[str] = ()) -> PageSummary:
    """Текстовая сводка видимой части страницы; элементы получают `data-acl-ai=<индекс>`.

    Диалоги и их элементы идут первыми, затем элементы вокруг якоря шага
    (без якоря — сверху вниз). Потолки: MAX_ELEMENTS строк и MAX_CHARS знаков.
    Строки списков (таблица кампаний) модели не показываются.
    """
    try:
        raw = page.evaluate(_SUMMARY_JS, {"anchor": str(anchor or ""), "maxScan": MAX_SCAN,
                                          "anchorMs": ANCHOR_SCAN_MS})
    except Exception as exc:
        log.warning("ai_rescue: DOM summary failed (%s), falling back to the aria snapshot", type(exc).__name__)
        return _fallback_summary(page, names)

    def s(t: str) -> str:
        return scrub_text(t, names)

    lines: list[str] = []
    for d in raw.get("dialogs") or []:
        lines.append(f'DIALOG "{s(d.get("title", ""))}": {s(d.get("text", ""))[:200]}')
    if raw.get("headings"):
        lines.append("HEADINGS: " + " | ".join(s(h) for h in raw["headings"]))
    lines.append('ELEMENTS (index role "label" [state] in "section"):')
    kept: dict[int, dict[str, Any]] = {}
    total = len(raw.get("elements") or [])
    used = sum(len(x) + 1 for x in lines)
    for el in raw.get("elements") or []:
        if len(kept) >= MAX_ELEMENTS:
            break
        state = f' [{",".join(el.get("state") or [])}]' if el.get("state") else ""
        sec = s(el.get("section", ""))
        line = (f'[{el["i"]}] {el["role"]} "{s(el.get("label", ""))[:MAX_LABEL]}"{state}'
                + (f' in "{sec}"' if sec else ""))
        if used + len(line) + 1 > MAX_CHARS - 40:
            break
        lines.append(line)
        used += len(line) + 1
        kept[int(el["i"])] = {**el, "label": str(el.get("label", ""))}
    truncated = total - len(kept)
    if truncated > 0:
        lines.append(f"... {truncated} more elements not shown")
    text = "\n".join(lines)[:MAX_CHARS]
    return PageSummary(
        text=text, elements=kept, dialogs=list(raw.get("dialogs") or []),
        headings=list(raw.get("headings") or []), truncated=max(0, truncated),
        digest=hashlib.sha256(text.encode("utf-8")).hexdigest()[:12], list_page=bool(raw.get("listPage")),
    )


def _fallback_summary(page, names) -> PageSummary:
    """JS не отработал: текст из aria-снимка без индексов (модель может лишь Esc/give_up)."""
    snap = ""
    try:
        from .intent_runner import capture_aria_snapshot  # ленивый импорт: тяжёлый модуль

        snap = capture_aria_snapshot(page)
    except Exception:
        snap = ""
    text = ("ARIA SNAPSHOT (no element indexes available):\n" + scrub_text(snap, names))[:MAX_CHARS]
    return PageSummary(text=text, digest=hashlib.sha256(text.encode("utf-8")).hexdigest()[:12])


# ── бюджет и результат ──────────────────────────────────────────────────────

@dataclass
class RescueBudget:
    """Счётчик одной кампании: 2 вызова на шаг, 4 на кампанию. Передаёт вызывающий.

    `names` — свободные имена для чистки (имя Facebook-страницы и Instagram, имя
    рекламного кабинета, имена пикселей, имя кампании); их заполняет вызывающий
    в PR-2, регулярками эти имена не угадать.
    """

    per_step: int = 2
    per_campaign: int = 4
    names: tuple[str, ...] = ()
    total: int = 0
    by_step: dict[str, int] = field(default_factory=dict)

    def allows(self, step: str) -> bool:
        return self.total < self.per_campaign and self.by_step.get(step, 0) < self.per_step

    def spend(self, step: str) -> None:
        self.total += 1
        self.by_step[step] = self.by_step.get(step, 0) + 1


@dataclass
class RescueResult:
    ok: bool
    mode: str
    action: str = ""
    label: str = ""  # уже вычищенная метка выбранного элемента
    reason: str = ""
    skipped: bool = False


def get_mode() -> str:
    m = (os.environ.get("ACL_AI_RESCUE") or "off").strip().lower()
    return m if m in MODES else "off"


def get_model() -> str:
    return (os.environ.get("ACL_AI_RESCUE_MODEL") or "").strip() or DEFAULT_MODEL


# ── модель ──────────────────────────────────────────────────────────────────

_TOOL = {
    "name": "rescue_action",
    "description": "Choose exactly one safe UI action that helps the stuck step, or give up.",
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": list(ACTIONS)},
            "index": {"type": "integer", "description": "Element index from the summary (not for press_escape/give_up)."},
            "reason": {"type": "string", "description": "One short sentence."},
        },
        "required": ["action"],
    },
}

_SYSTEM = (
    "You help a browser robot that is stuck while building an ad campaign in a web ads editor. "
    "You see a text summary of the visible page (dialogs, headings, interactive elements with an index). "
    "Choose exactly ONE action by calling the rescue_action tool: click(index), expand(index) for a collapsed "
    "section or card, close_dialog(index) for a stray dialog's close/cancel button, press_escape, "
    "scroll_to(index), or give_up(reason). Never choose anything that publishes, deletes, removes, discards, "
    "accepts terms, creates forms, logs out, or touches payment or billing. If unsure, give_up. "
    "Identifiers in the summary are replaced by placeholders on purpose."
)


def _load_key() -> str:
    key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if key:
        return key
    try:
        from config.paths import load_env  # общий загрузчик .env машины

        load_env()
    except Exception as exc:
        log.debug("ai_rescue: load_env failed (%s)", type(exc).__name__)
    return (os.environ.get("ANTHROPIC_API_KEY") or "").strip()


def _make_client():
    """(клиент, "") или (None, причина). Без ключа/SDK ступень пропускается."""
    key = _load_key()
    if not key:
        return None, "no_api_key"
    try:
        import anthropic
    except Exception:
        return None, "no_sdk"
    try:
        return anthropic.Anthropic(api_key=key, timeout=TIMEOUT_S, max_retries=0), ""
    except Exception as exc:
        return None, f"client_error:{type(exc).__name__}"


def _ask(client, prompt: str) -> tuple[Any, str]:
    """Один вызов модели с жёстким общим потолком по времени (поток-демон)."""
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["resp"] = client.messages.create(
                model=get_model(), max_tokens=MAX_TOKENS, system=_SYSTEM,
                messages=[{"role": "user", "content": prompt}],
                tools=[_TOOL], tool_choice={"type": "tool", "name": "rescue_action"},
                timeout=TIMEOUT_S,
            )
        except Exception as exc:
            box["err"] = type(exc).__name__

    th = threading.Thread(target=run, daemon=True, name="ai-rescue-call")
    th.start()
    th.join(TIMEOUT_S)
    if th.is_alive():
        return None, "timeout"
    if "err" in box:
        return None, f"call_failed:{box['err']}"
    return box.get("resp"), ""


def _parse(resp) -> tuple[dict[str, Any] | None, str]:
    """Достать и проверить действие; всё, что не по схеме, — (None, причина)."""
    try:
        data = None
        for block in getattr(resp, "content", None) or []:
            if getattr(block, "type", "") == "tool_use":
                data = getattr(block, "input", None)
                break
        if data is None:
            return None, "no_tool_use"
        if isinstance(data, str):
            data = json.loads(data)
        if not isinstance(data, dict):
            return None, "bad_answer"
        action = data.get("action")
        if action not in ACTIONS:
            return None, "action_not_allowed"
        out: dict[str, Any] = {"action": action, "reason": str(data.get("reason") or "")[:200]}
        if action in _NEEDS_INDEX:
            idx = data.get("index")
            if isinstance(idx, bool) or not isinstance(idx, int):
                return None, "bad_index"
            out["index"] = idx
        return out, ""
    except Exception as exc:
        return None, f"bad_answer:{type(exc).__name__}"


def _usage(resp) -> tuple[int | None, int | None]:
    u = getattr(resp, "usage", None)
    return getattr(u, "input_tokens", None), getattr(u, "output_tokens", None)


# ── решение «можно ли» и исполнение ─────────────────────────────────────────

def _vet(action: str, info: dict[str, Any], label: str, *, after_prepare: bool = False) -> str | None:
    """Причина отказа по свежим данным со страницы (`_LOCATE_JS`) или None.

    Два слоя, оба обязательны: чёрный список слов (в любой словоформе и языке) и ПОЛОЖИТЕЛЬНЫЙ
    белый список действий (`allow` в yaml). Сомневаешься — отказывай.
    """
    if info.get("found") != 1:
        return "element_gone_or_ambiguous"
    if not info.get("visible"):
        return "element_not_visible"
    if info.get("label", "") != label:
        return "dom_changed"
    if after_prepare and not info.get("stable"):
        return "rect_unstable"
    if action == "scroll_to":
        return None  # прокрутка кампанию изменить не может
    parts = info.get("parts") or []
    if not _norm_parts(parts):
        return "empty_label"
    if info.get("shadow"):
        return "shadow_dom"
    if info.get("link"):
        return "link_target"
    if info.get("inRow"):
        return "in_list_row"
    if info.get("dialogOpen") and not info.get("inDialog"):
        return "behind_dialog"
    dialogs = info.get("dialogs") or []
    close_dialog = action == "close_dialog"
    # слой 1: чёрный список (подпись цели, потомков, соседний текст, ПОЛНЫЙ текст окна)
    why = forbidden_reason(parts, info.get("nearby", ""), [d.get("text", "") for d in dialogs],
                           close_dialog=close_dialog)
    if why:
        return why
    if info.get("role") not in CLICKABLE_ROLES:
        return f"role_not_allowed:{info.get('role')}"
    desc = info.get("desc") or []
    if desc:
        if action != "expand":
            return "has_interactive_descendants"
        for d in desc:
            if d.get("role") in ("switch", "checkbox", "radio", "textbox", "searchbox"):
                return "has_interactive_descendants"
            why = forbidden_reason(d.get("parts") or [], "", ())
            if why:
                return f"descendant_{why}"
    # слой 2: белый список
    if close_dialog:
        if not info.get("inDialog") or not dialogs:
            return "not_a_dialog_close_button"
        if not is_close_label(parts):
            return f"not_allowlisted:{scrub_text(label)[:40]}"
        for d in dialogs:
            if d.get("editorLike"):
                return "editor_dialog_not_closable"  # окно с полем имени кампании — никогда, пустое или нет
            if close_blocked_by(d.get("text", "")):
                return "close_blocked_by_dialog_text"
            if d.get("dirty"):
                return "dirty_dialog"
        return None
    if is_close_label(parts):
        return "close_only_via_close_dialog"
    if action == "expand":
        if info.get("expanded") != "false" and info.get("pressed") != "false":
            return "not_collapsed"
        if expand_allowed(label):
            return None  # только заголовок свёрнутого раздела; Create new / Share / Advantage+ … — нет
        return f"not_allowlisted:{scrub_text(label)[:40]}"
    if action == "click":
        if click_allowed(parts, info.get("role", "")):
            return None
        return f"not_allowlisted:{scrub_text(label)[:40]}"
    return f"action_not_supported:{action}"


def _execute(page, act: dict[str, Any], summary: PageSummary, mode: str) -> tuple[bool, str, str, str]:
    """Проверить выбор и (в act) выполнить. Возвращает (ok, action, сырая метка, причина)."""
    action = act["action"]
    if action == "give_up":
        return False, "give_up", "", act.get("reason") or "model_gave_up"
    if action == "press_escape":
        try:
            dialogs = page.evaluate(_DIALOGS_JS) or []
        except Exception as exc:
            return False, "give_up", "", f"escape_check_failed:{type(exc).__name__}"
        if not dialogs:
            return False, "give_up", "", "no_dialog"
        for d in dialogs:
            why = forbidden_reason([], "", [d.get("text", "")])
            if why:
                return False, "give_up", "", f"escape_blocked:{why}"
            if close_blocked_by(d.get("text", "")):
                return False, "give_up", "", "close_blocked_by_dialog_text"
            if d.get("editorLike"):
                return False, "give_up", "", "editor_dialog_not_closable"
            if d.get("dirty"):
                return False, "give_up", "", "dirty_dialog"
        if mode == "act":
            try:
                page.keyboard.press("Escape")
            except Exception as exc:
                return False, "press_escape", "", f"escape_failed:{type(exc).__name__}"
        return True, "press_escape", "", ""

    idx = act["index"]
    el = summary.elements.get(idx)
    if el is None:
        return False, "give_up", "", "unknown_index"
    label = el["label"]
    # 1) проверка БЕЗ единой прокрутки: отказанный выбор страницу не трогает
    try:
        info = page.evaluate(_LOCATE_JS, {"i": idx, "prepare": False})
    except Exception as exc:
        return False, "give_up", label, f"recheck_failed:{type(exc).__name__}"
    why = _vet(action, info or {}, label)
    if why:
        return False, "give_up", label, why
    if mode != "act":
        return True, action, label, ""
    # 2) мгновенная прокрутка, два кадра покоя, повторная полная проверка и проверка попадания
    try:
        info = page.evaluate(_LOCATE_JS, {"i": idx, "prepare": True})
    except Exception as exc:
        return False, "give_up", label, f"prepare_failed:{type(exc).__name__}"
    why = _vet(action, info or {}, label, after_prepare=True)
    if why:
        return False, "give_up", label, why
    if action == "scroll_to":
        return True, action, label, ""
    hit = info.get("hit") or {}
    if not hit.get("ok"):
        return False, "give_up", label, f"hit_{hit.get('reason', 'unknown')}"
    # то, во что попадёт клик, тоже проверяем на запрещённое
    why = forbidden_reason(hit.get("parts") or [], "", (), close_dialog=action == "close_dialog")
    if why:
        return False, "give_up", label, f"hit_{why}"
    try:
        # Playwright сам ещё раз проверит, что в точке клика наша цель; ожидание не дольше 1 с
        page.locator(f'[{MARK}="{int(idx)}"]').click(position={"x": hit["x"], "y": hit["y"]}, timeout=1000)
    except Exception as exc:
        return False, action, label, f"action_failed:{type(exc).__name__}"
    return True, action, label, ""


def _prompt(step: str, goal: str, error: str, summary: PageSummary, names) -> str:
    def s(t: str) -> str:
        return scrub_text(t, names)

    return (
        f"Step: {step}\nGoal: {s(goal)}\nLast error: {s(error)[:300]}\n\n"
        f"Page summary:\n{summary.text}\n\nCall rescue_action with exactly one action."
    )


def _record(telemetry, ok: bool | None, action: str, diag: dict[str, Any]) -> None:
    try:
        (telemetry if telemetry is not None else _run_telemetry).record("ai_rescue", ok, note=action, diag=diag)
    except Exception:
        pass  # телеметрия не должна ломать шаг


def rescue(page, *, step: str, goal: str, error: str, budget: RescueBudget,
           telemetry=None, client=None, anchor: str = "", mode: str | None = None) -> RescueResult:
    """Спросить модель об одном действии. Никогда не бросает; `act` выполняет ровно одно действие."""
    mode = mode if mode in MODES else get_mode()
    if mode == "off":
        return RescueResult(False, mode, reason="off", skipped=True)
    try:
        return _rescue(page, step, goal, error, budget, telemetry, client, anchor, mode)
    except Exception as exc:  # последняя сетка: в поток ничего не уходит
        log.warning("ai_rescue: unexpected error (%s), rung skipped", type(exc).__name__)
        return RescueResult(False, mode, reason=f"error:{type(exc).__name__}", skipped=True)


def _rescue(page, step, goal, error, budget, telemetry, client, anchor, mode) -> RescueResult:
    base = {"step": str(step)[:60], "goal": scrub_text(goal, budget.names)[:200], "mode": mode}
    if not budget.allows(step):
        log.info("ai_rescue: budget exhausted for step %s (campaign %d/%d), skipped",
                 step, budget.total, budget.per_campaign)
        _record(telemetry, None, "skipped", {**base, "reason": "budget_exhausted"})
        return RescueResult(False, mode, reason="budget_exhausted", skipped=True)

    built = None
    try:
        summary = build_summary(page, anchor=anchor, names=budget.names)
        if summary.list_page:
            # страница-список: строки и переключатели живых кампаний — не наше дело, модель не зовём
            log.info("ai_rescue: list page, give_up without asking the model")
            _record(telemetry, False, "give_up", {**base, "action": "give_up", "reason": "list_page",
                                                   "summary_hash": summary.digest})
            return RescueResult(False, mode, "give_up", reason="list_page")
        if client is None:
            client, why = _make_client()
            if client is None:
                log.warning("ai_rescue: skipped (%s) - set ANTHROPIC_API_KEY in the machine .env", why)
                _record(telemetry, None, "skipped", {**base, "reason": why})
                return RescueResult(False, mode, reason=why, skipped=True)
            built = client
        budget.spend(step)
        t0 = time.monotonic()
        resp, err = _ask(client, _prompt(step, goal, error, summary, budget.names))
        latency = round(time.monotonic() - t0, 2)
        tin, tout = _usage(resp)
        diag = {**base, "latency_s": latency, "input_tokens": tin, "output_tokens": tout,
                "summary_hash": summary.digest}
        if err:
            log.warning("ai_rescue: model call failed (%s)", err)
            _record(telemetry, False, "give_up", {**diag, "action": "give_up", "reason": err})
            return RescueResult(False, mode, "give_up", reason=err)
        act, bad = _parse(resp)
        if act is None:
            log.warning("ai_rescue: unusable answer (%s) -> give_up", bad)
            _record(telemetry, False, "give_up", {**diag, "action": "give_up", "reason": bad})
            return RescueResult(False, mode, "give_up", reason=bad)
        ok, action, raw_label, reason = _execute(page, act, summary, mode)
        label = scrub_text(raw_label, budget.names)
        # Коды причин (`not_collapsed`, `forbidden_label:publish`) — наши, их не чистим;
        # свободный текст модели — чистим.
        reason = (reason or scrub_text(act.get("reason", ""), budget.names))[:200]
        log.info("ai_rescue[%s] step=%s action=%s label=%r ok=%s reason=%s", mode, step, action, label, ok, reason)
        _record(telemetry, ok, action, {**diag, "action": action, "label": label, "reason": reason})
        return RescueResult(ok, mode, action, label, reason)
    finally:
        clear_marks(page)  # страница остаётся такой, какой была (suggest — ни одного изменения)
        if built is not None:
            try:
                built.close()  # клиент создали мы — закрываем; чужой (подставной) не трогаем
            except Exception:
                pass
