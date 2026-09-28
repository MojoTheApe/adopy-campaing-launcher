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


# ── Второе и третье объявления: снимок для реализации (ACL-1353) ─────────
#
# Как новый редактор добавляет объявление в ту же группу, не видно ни в
# одном снимке (27–28.09: 1430c8e10bf0, 5b164b1ba9a4, 961252633112,
# 97314905598e, 9d3e41a0b373, a71e4fc14aa1, aa1e2fbf45c9, b3bc0d09d4f1,
# dd9e9c4362c7, e1f97ef423d4). Что в них есть:
#
# * дерево слева (`/am/editor_drawer`, `role=grid` → `role=rowheader`
#   Campaign / Ad set / Ad, по одной строке объявления); у каждой строки две
#   кнопки, которые рисуются при наведении: быстрое действие с надписью
#   «default» (в прежнем мастере на этом месте «Duplicate») и «Action menu»
#   (`editor_tree:<уровень>/lib:ads-campaign-structure-action-menu-item/…`);
# * кнопок «Create ad» / «+» / «Duplicate» в дереве и у заголовка «Ad» НЕТ;
#   единственная надпись «Create ad» — вариант радио «Ad setup» внутри
#   раздела объявления (`/am/editor:ad/ad_existing_post_header_section`,
#   «Create ad — Create a new ad.»), и это не добавление объявления.
#
# В прежнем мастере (живьём с 28.07) «Create ad» — пункт меню «Action menu»
# строки группы (`/am/editor_drawer/lib:campaign-structure-item-action-menu-create`),
# и те же кнопки меню стоят в дереве нового редактора. Поэтому шаг —
# снимок, а не реализация:
#
# 1. снимок страницы с блоками дерева и раздела объявления — до любого нажатия;
# 2. кнопка с собственной надписью ровно «Create ad» / «Crear anuncio» в
#    дереве — нажать один раз (через охрану `_press_allowed`), подождать,
#    снять открывшееся;
# 3. такой кнопки нет — открыть «Action menu» строки группы (оно только
#    открывает меню), снять меню; пункт с надписью ровно «Create ad» —
#    нажать один раз, подождать, снять открывшееся.
#
# Дальше ничего: ни заполнения, ни закрытия меню/окна, ни Escape, ни
# перезагрузки. «default», Duplicate, Delete, Discard, Publish и футер не
# нажимаются никогда. Причина в итоге кампании —
# `simple_create_more_ads:captured-for-implementation`.

#: Выключатель нажатий этого шага (снимок до нажатий остаётся):
#: FB_RPA_SIMPLE_MORE_ADS_PRESS=0.
MORE_ADS_PRESS_SETTING = "FB_RPA_SIMPLE_MORE_ADS_PRESS"

#: Имя шага в телеметрии, в снимках и в итоге кампании.
MORE_ADS_STEP = "simple_create_more_ads"

#: Причина в итоге кампании, когда снимок есть…
MORE_ADS_CAPTURED = "captured-for-implementation"
#: …и когда снять не удалось (лимит снимков прогона, страница не ответила).
MORE_ADS_NOT_CAPTURED = "not-created"

#: Собственная надпись кнопки добавления объявления — ровно она: «Create ad
#: set», «Create ad in…» и описание радио «Ad setup» не подходят.
CREATE_AD_RE = r"^(create ad|crear anuncio)$"

#: «Action menu» строки группы в дереве (хвост surface — тип родителя, по
#: нему не сверяем; см. `meta_dom.create_additional_ad`).
_ADSET_MENU_SURFACE = "editor_tree:adset/lib:ads-campaign-structure-action-menu-item"

