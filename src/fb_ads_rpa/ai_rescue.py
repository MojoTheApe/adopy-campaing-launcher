"""ACL-1613 (PR-1): последняя ступень лестницы отказов — спросить Claude, что нажать.

Робот застрял на шаге, которого не знает (чужое окно, свёрнутая карточка,
переехавшая кнопка). Модуль берёт ТЕКСТОВУЮ сводку страницы (без скриншотов),
вычищает из неё всё, по чему опознаётся аккаунт, спрашивает модель и получает
ровно одно действие из белого списка. Безопасность — в коде, не в подсказке:
запрещённая кнопка, неизвестный индекс, битый ответ — всё это `give_up`.

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
from dataclasses import dataclass, field
from typing import Any

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
#: Потолки сводки: что уходит модели.
MAX_ELEMENTS = 150
MAX_CHARS = 12000
MAX_LABEL = 80
MAX_SCAN = 600  # сколько кандидатов собирает JS на огромной странице

ACTIONS = ("click", "expand", "close_dialog", "press_escape", "scroll_to", "give_up")
_NEEDS_INDEX = ("click", "expand", "close_dialog", "scroll_to")
MARK = "data-acl-ai"


# ── запрещённое: в коде, а не в подсказке ───────────────────────────────────

#: Метка элемента и текст рядом/в окне. Сверх `config/rpa_forbidden_zones.yaml`
#: (его правила подключены ниже через wizard_guard — не дублируем).
FORBIDDEN_RE = re.compile(
    r"\bpublish(ed|ing)?\b"
    r"|\bdelete\b|\bremove\b|\bdiscard\b"
    r"|\baccept\b|\bagree\b|\bterms\b"
    r"|\b(create|new|add)\s+(a\s+)?(lead\s+|instant\s+)?form\b|\binstant forms?\b"
    r"|\blog\s?out\b|\bsign\s?out\b"
    r"|\bpayments?\b|\bbilling\b|credit card|\bdebit card\b"
    r"|close without saving|leave without|\bleave( page| this page)?\b|\bdon.t save\b",
    re.I,
)

#: Для close_dialog: метка должна быть «закрывающей».
_CLOSE_LABEL_RE = re.compile(r"^(close|cancel|not now|dismiss|no thanks|cerrar|cancelar|×|✕|x)$", re.I)


def forbidden_reason(label: str, nearby: str = "") -> str | None:
    """Причина запрета клика по элементу с такой меткой и таким окружением, иначе None."""
    for text, where in ((label, "label"), (nearby, "nearby")):
        t = str(text or "")
        if not t.strip():
            continue
        m = FORBIDDEN_RE.search(t)
        if m:
            return f"forbidden_{where}:{m.group(0).lower()[:30]}"
    try:
        # scope="ai_rescue": правило «Close» (scope wizard_chrome) не должно
        # мешать закрыть чужое окно; остальные правила yaml действуют.
        why = _wg.forbidden_click_reason(label, scope="ai_rescue")
    except Exception:
        why = None
    if why:
        return f"forbidden_zone:{str(why)[:40]}"
    return None


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
_DIGITS_RE = re.compile(r"\d{4,}")
#: Метка аккаунта: Facebook_9403A, UL921, DQ204, «Account 1234».
_ACCOUNT_RE = re.compile(r"\b(?:Facebook|FB|Account|Acct)[_\s-]*[A-Z]*\d+[A-Za-z]?\b|\b[A-Z]{2,3}\d{3,}[A-Za-z]?\b")
#: Имя кампании байера: KW_TRL_All_Video_Mix_35_05.10 (>= 4 частей через «_»).
_CAMPAIGN_RE = re.compile(r"\b[A-Za-z]{2,}(?:_[A-Za-z0-9.\-]+){3,}")
#: «Evgeni's account», «Evgeni's page».
_POSSESSIVE_RE = re.compile(r"\b[\w.-]+['’]s (?:account|profile|page|business)\b", re.I)


def scrub_text(text: str, names: tuple[str, ...] | list[str] = ()) -> str:
    """Убрать из строки всё, что опознаёт аккаунт, кампанию или человека.

    Порядок важен: сначала адреса и ссылки (в них цифры), потом токены, потом
    номера. `names` — известные имена (страницы, люди), которые нельзя угадать
    регуляркой; имя кампании прогона берётся из телеметрии.
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
    out = _CAMPAIGN_RE.sub("<campaign>", out)
    out = _ACCOUNT_RE.sub("<account>", out)
    out = _POSSESSIVE_RE.sub("<name>", out)
    out = _BLOB_RE.sub("<token>", out)
    out = _PHONE_RE.sub("<phone>", out)
    out = _DIGITS_RE.sub("<digits>", out)
    try:
        out = _run_telemetry.scrub(out)
    except Exception:
        pass
    return re.sub(r"\s+", " ", out).strip()


