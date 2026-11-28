"""ACL-2787: нативная кнопка Ads Manager «Use Facebook Page» внутри профиля аккаунта.

Публичное ребро POST для Page-backed Instagram отклонено (ACL-2221), поэтому идентичность создаётся
нативным контролом Ads Manager (решение владельца 2026-11-25): НОВОЕ неотправленное объявление ->
блок «Instagram profile» -> «Use Facebook Page» для ровно этой Страницы -> вкладка закрывается. Внутренний GraphQL-вызов не используется.

Правила:
- вкладка открывается ТОЛЬКО в браузере профиля этого аккаунта (`cdp_url` рабочей вкладки, как загрузочная
  вкладка в token_capture); обычного браузера нет, запасного пути нет;
- открывается только адрес нового объявления (`urls.new_ad` в config/ads_manager_selectors.yaml); редактор
  существующего объявления не открывается никогда (его открытие портит Leads-объявления) - если адрес
  оказался таким, ничего не нажимается;
- нажимается ровно один элемент: «Use Facebook Page» в блоке «Instagram profile»; Publish и всё из
  config/rpa_forbidden_zones.yaml не нажимается;
- любая неудача возвращает короткую причину (English), вкладка закрывается всегда, повторов нет.
- ВНИМАНИЕ: simple_create - редактор кампании Meta, он может оставить на стороне Meta черновик; драйвер его не
  удаляет (безопасного удаления ровно своего черновика в репозитории нет, решение владельца - ACL-2787).
Тексты блока - в config/ads_manager_selectors.yaml (`identity_block`), других селекторов у драйвера нет.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Callable

import yaml

_ROOT = Path(__file__).resolve().parents[2]
_SELECTORS = _ROOT / "config" / "ads_manager_selectors.yaml"
_FORBIDDEN = _ROOT / "config" / "rpa_forbidden_zones.yaml"

#: Сколько ждать появления блока «Instagram profile» после открытия нового объявления.
LOAD_WAIT_S = 45.0
#: Сколько ждать итога после клика (кнопка исчезает из блока).
SETTLE_WAIT_S = 12.0
POLL_S = 0.5
#: Блок найден, а кнопки нет столько опросов подряд - кнопки в нём нет.
NO_BUTTON_POLLS = 3

#: Всегда запрещено, даже если файл запретных зон не прочитался.
_ALWAYS_FORBIDDEN = re.compile(r"(?i)publish|review and publish|discard|^next$|^continue$")


def _norm(text: Any) -> str:
    return " ".join(str(text or "").split()).casefold()


def _config() -> dict:
    data = yaml.safe_load(_SELECTORS.read_text(encoding="utf-8")) or {}
    block = data.get("identity_block") or {}
    return {
        "url": str((data.get("urls") or {}).get("new_ad") or ""),
        "page": [_norm(t) for t in block.get("page_heading") or []],
        "ig": [_norm(t) for t in block.get("instagram_heading") or []],
        "btn": [_norm(t) for t in block.get("use_page_button") or []],
    }


def _forbidden_patterns() -> list[re.Pattern]:
    try:
        rows = (yaml.safe_load(_FORBIDDEN.read_text(encoding="utf-8")) or {}).get("forbidden_click_texts") or []
        return [re.compile(str(r["pattern"])) for r in rows if r.get("pattern")]
    except Exception:  # noqa: BLE001 - остаётся встроенный запрет
        return []


def _is_forbidden(text: str) -> bool:
    text = " ".join(str(text or "").split())
    return bool(_ALWAYS_FORBIDDEN.search(text)) or any(p.search(text) for p in _forbidden_patterns())


#: Одно чтение блока (страница не меняется, кроме прокрутки к кнопке). cfg - тексты из yaml.
_PROBE_JS = r"""
((cfg) => {
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const vis = (e) => e.getClientRects().length > 0;
  const out = {href: location.href, state: 'no_block', shown: '', modal: false};
  const modal = [...document.querySelectorAll('[role=dialog], [aria-modal=true]')]
    .find((e) => vis(e) && !/^notifications/i.test(norm(e.textContent)));
  out.modal = !!modal;
  const heads = [...document.querySelectorAll('[role=heading]')].filter(vis);
  const ig = heads.find((h) => cfg.ig.includes(norm(h.textContent)));
  if (!ig) return out;
  const pgs = heads.filter((h) => cfg.page.includes(norm(h.textContent)));
  let block = ig.parentElement;
  while (block && !pgs.some((h) => block.contains(h))) block = block.parentElement;
  if (!block) return out;
  const ph = pgs.find((h) => block.contains(h));
  const F = Node.DOCUMENT_POSITION_FOLLOWING, P = Node.DOCUMENT_POSITION_PRECEDING;
  const leaf = (e) => ![...e.children].some((c) => norm(c.textContent));
  const shown = [...block.querySelectorAll('span')].find((s) => vis(s) && leaf(s) && norm(s.textContent)
    && (ph.compareDocumentPosition(s) & F) && (ig.compareDocumentPosition(s) & P));
  out.shown = shown ? shown.textContent.trim() : '';
  out.state = 'no_button';
  const btn = [...block.querySelectorAll('span, div, button, [role=button]')].find((e) => vis(e) && leaf(e)
    && cfg.btn.includes(norm(e.textContent)) && (ig.compareDocumentPosition(e) & F));
  if (!btn) return out;
  btn.scrollIntoView({block: 'center'});
  const r = btn.getBoundingClientRect();
  const x = r.left + r.width / 2, y = r.top + r.height / 2;
  const hit = document.elementFromPoint(x, y);
  out.hit = hit ? (hit.textContent || '').trim().slice(0, 80) : '';
  out.state = hit && block.contains(hit) && cfg.btn.includes(norm(hit.textContent)) ? 'ready' : 'covered';
  out.x = x; out.y = y;
  return out;
})
"""


def _probe(tab: Any, cfg: dict) -> dict:
    got = tab.evaluate_main(f"({_PROBE_JS})({json.dumps(cfg)})")
    return got if isinstance(got, dict) else {"state": "no_block", "href": "", "modal": False}


def _bad_page(href: str, digits: str) -> str:
    """Белый список: только simple_create ЭТОГО кабинета без выбранных объектов; иначе причина (пусто - можно)."""
    from urllib.parse import parse_qs, urlsplit
    parts = urlsplit(href)
    path = parts.path.lower().rstrip("/")
    if any(k in path for k in ("/checkpoint", "/login", "/recover", "/two_step_verification", "/two_factor")):
        return "unexpected page: the profile shows a login or checkpoint page"
    query = parse_qs(parts.query)
    if path != "/adsmanager/manage/simple_create":
        return "unexpected page: it is not the new ad editor, nothing was clicked"
    if [re.sub(r"\D", "", v) for v in query.get("act", [])] != [digits]:
        return "unexpected page: Ads Manager opened another ad account, nothing was clicked"
    if any(k.startswith("selected_") for k in query):
        return "unexpected page: an existing campaign, ad set or ad is open, nothing was clicked"
    return ""


def press(act: str, page_id: str, *, cdp_url: str = "", page_name: str = "",
          open_tab: Callable[[str], Any] | None = None, load_wait_s: float = LOAD_WAIT_S,
          settle_wait_s: float = SETTLE_WAIT_S, poll_s: float = POLL_S) -> str:
    """Нажать «Use Facebook Page» для Страницы. Возвращает "" при успехе, иначе короткую причину (English)."""
    if not cdp_url:
        return "the account's profile tab is not available"
    if not _norm(page_name):
        return "the Page name is unknown, so the Page shown in the editor cannot be checked"
    cfg = _config()
    digits = re.sub(r"\D", "", str(act or ""))
    if not (cfg["url"] and cfg["ig"] and cfg["page"] and cfg["btn"] and digits):
        return "the Ads Manager selectors for the Instagram profile block are not configured"
    url = cfg["url"].format(act_id=digits)
    if _bad_page(url, digits):
        return "the new ad address is not a new ad"
    from src.fb_api import light_tab
    from src.fb_api.cdp import PageTab

    try:
        tab = (open_tab or PageTab.open)(cdp_url)
    except Exception as exc:  # noqa: BLE001
        return f"could not open a tab in the profile ({_short(exc)})"
    try:
        _leave_dialogs(tab)
        return _drive(tab, url, digits, cfg, _norm(page_name), load_wait_s, settle_wait_s, poll_s)
    except Exception as exc:  # noqa: BLE001 - любая неудача - причина, не падение
        return f"the press failed ({_short(exc)})"
    finally:
        light_tab.settle_and_close(tab)


def _short(exc: BaseException) -> str:
    return (str(exc) or type(exc).__name__).splitlines()[0][:100]


def _leave_dialogs(tab: Any) -> None:
    """«Leave site?» (beforeunload) - уйти без сохранения: черновик не нужен."""
    def on_event(ev: dict) -> None:
        if ev.get("method") == "Page.javascriptDialogOpening":
            leave = (ev.get("params") or {}).get("type") == "beforeunload"   # остальные окна - отказ
            try:
                tab.call("Page.handleJavaScriptDialog", {"accept": leave})
            except Exception:  # noqa: BLE001
                pass
    try:
        tab.on_event = on_event
    except Exception:  # noqa: BLE001
        pass


def _wait(tab: Any, seconds: float) -> None:
    pump = getattr(tab, "pump", None)
    if callable(pump):
        pump(seconds)
    else:
        time.sleep(seconds)


def _drive(tab: Any, url: str, digits: str, cfg: dict, want: str, load_wait_s: float, settle_wait_s: float, poll_s: float) -> str:
    try:
        tab.navigate(url, timeout=45.0)
    except Exception as exc:  # noqa: BLE001
        return f"the new ad editor did not open ({_short(exc)})"
    deadline = time.monotonic() + load_wait_s
    no_button = 0
    last: dict | None = None
    while True:
        got = _probe(tab, cfg)
        why = _bad_page(str(got.get("href") or ""), digits)
        if why:
            return why
        state = got.get("state")
        if state in ("no_button", "ready", "covered") and _norm(got.get("shown")) != want:
            return f"the editor shows another Page ({str(got.get('shown') or 'none')[:60]}), not the target Page"
        if state == "ready":
            # координаты берём, только когда два опроса подряд дали одно место (разметка ещё могла двигаться)
            if last and (last.get("x"), last.get("y")) == (got.get("x"), got.get("y")):
                break
            last = got
            if time.monotonic() >= deadline:
                return "timed out: the 'Use Facebook Page' control kept moving"
            _wait(tab, poll_s)
            continue
        last = None
        no_button = no_button + 1 if state == "no_button" else 0
        if no_button >= NO_BUTTON_POLLS:
            return "the Instagram profile block has no 'Use Facebook Page' control"
        if time.monotonic() >= deadline:
            return ("timed out: the Instagram profile block did not appear" if state == "no_block"
                    else "timed out: the 'Use Facebook Page' control was covered")
        _wait(tab, poll_s)
    if _is_forbidden(got.get("hit") or ""):
        return "the control under the pointer is a forbidden zone, nothing was clicked"
    x, y = float(got["x"]), float(got["y"])
    tab.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
    tab.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1})
    tab.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1})
    deadline = time.monotonic() + settle_wait_s
    while True:
        _wait(tab, poll_s)
        after = _probe(tab, cfg)
        if after.get("modal"):
            return "an unexpected dialog opened after the click"
        if after.get("state") != "ready":
            return ""
        if time.monotonic() >= deadline:
            return "timed out: the 'Use Facebook Page' control stayed after the click"