#: Блоки снимка: имя → JS-выражение, возвращающее элемент (`page_capture`).
#: Каждый найденный сохраняется целиком (`block_<имя>.html`), даже когда
#: страница обрезана по потолку.
MORE_ADS_BLOCKS = {
    # Дерево — список объявлений и навигация по уровням.
    "ad_tree": "document.querySelector('[data-surface=\"/am/editor_drawer\"]')",
    # Раздел объявления: объявление 1 до нажатия и то, что откроет «Create ad».
    "ad_section": "document.querySelector('[data-surface=\"/am/editor:ad\"]')",
    # Открытое меню (Action menu строки дерева).
    "menu": "[...document.querySelectorAll('[role=menu]')].find(e => e.getClientRects().length > 0) || null",
    # Открытое окно (мастер креатива, подтверждение) — кроме уведомлений.
    "dialog": ("[...document.querySelectorAll('[role=dialog], [aria-modal=true]')]"
               ".find(e => e.getClientRects().length > 0"
               " && !/^notifications/i.test((e.textContent || '').trim())) || null"),
}

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
      labels[l] = (labels[l] || 0) + 1;
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
имён строк), есть ли кнопка ровно «Create ad» и «Action menu» строки группы.
Найденное помечается `data-acl-more-ads` для нажатия."""

_MORE_ADS_MENU_JS = r"""
  (createRe) => {
    const norm = (s) => (s || '').replace(/[\s​]+/g, ' ').trim();
    const re = new RegExp(createRe, 'i');
    const shown = (e) => e.getClientRects().length > 0;
    const menus = [...document.querySelectorAll('[role=menu]')].filter(shown);
    // Пункты открытого меню, а не все видимые пункты документа (ревью PR #365).
    const items = (menus.length ? menus.flatMap(m => [...m.querySelectorAll('[role=menuitem]')])
                                : [...document.querySelectorAll('[role=menuitem]')]).filter(shown);
    const own = (b) => norm(b.getAttribute('aria-label')) || norm(b.textContent);
    const labels = items.map(own).filter(l => l && l.length <= 40).slice(0, 20);
    const create = items.filter(i => re.test(own(i)));
    document.querySelectorAll('[data-acl-more-ads="menu_create"]').forEach(e => e.removeAttribute('data-acl-more-ads'));
    if (create[0]) create[0].setAttribute('data-acl-more-ads', 'menu_create');
    const surf = create[0] ? create[0].closest('[data-surface]') : null;
    return { open: menus.length > 0 || items.length > 0, labels, create: create.length,
             create_surface: surf ? (surf.getAttribute('data-surface') || '').slice(0, 120) : '' };
  }
"""
"""Открытое меню: есть ли оно, надписи пунктов, пункт ровно «Create ad» (помечается)."""

_MORE_ADS_AFTER_JS = r"""
  () => {
    const tree = document.querySelector('[data-surface="/am/editor_drawer"]');
    const ads = tree ? tree.querySelectorAll('[role=rowheader][aria-label="Ad"]').length : 0;
    const dialog = [...document.querySelectorAll('[role=dialog], [aria-modal=true]')]
      .some(e => e.getClientRects().length > 0 && !/^notifications/i.test((e.textContent || '').trim()));
    return { ads, dialog };
  }
