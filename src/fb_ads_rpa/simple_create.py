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
import re
import time

from src.fb_ads_rpa import job_cancel

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
            # ACL-1354 (9d3e41a0b373): вкладки больше нет — раздела не будет,
            # ждать весь `budget_s` незачем, и это не разметка редактора.
            if md._liveness.is_gone_error(exc) or md._tab_gone(page):
                return {"ok": False, "why": "page_closed"}
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
      // ACL-1611: жмём в заголовок карточки (слева), а не в центр шапки: в
      // шапке Audience есть ссылка «About audiences», и центр мог попасть
      // в неё. Попадание годится, только если под точкой нет ничего
      // нажимаемого, кроме самой кнопки карточки.
      const hr = h.getBoundingClientRect();
      const r = (hr.width > 0 && hr.height > 0) ? hr : bar.getBoundingClientRect();
      const x = r.x + Math.min(r.width / 2, 60), y = r.y + r.height / 2;
      const hit = document.elementFromPoint(x, y);
      const inner = !!hit && !!hit.closest(
        'a, button, input, select, textarea, label, [role=link], [role=checkbox], [role=switch], [role=combobox]');
      const rb = hit && hit.closest('[role=button]');
      const foreign = !!rb && !btns.includes(rb);
      btn.setAttribute('data-acl-card-button', bar.id);
      return { found: true, pressed: false, x, y, hit: !!hit && btn.contains(hit) && !inner && !foreign,
               body: !!body, bar: bar.id };
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
        job_cancel.check()
    return False


_CARD_CONTROLS_JS = r"""
  () => {
    // ACL-1611: тело раскрытой карточки (метка ставится в _CARD_JS) и число
    // видимых управляющих элементов в нём: поля, комбобоксы, строки-кнопки.
    const body = document.querySelector('[data-acl-card-body]');
    if (!body) return { body: false, n: 0 };
    if (body.hidden || !body.isConnected) return { body: true, shown: false, n: 0 };
    const vis = (e) => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
    const n = [...body.querySelectorAll(
      'input, textarea, select, [role=combobox], [role=textbox], [role=button], [role=switch], [role=checkbox]')]
      .filter(vis).length;
    return { body: true, shown: true, n };
  }
"""
"""Есть ли в раскрытой карточке управляющие элементы (а не пустая оболочка)."""


def _card_controls(page, wait_s: float) -> dict:
    md = _md()
    got: dict = {}
    for _ in range(max(1, int(wait_s / 0.25))):
        try:
            got = md._eval_bounded(page, _CARD_CONTROLS_JS) or {}
        except Exception:
            return {}
        if not got.get("body") or got.get("n"):
            break
        time.sleep(0.25)
        job_cancel.check()
    return got


