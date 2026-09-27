"""Новый редактор Meta «в один экран» — `/adsmanager/manage/simple_create` (ACL-1327).

Живые прогоны 27.09 (Facebook_9943, Facebook_1443; снимки 74c3b477e021,
c98a1c094dfc, 9e7d67471c6b, a8b5c54c328b): после Create → Leads → Continue
Meta открывает не прежний мастер с Next между уровнями, а одну длинную
страницу. Кампания, группа и объявление — три раздела подряд, у каждого
заголовок второго уровня («Campaign», «Ad set», «Ad») и своя обёртка
`data-surface="/am/editor:campaign" | "/am/editor:ad_set" | "/am/editor:ad"`;
слева то же дерево «New Leads Campaign / New Leads Ad Set / New Leads Ad»,
внизу «All edits saved» и Publish.

Карточки внутри разделов те же, что в прежнем мастере (имя кампании, бюджет,
Special Ad Categories, Conversion location, Audience в раскладке «Suggest an
audience», Placements, имя объявления, креатив, Destination), и поля у них те
же (`Enter your campaign name here...`, `Please enter amount`, …). Разница в
двух вещах:

* **Навигация.** Next между уровнями нет — до раздела доходят прокруткой.
* **Свёрнутые карточки.** Карточка показывает сводку («Campaign details —
  Buying type Auction, Campaign objective Leads», «Audience — Locations United
  States, Age 18 - 65+ …»): шапка `role=toolbar` с заголовком и кнопка
  `role=button aria-pressed=false aria-labelledby=<id шапки>`, а поля лежат в
  `<div hidden>`. Прежние обработчики ищут видимые поля — сначала карточку
  надо раскрыть (`open_card`).

Здесь — только чтение и действия над этой разметкой; порядок шагов и
политики отказов остаются в `meta_dom.run_recorded_campaign_flow`.
Разметка проверена по вырезке `tests/fixtures/ads_manager/simple_create_2026_09_27.html`.
"""
from __future__ import annotations

import os
import time

#: Адрес нового редактора (в пути, не в параметрах).
SIMPLE_CREATE_PATH = "/adsmanager/manage/simple_create"

#: Уровни страницы: обёртка раздела и заголовок второго уровня над ним.
LEVELS = {
    "campaign": ("/am/editor:campaign", "Campaign"),
    "adset": ("/am/editor:ad_set", "Ad set"),
    "ad": ("/am/editor:ad", "Ad"),
}

#: Поле имени уровня — по нему видно, что раздел не только есть, но и отрисован.
NAME_FIELDS = {
    "campaign": "Enter your campaign name here",
    "adset": "Enter your ad set name here...",
    "ad": "Enter your ad name here...",
}

#: Откат к прежнему поведению (остановка `simple_create_layout` сразу):
#: FB_RPA_SIMPLE_CREATE=0.
_OFF_VALUES = ("0", "off", "false", "no")


def disabled() -> bool:
    """Путь нового редактора выключен переменной окружения."""
    return os.environ.get("FB_RPA_SIMPLE_CREATE", "").strip().lower() in _OFF_VALUES


def url_is_simple_create(url) -> bool:
    """Адрес — новый редактор (путь, а не `next=` в параметрах списка)."""
    if not isinstance(url, str) or not url:
        return False
    return SIMPLE_CREATE_PATH in url.split("?", 1)[0]


_MARKERS_JS = r"""
  () => {
    const has = (s) => !!document.querySelector('[data-surface="' + s + '"]');
    return {
      campaign: has('/am/editor:campaign'),
      adset: has('/am/editor:ad_set'),
      ad: has('/am/editor:ad'),
      publish: has('/am/lib:syd_simple_create_publish'),
    };
  }
"""
"""Приметы нового редактора в разметке. В прежнем мастере разделов
`/am/editor:*` нет вовсе (там `/am/editor_drawer/...`), а футер Publish
нового редактора подписан `syd_simple_create_publish` — он есть уже на
странице-заготовке, пока разделы ещё грузятся (74c3b477e021, c98a1c094dfc)."""


def _md():
    # Ленивая ссылка: meta_dom сам импортирует этот модуль.
    from src.fb_ads_rpa import meta_dom

    return meta_dom


def page_markers(page) -> dict:
    """Приметы нового редактора на странице; {} — спросить не удалось."""
    try:
        got = _md()._eval_bounded(page, _MARKERS_JS)
    except Exception:
        return {}
    return got if isinstance(got, dict) else {}