"""
"""После «Create ad»: строк объявлений в дереве и открыто ли окно."""


def more_ads_press_enabled() -> bool:
    """Нажимать ли в шаге второго объявления (по умолчанию — да)."""
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


def _wait_after_create(page, ads_before: int, wait_s: float) -> dict:
    md = _md()
    got: dict = {}
    md._sleep_h(1.5)
    deadline = time.time() + wait_s
    while True:
        try:
            got = md._eval_bounded(page, _MORE_ADS_AFTER_JS) or {}
        except Exception:
            got = {}
        if got.get("dialog") or int(got.get("ads") or 0) > ads_before or time.time() >= deadline:
            return got
        time.sleep(0.4)


def capture_more_ads(page, extra: int) -> dict:
    """Снять список объявлений и «Create ad» нового редактора (ACL-1353, шаг 1).

    Второе объявление здесь не создаётся и не заполняется. Робот снимает
    дерево до любого нажатия; затем (если нажатия не выключены
    `FB_RPA_SIMPLE_MORE_ADS_PRESS=0`) нажимает «Create ad» — кнопку дерева
    или пункт «Action menu» строки группы — один раз, снимает открывшееся и
    останавливает шаг. Строка телеметрии `simple_create_more_ads` (ok=False)
    называет снимки — по ней же событие Sentry.

    Возвращает {"captures": [...], "pressed": [...], "stop": <где и почему
    шаг кончился>, "reason": MORE_ADS_CAPTURED | MORE_ADS_NOT_CAPTURED,
    "tree"/"menu"/"after": что было прочитано}. Никогда не бросает.
    """
    md = _md()
    out: dict = {"captures": [], "pressed": [], "stop": "", "extra": extra}
    try:
        _capture_more_ads(page, out)
    except Exception as exc:
        out["stop"] = out.get("stop") or "error:" + type(exc).__name__
    out["reason"] = MORE_ADS_CAPTURED if out["captures"] else MORE_ADS_NOT_CAPTURED
    try:
        md._telemetry.record(
            MORE_ADS_STEP, False, note=out["reason"],
            diag={"captures": out["captures"], "pressed": out["pressed"], "stop": out["stop"],
                  "extra_ads": extra, "tree": out.get("tree"), "menu": out.get("menu"),
                  "after": out.get("after"),
                  "capture_note": (f"page capture saved on this machine: {', '.join(out['captures'])}"
                                   if out["captures"] else "no page capture was saved")})
    except Exception:
        pass
    return out


def _capture_more_ads(page, out: dict) -> None:
    md = _md()
    try:
        tree = md._eval_bounded(page, _MORE_ADS_TREE_JS, [CREATE_AD_RE, _ADSET_MENU_SURFACE]) or {}
    except Exception as exc:
        tree = {"tree": False, "error": type(exc).__name__}
    out["tree"] = tree
    ads_before = int((tree.get("levels") or {}).get("Ad") or 0)
    # 1. До любого нажатия.
    first = _more_ads_capture(page, "before_click", {"tree": tree})
    if first:
        out["captures"].append(first)
    if not tree.get("tree"):
        out["stop"] = "no_tree"
        return
    if not more_ads_press_enabled():
        out["stop"] = "press_off"
        return
    # Объявление 1 сохранено до первого нажатия (ревью PR #365).
    try:
        md.wait_editor_saved(page)
    except Exception:
        pass
    # 2. Кнопка ровно «Create ad» в самом дереве.
    if tree.get("create"):
        got = _press_marked(page, "create")
        if got != "pressed":
            out["stop"] = f"create_ad_{got}"
            return
        out["pressed"].append("tree_create_ad")
        _after_create(page, out, ads_before, via="tree")
        return
    # 3. «Action menu» строки группы — открыть меню и снять его.
    if not tree.get("adset_menu"):
        out["stop"] = "no_create_control"
        return
    got = _press_marked(page, "adset_menu", dom_fallback=True)
    if got not in ("pressed", "dom"):
        out["stop"] = f"action_menu_{got}"
        return
    out["pressed"].append("adset_action_menu" + ("_dom" if got == "dom" else ""))
    menu = _wait_menu(page, 4.0)
    out["menu"] = menu
    shot = _more_ads_capture(page, "action_menu", {"menu": menu})
    if shot:
        out["captures"].append(shot)
    if not menu.get("open"):
        out["stop"] = "action_menu_not_opened"
        return
    if not menu.get("create"):
        out["stop"] = "no_create_item"
        return
    got = _press_marked(page, "menu_create")
    if got != "pressed":
        out["stop"] = f"create_ad_{got}"
        return
    out["pressed"].append("menu_create_ad")
    _after_create(page, out, ads_before, via="action_menu")


def _after_create(page, out: dict, ads_before: int, *, via: str) -> None:
    after = _wait_after_create(page, ads_before, 6.0)
    out["after"] = after
    shot = _more_ads_capture(page, "after_create_ad", {"after": after, "via": via})
    if shot:
        out["captures"].append(shot)
    out["stop"] = "create_ad_pressed"