def open_card(page, title: str) -> str:
    """Раскрыть свёрнутую карточку нового редактора по её заголовку.

    "absent" — такой карточки-сводки нет (прежний мастер или карточка без
    сворачивания — обработчик работает как раньше); "open" — уже раскрыта;
    "opened" — раскрыли и в теле есть управляющие элементы; "no_controls" —
    нажатие прошло, а тело пустое или скрыто (ACL-1611); "failed" — не
    раскрылась ни доверенным кликом по шапке, ни DOM-кликом по кнопке.
    Нажимается только сама карточка (заголовок), не кнопка или ссылка
    внутри неё. Строка телеметрии `simple_card` (diag.how — чем раскрыли).
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
    diag = {"how": how, "hit": bool(state.get("hit")), "target": "card_header"}
    if how:
        # ACL-1611: «раскрылась» — это ещё и управляющие элементы в теле.
        # Тело не нашли (метки нет) — проверить нечем, считаем раскрытой.
        ctl = _card_controls(page, 2.0)
        diag["controls"] = ctl.get("n") if ctl.get("body") else None
        if ctl.get("body") and not ctl.get("n"):
            result = "no_controls"
    md._telemetry.record("simple_card", result == "opened", note=f"{title}:{result}", diag=diag)
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
        job_cancel.check()
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
        job_cancel.check()


# ── После перезагрузки ───────────────────────────────────────────────────

def back_to_level(page, level: str, where: str) -> bool:
    """Вернуться к разделу уровня после перезагрузки страницы нового редактора.

    Прежний мастер после перезагрузки открывается прямо на объявлении
    (`/ads/edit/…`). Новый редактор — всегда одна страница целиком, сверху, с
    разделами, которые ещё дорисовываются, и иногда с окном-экскурсией.
    97314905598e (28.09): загрузка ролика зависла, шаг перезагрузил страницу и
    сразу искал «Set up creative» — кнопка стояла под футером (клик ушёл в
    футер), а потом не находилась вовсе, и мастер «не открылся». Здесь —
    закрыть экскурсию и дойти до раздела так же, как после Continue.
    """
    md = _md()
    try:
        md._close_onboarding_tour(page, where)
    except Exception:
        pass
    got = scroll_to_level(page, level)
    md._telemetry.record("simple_reentry", True if got.get("ok") else None,
                         note=f"{where}:{level}", diag={"why": got.get("why", "")})
    return bool(got.get("ok"))


# ── Второе и третье объявления (ACL-1353) ────────────────────────────────
#
# Живые снимки шага 1 (30.09, Facebook_1443: f9b9fc8d7bb3, 8ce1b9985046;
# 29.09: b3a333ff43ab; вырезка
# `tests/fixtures/ads_manager/simple_create_more_ads_2026_09_30.html`):
#
# * дерево слева (`/am/editor_drawer`, `role=grid` → `role=rowheader`
#   Campaign / Ad set / Ad); у строки две кнопки, которые рисуются при
#   наведении: быстрое действие «default» и «Action menu»
#   (`editor_tree:<уровень>/lib:ads-campaign-structure-action-menu-item/…`);
#   кнопки «Create ad» в самом дереве нет;
# * «Action menu» строки группы открывает `role=menu` «Actions for this ad
#   set»: Duplicate / Copy / Paste / Delete / **Create ad**
#   (`/am/editor_drawer/lib:campaign-structure-item-action-menu-create`) /
#   Create multiple ads / Create rule / ID;
# * после «Create ad» в пределах ~7 с на экране окно (не панель уведомлений),
#   строк объявлений ещё одна; к ~13 с окна уже нет (в снимке его не
#   осталось — значит, Meta закрыла его сама: окно загрузки создания), в
#   дереве вторая строка «New Leads Ad», и раздел объявления показывает ЕЁ:
#   имя «New Leads Ad», Media «Set up creative» (Required), пустой Website
#   URL — тот же вид, что у объявления 1 до заполнения.
#
# Поэтому объявление N: «Action menu» группы → «Create ad» → дождаться новой
# строки И того, что раздел объявления открыл новое (имя в поле не из уже
# собранных) и окно ушло → заполнить теми же шагами, что объявление 1
# (`meta_dom.run_recorded_campaign_flow`). Каждое нажатие — через охрану
# `_press_allowed`; «default», Duplicate, Delete, Publish и футер не
# нажимаются никогда; ни Escape, ни перезагрузки: перезагрузка открыла бы
# редактор Leads-объявления заново (это портит объявление).

#: Выключатель: FB_RPA_SIMPLE_MORE_ADS_PRESS=0 — объявления 2+ не создаются,
#: кампания кончается блокирующей причиной `simple_create_more_ads`.
MORE_ADS_PRESS_SETTING = "FB_RPA_SIMPLE_MORE_ADS_PRESS"

#: Имя шага в телеметрии и в снимках.
MORE_ADS_STEP = "simple_create_more_ads"

#: Причина в итоге кампании, когда создание выключено.
MORE_ADS_SWITCHED_OFF = "switched-off"

#: Сколько ждать новое объявление после «Create ad» (живьём — ~13 с).
NEW_AD_WAIT_S = 30.0

#: Сколько ждать, пока Meta занята созданием объявления (окно «Creating...»,
#: `/am/lib:ads_progress_dialog_modal_*`): вкладка в это время может не
#: отвечать на пробу живости. Живьём 30.09 (d66a22d0fa30, fba2c3f5, 1443):
#: проба не ответила через ~2 с после появления окна, шаг назвал вкладку
#: зависшей, а Meta дособрала объявление позже. Отсчёт — от первого
#: неответа; за это время страница должна ответить И окно — уйти.
META_BUSY_WAIT_S = 120.0

#: Окно прогресса ушло — сколько ещё ждать строку и имя нового объявления,
#: если обычный срок `NEW_AD_WAIT_S` уже истёк (ACL-1412).
PROGRESS_SETTLE_S = 8.0

#: Окно прогресса Meta: surface или собственный текст.
_PROGRESS_SURFACE = "ads_progress_dialog"
_PROGRESS_TEXT = re.compile(r"^(creating|creando|loading creation)\b", re.I)

#: Собственная надпись кнопки добавления объявления — ровно она: «Create ad
#: set», «Create multiple ads» и описание радио «Ad setup» не подходят.
CREATE_AD_RE = r"^(create ad|crear anuncio)$"

#: «Action menu» строки группы в дереве (хвост surface — тип родителя, по
#: нему не сверяем; см. `meta_dom.create_additional_ad`).
_ADSET_MENU_SURFACE = "editor_tree:adset/lib:ads-campaign-structure-action-menu-item"

#: Блоки снимка отказа: имя → JS-выражение, возвращающее элемент (`page_capture`).
MORE_ADS_BLOCKS = {
    # Дерево — список объявлений и навигация по уровням.
    "ad_tree": "document.querySelector('[data-surface=\"/am/editor_drawer\"]')",
    # Раздел объявления: какое объявление открыто.
    "ad_section": "document.querySelector('[data-surface=\"/am/editor:ad\"]')",
    # Открытое меню (Action menu строки дерева).
    "menu": "[...document.querySelectorAll('[role=menu]')].find(e => e.getClientRects().length > 0) || null",
    # Открытое окно — кроме уведомлений.
    "dialog": ("[...document.querySelectorAll('[role=dialog], [aria-modal=true]')]"
               ".find(e => e.getClientRects().length > 0"
               " && !/^notifications/i.test((e.textContent || '').trim())) || null"),
}

#: Причины отказа создания — для человека (итог кампании, English).
_WHY_TEXT = {
    "no_ad_list": "the ad list of the single-screen editor was not found",
    "no_create_control": "the ad set row in the ad list has no Action menu",
    "action_menu_not_opened": "the ad set's Action menu did not open",
    "no_create_item": "the ad set's Action menu has no Create ad item",
    "create_ad_refused": ("Create ad was covered by Publish or the editor footer, and the robot "
                          "never presses there"),
    "no_new_ad_row": "Create ad did not add an ad to the ad set within {wait} s",
    "new_ad_not_open": ("Create ad added an ad, but the editor did not open it; the new empty ad "
                        "is left in the draft - fill it in or delete it by hand"),
    "tab_frozen": "the browser tab stopped answering while the ad was being created",
    "dialog_open": ("a Meta dialog that is not its progress dialog opened after Create ad, so the "
                    "robot did not fill the new ad; check the dialog and the new ad in the draft by hand"),
    # ACL-1412: окно прогресса ждём `META_BUSY_WAIT_S`, а не `NEW_AD_WAIT_S`.
    "dialog_open_progress": ("Meta's progress dialog stayed open for {busy} s after Create ad, so the "
                             "robot did not fill the new ad; check the new ad in the draft by hand"),
    "dialog_open_media_picker": ("Meta's media picker opened over the new ad after Create ad and "
                                 "could not be closed with its Cancel, so the robot did not fill the "
                                 "new ad; close it and fill the new ad in the draft by hand"),
    # ACL-1551 (live 04.10): мастер медиа прошлого объявления остался открыт.
    "creative_wizard_left_open": ("Meta's media wizard of the previous ad stayed open and could not be "
                                  "closed with its own Close, so the ad set's Action menu could not "
                                  "be used"),
}


#: Причины отказа шага креатива — для человека (итог кампании, English).
#: ACL-1426, ревью PR #440 (L4): у `ad_form_gone_after_media` была только метка.
_CREATIVE_WHY_TEXT = {
    "ad_form_gone_after_media": ("After the media step Meta stopped showing this ad's form "
                                 "('Verifying your changes'); the ad is left in the draft for review."),
    # ACL-1476: warnings, not refusals - the ad keeps the post file.
    "story_not_placed:no_crop_step": ("Story placement uses the post file: Meta showed no Crop "
                                      "step, so the story file could not be placed."),
    "story_not_placed:not_in_library": ("Story placement uses the post file: the story file is not "
                                        "in Meta's media library, so Replace was skipped (Original "
                                        "and no Meta AI expansion are set)."),
    # ACL-1533: Replace on Vertical without evidence that the story is there.
    "crop_vertical_replace_unconfirmed": ("The story file could not be confirmed on the Vertical "
                                          "crop after Replace (the post may still be there); the "
                                          "ad is left in the draft for review."),
    "crop_vertical_changed_without_story": ("The Vertical crop changed although no story file was "
                                            "picked; the ad is left in the draft for review."),
    # ACL-1637 (live 06.10): the same video on several ads of one campaign.
    "library_has_no_copy": ("This ad has no media: the same video was already used by an earlier "
                            "ad, Meta's media library showed no copy of it and the file could not "
                            "be uploaded again for this ad; the ad is left in the draft for review."),
    "same_file_already_uploaded_this_run": ("This ad has no media: the same video was already "
                                            "uploaded for an earlier ad, but it could not be "
                                            "attached to this ad; the ad is left in the draft for "
                                            "review."),
    "same_file_already_failed_this_run": ("This ad has no media: the same video already failed to "
                                          "upload for an earlier ad and was not accepted again for "
                                          "this ad; the ad is left in the draft for review."),
    # ACL-1669 (B97, 07.10): the library grid showed no selection counter.
    "library_selection_unreadable": ("This ad has no media: Meta's media library showed no "
                                     "selection counter, so the robot could not tell what is "
                                     "selected and neither clicked a tile nor uploaded over it; "
                                     "attach the media to this ad by hand (the ad is left in the "
                                     "draft for review)."),
    "library_not_searched_counter_unreadable": (
        "This ad has no media: Meta's media library showed no selection counter, so the robot "
        "did not search it for a copy of the video (this is not a 'no copy' answer) and did "
        "not upload again; attach the media to this ad by hand (the ad is left in the draft for "
        "review)."),
    "file_not_landed": ("This ad has no media: the browser did not accept the video file in "
                        "Meta's upload dialog; the ad is left in the draft for review."),
    "wizard_not_opened": ("This ad has no media: Meta's 'Set up creative' wizard did not open "
                          "for this ad; the ad is left in the draft for review."),
    # ACL-1551 (live 04.10): Next on Crop did not move Meta's wizard on.
    "crop_wizard_stuck": ("Meta's media wizard stayed on its Crop step (Next did not move it on), "
                          "so the robot closed the wizard with its own Close; the ad's media is not "
                          "confirmed and the ad is left in the draft for review."),
    # ACL-1559 (live 04.10, 2f140fa687fa): Meta's media picker stayed on "Loading...".
    "media_picker_loading": ("Meta's media wizard opened on its Media step, but the media picker "
                             "kept showing 'Loading...' (no Upload, no media library), so the "
                             "robot could not attach this ad's media; the ad is left in the "
                             "draft for review."),
}


#: Причины отказа шага назначения — для человека (итог кампании, English).
#: ACL-1543: Meta оставила объявление на «Instant form (suggested)».
_DESTINATION_WHY_TEXT = {
    "instant_form_not_switched": ("Meta kept this ad on Instant form (suggested) and the robot could "
                                  "not switch it back to Website, so the website URL is not set. The "
                                  "robot never accepts Lead Ads Terms or creates an instant form - "
                                  "select Website (Browser add-ons: None) and enter the URL by hand."),
    # ACL-1669 (B97, 07.10): no media on the ad, so Meta showed no URL field.
    "url_field_missing_no_media": ("The website URL field is missing because this ad has no media "
                                   "(Meta shows no destination fields without it) - a consequence "
                                   "of the media step, not a separate destination problem; fix the "
                                   "media first."),
}


def destination_why_text(why: str) -> str:
    """Причина отказа шага назначения одной фразой; "" — фразы для неё нет."""
    return _DESTINATION_WHY_TEXT.get(why or "", "")


def creative_why_text(why: str) -> str:
    """Причина отказа шага креатива одной фразой; "" — фразы для неё нет."""
    why = why or ""
    if why in _CREATIVE_WHY_TEXT:
        return _CREATIVE_WHY_TEXT[why]
    if why.startswith("crop_"):
        return ("The Crop step could not be set as planned (" + why + "); the ad is left in the "
                "draft for review.")
    return ""


def why_text(why: str) -> str:
    """Причина отказа создания объявления — одной фразой для человека."""
    why = why or ""
    if why in _WHY_TEXT:
        return _WHY_TEXT[why].format(wait=int(NEW_AD_WAIT_S), busy=int(META_BUSY_WAIT_S))
    if why.startswith("dialog_open"):
        return _WHY_TEXT["dialog_open"]
    if why.startswith("action_menu_"):
        return "the ad set's Action menu could not be pressed (" + why[len("action_menu_"):] + ")"
    if why.startswith("create_ad_"):
        return "Create ad could not be pressed (" + why[len("create_ad_"):] + ")"
    return "the ad could not be created (" + (why or "unknown") + ")"


_MORE_ADS_TREE_JS = r"""
  ([createRe, menuSurface]) => {
    const norm = (s) => (s || '').replace(/[\s​]+/g, ' ').trim();
    const re = new RegExp(createRe, 'i');
    document.querySelectorAll('[data-acl-more-ads]').forEach(e => e.removeAttribute('data-acl-more-ads'));
    const tree = document.querySelector('[data-surface="/am/editor_drawer"]');
    if (!tree) return { tree: false };
    const rows = [...tree.querySelectorAll('[role=rowheader]')];
    const levels = {};
    for (const r of rows) {
      const k = (r.getAttribute('aria-label') || r.getAttribute('data-objecttype') || '?').slice(0, 20);
      levels[k] = (levels[k] || 0) + 1;
    }
    // Имена строк (кампания, группа, объявления) в телеметрию не идут.
    const names = new Set(rows.map(r => {
      const n = r.querySelector('[id^="default-button-for-action-menu_"]');
      return n ? norm(n.textContent) : '';
    }).filter(Boolean));
    const own = (b) => norm(b.getAttribute('aria-label')) || norm(b.textContent);
    // Разделы уровней (`/am/editor:ad` с радио «Create ad») — не дерево.
    const ctrls = [...tree.querySelectorAll('button, [role=button], [role=menuitem], a, [role=link]')]
      .filter(b => !b.closest('[data-surface*="/editor:"]'));
    const labels = {};
    for (const b of ctrls) {
      const l = own(b);
      if (!l || names.has(l) || l.length > 40) continue;
      const k = l.replace(/\d{5,}/g, '#');
      labels[k] = (labels[k] || 0) + 1;
    }
    let create = ctrls.filter(b => re.test(own(b)));
    create = create.filter(b => !create.some(o => o !== b && b.contains(o)));
    if (create[0]) create[0].setAttribute('data-acl-more-ads', 'create');
    const menu = tree.querySelector('[data-surface*="' + menuSurface + '"]');
    if (menu) menu.setAttribute('data-acl-more-ads', 'adset_menu');
    return { tree: true, rows: rows.length, levels, labels, create: create.length,
             adset_menu: !!menu };
  }