def is_simple_create(page) -> bool:
    """Открыт новый редактор — по адресу или по разметке.

    9e7d67471c6b (27.09): проверка по одному адресу после Continue промахнулась
    (адрес сменился позже), и поток 46 с искал Next, которого на этой странице
    нет. Разметка отвечает в любой момент: разделы кампании и группы на одной
    странице или футер Publish нового редактора.
    """
    try:
        url = page.url
    except Exception:
        url = ""
    if url_is_simple_create(url):
        return True
    got = page_markers(page)
    return (got.get("campaign") is True and got.get("adset") is True) or got.get("publish") is True


# ── Разделы уровней ──────────────────────────────────────────────────────

_LEVEL_JS = r"""
  ([surface, title]) => {
    const norm = (s) => (s || '').replace(/[\s​]+/g, ' ').trim();
    if (!document.querySelector('[data-surface="' + surface + '"]')) return { found: false, why: 'no_section' };
    const head = [...document.querySelectorAll('[role=heading][aria-level="2"], h2')]
      .find(h => norm(h.textContent) === title);
    if (!head) return { found: false, why: 'no_heading' };
    head.scrollIntoView({ block: 'start', behavior: 'instant' });
    return { found: true, y: head.getBoundingClientRect().y };
  }
"""
"""Заголовок уровня («Campaign» / «Ad set» / «Ad») — прокрутить к нему."""


def scroll_to_level(page, level: str, *, budget_s: float = 20.0) -> dict:
    """Дойти до раздела уровня прокруткой — вместо Next прежнего мастера.

    Успех — не прокрутка сама по себе, а видимое поле имени этого уровня:
    раздел отрисован, и дальше работают прежние обработчики карточек.
    Возвращает {"ok": bool, "why": …} — причина отказа уходит в телеметрию.
    """
    md = _md()
    surface, title = LEVELS[level]
    field = page.get_by_placeholder(NAME_FIELDS[level]).first
    deadline = time.time() + budget_s
    last: dict = {}
    while True:
        try:
            last = md._eval_bounded(page, _LEVEL_JS, [surface, title]) or {}
        except Exception as exc:
            last = {"found": False, "why": "error:" + type(exc).__name__}
        if last.get("found"):
            try:
                field.wait_for(state="visible", timeout=3000)
                field.scroll_into_view_if_needed(timeout=3000)
                return {"ok": True}
            except Exception:
                last = {"found": True, "why": "name_field_hidden"}
        if time.time() >= deadline:
            return {"ok": False, "why": last.get("why", "unknown")}
        md._sleep_h(0.8)


# ── Свёрнутые карточки ───────────────────────────────────────────────────

_CARD_JS = r"""
  (title) => {
    const norm = (s) => (s || '').replace(/[\s​]+/g, ' ').trim();
    // Ревью PR #340 (F1): метки прошлой карточки снимаются обе — иначе
    // запасной клик нашёл бы кнопку прежней (уже раскрытой) карточки.
    document.querySelectorAll('[data-acl-card-body], [data-acl-card-button]').forEach(e => {
      e.removeAttribute('data-acl-card-body'); e.removeAttribute('data-acl-card-button'); });
    const heads = [...document.querySelectorAll('[role=toolbar] [role=heading]')]
      .filter(h => norm(h.textContent) === title);
    for (const h of heads) {
      const bar = h.closest('[role=toolbar]');
      if (!bar || !bar.id) continue;
      const btns = [...document.querySelectorAll('[role=button][aria-pressed]')]
        .filter(b => (b.getAttribute('aria-labelledby') || '').split(/\s+/).includes(bar.id));
      if (!btns.length) continue;
      const pressed = btns.some(b => b.getAttribute('aria-pressed') === 'true');
      // Тело карточки — первый скрытый блок в общем предке кнопок сводки
      // (шапка и сводка — разные кнопки на разной глубине).
      const anc = (e) => { const a = []; for (let n = e; n; n = n.parentElement) a.push(n); return a; };
      let common = anc(btns[0]);
      for (const b of btns.slice(1)) { const s = new Set(anc(b)); common = common.filter(n => s.has(n)); }
      const root = btns.length > 1 ? common[0] : btns[0].parentElement;
      const body = root && [...root.querySelectorAll('[hidden]')].find(e => !btns.some(b => b.contains(e)));
      if (pressed) return { found: true, pressed: true };
      if (body) body.setAttribute('data-acl-card-body', '1');
      const btn = btns[0];
      btn.scrollIntoView({ block: 'center', behavior: 'instant' });
      const r = bar.getBoundingClientRect();
      const x = r.x + Math.min(r.width / 2, 120), y = r.y + r.height / 2;
      const hit = document.elementFromPoint(x, y);
      btn.setAttribute('data-acl-card-button', bar.id);
      return { found: true, pressed: false, x, y, hit: !!hit && btn.contains(hit), body: !!body, bar: bar.id };
    }
    return { found: false };
  }
"""
"""Карточка по заголовку: нажата ли её кнопка-сводка и куда жать, чтобы раскрыть."""

