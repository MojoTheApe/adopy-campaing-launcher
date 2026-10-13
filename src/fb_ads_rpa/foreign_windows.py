"""Чужие окна Meta поверх страницы: узнать, закрыть безопасно, назвать (ACL-1573, срез A).

Во время прогона Meta показывает окна, которых шаг не открывал: панель
Notifications, «Turn off ad blocker», тост «Your internet connection was
lost», экскурсию «1 of 3», диалог шаблона имени («Ad set name — Template»),
опросы. До ACL-1573 каждый шаг знал несколько таких окон сам, а незнакомое
окно молча роняло шаг. Живой пример — ACL-1572 (Doc21, 01.10 308c4f7c8a5a и
04.10 254ee3b3e0c7): диалог шаблона имени «Ad set name» встал поверх
Conversion location ПОСЛЕ единственной проверки `_cancel_naming_template_dialog`,
и все восемь способов выбрать Website видели `menu_closed`, `reopened: 0`.

Здесь — одно место для всех таких окон:

* **реестр** `KNOWN` — вид окна, как его узнать и чем закрыть: своим Close /
  Cancel / крестиком или Escape (а для окон, которые уже умел закрывать
  `meta_dom`, — тем же обработчиком, без изменений: ad blocker, экскурсия,
  шаблон имени, черновики);
* **«свои» окна** (`OWNED`) — мастер креатива, окно цели, загрузка: их
  открывает сам шаг, сторож их не трогает никогда;
* **незнакомое окно** — снимок страницы, строка `foreign_window`, событие
  Sentry с заголовком окна; закрывается только безопасно (кнопка Close /
  Not now / Dismiss / Cancel / × внутри самого окна, иначе Escape) и только
  после отказа шага; затем шаг повторяется один раз. Не закрылось — шаг
  останавливается с причиной «Unknown Meta window blocked the step: <title>».

Никогда: Publish, Confirm, Accept, Submit, Create, Continue, Save, Discard и
любые кнопки футера редактора или вне самого окна. Скрытое окно (например,
предзагруженная панель Notifications с `toggleTargetClosed`, которая есть в
разметке КАЖДОЙ страницы Ads Manager) — не окно поверх страницы.

В телеметрию и Sentry уходят заголовок, роль и data-surface окна — без
номеров, адресов и имён (имя кампании прогона заменяется заглушкой).

Срезы B (обновление страницы и возврат к объекту) и C (контрольные точки и
продолжение) встанут на эти же точки: `after_failure` говорит, что окно не
закрылось, — там и будет следующая ступень.
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

from src.fb_ads_rpa import job_cancel
from src.fb_ads_rpa import run_telemetry as _telemetry

#: Строка телеметрии сторожа.
STEP = "foreign_window"

#: Метка остановки в тексте отказа (`шаг:<причина> (unknown_window)`).
STOP_TOKEN = "unknown_window"

STOP_PREFIX = "Unknown Meta window blocked the step: "


def stop_reason(title: str) -> str:
    """Английская причина остановки шага из-за незнакомого окна."""
    return STOP_PREFIX + (clean_title(title) or "untitled window")


# ── реестр ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class WindowKind:
    """Вид окна: имя, как узнать (регулярки по заголовку/тексту окна), чем закрыть.

    `closer` — имя обработчика: "escape" (своё Close/× окна, иначе Escape),
    "safe" (своё Close / Not now / Dismiss / Cancel / ×, иначе Escape) или
    имя функции `meta_dom`, которая это окно уже закрывала до ACL-1573.
    `owned=True` — окно открывает сам шаг, сторож его не закрывает.
    """

    name: str
    title: str = ""
    text: str = ""
    closer: str = "safe"
    owned: bool = False
    note: str = ""


KNOWN: tuple[WindowKind, ...] = (
    # ACL-1572: панель «Notifications · Unread» (role=dialog,
    # #fbNotificationsFlyout). Своей кнопки закрытия нет — Escape, затем её
    # же переключатель в шапке. Предзагруженная скрытая панель есть на каждой
    # странице; закрывается только показанная.
    WindowKind("notifications", title=r"^Notifications$", closer="escape",
               note="Meta's Notifications panel (Unread switch, Preferences)"),
    # ACL-1313: «Turn off ad blocker» — OK или крестик + перезагрузка.
    WindowKind("ad_blocker", text=r"Turn off ad blocker", closer="_close_ad_blocker_modal",
               note="network/proxy trouble; reload stays in _clear_blocking_overlays"),
    # ACL-1320: экскурсия «A streamlined way to create campaigns» (1 of 3).
    WindowKind("onboarding", text=r"streamlined way to create|create your campaign in a single step"
               r"|take a (quick )?tour|^\d+\s*of\s*\d+$", closer="_close_onboarding_tour"),
    # 8dbafcecb31f / ACL-1572: «Ad set name — Template», только Cancel.
    WindowKind("naming_template", title=r"^(Ad set|Campaign|Ad) name",
               text=r"naming convention|Choose existing template|Add component",
               closer="_cancel_naming_template_dialog"),
    # «Publish draft items?» / «Review draft items» / «Discard drafts?» — Cancel/Close.
    WindowKind("draft_review", text=r"Publish draft|Review draft|draft items|Discard drafts",
               closer="dismiss_publish_draft_dialog"),
    # «Creative setup → Extensions / Site links» (21.09) в реестре нет: это
    # экран мастера креатива, его узнаёт проверка устройства (строка шагов) и
    # считает «своим»; закрывает его по-прежнему шаг текста объявления
    # (`meta_dom._cancel_site_links_modal`, Cancel, не Save). Ревью PR #575.
    # Опросы Meta: только Close / Not now / Dismiss / × — никогда «Submit».
    WindowKind("survey", text=r"how (satisfied|likely|easy|would you rate)|rate your experience"
               r"|take (a|our) (quick |short )?survey|share your feedback|give (us )?feedback"
               r"|tell us what you think", closer="safe"),
    # «Свои» окна шагов: их открывает сам поток.
    # Ревью PR #575 (B2): узнаётся прежде всего по устройству (строка шагов,
    # раздел медиа объявления — см. _SCAN_JS); текст — запасной признак.
    WindowKind("creative_wizard", text=r"Set up your creative|Creative setup|Media library"
               r"|Replace media for|Select media",
               owned=True, note="the ad's media wizard, owned by the media steps"),
    # Ревью PR #575 (R2-3): только по ЗАГОЛОВКУ окна, не по любой строке в
    # нём, и после видов реестра — иначе строка «Loading...» в диалоге
    # шаблона имени делала его «своим», и ACL-1572 возвращался молча.
    WindowKind("objective", title=r"^(Choose a campaign objective|Create new campaign)\b",
               owned=True, note="the Create window, owned by the campaign-open step"),
    WindowKind("loading", title=r"^Loading\b", owned=True,
               note="Meta's loading modal; waited for, not closed"),
)

#: Тост сети — не диалог: строка текста со своим «×» (снимок 20583efb8c41).
NETWORK_TOAST = r"^Your internet connection was lost"

_SAFE_LABEL = r"^(close|not now|dismiss|cancel|cerrar|cancelar|×|✕|x)$"
_UNSAFE_LABEL = (r"publish|confirm|accept|submit|create|continue|save|discard|delete|next|done"
                 r"|apply|agree|allow|get started|start|turn on|try it|ok\b|yes\b|remove|send|pay")


def _kinds_js() -> list[dict[str, Any]]:
    return [{"name": k.name, "title": k.title, "text": k.text, "owned": k.owned} for k in KNOWN]


_SCAN_JS = r"""
(args) => {
  const {kinds, safe, unsafe, toast} = args;
  const SAFE = new RegExp(safe, 'i');
  const UNSAFE = new RegExp(unsafe, 'i');
  const TOAST = new RegExp(toast, 'i');
  const norm = (s) => (s || '').replace(/[​-‍﻿]/g, '').replace(/\s+/g, ' ').trim();
  const shown = (e) => {
    if (!e || !e.getBoundingClientRect) return false;
    const r = e.getBoundingClientRect();
    if (r.width <= 1 || r.height <= 1) return false;
    if (r.bottom <= 0 || r.right <= 0 || r.top >= innerHeight || r.left >= innerWidth) return false;
    for (let n = e; n && n.nodeType === 1; n = n.parentElement) {
      const s = getComputedStyle(n);
      if (s.display === 'none' || s.visibility === 'hidden' || s.opacity === '0') return false;
      // Предзагруженная скрытая панель Meta (toggle-флайаут): закрыта.
      if (n.classList && n.classList.contains('toggleTargetClosed')) return false;
      if (n.hasAttribute('hidden') || n.hasAttribute('inert')) return false;
    }
    return true;
  };
  for (const e of document.querySelectorAll('[data-acl-fw], [data-acl-fw-close]')) {
    e.removeAttribute('data-acl-fw'); e.removeAttribute('data-acl-fw-close');
  }
  const target = document.querySelector('[data-acl-fw-target]');
  const SEL = '[role=dialog], [role=alertdialog], [aria-modal="true"]';
  // Сам редактор (поля имён, раздел /am/editor*, футер с Publish) — не чужое окно.
  const EDITOR = 'input[placeholder^="Enter your campaign name"], input[placeholder^="Enter your ad set name"],'
    + ' input[placeholder^="Enter your ad name"], [data-surface*="editor_drawer_footer"],'
    + ' [data-surface*="simple_create_publish"], [data-surface="/am/editor_drawer"],'
    + ' [data-surface$="editor:campaign"], [data-surface$="editor:ad_set"], [data-surface$="editor:ad"]';
  const FOOTER = /footer|publish/i;
  const out = [];
  const all = [...document.querySelectorAll(SEL)];
  for (const d of all) {
    if (!shown(d)) continue;
    if (d.querySelector(EDITOR)) continue;
    // Вложенное окно учитываем само по себе; внешнее, если внутри только оно, — тоже.
    const own = (e) => e.closest(SEL) === d;
    if (target && d.contains(target)) continue;
    const heading = [...d.querySelectorAll('[role=heading], h1, h2, h3, h4')]
      .filter((e) => own(e) && shown(e)).map((e) => norm(e.textContent)).find((t) => t) || '';
    let label = norm(d.getAttribute('aria-label'));
    const lb = d.getAttribute('aria-labelledby');
    if (!label && lb) {
      label = lb.split(/\s+/).map((id) => document.getElementById(id))
        .filter(Boolean).map((e) => norm(e.textContent)).join(' ');
    }
    const leaves = [...d.querySelectorAll('*')]
      .filter((e) => e.children.length === 0 && own(e) && shown(e)).map((e) => norm(e.textContent)).filter(Boolean);
    const text = leaves.join(' ').slice(0, 600);
    const title = (heading || label || text).slice(0, 80);
    let kind = 'unknown', owned = false;
    // Ревью PR #575 (B2): мастер креатива и его окно Replace узнаются по
    // устройству, а не по заголовку экрана (Text, Trim video, Select media
    // crop…, Advantage+ creative enhancements, Replace media for…): строка
    // шагов мастера (aria-current="step" / подписи шагов) или раздел медиа
    // объявления. «Свои» виды — ПЕРВЫМИ: их не перехватит вид реестра.
    const STEP = /^(Media|Trim|Crop|Text|Creative generation|Image generation|Creative setup|Enhancements|Translation)$/i;
    const steps = new Set([...d.querySelectorAll('[role=button], [role=listitem], [aria-current="step"]')]
      .map((e) => norm(e.textContent)).filter((t) => STEP.test(t)).map((t) => t.toLowerCase()));
    const MEDIA_SURFACE = /ad_image_video_selector_section|creative-tool-asset/;
    const hostEl = d.closest('[data-surface]');
    const hostSurface = hostEl ? (hostEl.getAttribute('data-surface') || '') : '';
    const mediaSurface = MEDIA_SURFACE.test(hostSurface)
      || [...d.querySelectorAll('[data-surface]')].some((e) => MEDIA_SURFACE.test(e.getAttribute('data-surface') || ''));
    const wizard = (steps.size >= 3) || (steps.size >= 2 && !!d.querySelector('[aria-current="step"]'))
      || mediaSurface || /^(Replace media for|Select media)/i.test(title);
    if (wizard) { kind = 'creative_wizard'; owned = true; }
    // Порядок (ревью PR #575, R2-3): устройство мастера (выше) — первым;
    // затем виды реестра; «свои» виды по тексту/заголовку — последними.
    const ordered = [...kinds.filter((k) => !k.owned), ...kinds.filter((k) => k.owned)];
    for (const k of (owned ? [] : ordered)) {
      const tOk = k.title ? new RegExp(k.title, 'i').test(title) : null;
      const xOk = k.text ? (new RegExp(k.text, 'i').test(text) || leaves.some((l) => new RegExp(k.text, 'i').test(l))) : null;
      const ok = (tOk === null || tOk) && (xOk === null || xOk) && (tOk !== null || xOk !== null);
      if (ok) { kind = k.name; owned = !!k.owned; break; }
    }
    if (kind === 'unknown' && d.id === 'fbNotificationsFlyout') kind = 'notifications';
    if (kind === 'unknown' && d.querySelector('[role=switch][aria-label="Unread"]')
        && /^Notifications$/i.test(heading)) kind = 'notifications';
    const buttons = [...d.querySelectorAll('[role=button], button, [aria-label]')]
      .filter((b) => own(b) && shown(b) && (b.matches('[role=button], button') || /^(close|dismiss)$/i.test(b.getAttribute('aria-label') || '')));
    const names = [];
    let close = null, rank = 99;
    for (const b of buttons) {
      const t = norm(b.textContent);
      const a = norm(b.getAttribute('aria-label'));
      const name = t || a;
      if (name && names.length < 8 && !names.includes(name)) names.push(name.slice(0, 40));
      // Кнопка футера/Publish — даже внутри окна не наша.
      let inFooter = false;
      for (let n = b; n && n !== d; n = n.parentElement) {
        if (FOOTER.test(n.getAttribute && (n.getAttribute('data-surface') || ''))) { inFooter = true; break; }
      }
      if (inFooter) continue;
      if (UNSAFE.test(t) || UNSAFE.test(a)) continue;
      const okName = SAFE.test(t) || ((!t || /^[×✕]$/.test(t)) && SAFE.test(a));
      if (!okName) continue;
      const r = /^(close|cerrar|×|✕|x)$/i.test(t || a) ? 0 : /^not now$/i.test(t || a) ? 1
        : /^dismiss$/i.test(t || a) ? 2 : 3;
      if (r < rank) { rank = r; close = b; }
    }
    const i = out.length;
    d.setAttribute('data-acl-fw', String(i));
    if (close) close.setAttribute('data-acl-fw-close', String(i));
    const host = d.closest('[data-surface]') || d.querySelector('[data-surface]');
    out.push({
      i, kind, owned, title, role: d.getAttribute('role') || (d.getAttribute('aria-modal') ? 'modal' : ''),
      surface: host ? (host.getAttribute('data-surface') || '').slice(0, 120) : '',
      close: close ? (norm(close.textContent) || norm(close.getAttribute('aria-label'))).slice(0, 20) : '',
      buttons: names,
    });
  }
  // Тост сети: строка с собственным «×» (role=button без подписи).
  for (const e of document.querySelectorAll('span, div')) {
    const ownText = norm([...e.childNodes].filter((n) => n.nodeType === 3).map((n) => n.textContent).join(' '));
    if (!ownText || !TOAST.test(ownText) || !shown(e)) continue;
    const x = [...e.querySelectorAll('[role=button], button')].find((b) => shown(b)
      && (/^[×✕]$/.test(norm(b.textContent)) || /^(close|dismiss)$/i.test(b.getAttribute('aria-label') || '')));
    const i = out.length;
    e.setAttribute('data-acl-fw', String(i));
    if (x) x.setAttribute('data-acl-fw-close', String(i));
    out.push({i, kind: 'network_toast', owned: false, title: ownText.slice(0, 80), role: 'status',
              surface: '', close: x ? '×' : '', buttons: x ? ['×'] : []});
    break;
  }
  return out;
}
"""

_GONE_JS = r"""
(i) => {
  const e = document.querySelector('[data-acl-fw="' + i + '"]');
  if (!e) return true;
  const r = e.getBoundingClientRect();
  if (r.width <= 1 || r.height <= 1) return true;
  for (let n = e; n && n.nodeType === 1; n = n.parentElement) {
    const s = getComputedStyle(n);
    if (s.display === 'none' || s.visibility === 'hidden') return true;
    if (n.classList && n.classList.contains('toggleTargetClosed')) return true;
  }
  return false;
}
"""

_FOCUS_JS = r"""
(i) => {
  const e = document.querySelector('[data-acl-fw="' + i + '"]');
  if (!e) return false;
  if (!e.hasAttribute('tabindex')) e.setAttribute('tabindex', '-1');
  e.focus();
  return document.activeElement === e || e.contains(document.activeElement);
}
"""

#: Переключатель панели Notifications — её собственный (шапка Meta).
_NOTIFICATIONS_TOGGLE_JS = r"""
() => {
  const t = document.querySelector('[data-target="fbNotificationsFlyout"], a[name="notifications"]');
  if (!t) return false;
  t.click();
  return true;
}
"""

_CLEAR_MARKS_JS = ("() => document.querySelectorAll('[data-acl-fw], [data-acl-fw-close], [data-acl-fw-target]')"
                   ".forEach(e => { e.removeAttribute('data-acl-fw'); e.removeAttribute('data-acl-fw-close');"
                   " e.removeAttribute('data-acl-fw-target'); })")


@dataclass
class Window:
    index: int
    kind: str
    title: str
    role: str = ""
    surface: str = ""
    close: str = ""
    buttons: list[str] = field(default_factory=list)
    owned: bool = False

    @property
    def known(self) -> bool:
        return self.kind != "unknown"

    def diag(self, where: str) -> dict[str, Any]:
        return {"where": where, "kind": self.kind, "title": clean_title(self.title), "role": self.role,
                "surface": clean_surface(self.surface), "close": self.close,
                "buttons": [clean_title(b) for b in self.buttons][:8]}


# ── чистка того, что уходит наружу ──────────────────────────────────────────

_URL_RE = re.compile(r"(https?://\S+|www\.\S+|\b[\w.-]+\.(com|net|org|io)\b\S*)", re.I)
_DIGITS_RE = re.compile(r"\d{5,}")
_ACT_RE = re.compile(r"act[=_]\S+", re.I)
_EMAIL_RE = re.compile(r"\S+@\S+")


def clean_title(text: str) -> str:
    """Заголовок окна без адресов, номеров и имени кампании прогона."""
    t = str(text or "")
    try:
        campaign = _telemetry.current_campaign()
    except Exception:
        campaign = ""
    if campaign and campaign in t:
        t = t.replace(campaign, "<campaign>")
    t = _EMAIL_RE.sub("<email>", t)
    t = _URL_RE.sub("<url>", t)
    t = _ACT_RE.sub("act=<id>", t)
    t = _DIGITS_RE.sub("<digits>", t)
    return re.sub(r"\s+", " ", t).strip()[:80]


def clean_surface(surface: str) -> str:
    """data-surface без номеров и личного (`business_scope:personal:<id>:<имя>`)."""
    s = re.sub(r"business_scope:[^/]*", "business_scope", str(surface or ""))
    return _DIGITS_RE.sub("<digits>", s)[:120]


# ── состояние прогона ──────────────────────────────────────────────────────

_state = threading.local()


def _st() -> dict[str, Any]:
    st = getattr(_state, "v", None)
    if st is None:
        st = {"reported": set(), "fallback_used": set(), "blocked": None}
        _state.v = st
    return st


def reset() -> None:
    """Начало прогона кампании: счётчики сторожа — заново."""
    _state.v = {"reported": set(), "fallback_used": set(), "blocked": None}


def blocked() -> dict[str, str] | None:
    """Незнакомое окно, которое не закрылось и не дало шагу пройти (или None)."""
    return _st().get("blocked")


def step_ok(where: str = "") -> None:
    """Шаг прошёл — прежняя остановка из-за окна к нему уже не относится."""
    _st()["blocked"] = None


def attach_block(fail_step: str) -> str:
    """К отказу потока — причина, если шаг остановило незнакомое окно.

    `conversion_website` → `conversion_website:Unknown Meta window blocked the
    step: <title> (unknown_window)`. Имя шага (до «:») не меняется — политика
    повторов и отказов читает его как прежде.
    """
    got = blocked()
    if not fail_step or not got or ":" in fail_step:
        return fail_step
    return f"{fail_step}:{stop_reason(got.get('title', ''))} ({STOP_TOKEN})"


# ── чтение страницы ────────────────────────────────────────────────────────


def _mark_target(target) -> None:
    if target is None:
        return
    try:
        target.first.evaluate("e => e.setAttribute('data-acl-fw-target', '1')", timeout=300)
    except Exception:
        pass


def scan(page, *, target=None, timeout_s: float = 2.0) -> list[Window]:
    """Окна поверх страницы, которых не открывал шаг (`target` — его элемент).

    Чтение ограниченное (`meta_dom._eval_bounded`): на зависшей вкладке —
    пустой список, а не ожидание.
    """
    from src.fb_ads_rpa import meta_dom as md

    _mark_target(target)
    try:
        got = md._eval_bounded(page, _SCAN_JS, {"kinds": _kinds_js(), "safe": _SAFE_LABEL,
                                               "unsafe": _UNSAFE_LABEL, "toast": NETWORK_TOAST},
                               timeout_s=timeout_s)
    except Exception:
        return []
    out: list[Window] = []
    for w in got or []:
        if not isinstance(w, dict):
            continue
        out.append(Window(index=int(w.get("i", 0)), kind=str(w.get("kind") or "unknown"),
                          title=str(w.get("title") or ""), role=str(w.get("role") or ""),
                          surface=str(w.get("surface") or ""), close=str(w.get("close") or ""),
                          buttons=[str(b) for b in (w.get("buttons") or [])], owned=bool(w.get("owned"))))
    return out


def _clear_marks(page) -> None:
    job_cancel.check()  # ACL-1614: после Stop страницу не трогаем
    try:
        page.evaluate(_CLEAR_MARKS_JS)
    except Exception:
        pass


def _gone(page, w: Window) -> bool:
    try:
        return bool(page.evaluate(_GONE_JS, w.index))
    except Exception:
        return False


# ── закрытие ───────────────────────────────────────────────────────────────


def _press_own_close(page, w: Window) -> bool:
    """Нажать СОБСТВЕННУЮ безопасную кнопку окна (помечена при чтении)."""
    if not w.close:
        return False
    from src.fb_ads_rpa import meta_dom as md

    return md._click_close_mark(page, f'[data-acl-fw-close="{w.index}"]')


def _escape(page, w: Window) -> bool:
    """Escape — в само окно: фокус на нём, чтобы клавиша не ушла редактору."""
    try:
        page.evaluate(_FOCUS_JS, w.index)
    except Exception:
        pass
    try:
        page.keyboard.press("Escape")
        return True
    except Exception:
        return False


def _settle() -> None:
    from src.fb_ads_rpa import meta_dom as md

    md._sleep_h(0.5)


def _close_safely(page, w: Window) -> str:
    """Своя Close / Not now / Dismiss / Cancel / ×, иначе Escape. Чем закрыли или ""."""
    if w.owned:
        # Окно шага (мастер креатива, окно цели, загрузка) — никогда.
        return ""
    if _press_own_close(page, w):
        _settle()
        if _gone(page, w):
            return "own_close"
    if _escape(page, w):
        _settle()
        if _gone(page, w):
            return "escape"
    return ""


def _close_known(page, w: Window, where: str) -> str:
    """Закрыть окно из реестра его обработчиком. Чем закрыли или ""."""
    from src.fb_ads_rpa import meta_dom as md

    kind = next((k for k in KNOWN if k.name == w.kind), None)
    if w.kind == "network_toast":
        if _press_own_close(page, w):
            _settle()
            if _gone(page, w):
                return "own_close"
        return ""
    if kind is None or kind.owned:
        return ""
    if kind.closer == "escape":
        how = _close_safely(page, w)
        if not how and w.kind == "notifications":
            try:
                if page.evaluate(_NOTIFICATIONS_TOGGLE_JS):
                    _settle()
                    if _gone(page, w):
                        return "own_toggle"
            except Exception:
                pass
        return how
    if kind.closer == "safe":
        return _close_safely(page, w)
    if w.kind == "ad_blocker":
        # Как в `_clear_blocking_overlays` (OK или крестик, строка
        # `overlays_cleared network_trouble`), но без перезагрузки посреди
        # шага: перезагрузка остаётся в начале разделов (срез B — потом).
        try:
            got = md._clear_blocking_overlays(page, where, editor=md._on_editor(page), may_reload=False)
        except Exception:
            got = []
        return "ok_or_x" if any(str(c).startswith("ad_blocker_modal") for c in got) else ""
    fn: Callable | None = getattr(md, kind.closer, None)
    if fn is None:
        return ""
    try:
        got = fn(page, where) if kind.closer == "_close_onboarding_tour" else fn(page)
    except Exception:
        got = None
    if got:
        return kind.closer.strip("_")
    return ""


def _record(ok: bool | None, w: Window, where: str, action: str, **extra) -> None:
    try:
        _telemetry.record("foreign_window", ok, note=f"{w.kind}:{action}", diag={**w.diag(where), "action": action, **extra})
    except Exception:
        pass


def _report_unknown(page, w: Window, where: str) -> None:
    """Незнакомое окно: снимок страницы и событие Sentry — раз на заголовок за прогон."""
    title = clean_title(w.title)
    key = (title, w.role)
    st = _st()
    if key in st["reported"]:
        return
    st["reported"].add(key)
    diag = w.diag(where)
    try:
        from src.fb_ads_rpa import page_capture

        saved = page_capture.capture(page, step=STEP, diag=diag, timeout_ms=3000,
                                     quota=STEP, quota_max=3)
        if saved:
            diag["page"] = saved
    except Exception:
        pass
    try:
        from src import acl_sentry

        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40] or "untitled"
        acl_sentry.report_step_failure(
            STEP, f"{STOP_TOKEN}: {title}", run_id=_telemetry.current_run_id(),
            campaign=_telemetry.current_campaign(), diag=diag, reason="unknown-window",
            job=_telemetry.current_job_id(), kind=STEP, group_step=f"{STEP}:{slug}")
    except Exception:
        pass


#: Сколько окон подряд сторож разбирает за один вызов (каждое — новое чтение:
#: обработчик реестра читает страницу сам и сбивает пометки предыдущего).
MAX_WINDOWS_PER_CALL = 4


def _next_window(page, handled: set, *, target=None, only=None) -> Window | None:
    for w in scan(page, target=target):
        if w.owned or (only is not None and w.kind not in only):
            continue
        if (w.kind, clean_title(w.title)) in handled:
            continue
        return w
    return None


def _refresh(page, w: Window, *, target=None) -> Window:
    """То же окно после нового чтения (пометки `data-acl-fw` обновлены); нет — прежнее."""
    key = (w.kind, clean_title(w.title))
    return next((x for x in scan(page, target=target) if (x.kind, clean_title(x.title)) == key), w)


def before_step(page, where: str, *, target=None, only: tuple[str, ...] | None = None) -> list[str]:
    """Перед шагом: закрыть окна из реестра; о незнакомых — только сказать.

    Незнакомое окно до шага не закрывается: оно может быть частью шага, а
    закрытие — только когда шаг из-за него не прошёл (`after_failure`).
    Возвращает, что закрыли (`<вид>:<чем>`). `only` — закрывать только эти
    виды (из `_clear_blocking_overlays`: остальное он закрывает сам).
    """
    # ACL-1614: перед шагом — Stop слышен до первого обращения к странице
    # (05.10 поток зависал именно в `_clear_marks` отсюда).
    job_cancel.check()
    closed: list[str] = []
    handled: set = set()
    try:
        for _ in range(MAX_WINDOWS_PER_CALL):
            w = _next_window(page, handled, target=target, only=only)
            if w is None:
                break
            handled.add((w.kind, clean_title(w.title)))
            if not w.known:
                key = ("seen", clean_title(w.title), where)
                if key not in _st()["reported"]:
                    _st()["reported"].add(key)
                    _record(None, w, where, "seen")
                continue
            how = _close_known(page, w, where)
            _record(bool(how), w, where, how or "not_closed")
            if how:
                closed.append(f"{w.kind}:{how}")
    finally:
        _clear_marks(page)
    return closed


def after_failure(page, where: str, *, target=None) -> bool:
    """Шаг не прошёл: закрыть то, что над ним. True — что-то закрыли (повторить шаг).

    Не больше одного раза на шаг (`where`) за прогон. Незнакомое окно —
    снимок, Sentry, затем только безопасное закрытие; не закрылось — шаг
    помечается остановленным (`blocked`, `attach_block`).
    """
    st = _st()
    if where in st["fallback_used"]:
        return False
    handled: set = set()
    closed_any = False
    try:
        for _ in range(MAX_WINDOWS_PER_CALL):
            w = _next_window(page, handled, target=target)
            if w is None:
                break
            st["fallback_used"].add(where)
            handled.add((w.kind, clean_title(w.title)))
            if w.known:
                how = _close_known(page, w, where)
                if not how and w.kind != "network_toast":
                    # Обработчик реестра не справился — безопасное закрытие.
                    w = _refresh(page, w, target=target)
                    how = _close_safely(page, w)
                _record(bool(how), w, where, how or "not_closed", fallback=True)
                closed_any = closed_any or bool(how)
                continue
            _report_unknown(page, w, where)
            # Снимок мог прочитать страницу заново — пометки окна обновить.
            w = _refresh(page, w, target=target)
            how = _close_safely(page, w)
            if how:
                _record(True, w, where, how, fallback=True)
                closed_any = True
            else:
                st["blocked"] = {"where": where, "title": clean_title(w.title)}
                _record(False, w, where, "blocked", fallback=True,
                        reason=stop_reason(w.title))
    finally:
        _clear_marks(page)
    return closed_any


def guarded(page, where: str, attempt: Callable[[], bool], *, target=None) -> bool:
    """Шаг под сторожем: окна реестра — до шага; отказ — закрыть и повторить один раз."""
    # До шага — без пометки цели: окна реестра не бывают частью шага, а
    # пометка ждала бы ещё не появившийся элемент на каждом нажатии.
    try:
        before_step(page, where)
    except Exception:
        pass
    ok = bool(attempt())
    if not ok:
        try:
            retry = after_failure(page, where, target=target)
        except Exception:
            retry = False
        if retry:
            ok = bool(attempt())
    if ok:
        step_ok(where)
    return ok