"""
"""Дерево нового редактора: сколько строк какого уровня, надписи кнопок (без
имён строк и без номеров), есть ли кнопка ровно «Create ad» и «Action menu»
строки группы. Найденное помечается `data-acl-more-ads` для нажатия."""

_MORE_ADS_MENU_JS = r"""
  (createRe) => {
    const norm = (s) => (s || '').replace(/[\s​]+/g, ' ').trim();
    const re = new RegExp(createRe, 'i');
    const shown = (e) => e.getClientRects().length > 0;
    const menus = [...document.querySelectorAll('[role=menu]')].filter(shown);
    // Пункты открытого меню, а не все видимые пункты документа (ревью PR #365).
    const items = (menus.length ? menus.flatMap(m => [...m.querySelectorAll('[role=menuitem]')])
                                : [...document.querySelectorAll('[role=menuitem]')]).filter(shown);
    // Своя надпись пункта — без подсказки клавиш («DuplicateCtrl+Shift+D»).
    const own = (b) => {
      const lab = b.getAttribute('aria-labelledby');
      const el = lab ? document.getElementById(lab.split(' ')[0]) : null;
      return norm(b.getAttribute('aria-label')) || norm(el && el.textContent) || norm(b.textContent);
    };
    // Номер объекта («ID: 1202…») в телеметрию не идёт (снимок шага 1 его унёс).
    const labels = items.map(own).filter(l => l && l.length <= 40)
                        .map(l => l.replace(/\d{5,}/g, '#')).slice(0, 20);
    const create = items.filter(i => re.test(own(i)) && i.getAttribute('aria-disabled') !== 'true');
    document.querySelectorAll('[data-acl-more-ads="menu_create"]').forEach(e => e.removeAttribute('data-acl-more-ads'));
    if (create[0]) create[0].setAttribute('data-acl-more-ads', 'menu_create');
    return { open: menus.length > 0 || items.length > 0, labels, create: create.length,
             create_surface: create[0] ? (create[0].getAttribute('data-surface') || '').slice(0, 120) : '' };
  }