# ── сводка страницы ─────────────────────────────────────────────────────────

_SUMMARY_JS = r"""
(args) => {
  const {anchor, maxScan} = args;
  for (const e of document.querySelectorAll('[data-acl-ai]')) e.removeAttribute('data-acl-ai');
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
  const ROLES = new Set(['button','link','checkbox','radio','combobox','textbox','tab','switch','menuitem','searchbox']);
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
  const labelOf = (e) => {
    let l = norm(e.getAttribute('aria-label'));
    const lb = e.getAttribute('aria-labelledby');
    if (!l && lb) l = norm(lb.split(/\s+/).map((id) => document.getElementById(id)).filter(Boolean)
      .map((x) => x.textContent).join(' '));
    if (!l && e.labels && e.labels.length) l = norm([...e.labels].map((x) => x.textContent).join(' '));
    if (!l) l = norm(e.textContent);
    if (!l) l = norm(e.getAttribute('title') || e.getAttribute('placeholder') || e.getAttribute('alt'));
    return l.slice(0, 80);
  };
  const dlgTitle = (d) => {
    const h = [...d.querySelectorAll(HEAD)].filter(shown).map((x) => norm(x.textContent)).find(Boolean);
    return (h || norm(d.getAttribute('aria-label')) || norm(d.textContent)).slice(0, 80);
  };
  const leaves = (root, lim) => [...root.querySelectorAll('*')]
    .filter((x) => x.children.length === 0 && shown(x)).map((x) => norm(x.textContent)).filter(Boolean)
    .join(' ').slice(0, lim);
  const sectionOf = (e) => {
    let n = e.parentElement;
    for (let i = 0; n && i < 7; i++, n = n.parentElement) {
      const h = [...n.querySelectorAll(HEAD)].find((x) => !e.contains(x) && shown(x));
      if (h) return norm(h.textContent).slice(0, 60);
    }
    return '';
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
  const dialogs = [...document.querySelectorAll(DLG)].filter(shown);
  const dlgOut = dialogs.slice(0, 5).map((d) => ({title: dlgTitle(d), text: leaves(d, 300)}));
  const headings = [...new Set([...document.querySelectorAll(HEAD)].filter(shown)
    .map((h) => norm(h.textContent).slice(0, 80)).filter(Boolean))].slice(0, 25);
  // якорь шага: самый короткий видимый элемент с таким текстом
  let ay = null, ax = 0;
  if (anchor) {
    const a = anchor.toLowerCase();
    const hits = [...document.querySelectorAll('body *')]
      .filter((x) => shown(x) && norm(x.textContent).toLowerCase().includes(a))
      .sort((p, q) => norm(p.textContent).length - norm(q.textContent).length);
    if (hits.length) {
      const r = hits[0].getBoundingClientRect();
      ay = r.top + r.height / 2; ax = r.left + r.width / 2;
    }
  }
  const out = [];
  let scanned = 0;
  const sel = 'button, a[href], input, select, textarea, [role], [aria-expanded], [contenteditable="true"]';
  for (const e of document.querySelectorAll(sel)) {
    if (scanned >= maxScan) break;
    const role = roleOf(e);
    if (!role || !shown(e)) continue;
    scanned++;
    const r = e.getBoundingClientRect();
    const d = e.closest(DLG);
    const inDialog = !!(d && dialogs.includes(d));
    const dist = ay === null ? r.top : Math.abs(r.top + r.height / 2 - ay) + 0.25 * Math.abs(r.left + r.width / 2 - ax);
    out.push({el: e, role, label: labelOf(e), state: stateOf(e), inDialog, dist,
              dialog: inDialog ? dlgTitle(d) : '', dlgEl: inDialog ? d : null});
  }
  out.sort((p, q) => (q.inDialog - p.inDialog) || (p.dist - q.dist));
  const dlgText = new Map();
  const res = out.map((o, i) => {
    o.el.setAttribute('data-acl-ai', String(i));
    let nearby;
    if (o.dlgEl) {
      if (!dlgText.has(o.dlgEl)) dlgText.set(o.dlgEl, leaves(o.dlgEl, 500));
      nearby = dlgText.get(o.dlgEl);
    } else {
      const p = o.el.parentElement;
      nearby = norm(p ? p.textContent : '').slice(0, 160);
    }
    return {i, role: o.role, label: o.label, state: o.state, inDialog: o.inDialog,
            dialog: o.dialog, section: o.inDialog ? o.dialog : sectionOf(o.el), nearby};
  });
  return {dialogs: dlgOut, headings, elements: res, scanned};
}
"""