_CARD_OPEN_JS = r"""
  (title) => {
    const norm = (s) => (s || '').replace(/[\s​]+/g, ' ').trim();
    const body = document.querySelector('[data-acl-card-body]');
    if (body && !body.hidden && body.isConnected) return true;
    const heads = [...document.querySelectorAll('[role=toolbar] [role=heading]')]
      .filter(h => norm(h.textContent) === title);
    return heads.some(h => {
      const bar = h.closest('[role=toolbar]');
      return !!bar && bar.id && [...document.querySelectorAll('[role=button][aria-pressed="true"]')]
        .some(b => (b.getAttribute('aria-labelledby') || '').split(/\s+/).includes(bar.id));
    });
  }
"""


_CARD_DOM_CLICK_JS = r"""
  (bar) => {
    if (!bar) return false;
    const b = [...document.querySelectorAll('[data-acl-card-button]')]
      .find(e => e.getAttribute('data-acl-card-button') === bar
                 && (e.getAttribute('aria-labelledby') || '').split(/\s+/).includes(bar));
    if (!b) return false;
    b.click();
    return true;
  }
"""
"""DOM-клик по кнопке-сводке карточки с шапкой `bar` — и ни по какой другой."""


def _card_open(page, title: str, wait_s: float) -> bool:
    md = _md()
    for _ in range(max(1, int(wait_s / 0.25))):
        try:
            if md._eval_bounded(page, _CARD_OPEN_JS, title) is True:
                return True
        except Exception:
            return False
        time.sleep(0.25)
    return False


def open_card(page, title: str) -> str:
    """Раскрыть свёрнутую карточку нового редактора по её заголовку.

    "absent" — такой карточки-сводки нет (прежний мастер или карточка без
    сворачивания — обработчик работает как раньше); "open" — уже раскрыта;
    "opened" — раскрыли; "failed" — не раскрылась ни доверенным кликом по
    шапке, ни DOM-кликом по кнопке. Строка телеметрии `simple_card`.
    """
    md = _md()
    try:
        state = md._eval_bounded(page, _CARD_JS, title) or {}
    except Exception as exc:
        state = {"found": False, "error": type(exc).__name__}
    if not state.get("found"):
        return "absent"
    if state.get("pressed"):
        return "open"
    how = ""
    if state.get("hit"):
        md._cdp_mouse_click(page, state["x"], state["y"])
        if _card_open(page, title, 3.0):
            how = "cdp"
    if not how:
        # Запасной DOM-клик — только по кнопке этой карточки: метка несёт id
        # её шапки, и он сверяется с подписью кнопки.
        try:
            md._eval_bounded(page, _CARD_DOM_CLICK_JS, state.get("bar") or "")
        except Exception:
            pass
        if _card_open(page, title, 3.0):
            how = "dom"
    result = "opened" if how else "failed"
    md._telemetry.record("simple_card", bool(how), note=f"{title}:{result}",
                         diag={"how": how, "hit": bool(state.get("hit"))})
    return result


# ── Цель кампании ────────────────────────────────────────────────────────

_OBJECTIVE_JS = r"""
  () => {
    const opts = [...document.querySelectorAll('[data-surface*="objective-selector-option-"]')];
    if (!opts.length) return { found: false };
    const on = (o) => {
      const r = o.querySelector('input[type=radio], [role=radio]');
      return !!r && (r.checked === true || r.getAttribute('aria-checked') === 'true');
    };
    const name = (o) => (o.getAttribute('data-surface') || '').split('objective-selector-option-').pop();
    const leads = opts.find(o => name(o) === 'outcome_leads');
    const checked = opts.filter(on).map(name);
    return { found: true, has_leads: !!leads, leads: !!leads && on(leads), checked };
  }
"""
"""Список целей в «Campaign details»: какая выбрана (по радио внутри варианта)."""