"""
"""Открытое меню: есть ли оно, надписи пунктов, пункт ровно «Create ad» (помечается)."""

_NEW_AD_JS = r"""
  () => {
    const norm = (s) => (s || '').replace(/[\s​]+/g, ' ').trim();
    const tree = document.querySelector('[data-surface="/am/editor_drawer"]');
    const rows = tree ? [...tree.querySelectorAll('[role=rowheader][aria-label="Ad"]')] : [];
    const names = rows.map(r => {
      const n = r.querySelector('[id^="default-button-for-action-menu_"]');
      return n ? norm(n.textContent) : '';
    });
    const section = document.querySelector('[data-surface="/am/editor:ad"]');
    const field = section && section.querySelector('input[placeholder="Enter your ad name here..."]');
    const dlg = [...document.querySelectorAll('[role=dialog], [aria-modal=true]')]
      .find(e => e.getClientRects().length > 0 && !/^notifications/i.test(norm(e.textContent)));
    let dialog = null;
    if (dlg) {
      const s = dlg.closest('[data-surface]') || dlg.querySelector('[data-surface]');
      dialog = { text: norm(dlg.textContent).replace(/\d{5,}/g, '#').slice(0, 80),
                 surface: s ? (s.getAttribute('data-surface') || '').replace(/\d{5,}/g, '#').slice(0, 100) : '' };
      // ACL-1412 (d1755a59e139, ad3): после «Create ad» на экране осталось
      // окно выбора медиа нового объявления — кнопки внутри него несут
      // surface блока Media раздела объявления.
      if (dlg.querySelector('[data-surface*="ad_image_video_selector_section"]')) dialog.picker = true;
    }
    return { ads: rows.length, names, name_value: field ? field.value : null, dialog };
  }