_CHECK_JS = r"""
(i) => {
  const hits = document.querySelectorAll('[data-acl-ai="' + i + '"]');
  if (hits.length !== 1) return {found: hits.length};
  const e = hits[0];
  const norm = (s) => (s || '').replace(/[​-‍﻿]/g, '').replace(/\s+/g, ' ').trim();
  const r = e.getBoundingClientRect();
  let vis = r.width > 1 && r.height > 1;
  for (let n = e; vis && n && n.nodeType === 1; n = n.parentElement) {
    const s = getComputedStyle(n);
    if (s.display === 'none' || s.visibility === 'hidden' || s.opacity === '0') vis = false;
  }
  let l = norm(e.getAttribute('aria-label'));
  const lb = e.getAttribute('aria-labelledby');
  if (!l && lb) l = norm(lb.split(/\s+/).map((id) => document.getElementById(id)).filter(Boolean)
    .map((x) => x.textContent).join(' '));
  if (!l && e.labels && e.labels.length) l = norm([...e.labels].map((x) => x.textContent).join(' '));
  if (!l) l = norm(e.textContent);
  if (!l) l = norm(e.getAttribute('title') || e.getAttribute('placeholder') || e.getAttribute('alt'));
  const d = e.closest('[role=dialog], [role=alertdialog], [aria-modal="true"]');
  let nearby;
  if (d) {
    nearby = [...d.querySelectorAll('*')].filter((x) => x.children.length === 0)
      .map((x) => norm(x.textContent)).filter(Boolean).join(' ').slice(0, 500);
  } else {
    const p = e.parentElement;
    nearby = norm(p ? p.textContent : '').slice(0, 160);
  }
  return {found: 1, label: l.slice(0, 80), nearby, visible: vis, inDialog: !!d,
          expanded: e.getAttribute('aria-expanded'), pressed: e.getAttribute('aria-pressed')};
}
"""