_PICK_LEADS_JS = r"""
  () => {
    const o = document.querySelector('[data-surface$="objective-selector-option-outcome_leads"]');
    if (!o) return null;
    o.scrollIntoView({ block: 'center', behavior: 'instant' });
    const r = o.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return { x: 0, y: 0, shown: false };
    const x = r.x + Math.min(r.width / 2, 60), y = r.y + r.height / 2;
    const hit = document.elementFromPoint(x, y);
    return { x, y, shown: true, hit: !!hit && o.contains(hit) };
  }
"""


def _objective(page) -> dict:
    try:
        got = _md()._eval_bounded(page, _OBJECTIVE_JS)
    except Exception:
        return {}
    return got if isinstance(got, dict) else {}


def ensure_leads_objective(page) -> str:
    """Цель кампании в новом редакторе — Leads; выбрать на месте, если нет.

    В новом редакторе цель — не окно перед мастером, а список вариантов в
    свёрнутой карточке «Campaign details». Прогоны 27.09 приходили сюда уже
    с Leads (окно цели было до редактора), но это не обещание: карточка
    раскрывается и Leads выбирается, только если выбрано другое.

    "leads" — уже Leads; "set" — выбрали; "absent" — списка целей нет
    (проверять нечем, решение за окном цели); "failed" — не встало.
    """
    md = _md()
    got = _objective(page)
    if not got.get("found"):
        return "absent"
    if got.get("leads"):
        return "leads"
    if not got.get("has_leads"):
        md._telemetry.record("simple_objective", False, note="no_leads_option",
                             diag={"checked": got.get("checked")})
        return "failed"
    open_card(page, "Campaign details")
    try:
        where = md._eval_bounded(page, _PICK_LEADS_JS) or {}
    except Exception:
        where = {}
    if where.get("hit"):
        md._cdp_mouse_click(page, where["x"], where["y"])
    for i in range(12):
        if _objective(page).get("leads"):
            md._telemetry.record("simple_objective", True, note="set",
                                 diag={"was": got.get("checked")})
            return "set"
        if i == 4:
            # Доверенный клик не попал (или вариант не на экране) — DOM-клик
            # по самому радио варианта.
            try:
                md._eval_bounded(page, "() => { const r = document.querySelector("
                                       "'[data-surface$=\"objective-selector-option-outcome_leads\"] "
                                       "input[type=radio]'); if (r) r.click(); return !!r; }")
            except Exception:
                pass
        time.sleep(0.25)
    md._telemetry.record("simple_objective", False, note="not_set",
                         diag={"was": got.get("checked"), "hit": bool(where.get("hit"))})
    return "failed"


# ── Условия Lead Ads ─────────────────────────────────────────────────────

_LEAD_TERMS_RE = (r"Please read and accept Meta.s Lead Ads Terms|Lead Generation Terms of Service"
                  r"|Terms of Service Not Accepted")

_LEAD_TERMS_JS = r"""
  (pattern) => {
    const re = new RegExp(pattern, 'i');
    const shown = (e) => {
      for (let n = e; n; n = n.parentElement) {
        if (n.hidden) return false;
        const s = getComputedStyle(n);
        if (s.display === 'none' || s.visibility === 'hidden') return false;
      }
      return e.getClientRects().length > 0;
    };
    for (const sec of document.querySelectorAll('[data-surface="/am/editor:ad_set"]')) {
      for (const e of sec.querySelectorAll('div, span')) {
        if (e.children.length > 2) continue;
        const t = (e.textContent || '').replace(/\s+/g, ' ').trim();
        if (t.length < 400 && re.test(t) && shown(e)) return t.slice(0, 200);
      }
    }
    return '';
  }
"""
"""Карточка «Please read and accept Meta's Lead Ads Terms…» / «Terms of Service
Not Accepted» — только в разделе группы (там, где выбирают страницу для
Instant forms), а не в правой панели ошибок: та может отставать от правки."""


def lead_terms_block(page, *, wait_s: float = 4.0) -> str:
    """Текст требования принять условия Lead Ads, если оно держится; "" — нет.

    Принимает условия человек (владелец страницы) — робот «View terms» /
    «Accept» не нажимает никогда. На странице с Instant forms (так Meta
    открывает новый редактор) карточка стоит всегда; после выбора Website
    она должна уйти. Даём ей `wait_s` уйти, чтобы не остановить аккаунт по
    устаревшей отрисовке.
    """
    md = _md()
    text = ""
    deadline = time.time() + max(0.0, wait_s)
    while True:
        try:
            text = md._eval_bounded(page, _LEAD_TERMS_JS, _LEAD_TERMS_RE) or ""
        except Exception:
            return ""
        if not text or time.time() >= deadline:
            return text if isinstance(text, str) else ""
        time.sleep(0.5)