"""
"""Список объявлений: сколько строк «Ad» и их имена; значение поля имени
открытого объявления; окно на экране (кроме уведомлений) — текст без номеров."""


def more_ads_enabled() -> bool:
    """Создавать ли объявления 2+ в новом редакторе (по умолчанию — да)."""
    return os.environ.get(MORE_ADS_PRESS_SETTING, "").strip().lower() not in _OFF_VALUES


def _more_ads_capture(page, stage: str, diag: dict) -> str:
    """Снимок шага с блоками дерева/раздела/меню/окна; «прогон/NN_шаг» или ""."""
    try:
        from src.fb_ads_rpa import page_capture as _pc

        saved = _pc.capture(page, step=MORE_ADS_STEP, diag={"stage": stage, **diag},
                            blocks=MORE_ADS_BLOCKS)
    except Exception:
        saved = ""
    if not saved or not isinstance(saved, str):
        return ""
    # Только «прогон/NN_шаг»: полный путь несёт имя пользователя машины.
    parts = saved.replace("\\", "/").rstrip("/").split("/")
    return "/".join(parts[-2:])


def _box_ok(box) -> bool:
    return bool(box) and box.get("width", 0) > 0 and box.get("height", 0) > 0


def _press_marked(page, mark: str, *, dom_fallback: bool = False) -> str:
    """Нажать элемент с `data-acl-more-ads=<mark>` один раз.

    "pressed" — доверенное нажатие прошло охрану `_press_allowed`;
    "dom" — у элемента нет коробки и после наведения на строку (кнопки
    дерева рисуются при наведении), DOM-клик разрешён вызывающим и охрана в
    точке строки пустила; "refused" — охрана не пустила; "absent" —
    элемента нет; "no_box" — коробки нет, DOM-клик не разрешён;
    "frozen" — вкладка не ответила, нажатия не было.
    """
    md = _md()
    try:
        handle = page.query_selector(f'[data-acl-more-ads="{mark}"]')
    except Exception:
        handle = None
    if handle is None:
        return "absent"
    try:
        handle.evaluate("el => el.scrollIntoView({block: 'center', behavior: 'instant'})")
    except Exception:
        pass
    try:
        box = handle.bounding_box()
    except Exception:
        box = None
    if not _box_ok(box):
        # Навести на строку (движение мыши, не нажатие) и спросить ещё раз.
        rbox = None
        try:
            row = handle.evaluate_handle("el => el.closest('[role=rowheader]') || el.parentElement")
            el = row.as_element() if row else None
            rbox = el.bounding_box() if el else None
        except Exception:
            rbox = None
        if _box_ok(rbox):
            try:
                page.mouse.move(rbox["x"] + rbox["width"] - 30, rbox["y"] + rbox["height"] / 2)
            except Exception:
                pass
            md._sleep_h(0.4)
            try:
                box = handle.bounding_box()
            except Exception:
                box = None
        if not _box_ok(box):
            if not dom_fallback or not _box_ok(rbox):
                return "no_box"
            # DOM-клик достаётся только этому элементу, но охрана всё равно
            # спрашивается в точке строки: Publish или футер там — не жмём.
            x, y = rbox["x"] + rbox["width"] / 2, rbox["y"] + rbox["height"] / 2
            if not md._press_allowed(page, x, y, ""):
                return "refused"
            try:
                done = handle.evaluate("el => { if (!el.isConnected) return false; el.click(); return true; }")
            except Exception:
                done = False
            return "dom" if done else "absent"
    md._mark_press_target(page, handle)
    got = md._cdp_mouse_click(page, box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    if got is False:
        return "refused"
    if got is None:
        return "frozen"
    return "pressed"


def _wait_menu(page, wait_s: float) -> dict:
    md = _md()
    got: dict = {}
    deadline = time.time() + wait_s
    while True:
        try:
            got = md._eval_bounded(page, _MORE_ADS_MENU_JS, CREATE_AD_RE) or {}
        except Exception:
            got = {}
        if got.get("open") or time.time() >= deadline:
            return got
        time.sleep(0.3)
        job_cancel.check()


def _read_ads(page) -> dict:
    md = _md()
    try:
        got = md._eval_bounded(page, _NEW_AD_JS)
    except Exception as exc:
        return {"error": type(exc).__name__}
    return got if isinstance(got, dict) else {"error": "no_answer"}


def _fresh(name_value, known: list[str]) -> bool:
    """В поле имени — непустое имя, и не одно из уже собранных объявлений
    (новое открыто). Пустое поле — раздел ещё не дорисован (ревью PR #365, F3)."""
    if name_value is None or not name_value.strip():
        return False
    seen = {(n or "").strip().lower() for n in known}
    return name_value.strip().lower() not in seen


def is_progress_dialog(dialog) -> bool:
    """Окно — прогресс Meta («Creating...», `ads_progress_dialog_modal_*`)."""
    if not dialog:
        return False
    return (_PROGRESS_SURFACE in (dialog.get("surface") or "")
            or bool(_PROGRESS_TEXT.match((dialog.get("text") or "").strip())))


def _wv():
    from src.fb_ads_rpa import window_visibility

    return window_visibility


def is_media_picker(dialog) -> bool:
    """Окно — выбор медиа открытого объявления (ACL-1412, d1755a59e139 ad3):
    внутри кнопки с surface блока Media раздела объявления."""
    return bool(dialog) and bool(dialog.get("picker"))


def _wait_new_ad(page, ads_before: int, known: list[str], wait_s: float,
                 busy_wait_s: float | None = None) -> dict:
    """Ждать после «Create ad»: новая строка «Ad», раздел объявления показывает
    новое объявление, окна нет. Первое увиденное окно и сколько оно висело —
    в итог (в снимках шага 1 его уже не было).

    Окно прогресса Meta («Creating...») — это «Meta занята», а не зависание
    (живьём 30.09): пока оно на экране, ждём до `busy_wait_s` от его первого
    появления — отвечает страница или нет (ACL-1412: d1755a59e139, ad3 —
    окно висело дольше 30 с при живой странице). Не ответила — отсчёт от
    первого неответа, как прежде. Ничего не нажимаем и не перезагружаем.
    `busy_s` — сколько страница не отвечала, `progress_s` — сколько висело
    окно прогресса. Закрытая вкладка ожиданием не лечится.

    Другое окно (не прогресс и не выбор медиа нового объявления) — два
    чтения подряд, и ждать нечего: `other_dialog`. Выбор медиа нового
    объявления готовности не мешает — его закрывает вызывающий."""
    md = _md()
    if busy_wait_s is None:
        busy_wait_s = META_BUSY_WAIT_S
    md._sleep_h(1.0)
    start = time.time()
    got: dict = {}
    dialog = None
    dialog_s = 0.0
    last_name = None
    last_seen_dialog = None      # окно в последнем ответившем чтении
    busy_since = None            # первый неответ при окне прогресса
    busy_s = 0.0                 # сколько всего страница не отвечала
    unanswered_at = None
    progress_since = None        # первое ответившее чтение с окном прогресса
    progress_last = None         # последнее такое чтение
    other_reads = 0              # чтений подряд с чужим окном
    while True:
        got = _read_ads(page)
        now = time.time()
        if got.get("error"):
            closed = "closed" in str(got["error"]).lower()
            if unanswered_at is None:
                unanswered_at = now
            if not closed and (busy_since is not None or is_progress_dialog(last_seen_dialog)):
                if busy_since is None:
                    busy_since = now
                if now - busy_since < busy_wait_s:
                    if now - busy_since >= _wv().WAKE_AFTER_S:
                        # ACL-1402: долго не отвечает — не скрыто ли окно.
                        _wv().wake_if_hidden(page, where="create_next_ad",
                                             waited_s=now - start, answered=False)
                    time.sleep(1.0)
                    job_cancel.check()
                    continue
            busy_s += now - unanswered_at
            if not closed and _wv().woken_recently(page):
                _wv().wake_did_not_help(page)
            break
        if unanswered_at is not None:
            busy_s += now - unanswered_at
            unanswered_at = None
        cur = got.get("dialog")
        last_seen_dialog = cur
        if cur:
            if dialog is None:
                dialog = dict(cur, seen_at_s=round(now - start, 1))
            dialog_s = round(now - start, 1)
        if is_progress_dialog(cur):
            if progress_since is None:
                progress_since = now
            progress_last = now
        other = bool(cur) and not is_progress_dialog(cur) and not is_media_picker(cur)
        other_reads = other_reads + 1 if other else 0
        # Имя нового объявления — одно и то же два опроса подряд (F3): раздел
        # мог ещё дорисовываться.
        name = got.get("name_value")
        stable = _fresh(name, known) and name == last_name
        last_name = name
        got["name_stable"] = stable
        ready = (int(got.get("ads") or 0) > ads_before and stable
                 and (not cur or is_media_picker(cur)))
        # Пока Meta занята, срок — от первого неответа или от первого окна
        # прогресса (`busy_wait_s`), а не только обычный `wait_s`; окно ушло —
        # ещё `PROGRESS_SETTLE_S` на строку и имя нового объявления.
        deadline = start + wait_s
        if busy_since is not None:
            deadline = max(deadline, busy_since + busy_wait_s)
        if progress_since is not None:
            if is_progress_dialog(cur):
                deadline = max(deadline, progress_since + busy_wait_s)
            else:
                deadline = max(deadline, min(progress_last + PROGRESS_SETTLE_S,
                                             progress_since + busy_wait_s))
        if ready or now >= deadline or other_reads >= 2:
            break
        if now - start >= _wv().WAKE_AFTER_S and not is_progress_dialog(cur):
            # ACL-1402: страница отвечает, а объявления всё нет — окно может
            # быть закрыто другими окнами (Chrome придерживает кадры). Окно
            # прогресса Meta на экране — Meta занята, это не симптом скрытого
            # окна (ревью PR #420, F3). Страницу о видимости не спрашиваем —
            # она только что ответила, решает Windows (без 3 с в опросе).
            _wv().wake_if_hidden(page, where="create_next_ad", waited_s=now - start,
                                 answered=True, probe=False)
        time.sleep(0.5)
        job_cancel.check()
    got["dialog_first"] = dialog
    got["dialog_until_s"] = dialog_s if dialog else None
    got["waited_s"] = round(time.time() - start, 1)
    got["busy_s"] = round(busy_s, 1) if (busy_since is not None or busy_s) else None
    got["progress_s"] = (round(progress_last - progress_since, 1)
                         if progress_since is not None else None)
    got["other_dialog"] = other_reads >= 2
    return got


_PICKER_CANCEL_JS = r"""
  () => {
    const norm = (s) => (s || '').replace(/[\s​]+/g, ' ').trim();
    const shown = (e) => e.getClientRects().length > 0;
    document.querySelectorAll('[data-acl-more-ads="picker_cancel"]').forEach(e => e.removeAttribute('data-acl-more-ads'));
    const dlg = [...document.querySelectorAll('[role=dialog]')]
      .find(d => shown(d) && d.querySelector('[data-surface*="ad_image_video_selector_section"]'));
    if (!dlg) return { picker: false };
    const selected = [...dlg.querySelectorAll('span')].map(e => norm(e.textContent))
      .find(t => /^\d+( of \d+)? selected$/.test(t)) || '';
    // Своя кнопка окна ровно «Cancel» (не Close в строке шагов, не Next).
    const btn = [...dlg.querySelectorAll('[role=button], button')]
      .find(b => shown(b) && norm(b.textContent) === 'Cancel' && b.getAttribute('aria-disabled') !== 'true');
    if (btn) btn.setAttribute('data-acl-more-ads', 'picker_cancel');
    return { picker: true, cancel: !!btn, selected };
  }
"""
"""Окно выбора медиа объявления: есть ли, что выбрано, его кнопка Cancel (помечается)."""


def close_media_picker(page, *, wait_s: float = 6.0) -> dict:
    """Закрыть окно выбора медиа, которое Meta оставила после «Create ad».

    ACL-1412 (d1755a59e139, 30.09, ad3): через ~30 с после «Create ad» на
    экране было окно выбора медиа (Media · Trim · Crop …, медиатека, «1
    selected», Cancel / Next), а раздел объявления уже показывал новое
    объявление. Под окном не набрать имя, а «Next» закрепил бы в новом
    объявлении чужой выбор. Поэтому — его собственный Cancel (охраняемое
    нажатие), а медиа новое объявление получает своим путём, как
    объявление 1. Возвращает {"closed", "why", "selected"}.
    """
    md = _md()
    try:
        got = md._eval_bounded(page, _PICKER_CANCEL_JS) or {}
    except Exception as exc:
        return {"closed": False, "why": "tab_frozen", "error": type(exc).__name__}
    if not isinstance(got, dict) or not got.get("picker"):
        return {"closed": True, "why": "", "selected": ""}
    out = {"closed": False, "why": "", "selected": got.get("selected") or ""}
    if not got.get("cancel"):
        out["why"] = "no_cancel"
        return out
    pressed = _press_marked(page, "picker_cancel")
    if pressed != "pressed":
        out["why"] = "tab_frozen" if pressed == "frozen" else "cancel_" + pressed
        return out
    deadline = time.time() + wait_s
    while True:
        now = _read_ads(page)
        cur = None if now.get("error") else now.get("dialog")
        if not now.get("error") and not cur:
            out["closed"] = True
            return out
        if cur and not is_media_picker(cur):
            # На Cancel Meta ответила другим окном (например, «Discard?») —
            # ничего в нём не жмём.
            out["why"] = "other_dialog"
            out["dialog"] = cur
            return out
        if time.time() >= deadline:
            out["why"] = "still_open"
            return out
        time.sleep(0.4)
        job_cancel.check()


def create_next_ad(page, *, tag: str, known_names: list[str],
                   wait_s: float = NEW_AD_WAIT_S) -> dict:
    """Создать в той же группе новое объявление и дождаться, что оно открыто.

    `known_names` — имена уже собранных объявлений кампании: новое узнаётся по
    тому, что в поле имени открытого объявления не одно из них (у Meta это
    «New Leads Ad»). Заполняет вызывающий — теми же шагами, что объявление 1.

    Возвращает {"ok", "why" (код причины, пусто при успехе), "text" (для
    человека), "pressed", "ads_before", "ads_after", "extra_shells" (сколько
    лишних пустых объявлений добавилось сверх одного), "dialog" (окно после
    «Create ad»), "capture" (снимок при отказе)}. Никогда не бросает. Строка
    телеметрии `simple_create_more_ads` — на каждый вызов.
    """
    md = _md()
    out: dict = {"ok": False, "why": "", "tag": tag, "pressed": [], "ads_before": None,
                 "ads_after": None, "extra_shells": 0, "dialog": None, "capture": ""}
    try:
        _create_next_ad(page, out, list(known_names or []), wait_s)
    except Exception as exc:
        out["why"] = out["why"] or "error:" + type(exc).__name__
    if not out["ok"] and not out["why"]:
        out["why"] = "unknown"
    out["text"] = "" if out["ok"] else why_text(out["why"])
    if out["why"] == "dialog_open" and out.get("dialog_kind") in ("progress", "media_picker"):
        out["text"] = why_text("dialog_open_" + out["dialog_kind"])
    if out["why"] == "tab_frozen" and out.get("busy_s"):
        # Живьём 30.09: Meta показывала «Creating...», и страница не отвечала.
        out["text"] += (f" (Meta showed its progress dialog and the page did not answer for "
                        f"{out['busy_s']:.0f} s, longer than the robot waits: "
                        f"{META_BUSY_WAIT_S:.0f} s; the new ad may still appear in the draft empty - "
                        "fill it in or delete it by hand)")
    shown = out.get("dialog_last") or out.get("dialog") or {}
    if out["why"] == "dialog_open" and shown.get("text"):
        # Текст окна (без номеров) — в причину: по нему человек его найдёт.
        # ACL-1412: окно, что осталось на экране, а не первое увиденное.
        out["text"] += f' (dialog: "{shown["text"][:80]}")'
    if not out["ok"]:
        out["capture"] = _more_ads_capture(page, out["why"], {
            k: out.get(k) for k in ("tag", "pressed", "ads_before", "ads_after", "dialog",
                                    "tree", "menu")})
    try:
        md._telemetry.record(
            MORE_ADS_STEP, True if out["ok"] else False,
            note="created" if out["ok"] else out["why"],
            diag={k: out.get(k) for k in ("tag", "pressed", "ads_before", "ads_after",
                                          "extra_shells", "dialog", "dialog_kind", "dialog_last",
                                          "waited_s", "busy_s",
                                          "progress_s", "media_picker", "tree", "menu",
                                          "wizard_cleared", "capture", "text")})
    except Exception:
        pass
    return out


_ADSET_ROW_POINT_JS = r"""
  () => {
    const tree = document.querySelector('[data-surface="/am/editor_drawer"]');
    const row = tree && tree.querySelector('[data-surface$="editor_tree:adset"] [role=rowheader]');
    if (!row) return { why: 'no_row' };
    row.scrollIntoView({ block: 'center', behavior: 'instant' });
    const r = row.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return { why: 'hidden' };
    // Кнопки строки стоят справа; точка — внутри строки, левее края.
    return { x: r.x + Math.max(10, r.width - 40), y: r.y + r.height / 2,
             x0: r.x + Math.min(r.width / 2, 80) };
  }
"""
"""Точка над строкой группы в дереве (движение мыши, не нажатие).

Фокус клавиатуры на строку не ставится ни на одной попытке (ревью PR #632,
F5): фокус строки дерева Meta может её выбрать и увести редактор на группу."""

#: Сколько раз навести курсор на строку группы, прежде чем признать, что кнопок нет.
#: Кнопки появились — наведение сразу прекращается.
_WAKE_TRIES = 3


def _wake_adset_row(page, tree: dict) -> dict:
    """Навести курсор на строку группы, чтобы Meta нарисовала её кнопки.

    ACL-1630 (06.10, Facebook_5265): в дереве у строк кампании, группы и
    объявления на месте блока кнопок («default», «Action menu») пустой
    `<div></div>` — Meta рисует их только под курсором (снимки 24.09 и 30.09
    держали кнопки в DOM). Движение мыши — не нажатие и охраны не требует.
    Только `mouse.move`: ни нажатия, ни фокуса (тесты это проверяют).
    Возвращает дерево, перечитанное после наведения, с `hover` =
    {"tries", "woke"}; не появились — прежний отказ `no_create_control`;
    вкладка не ответила — дерево с `tree: False` и `error` (`tab_frozen`).
    """
    md = _md()
    hover = {"tries": 0, "woke": False}
    for n in range(_WAKE_TRIES):
        try:
            pt = md._eval_bounded(page, _ADSET_ROW_POINT_JS) or {}
        except Exception:
            pt = {}
        if "x" not in pt:
            hover["why"] = pt.get("why") or "no_point"
            break
        hover["tries"] = n + 1
        try:
            # Сначала в сторону, потом на строку: `mouseover` приходит при
            # входе курсора, а не при стоянии над ней.
            page.mouse.move(pt["x0"], pt["y"] + 90)
            page.mouse.move(pt["x0"], pt["y"], steps=4)
            page.mouse.move(pt["x"], pt["y"], steps=4)
        except Exception:
            hover["why"] = "mouse_error"
            break
        md._sleep_h(0.5)
        try:
            fresh = md._eval_bounded(page, _MORE_ADS_TREE_JS, [CREATE_AD_RE, _ADSET_MENU_SURFACE]) or {}
        except Exception as exc:
            fresh = {"tree": False, "error": type(exc).__name__}
        if not fresh.get("tree"):
            tree = fresh
            break
        tree = fresh
        if tree.get("create") or tree.get("adset_menu"):
            hover["woke"] = True
            break
    tree["hover"] = hover
    return tree


def clear_leftover_wizard(page) -> dict | None:
    """Мастер медиа прошлого объявления ещё на экране — закрыть его своим Close.

    ACL-1551 (232d398471bd, 04.10): мастер объявления 1 остался открытым на
    Crop, и «Action menu» группы не открылось — объявления 2 и 3 не созданы.
    Перед «Action menu» мастер закрывается его собственным Close (шапка
    мастера; ни Next, ни Save — в объявление 1 ничего не сохраняется).
    None — мастера не было; иначе {"closed_by", "closed", "dialog_left", "tried"}.
    """
    md = _md()
    try:
        present = md._eval_bounded(page, md._WIZARD_ROOT_JS.replace("return d;", "return true;")
                                   .replace("return null;", "return false;"))
    except Exception:
        return None
    if not present:
        return None
    return md._leave_wizard(page, allow_complete=False)


def _create_next_ad(page, out: dict, known: list[str], wait_s: float) -> None:
    md = _md()
    # Предыдущее объявление сохранено до нажатия (ревью PR #365).
    try:
        md.wait_editor_saved(page)
    except Exception:
        pass
    # ACL-1551: открытый мастер медиа прошлого объявления перекрывает дерево.
    cleared = clear_leftover_wizard(page)
    if cleared is not None:
        out["wizard_cleared"] = cleared
        if not cleared.get("closed"):
            out["why"] = "creative_wizard_left_open"
            return
    try:
        tree = md._eval_bounded(page, _MORE_ADS_TREE_JS, [CREATE_AD_RE, _ADSET_MENU_SURFACE]) or {}
    except Exception as exc:
        tree = {"tree": False, "error": type(exc).__name__}
    out["tree"] = tree
    if not tree.get("tree"):
        out["why"] = "tab_frozen" if tree.get("error") else "no_ad_list"
        return
    if not tree.get("create") and not tree.get("adset_menu"):
        # ACL-1630: кнопок строки в DOM нет, пока над ней нет курсора.
        tree = _wake_adset_row(page, tree)
        out["tree"] = tree
        # Ревью PR #632 (F6): вкладка перестала отвечать во время наведения —
        # это `tab_frozen`, а не «нет Action menu».
        if not tree.get("tree"):
            out["why"] = "tab_frozen" if tree.get("error") else "no_ad_list"
            return
    ads_before = int((tree.get("levels") or {}).get("Ad") or 0)
    out["ads_before"] = ads_before
    if tree.get("create"):
        # Кнопка ровно «Create ad» в самом дереве (живьём её нет — на случай,
        # если Meta её вернёт).
        got = _press_marked(page, "create")
        if got != "pressed":
            out["why"] = "tab_frozen" if got == "frozen" else f"create_ad_{got}"
            return
        out["pressed"].append("tree_create_ad")
    else:
        if not tree.get("adset_menu"):
            out["why"] = "no_create_control"
            return
        got = _press_marked(page, "adset_menu", dom_fallback=True)
        if got not in ("pressed", "dom"):
            out["why"] = "tab_frozen" if got == "frozen" else f"action_menu_{got}"
            return
        out["pressed"].append("adset_action_menu" + ("_dom" if got == "dom" else ""))
        menu = _wait_menu(page, 4.0)
        out["menu"] = menu
        if not menu.get("open"):
            out["why"] = "action_menu_not_opened"
            return
        if not menu.get("create"):
            out["why"] = "no_create_item"
            return
        got = _press_marked(page, "menu_create")
        if got != "pressed":
            out["why"] = "tab_frozen" if got == "frozen" else f"create_ad_{got}"
            return
        out["pressed"].append("menu_create_ad")
    after = _wait_new_ad(page, ads_before, known, wait_s)
    out["ads_after"] = after.get("ads")
    out["dialog"] = after.get("dialog_first")
    if out["dialog"] is not None:
        out["dialog"]["until_s"] = after.get("dialog_until_s")
        # ACL-1412: сколько окно было на экране (от первого до последнего чтения с окном).
        out["dialog"]["open_s"] = round((after.get("dialog_until_s") or 0)
                                        - (out["dialog"].get("seen_at_s") or 0), 1)
        out["dialog"]["still_open"] = bool(after.get("dialog"))
    out["waited_s"] = after.get("waited_s")
    out["busy_s"] = after.get("busy_s")
    out["progress_s"] = after.get("progress_s")
    out["name_is_new"] = bool(after.get("name_stable"))
    if after.get("error"):
        out["why"] = "tab_frozen"
        return
    last = after.get("dialog")
    if last and not is_media_picker(last):
        # Ревью PR #365, F1: окно после «Create ad» так и не ушло — что оно
        # ждёт, неизвестно; под ним ничего не заполняем. ACL-1412: окно
        # прогресса — после `META_BUSY_WAIT_S`, чужое окно — сразу.
        out["why"] = "dialog_open"
        out["dialog_kind"] = "progress" if is_progress_dialog(last) else "other"
        out["dialog_last"] = last
        return
    ads_after = int(after.get("ads") or 0)
    if ads_after <= ads_before:
        out["why"] = "no_new_ad_row"
        return
    if not out["name_is_new"]:
        out["why"] = "new_ad_not_open"
        return
    if last:
        # ACL-1412 (d1755a59e139, ad3): поверх нового объявления — окно выбора
        # медиа. Закрываем его собственным Cancel; медиа — своим путём, как у
        # объявления 1.
        closed = close_media_picker(page)
        out["media_picker"] = {k: closed.get(k) for k in ("closed", "why", "selected")}
        if not closed.get("closed"):
            out["why"] = "tab_frozen" if closed.get("why") == "tab_frozen" else "dialog_open"
            out["dialog_kind"] = "media_picker" if closed.get("why") != "other_dialog" else "other"
            if closed.get("dialog"):
                out["dialog_last"] = closed["dialog"]
            return
    # 17.09 (прежний мастер, Doc20): один «Create ad» дал ДВА новых объявления.
    out["extra_shells"] = max(0, ads_after - ads_before - 1)
    out["ok"] = True


def ad_listed(page, name: str, *, wait_s: float = 8.0) -> dict:
    """Стоит ли объявление с этим именем в списке объявлений (дерево).

    Сверка объявления N после заполнения — без повторного открытия редактора
    (это портит Leads-объявление): имя в дереве меняется вслед за полем (в
    снимке шага 1 строка объявления 1 уже носила его имя). Возвращает
    {"listed": bool, "ads": строк «Ad»}; имена в итог не идут.
    """
    md = _md()
    try:
        md.wait_editor_saved(page)
    except Exception:
        pass
    want = (name or "").strip().lower()
    deadline = time.time() + wait_s
    while True:
        got = _read_ads(page)
        names = [(n or "").strip().lower() for n in (got.get("names") or [])]
        listed = bool(want) and want in names
        if listed or time.time() >= deadline or got.get("error"):
            return {"listed": listed, "ads": got.get("ads"), "error": got.get("error")}
        time.sleep(0.5)
        job_cancel.check()


# ── Медиа нового объявления (ACL-1412) ───────────────────────────────────
#
# Живой прогон d1755a59e139 (30.09, Facebook_1443, объявление 2): мастер
# медиа открылся, файл ушёл в медиатеку, а дальше робот ни разу не увидел
# блок Media объявления: проверка медиа 85 с читала страницу, на которой
# нет ни «Please specify the media», ни «Edit media» (раздел объявления
# размонтирован — страница стояла на кампании и группе, в снимке от раздела
# «Ad» только превью), вторая попытка не нашла «Set up creative» (`button:
# null`, `ad_section: false`), а назначение — раздела Destination
# (`destination_section_missing`). Блок Media нового объявления тот же, что
# у объявления 1 (вырезка `new_ad_media`: «Set up creative» — кнопка меню,
# «Please specify the media…»; заполненный — «Edit media» / «Remove media»,
# `simple_create_new_ad_media_2026_09_30.html`). Значит, прежде чем открывать
# мастер или судить о медиа, блок надо вернуть на экран.

#: Блок Media раздела объявления (не внутри окна выбора медиа).
_MEDIA_BLOCK_JS = r"""
  (scroll) => {
    const norm = (s) => (s || '').replace(/[\s​]+/g, ' ').trim();
    const shown = (e) => e.getClientRects().length > 0;
    const outside = (e) => !e.closest('[role=dialog]');
    const picker = [...document.querySelectorAll('[role=dialog]')]
      .some(d => shown(d) && d.querySelector('[data-surface*="ad_image_video_selector_section"]'));
    const block = [...document.querySelectorAll('#imageVideoSelectorEditorComponent')]
                    .find(b => outside(b) && shown(b))
      || [...document.querySelectorAll('[data-surface="/am/editor:ad/ad_image_video_selector_section"]')]
                    .find(b => outside(b) && shown(b));
    if (!block) return { mounted: false, picker };
    if (scroll) block.scrollIntoView({ block: 'center', behavior: 'instant' });
    const texts = [...block.querySelectorAll('*')].filter(e => e.children.length === 0)
      .map(e => norm(e.textContent)).filter(Boolean);
    const setUp = [...block.querySelectorAll('[role=button], button')]
      .some(b => norm(b.textContent) === 'Set up creative');
    const empty = texts.some(t => t.includes('Please specify the media'));
    const filled = texts.some(t => ['Edit media', 'Remove media', 'Change selections',
                                    'Customize media'].includes(t));
    const state = filled && !empty ? 'filled' : (setUp || empty) ? 'empty' : 'unknown';
    return { mounted: true, state, set_up_creative: setUp, picker };
  }
"""
"""Блок Media объявления: отрисован ли, пуст/заполнен, есть ли «Set up creative»;
открыто ли окно выбора медиа. `scroll` — прокрутить блок в середину окна."""

#: Для `meta_dom._scroll_until_mounted`: блок Media отрисован.
MEDIA_BLOCK_PRESENT_JS = r"""
  () => [...document.querySelectorAll('#imageVideoSelectorEditorComponent')]
    .some(b => !b.closest('[role=dialog]') && b.getClientRects().length > 0)
"""


def media_block(page, *, scroll: bool = True) -> dict:
    """Блок Media открытого объявления — одним ограниченным чтением."""
    md = _md()
    try:
        got = md._eval_bounded(page, _MEDIA_BLOCK_JS, bool(scroll))
    except Exception as exc:
        return {"mounted": False, "error": type(exc).__name__}
    return dict(got) if isinstance(got, dict) else {"mounted": False, "error": "no_answer"}


def show_media_block(page, *, budget_s: float = 12.0) -> dict:
    """Вернуть блок Media открытого объявления на экран (без нажатий).

    Порядок: уже отрисован — прокрутить к нему; нет — дойти до раздела «Ad»
    (заголовок и поле имени, `scroll_to_level`); всё ещё нет — прокрутка
    контейнера долями (`meta_dom._scroll_until_mounted`). В итоге — `via`:
    "mounted" | "ad_heading" | "scroll" | "" (не нашли). Только прокрутка:
    ни нажатий, ни перезагрузок — годится внутри `hold_open_ad`.
    """
    md = _md()
    got = media_block(page)
    if got.get("mounted"):
        got["via"] = "mounted"
        return got
    if got.get("error"):
        got["via"] = ""
        return got
    level = scroll_to_level(page, "ad", budget_s=max(1.0, min(8.0, budget_s / 2)))
    got = media_block(page)
    if got.get("mounted"):
        got["via"] = "ad_heading"
        return got
    try:
        md._scroll_until_mounted(page, MEDIA_BLOCK_PRESENT_JS)
    except Exception:
        pass
    got = media_block(page)
    got["via"] = "scroll" if got.get("mounted") else ""
    if not got.get("mounted"):
        got["ad_section"] = level.get("why") or bool(level.get("ok"))
    return got