_DIALOG_TEXT_JS = r"""
() => {
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
  return [...document.querySelectorAll('[role=dialog], [role=alertdialog], [aria-modal="true"]')]
    .filter((d) => { const r = d.getBoundingClientRect(); return r.width > 1 && r.height > 1; })
    .map((d) => norm(d.textContent).slice(0, 800));
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


def clear_marks(page) -> None:
    try:
        page.evaluate(_CLEAR_JS)
    except Exception:
        pass


def build_summary(page, *, anchor: str = "", names: tuple[str, ...] | list[str] = ()) -> PageSummary:
    """Текстовая сводка видимой части страницы; элементы получают `data-acl-ai=<индекс>`.

    Диалоги и их элементы идут первыми, затем элементы вокруг якоря шага
    (без якоря — сверху вниз). Потолки: MAX_ELEMENTS строк и MAX_CHARS знаков.
    """
    try:
        raw = page.evaluate(_SUMMARY_JS, {"anchor": str(anchor or ""), "maxScan": MAX_SCAN})
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
        kept[int(el["i"])] = {**el, "label": str(el.get("label", "")), "nearby": str(el.get("nearby", ""))}
    truncated = total - len(kept)
    if truncated > 0:
        lines.append(f"... {truncated} more elements not shown")
    text = "\n".join(lines)[:MAX_CHARS]
    return PageSummary(
        text=text, elements=kept, dialogs=list(raw.get("dialogs") or []),
        headings=list(raw.get("headings") or []), truncated=max(0, truncated),
        digest=hashlib.sha256(text.encode("utf-8")).hexdigest()[:12],
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
    """Счётчик одной кампании: 2 вызова на шаг, 4 на кампанию. Передаёт вызывающий."""

    per_step: int = 2
    per_campaign: int = 4
    names: tuple[str, ...] = ()  # известные имена страниц/людей для чистки
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


# ── исполнение ──────────────────────────────────────────────────────────────

def _execute(page, act: dict[str, Any], summary: PageSummary, mode: str) -> tuple[bool, str, str, str]:
    """Проверить выбор и (в act) выполнить. Возвращает (ok, action, сырая метка, причина)."""
    action = act["action"]
    if action == "give_up":
        return False, "give_up", "", act.get("reason") or "model_gave_up"
    if action == "press_escape":
        # Escape в окне вида «Discard changes?» не нажимаем — по тексту любого видимого окна.
        try:
            texts = page.evaluate(_DIALOG_TEXT_JS) or []
        except Exception:
            texts = []
        for t in texts:
            why = forbidden_reason("", t)
            if why:
                return False, "give_up", "", f"escape_blocked:{why}"
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
    label, nearby = el["label"], el["nearby"]
    why = forbidden_reason(label, nearby)
    if why:
        return False, "give_up", label, why
    if action == "close_dialog" and (not el.get("inDialog") or not _CLOSE_LABEL_RE.match(label.strip())):
        return False, "give_up", label, "not_a_dialog_close_button"
    if action == "expand" and not any(x in (el.get("state") or []) for x in ("collapsed", "not-pressed")):
        return False, "give_up", label, "not_collapsed"
    if mode != "act":
        return True, action, label, ""

    # Страница могла измениться между сводкой и действием: метку и окружение
    # читаем заново и сверяем непосредственно перед кликом.
    try:
        cur = page.evaluate(_CHECK_JS, idx)
    except Exception as exc:
        return False, "give_up", label, f"recheck_failed:{type(exc).__name__}"
    if not cur or cur.get("found") != 1:
        return False, "give_up", label, "element_gone_or_ambiguous"
    if not cur.get("visible"):
        return False, "give_up", label, "element_not_visible"
    why = forbidden_reason(cur.get("label", ""), cur.get("nearby", ""))
    if why:
        return False, "give_up", label, f"recheck_{why}"
    if cur.get("label", "") != label:
        return False, "give_up", label, "dom_changed"
    if action == "expand" and cur.get("expanded") != "false" and cur.get("pressed") != "false":
        return False, "give_up", label, "not_collapsed"
    try:
        loc = page.locator(f'[{MARK}="{int(idx)}"]')
        if action == "scroll_to":
            loc.scroll_into_view_if_needed(timeout=3000)
        else:
            loc.click(timeout=3000)
    except Exception as exc:
        return False, action, label, f"action_failed:{type(exc).__name__}"
    return True, action, label, ""


def _prompt(step: str, goal: str, error: str, summary: PageSummary, names) -> str:
    def s(t: str) -> str:
        return scrub_text(t, names)

    return (
        f"Step: {s(step)}\nGoal: {s(goal)}\nLast error: {s(error)[:300]}\n\n"
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
    if client is None:
        client, why = _make_client()
        if client is None:
            log.warning("ai_rescue: skipped (%s) - set ANTHROPIC_API_KEY in the machine .env", why)
            _record(telemetry, None, "skipped", {**base, "reason": why})
            return RescueResult(False, mode, reason=why, skipped=True)

    try:
        summary = build_summary(page, anchor=anchor, names=budget.names)
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
