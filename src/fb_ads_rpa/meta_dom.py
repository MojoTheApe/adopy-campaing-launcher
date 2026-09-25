"""DOM helpers for Meta Ads Manager — label-based fills that survive UI refactors."""
from __future__ import annotations

import json
import math
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import Frame, Page

from src.fb_ads_rpa import run_telemetry as _telemetry

#: Порог, за которым Playwright отказывается передавать файл.
#:
#: Дословно из его же ошибки, пойманной 14.09 на живом прогоне:
#: «FileChooser.set_files: Cannot transfer files larger than 50Mb to a browser
#: not co-located with the server». Ограничение клиента Playwright: он шлёт
#: СОДЕРЖИМОЕ файла по CDP и отказывается делать это для больших файлов, когда
#: считает соединение удалённым. У нас оно таким и считается — CDP по TCP через
#: Multilogin, — хотя браузер физически на этой же машине.
#:
#: Берём с запасом: 45, а не 50. Порог у них на содержимом, а не на размере
#: файла, и упереться в него на 49.8 МБ было бы обидно.
#:
#: **15.09: порог опущен до 4 МБ, и вот почему.** Пятьдесят мегабайт — это
#: граница, за которой Playwright отказывается передавать *в принципе*. Но
#: задолго до неё есть вторая, куда более близкая: у `set_files` свой таймаут в
#: **30 секунд**, а содержимое идёт по CDP через Multilogin со скоростью,
#: которую мы в тот день намерили — **0.13 МБ/с**. Тридцать секунд на такой
#: скорости — это около четырёх мегабайт. Всё, что тяжелее, физически не
#: успевает, и шаг отказывает не «сломавшись», а упёршись в чужой таймаут:
#:
#:     попытка 1  chooser: True   FileChooser.set_files: Timeout 30000ms exceeded
#:     попытка 2  окно выбора уже не приходит вовсе
#:     попытка 3  то же самое
#:
#: Ролик на 24 МБ отказывал так трижды подряд — и с включённым поиском по
#: медиатеке, и с выключенным, то есть дело именно здесь.
#:
#: Тяжёлая дорога этой беды не знает: она передаёт **путь**, а не содержимое, —
#: Chrome читает файл с диска сам, мгновенно, и скорость CDP значения не имеет.
#: Она и написана ради этого; просто включалась непозволительно поздно.
#:
#: Почему не ноль: для мелочи (медиана креатива — 0.8 МБ) простая дорога
#: проверена сотней загрузок, а тяжёлая тянет за собой перехват диалога и
#: отдельную CDP-сессию.
#:
#: Почему именно три, а не четыре: 30 секунд × 0.13 МБ/с = 3.9 МБ. Четыре —
#: это уже за краем, и тест на это падает. Число здесь **выводится из замера**,
#: а не выбирается на глаз; в этом и смысл проверки рядом.
#:
#: Скорость сети меряется в мегабайтах в секунду и меняется; поэтому порог
#: задан переменной. Если однажды окажется, что и четыре не успевают, менять
#: надо здесь, а не гадать по симптомам.
def _pw_file_limit_mb() -> float:
    try:
        value = float(os.environ.get("FB_RPA_PW_FILE_LIMIT_MB") or 3)
    except (TypeError, ValueError):
        return 3.0
    return value if math.isfinite(value) and 0 < value <= 45 else 3.0


_PW_FILE_LIMIT_MB = _pw_file_limit_mb()

#: Сколько даём `set_files` на передачу содержимого.
#:
#: Умолчание Playwright — 30 секунд, и именно в него упирался ролик на 24 МБ.
#: Порог выше сделан так, чтобы до этого таймаута дело не доходило вовсе; но
#: страховка дешёвая, а сеть у байера может оказаться медленнее нашей. Три
#: минуты — это 4 МБ даже на 0.02 МБ/с, вдесятеро хуже намеренного.
#:
#: Ждать дольше незачем: если за три минуты не ушло четыре мегабайта, дело не
#: в терпении, и отказ полезнее ещё одного молчаливого ожидания.
_PW_SET_FILES_MS = 180_000


def _upload_heavy_via_chooser_event(
    page: Page, creative_path: str, diag: dict, click, *, wait_s: float = 25.0
) -> bool:
    """Set a local file through the CDP target that emits its chooser event.

    An out-of-process media frame has a separate CDP session. Its backend
    node ID must be used in that session, not in the main page's renderer.
    Pump Playwright while waiting so delayed events can actually be received.
    Temporary interception is disarmed and detached on every exit.
    """
    path = str(Path(creative_path).resolve())
    sessions = []
    attempted = set()
    caught = {}

    def disarm(session):
        try:
            session.send("Page.setInterceptFileChooserDialog", {"enabled": False})
        except Exception:
            pass
        try:
            session.detach()
        except Exception:
            pass

    def arm(target, *, required=False):
        try:
            session = page.context.new_cdp_session(target)
        except Exception as exc:
            if required:
                diag["heavy_error"] = f"new_cdp_session: {type(exc).__name__}: {exc}"[:200]
            return False
        try:
            def chooser(params):
                if not caught and (params or {}).get("backendNodeId"):
                    caught.update(params)
                    caught["session"] = session
            session.on("Page.fileChooserOpened", chooser)
            session.send("Page.enable")
            session.send("DOM.enable")
            session.send("Page.setInterceptFileChooserDialog", {"enabled": True})
            sessions.append(session)
            return True
        except Exception as exc:
            if required:
                diag["heavy_error"] = f"intercept: {type(exc).__name__}: {exc}"[:200]
            disarm(session)
            return False

    def arm_frames():
        for frame in getattr(page, "frames", []):
            if frame == page.main_frame:
                continue
            key = (frame, frame.url)
            if key not in attempted:
                attempted.add(key)
                # In-process frames share their parent's session. Playwright
                # refuses a separate CDP session for those; that is expected.
                arm(frame)

    try:
        if not arm(page, required=True):
            return False
        arm_frames()
        diag["heavy_clicked"] = bool(click())
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline and not caught:
            page.wait_for_timeout(100)
            arm_frames()
        backend = caught.get("backendNodeId")
        diag["heavy_backend_node"] = backend
        if not backend:
            diag["heavy_error"] = "событие Page.fileChooserOpened не пришло"
            return False
        caught["session"].send("DOM.setFileInputFiles", {"files": [path], "backendNodeId": backend})
        return True
    except Exception as exc:
        diag["heavy_error"] = f"setFileInputFiles: {type(exc).__name__}: {exc}"[:200]
        return False
    finally:
        for session in reversed(sessions):
            disarm(session)


#: След последней попытки загрузить креатив — что шаг видел и делал.
#: Читается тем, кто пишет отказ в телеметрию: без него `ad_creative_upload`
#: приходил пустым, и шесть отказов подряд 14.09 не дали ни одной зацепки.
_LAST_UPLOAD_DIAG: dict = {}

#: The same record, per thread. Two publishes run at once since acl-1210,
#: and the module-level copy above is whichever thread wrote last — kept for
#: the tests and for anyone reading it from the outside, but the flow reads
#: its own thread's record through `last_upload_diag()`.
_upload_diag_tls = threading.local()


def last_upload_diag() -> dict:
    """The upload diagnostic of THIS thread's last creative, or the global one."""
    own = getattr(_upload_diag_tls, "diag", None)
    return dict(own if own is not None else _LAST_UPLOAD_DIAG)


def _set_last_upload_diag(diag: dict) -> None:
    global _LAST_UPLOAD_DIAG
    _LAST_UPLOAD_DIAG = diag
    _upload_diag_tls.diag = diag
from src.fb_ads_rpa.config import get_reuse_library_media

PageLike = Page | Frame


def _roots(page: Page) -> list[PageLike]:
    out: list[PageLike] = [page]
    for frame in page.frames:
        if frame != page.main_frame:
            out.append(frame)
    return out


def _norm_label(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def fill_input_near_label(
    page: Page,
    label_variants: list[str],
    value: str,
    *,
    exact: bool = False,
) -> bool:
    """Find a visible input/textarea under a label-like element and set value."""
    targets = [_norm_label(v) for v in label_variants if v.strip()]
    if not targets or not value:
        return False

    script = """
    ([labels, value, exact]) => {
      const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
      const targets = labels.map(norm);
      const match = (text) => {
        const t = norm(text);
        if (!t) return false;
        return exact
          ? targets.includes(t)
          : targets.some((l) => t === l || t.startsWith(l));
      };
      const setValue = (inp, val) => {
        if (!inp || inp.offsetParent === null) return false;
        const rect = inp.getBoundingClientRect();
        if (rect.width < 2 || rect.height < 2) return false;
        inp.focus();
        inp.click();
        const proto = inp.tagName === 'TEXTAREA'
          ? window.HTMLTextAreaElement.prototype
          : window.HTMLInputElement.prototype;
        const desc = Object.getOwnPropertyDescriptor(proto, 'value');
        if (desc && desc.set) {
          desc.set.call(inp, val);
        } else if (inp.isContentEditable) {
          inp.textContent = val;
        } else if ('value' in inp) {
          inp.value = val;
        } else {
          return false;
        }
        inp.dispatchEvent(new InputEvent('input', { bubbles: true, data: val }));
        inp.dispatchEvent(new Event('change', { bubbles: true }));
        return true;
      };
      const candidates = document.querySelectorAll(
        'label, span, div, h2, h3, [role=heading]'
      );
      for (const el of candidates) {
        if (el.children.length > 3) continue;
        if (!match(el.textContent)) continue;
        let node = el;
        for (let depth = 0; depth < 8 && node; depth++) {
          const inputs = node.querySelectorAll(
            'input:not([type=hidden]):not([type=checkbox]):not([type=radio]), textarea, [contenteditable=true]'
          );
          for (const inp of inputs) {
            if (setValue(inp, value)) return true;
          }
          node = node.parentElement;
        }
      }
      return false;
    }
    """
    for root in _roots(page):
        try:
            if root.evaluate(script, [label_variants, value, exact]):
                return True
        except Exception:
            pass
    return False


def click_sidebar_tree_index(page: Page, index: int) -> bool:
    """Click campaign tree item: 0=campaign, 1=ad set, 2=ad.

    Live evidence (2026-07-20): a real user click on this row changes the URL
    path itself (/manage/campaigns/edit/... -> /manage/adsets/edit/...), i.e.
    it triggers Meta's client-side router. A JS `el.click()` synthetic click
    only toggled this row's own highlighted/selected look without ever
    triggering that route change — the main panel stayed on stale Campaign
    content indefinitely (confirmed across multiple runs and several seconds
    of extra waiting, so it isn't a render-timing race). Real mouse input via
    CDP (same workaround already used elsewhere in this file for Playwright
    connect_over_cdp click drops, #41286) is required for the router to fire.
    So: JS only *locates and scrolls* the target row and returns its bounding
    box; Python dispatches the actual click via _mouse_click_chain.
    """
    locate_script = """
    (index) => {
      const visible = (el) => {
        if (!el || !el.offsetParent) return false;
        const st = getComputedStyle(el);
        return st.visibility !== 'hidden' && st.display !== 'none';
      };
      const bboxOf = (el) => {
        if (!el) return null;
        el.scrollIntoView({ block: 'nearest', inline: 'nearest' });
        const r = el.getBoundingClientRect();
        return { x: r.x, y: r.y, width: r.width, height: r.height };
      };

      const tree = [...document.querySelectorAll('[role="treeitem"]')].filter(visible);
      if (tree.length > index) {
        return bboxOf(tree[index]);
      }

      // Left wizard nav: one clickable row per vertical band.
      const bands = new Map();
      for (const el of document.querySelectorAll('div, a, [role="button"], span')) {
        if (!visible(el)) continue;
        const r = el.getBoundingClientRect();
        if (r.left > 300 || r.top < 70 || r.height < 16 || r.height > 100) continue;
        if (r.width < 80) continue;
        const text = (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
        if (!text || text.length > 160) continue;
        const band = Math.round(r.top / 18) * 18;
        const area = r.width * r.height;
        const prev = bands.get(band);
        if (!prev || area > prev.area) {
          bands.set(band, { el, top: r.top, area, text });
        }
      }
      const rows = [...bands.values()].sort((a, b) => a.top - b.top);
      if (rows.length > index) {
        return bboxOf(rows[index].el);
      }

      if (index === 1) {
        const patterns = [/\\bad\\s*set\\b/i, /\\bconjunto\\b/i, /new leads ad set/i];
        for (const el of document.querySelectorAll('div, span, a, [role="button"]')) {
          if (!visible(el)) continue;
          const r = el.getBoundingClientRect();
          if (r.left > 300) continue;
          const text = (el.innerText || el.textContent || '').trim();
          if (!patterns.some((p) => p.test(text))) continue;
          let node = el;
          let best = el;
          while (node && node.parentElement) {
            const pr = node.getBoundingClientRect();
            if (pr.left > 300) break;
            if (pr.width >= 80 && pr.height >= 16 && pr.height <= 100) best = node;
            node = node.parentElement;
          }
          return bboxOf(best);
        }
      }

      if (index === 2) {
        const patterns = [/^new leads ad$/i, /^anuncio$/i];
        for (const el of document.querySelectorAll('div, span, a, [role="button"]')) {
          if (!visible(el)) continue;
          const r = el.getBoundingClientRect();
          if (r.left > 300) continue;
          const text = (el.innerText || el.textContent || '').trim();
          if (!patterns.some((p) => p.test(text))) continue;
          if (/ad\\s*set|conjunto/i.test(text)) continue;
          return bboxOf(el);
        }
      }

      return null;
    }
    """
    for root in _roots(page):
        try:
            bbox = root.evaluate(locate_script, index)
        except Exception:
            bbox = None
        if not bbox:
            continue
        cx = float(bbox["x"]) + float(bbox["width"]) / 2
        cy = float(bbox["y"]) + float(bbox["height"]) / 2
        _mouse_click_chain(page, cx, cy)
        _sleep_h(0.3)
        return True
    return False


def wizard_sidebar_selected_index(page: Page) -> int | None:
    """Return 0=campaign, 1=ad set, 2=ad from left wizard tree selection."""
    script = """
    () => {
      const visible = (el) => !!(el && el.offsetParent !== null);
      const items = [...document.querySelectorAll('[role=treeitem]')].filter(visible);
      for (let i = 0; i < items.length; i++) {
        const el = items[i];
        if (
          el.getAttribute('aria-selected') === 'true' ||
          el.getAttribute('aria-current') === 'page' ||
          el.getAttribute('aria-current') === 'step'
        ) {
          return i;
        }
      }
      // Meta often uses a blue left bar on the active row without aria-selected.
      const rows = [];
      for (const el of document.querySelectorAll('div, a, [role=button], [role=treeitem]')) {
        if (!visible(el)) continue;
        const r = el.getBoundingClientRect();
        if (r.left > 300 || r.top < 70 || r.height < 16 || r.height > 100) continue;
        if (r.width < 80) continue;
        const text = (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
        if (!text || text.length > 160) continue;
        const style = getComputedStyle(el);
        const bg = style.backgroundColor || '';
        const active =
          bg.includes('rgb(231') ||
          bg.includes('rgb(230') ||
          bg.includes('rgb(24, 119, 242') ||
          el.querySelector('[style*="border-left"]') !== null;
        rows.push({ i: rows.length, top: r.top, text, active, el });
      }
      rows.sort((a, b) => a.top - b.top);
      const deduped = [];
      let lastTop = -999;
      for (const row of rows) {
        if (Math.abs(row.top - lastTop) < 10) continue;
        deduped.push(row);
        lastTop = row.top;
      }
      for (let i = 0; i < deduped.length; i++) {
        if (deduped[i].active) return i;
      }
      return null;
    }
    """
    for root in _roots(page):
        try:
            idx = root.evaluate(script)
            if isinstance(idx, int) and 0 <= idx <= 4:
                return idx
        except Exception:
            pass
    return None


def click_wizard_breadcrumb_index(page: Page, index: int) -> bool:
    """Click breadcrumb segment: 0=campaign, 1=ad set, 2=ad."""
    for root in _roots(page):
        try:
            nav_links = root.locator('[role="navigation"] a, [role="navigation"] [role="link"]')
            if nav_links.count() > index:
                nav_links.nth(index).click(timeout=5000)
                _sleep_h(0.5)
                return True
        except Exception:
            pass
    script = """
    (index) => {
      const visible = (el) => !!(el && el.offsetParent !== null);
      const skip = new Set(['edit', 'in draft', 'borrador', 'campaigns', 'campañas']);
      const items = [];
      for (const el of document.querySelectorAll('a, span, div, [role=link], [role=button]')) {
        if (!visible(el)) continue;
        const r = el.getBoundingClientRect();
        if (r.top > 130 || r.top < 40 || r.left < 160) continue;
        const text = (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
        if (!text || text.length > 120) continue;
        if (skip.has(text.toLowerCase())) continue;
        if (text.includes('>')) continue;
        items.push({ el, left: r.left, top: r.top, text });
      }
      items.sort((a, b) => a.left - b.left || a.top - b.top);
      const trail = [];
      let lastLeft = -999;
      for (const item of items) {
        if (item.left <= lastLeft + 5) continue;
        trail.push(item);
        lastLeft = item.left;
      }
      if (trail.length > index) {
        trail[index].el.click();
        return true;
      }
      return false;
    }
    """
    for root in _roots(page):
        try:
            if root.evaluate(script, index):
                _sleep_h(0.5)
                return True
        except Exception:
            pass
    return False


def wizard_editor_level(page: Page) -> str:
    """Return campaign, ad_set, ad, or empty string."""
    idx = wizard_sidebar_selected_index(page)
    if idx == 2:
        return "ad"
    if idx == 1:
        return "ad_set"
    if idx == 0:
        return "campaign"

    if main_panel_has_label(
        page,
        [
            "Instant form",
            "Create instant form",
            "Destination",
            "Ad setup",
            "Primary text",
            "Add media",
            "Follow up with leads",
            "Formulario instantáneo",
        ],
    ):
        return "ad"
    if main_panel_has_label(
        page,
        [
            "Conversion location",
            "Conversion event",
            "Performance goal",
            "Detailed targeting",
        ],
    ):
        return "ad_set"
    if main_panel_has_label(
        page,
        ["Campaign details", "Campaign name", "Advantage+ campaign budget"],
    ):
        return "campaign"
    if main_panel_has_label(page, ["Ad set name", "Nombre del conjunto"]):
        return "ad_set"
    return ""


def click_wizard_sidebar_level(page: Page, level: int) -> bool:
    """Navigate wizard sidebar: 0=campaign, 1=ad set, 2=ad."""
    for root in _roots(page):
        try:
            items = root.locator('[role="treeitem"]')
            count = items.count()
            if count > level:
                item = items.nth(level)
                item.scroll_into_view_if_needed(timeout=5000)
                item.click(timeout=5000)
                _sleep_h(0.6)
                return True
        except Exception:
            pass
    if click_sidebar_tree_index(page, level):
        _sleep_h(0.5)
        return True
    if level in (0, 1, 2) and click_wizard_breadcrumb_index(page, level):
        _sleep_h(0.5)
        return True
    return False


def click_wizard_back_button(page: Page) -> bool:
    """Click wizard footer Back (Ad → Ad Set, Ad Set → Campaign)."""
    return click_text_button(page, ["Back", "Atrás", "Volver"])


def click_text_button(page: Page, texts: list[str]) -> bool:
    targets = [_norm_label(t) for t in texts]
    script = """
    (labels) => {
      const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
      const targets = labels.map(norm);
      for (const el of document.querySelectorAll('button, [role=button], div[role=button]')) {
        if (!el.offsetParent) continue;
        const t = norm(el.textContent);
        if (targets.includes(t)) {
          el.click();
          return true;
        }
      }
      return false;
    }
    """
    for root in _roots(page):
        try:
            if root.evaluate(script, targets):
                return True
        except Exception:
            pass
    return False


def main_panel_has_label(page: Page, label_variants: list[str]) -> bool:
    targets = [_norm_label(v) for v in label_variants]
    script = """
    (labels) => {
      const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
      const targets = labels.map(norm);
      for (const el of document.querySelectorAll('label, h2, h3, [role=heading], span, div')) {
        if (el.children.length > 4) continue;
        const t = norm(el.textContent);
        if (!targets.some((l) => t === l || t.startsWith(l))) continue;
        const rect = el.getBoundingClientRect();
        if (rect.width < 2 || rect.height < 2) continue;
        if (rect.left < 280) continue;
        return true;
      }
      return false;
    }
    """
    for root in _roots(page):
        try:
            if root.evaluate(script, targets):
                return True
        except Exception:
            pass
    return False


def read_input_near_label(page: Page, label_variants: list[str]) -> str:
    script = """
    (labels) => {
      const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
      const targets = labels.map(norm);
      const match = (text) => {
        const t = norm(text);
        return targets.some((l) => t === l || t.startsWith(l));
      };
      for (const el of document.querySelectorAll('label, span, div, h2, h3, [role=heading]')) {
        if (el.children.length > 3) continue;
        if (!match(el.textContent)) continue;
        let node = el;
        for (let depth = 0; depth < 8 && node; depth++) {
          for (const inp of node.querySelectorAll(
            'input:not([type=hidden]):not([type=checkbox]):not([type=radio]), textarea, [contenteditable=true]'
          )) {
            if (!inp.offsetParent) continue;
            return inp.value || inp.textContent || '';
          }
          node = node.parentElement;
        }
      }
      return '';
    }
    """
    for root in _roots(page):
        try:
            val = root.evaluate(script, label_variants)
            if isinstance(val, str):
                return val.strip()
        except Exception:
            pass
    return ""


def fill_input_near_label_verified(
    page: Page,
    label_variants: list[str],
    value: str,
) -> bool:
    if not fill_input_near_label(page, label_variants, value):
        return False
    import time
    _sleep_h(0.4)
    current = read_input_near_label(page, label_variants)
    return value.strip() in current or current.strip() == value.strip()


def select_option_near_section(page: Page, section_labels: list[str], option_labels: list[str]) -> bool:
    """Click a radio/card option under a section heading (e.g. Conversion location → Website)."""
    script = """
    ([sections, options]) => {
      const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
      const sectionTargets = sections.map(norm);
      const optionTargets = options.map(norm);
      const visible = (el) => !!(el && el.offsetParent !== null);
      let sectionNode = null;
      for (const el of document.querySelectorAll('label, h2, h3, span, div, [role=heading]')) {
        if (!visible(el) || el.children.length > 5) continue;
        const t = norm(el.textContent);
        if (!sectionTargets.some((s) => t === s || t.startsWith(s))) continue;
        const rect = el.getBoundingClientRect();
        if (rect.left < 200) continue;
        sectionNode = el;
        break;
      }
      const roots = sectionNode ? [sectionNode.parentElement, sectionNode] : [document.body];
      for (const root of roots) {
        if (!root) continue;
        for (const el of root.querySelectorAll(
          '[role=radio], [role=option], label, button, [role=button], div, span'
        )) {
          if (!visible(el)) continue;
          const t = norm(el.textContent);
          if (!optionTargets.some((o) => t === o || t.startsWith(o))) continue;
          el.click();
          return true;
        }
      }
      return false;
    }
    """
    for root in _roots(page):
        try:
            if root.evaluate(script, [section_labels, option_labels]):
                return True
        except Exception:
            pass
    return False


def _website_only_label(text: str) -> bool:
    n = _norm_label(text)
    return n in ("website", "sitio web")


#: Сколько всего ждать появления раздела (все подписи, все фреймы вместе).
#: Прежний вариант ждал 5 с КАЖДЫЙ способ поиска по очереди: `role=heading`
#: с такой подписью у Meta нет, и каждая прокрутка к «Conversion location» /
#: «Audience» / «Conversion event» платила 5 с за заголовок, которого нет,
#: прежде чем найти текст (ACL-1313: 10–15 с на кампанию). Найденное
#: возвращается сразу; 10 с — не меньше, чем прежний путь давал тексту
#: (5 с на заголовок + 5 с на текст).
SCROLL_SECTION_BUDGET_S = 10.0


def scroll_to_section(page: Page, *labels: str, budget_s: float = SCROLL_SECTION_BUDGET_S) -> bool:
    """Scroll main panel so a section heading/label is visible.

    Опрос без ожиданий: на каждом круге по порядку — подпись, фрейм,
    заголовок → текст → label; первый видимый прокручивается в окно. Порядок
    предпочтения прежний, но отсутствующий заголовок больше не стоит 5 с.
    """
    patterns = [re.compile(re.escape(label), re.I) for label in labels if label.strip()]
    if not patterns:
        return False
    deadline = time.time() + budget_s
    while True:
        for pattern in patterns:
            for root in _roots(page):
                for getter in (
                    lambda: root.get_by_role("heading", name=pattern).first,
                    lambda: root.get_by_text(pattern).first,
                    lambda: root.get_by_label(pattern).first,
                ):
                    try:
                        loc = getter()
                        if not loc.is_visible():
                            continue
                        loc.scroll_into_view_if_needed(timeout=5000)
                        _sleep_h(0.3)
                        return True
                    except Exception:
                        pass
        if time.time() >= deadline:
            return False
        time.sleep(0.25)


def _conversion_location_display_value(page: Page) -> str:
    script = """
    () => {
      const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
      const visible = (el) => !!(el && el.offsetParent !== null);
      let marker = null;
      for (const el of document.querySelectorAll('label, span, div, h2, h3, [role=heading]')) {
        if (!visible(el) || el.children.length > 4) continue;
        const t = norm(el.textContent);
        if (t === 'conversion location' || t.startsWith('conversion location')) {
          marker = el;
          break;
        }
        if (t === 'ubicación de la conversión' || t.startsWith('ubicación de la conversión')) {
          marker = el;
          break;
        }
      }
      if (!marker) return '';
      let node = marker.parentElement;
      for (let depth = 0; depth < 10 && node; depth++) {
        for (const ctrl of node.querySelectorAll(
          '[role=combobox], [role=button][aria-haspopup], button[aria-haspopup], select, [role=button], span, div'
        )) {
          if (!visible(ctrl)) continue;
          const raw = (ctrl.textContent || ctrl.value || ctrl.getAttribute('aria-label') || '').trim();
          const val = norm(raw);
          if (!val || val.length > 80) continue;
          if (
            val.includes('website') ||
            val.includes('instant') ||
            val.includes('form') ||
            val.includes('sitio web') ||
            val.includes('formularios')
          ) {
            return raw;
          }
        }
        node = node.parentElement;
      }
      return '';
    }
    """
    for root in _roots(page):
        try:
            val = root.evaluate(script)
            if isinstance(val, str) and val.strip():
                return val.strip()
        except Exception:
            pass
    return ""


def is_website_conversion_selected(page: Page) -> bool:
    """Check if 'Website' (and only Website, not 'Website and instant forms') is selected.

    Reads ONLY the dropdown trigger button's own text — not surrounding banners.
    Meta often shows an Advantage+ recommendation banner mentioning 'website and instant forms'
    after selecting 'Website', which confused the old text-search approach.
    """
    script = """
    () => {
      const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
      const ownText = (el) => norm(
        Array.from(el.childNodes)
          .filter(n => n.nodeType === Node.TEXT_NODE)
          .map(n => n.textContent).join('')
      );

      // Find the small 'Conversion location' heading/label element itself
      // (not a big wrapping container — cap children count so we get the
      // heading, not a div that also wraps the description + control).
      let marker = null;
      for (const el of document.querySelectorAll('label, [role=label], span, div, h2, h3, [role=heading]')) {
        if (el.offsetParent === null || el.children.length > 2) continue;
        const t = norm(el.textContent);
        if (t === 'conversion location' || t === 'ubicación de la conversión' ||
            t.startsWith('conversion location') || t.startsWith('ubicación de la conversión')) {
          marker = el;
          break;
        }
      }
      if (!marker) return false;

      // Walk forward in DOCUMENT ORDER from the marker (TreeWalker over the
      // whole body), not up-then-back-down through querySelectorAll on an
      // ancestor — that pattern matched elements anywhere inside a large
      // section (confirmed live 2026-07-24: a role-restricted version found
      // nothing at all since Meta's trigger has no button/combobox role, and
      // a div/span-widened version over-matched banner/description text
      // instead of the actual small trigger). Direct own-text-node equality
      // on each candidate, closest one after the marker, is what the earlier
      // 'after selection the menu collapses to a compact dropdown showing
      // just the value' behavior actually looks like in the DOM.
      const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT);
      walker.currentNode = marker;
      let steps = 0;
      while (walker.nextNode() && steps < 400) {
        steps++;
        const el = walker.currentNode;
        if (el.offsetParent === null) continue;
        const t = ownText(el);
        if (!t || t.length > 40) continue;
        if (t === 'website' || t === 'sitio web') return true;
        if (t === 'website and instant forms' || t === 'website and calls' ||
            t === 'instant forms and messenger' ||
            t.includes('website') || t.includes('instant') || t.includes('formulario')) {
          // First conversion-location-flavored value we hit after the marker
          // is the current trigger value — stop here either way.
          return false;
        }
        // 'Conversion event' marks the end of this section — stop searching.
        if (t === 'conversion event' || t === 'evento de conversión') break;
      }
      return false;
    }
    """
    for root in _roots(page):
        try:
            if root.evaluate(script):
                return True
        except Exception:
            pass
    # Fallback: use display value helper but exclude banner text
    current = _conversion_location_display_value(page)
    if current:
        return _website_only_label(current)
    return False


def _is_conversion_event_menu(page: Page) -> bool:
    """Return True if the currently open dropdown is the Conversion event menu (not location)."""
    script = """
    () => {
      const visible = (el) => !!(el && el.offsetParent !== null);
      const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
      const eventTerms = ['active events', 'inactive events', 'eventos activos', 'eventos inactivos',
                          'lead', 'complete registration', 'contact', 'find location'];
      for (const container of document.querySelectorAll('[role=listbox], [role=menu], [role=dialog]')) {
        if (!visible(container)) continue;
        const text = norm(container.textContent || '');
        if (eventTerms.some(t => text.includes(t))) return true;
      }
      return false;
    }
    """
    for root in _roots(page):
        try:
            if root.evaluate(script):
                return True
        except Exception:
            pass
    return False


def click_conversion_location_dropdown(page: Page) -> bool:
    """Open Conversion location picker.

    The trigger button shows the current value: 'Website and instant forms' (default),
    'Website', etc.  That text is UNIQUE to conversion location — use it as the selector.
    Never use ancestor walk with .last because the Conversion event dropdown sits in the
    same section and .last picks the wrong (lower) element.
    """
    scroll_to_section(page, "Conversion location", "Ubicación de la conversión")

    # Strategy 1: find a button/combobox CONTAINING known location value text.
    # These texts only appear in Conversion location, never in Conversion event.
    location_values = (
        "Website and instant forms",
        "Website and Instant Forms",
        "Website and calls",
        "Instant forms and Messenger",
        "Formularios instantáneos",
        "Sitio web y formularios",
    )
    for root in _roots(page):
        for val_text in location_values:
            for sel in (
                f"[role=combobox]:has-text('{val_text}')",
                f"[role=button]:has-text('{val_text}')",
                f"button:has-text('{val_text}')",
                f"[aria-haspopup]:has-text('{val_text}')",
            ):
                try:
                    btn = root.locator(sel).first
                    if btn.count() and btn.is_visible():
                        btn.scroll_into_view_if_needed(timeout=4000)
                        box = btn.bounding_box()
                        if box:
                            # Recorded path: click the value chip itself (CDP-safe mouse)
                            _mouse_click_chain(
                                page,
                                box["x"] + box["width"] / 2,
                                box["y"] + box["height"] / 2,
                            )
                        else:
                            btn.click(timeout=5000)
                        _sleep_h(0.8)
                        return True
                except Exception:
                    continue

    # Strategy 2: JS click — find button whose trimmed text IS a location value.
    script = """
    () => {
      const visible = (el) => !!(el && el.offsetParent !== null);
      const locationValues = [
        'website and instant forms', 'website and instant forms',
        'website and calls', 'instant forms and messenger',
        'formularios instantáneos', 'sitio web y formularios'
      ];
      for (const el of document.querySelectorAll(
        '[role=combobox], [role=button], button, [aria-haspopup]'
      )) {
        if (!visible(el)) continue;
        const t = (el.textContent || '').replace(/\\s+/g, ' ').trim().toLowerCase();
        if (locationValues.some(v => t === v || t.startsWith(v))) {
          el.scrollIntoView({ block: 'nearest' });
          el.click();
          return true;
        }
      }
      return false;
    }
    """
    for root in _roots(page):
        try:
            if root.evaluate(script):
                _sleep_h(0.8)
                return True
        except Exception:
            pass

    # Strategy 3: find "Conversion location" label → look for the FIRST (not last)
    # combobox/button in a TIGHT ancestor — stop early to avoid picking Conversion event.
    location_label = re.compile(r"Conversion location|Ubicación de la conversión", re.I)
    for root in _roots(page):
        try:
            marker = root.get_by_text(location_label).first
            marker.scroll_into_view_if_needed(timeout=5000)
            for depth in range(1, 8):   # shallow: don't go up past the field's own container
                row = marker.locator(f"xpath=ancestor::*[{depth}]")
                for sel in (
                    "[role=combobox]",
                    "[aria-haspopup='listbox']",
                    "[aria-haspopup='menu']",
                ):
                    try:
                        ctrl = row.locator(sel).first  # FIRST, not last
                        if ctrl.count() and ctrl.is_visible():
                            ctrl.click(timeout=5000)
                            _sleep_h(0.8)
                            # Verify we opened the right menu
                            if _is_conversion_event_menu(page):
                                close_open_dropdown(page)
                                _sleep_h(0.3)
                                continue
                            return True
                    except Exception:
                        continue
        except Exception:
            pass

    return False


def _click_conversion_location_dropdown(page: Page) -> bool:
    return click_conversion_location_dropdown(page)


def _menu_first_line(text: str) -> str:
    return (text or "").strip().split("\n")[0].strip().lower()


def _pick_website_by_mouse_below_dropdown(page: Page) -> bool:
    """Meta often renders menu rows below the trigger without listbox roles."""
    triggers = (
        "Website and instant forms",
        "Website and Instant Forms",
    )
    for root in _roots(page):
        for text in triggers:
            try:
                trigger = root.get_by_text(text, exact=True).first
                if not trigger.is_visible():
                    continue
                box = trigger.bounding_box()
                if not box:
                    continue
                cx = box["x"] + min(box["width"] * 0.35, 180)
                # Typical menu: row2 = Website only (below hybrid default)
                for offset in (52, 92, 132):
                    page.mouse.click(cx, box["y"] + box["height"] + offset)
                    _sleep_h(0.55)
                    if is_website_conversion_selected(page):
                        return True
            except Exception:
                pass
    return False


def _pick_website_from_aria_controls(page: Page) -> bool:
    script = """
    () => {
      const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
      const firstLine = (s) => norm((s || '').split('\\n')[0]).toLowerCase();
      const visible = (el) => !!(el && el.offsetParent !== null);
      const combos = [...document.querySelectorAll('[role=combobox][aria-expanded="true"], [aria-haspopup][aria-expanded="true"]')];
      for (const combo of combos) {
        const controls = combo.getAttribute('aria-controls');
        const roots = [];
        if (controls) {
          const pop = document.getElementById(controls);
          if (pop) roots.push(pop);
        }
        roots.push(document.body);
        for (const root of roots) {
          for (const el of root.querySelectorAll('[role=option], [role=menuitemradio], [role=radio], li, div, span, label')) {
            if (!visible(el)) continue;
            const line = firstLine(el.textContent || el.getAttribute('aria-label') || '');
            if (line !== 'website' && line !== 'sitio web') continue;
            const full = norm(el.textContent || '').toLowerCase();
            if (full.includes('instant forms') || full.includes('and instant')) continue;
            el.click();
            return true;
          }
        }
      }
      return false;
    }
    """
    for root in _roots(page):
        try:
            if root.evaluate(script):
                _sleep_h(0.6)
                return True
        except Exception:
            pass
    return False


def expand_conversion_location_single(page: Page) -> bool:
    """Click the 'Single' group row in Conversion location dropdown."""
    return _expand_conversion_location_single(page)


def _expand_conversion_location_single(page: Page) -> bool:
    """Click the 'Single' accordion — golden path from human recording.

    Recorded recipe (Costa_Rica_campaign_20260714_124938):
      click role=heading own_text='Single'
    Prefer heading + mouse chain (CDP-safe); fall back to subtitle / exact text.

    IMPORTANT: Single sits below Multiple in the dropdown — scroll first.
    Success = exact Website radio becomes visible (not 'Website and …').
    """
    if _is_website_option_visible(page, timeout_ms=500):
        return True

    SUBTITLE_PATTERNS = [
        re.compile(r"Send people to one location", re.I),
        re.compile(r"Enviar personas a una ubicación", re.I),
        re.compile(r"una ubicación donde", re.I),
    ]
    HEADING_TEXTS = ["Single", "Único", "Única"]

    for attempt in range(3):
        _scroll_conversion_menu_to_single(page)
        if _is_website_option_visible(page, timeout_ms=400):
            return True

        clicked = False
        for root in _roots(page):
            # Strategy 0 (recorded): role=heading name=Single + mouse chain
            for heading in HEADING_TEXTS:
                try:
                    el = root.get_by_role(
                        "heading", name=re.compile(rf"^{re.escape(heading)}$", re.I)
                    ).first
                    if el.is_visible(timeout=1500):
                        el.scroll_into_view_if_needed(timeout=2000)
                        box = el.bounding_box()
                        if box:
                            _mouse_click_chain(
                                page,
                                box["x"] + box["width"] / 2,
                                box["y"] + box["height"] / 2,
                            )
                        else:
                            el.click(timeout=5000, force=True)
                        clicked = True
                        _sleep_h(0.5)
                        break
                except Exception:
                    pass
            if clicked:
                break

            # Strategy 1: subtitle text
            for pat in SUBTITLE_PATTERNS:
                try:
                    el = root.get_by_text(pat).first
                    el.wait_for(state="visible", timeout=1500)
                    el.scroll_into_view_if_needed(timeout=2000)
                    box = el.bounding_box()
                    if box:
                        _mouse_click_chain(
                            page,
                            box["x"] + min(box["width"] * 0.4, 80),
                            box["y"] + box["height"] / 2,
                        )
                    else:
                        el.click(timeout=5000)
                    clicked = True
                    _sleep_h(0.45)
                    break
                except Exception:
                    pass
            if clicked:
                break

            # Strategy 2: exact "Single" text
            for heading in HEADING_TEXTS:
                try:
                    el = root.get_by_text(heading, exact=True).first
                    el.wait_for(state="visible", timeout=1500)
                    el.scroll_into_view_if_needed(timeout=2000)
                    el.click(timeout=5000, force=True)
                    clicked = True
                    _sleep_h(0.45)
                    break
                except Exception:
                    pass
            if clicked:
                break

        if _wait_for_single_expanded(page, timeout_s=2.5):
            return True
        if not clicked:
            break

    return _is_website_option_visible(page, timeout_ms=600)


def _click_recorded_website_label(page: Page) -> bool:
    """Select 'Website' the way the human recording showed.

    Recording (Costa_Rica_campaign_20260714_185153):
      action 3: keydown key=" " on SPAN own_text='Website', css: span.x1vvvo52.x1fvot60
      The Space keydown on the SPAN selected Website — mouse click did NOT register.

    Strategy order (fastest-first):
      1. Focus SPAN + press Space  (golden path from recording)
      2. Focus SPAN + press Enter
      3. Mouse click center of SPAN
      4. Mouse click radio circle (parent left edge)
      5. Parent container click
    """
    EXCLUDE = ("instant", "and calls", "messenger", "formularios", "forms")
    LABELS = ("Website", "Sitio web")

    def _get_own_text(el) -> str:
        try:
            return (el.evaluate(
                """el => {
                  const own = Array.from(el.childNodes)
                    .filter(n => n.nodeType === 3)
                    .map(n => n.textContent).join('')
                    || el.getAttribute('aria-label') || '';
                  return (own || el.textContent || '').replace(/\\s+/g,' ').trim();
                }"""
            ) or "").strip()
        except Exception:
            return ""

    def _try_select_el(el) -> bool:
        try:
            if not el.is_visible():
                return False
            own = _get_own_text(el)
            low = own.lower()
            if low not in ("website", "sitio web"):
                return False
            if any(x in low for x in EXCLUDE):
                return False
            box = el.bounding_box()
            if not box or box["width"] < 4 or box["height"] < 4 or box["height"] > 80:
                return False

            # Strategy 1: Focus + Space (matches recording exactly)
            try:
                el.focus(timeout=2000)
                el.press(" ", timeout=2000)
                _sleep_h(0.5)
                if is_website_conversion_selected(page):
                    return True
            except Exception:
                pass

            # Strategy 2: Focus + Enter
            try:
                el.focus(timeout=2000)
                el.press("Enter", timeout=2000)
                _sleep_h(0.5)
                if is_website_conversion_selected(page):
                    return True
            except Exception:
                pass

            cx = box["x"] + box["width"] / 2
            cy = box["y"] + box["height"] / 2

            # Strategy 3: Mouse click center of SPAN
            _mouse_click_chain(page, cx, cy)
            _sleep_h(0.5)
            if is_website_conversion_selected(page):
                return True

            # Strategy 4: Radio circle (left edge)
            _mouse_click_chain(page, box["x"] + min(16, box["width"] * 0.12), cy)
            _sleep_h(0.5)
            if is_website_conversion_selected(page):
                return True

            # Strategy 5: Parent container
            try:
                parent = el.locator("xpath=../..")
                parent.click(timeout=3000, force=True)
                _sleep_h(0.5)
                if is_website_conversion_selected(page):
                    return True
            except Exception:
                pass

            el.click(timeout=4000, force=True)
            _sleep_h(0.45)
            if is_website_conversion_selected(page):
                return True
        except Exception:
            pass
        return False

    for root in _roots(page):
        # Priority 1: CSS class selector from recording (most specific)
        for css in ("span.x1vvvo52.x1fvot60", "span.x1vvvo52"):
            try:
                for el in root.locator(css).all()[:8]:
                    if _try_select_el(el):
                        return True
            except Exception:
                pass

        # Priority 2: get_by_text exact (any tag)
        for label in LABELS:
            try:
                for el in root.get_by_text(label, exact=True).all()[:12]:
                    if _try_select_el(el):
                        return True
            except Exception:
                pass

    return False


def _pick_website_via_recorded_recipe(page: Page) -> bool:
    """Primary path from config/rpa_recipes/conversion_location_website.json.

    Human sequence:
      1) menu already open (caller opens via 'Website and instant forms')
      2) click Single (heading)
      3) click Website (exact)
    """
    if is_website_conversion_selected(page):
        return True

    if not _is_website_option_visible(page):
        _expand_conversion_location_single(page)
        _wait_for_single_expanded(page, timeout_s=5.0)
    else:
        # Website already visible — do NOT toggle Single closed
        pass

    if is_website_conversion_selected(page):
        return True

    if _click_recorded_website_label(page):
        return True

    # One more expand+click cycle if first pass missed
    if not _is_website_option_visible(page):
        _expand_conversion_location_single(page)
        _wait_for_single_expanded(page, timeout_s=4.0)
        if _click_recorded_website_label(page):
            return True

    return is_website_conversion_selected(page)


def _wait_for_single_expanded(page: Page, timeout_s: float = 5.0) -> bool:
    """Wait until Single → Website (exact) is visible — never 'Website and …'."""
    timeout_ms = int(timeout_s * 1000)
    exact_radio = re.compile(r"^(Website|Sitio web)$", re.I)

    for root in _roots(page):
        try:
            root.get_by_role("radio", name=exact_radio).first.wait_for(
                state="visible", timeout=timeout_ms
            )
            return True
        except Exception:
            pass
        for label in ("Website", "Sitio web"):
            try:
                # exact text only — must not match "Website and instant forms"
                el = root.get_by_text(label, exact=True).first
                el.wait_for(state="visible", timeout=max(800, timeout_ms // 2))
                own = (el.evaluate(
                    """el => Array.from(el.childNodes)
                      .filter(n => n.nodeType === 3)
                      .map(n => n.textContent).join('')
                      || el.getAttribute('aria-label')
                      || (el.childElementCount === 0 ? el.textContent : '')
                      || ''"""
                ) or "").strip().lower()
                if own in ("website", "sitio web"):
                    return True
            except Exception:
                pass
    return False


def _is_website_option_visible(page: Page, timeout_ms: int = 600) -> bool:
    """True only for Single → Website, not 'Website and instant forms'."""
    exact_radio = re.compile(r"^(Website|Sitio web)$", re.I)
    for root in _roots(page):
        try:
            el = root.get_by_role("radio", name=exact_radio).first
            if el.is_visible(timeout=timeout_ms):
                return True
        except Exception:
            pass
        for label in ("Website", "Sitio web"):
            try:
                el = root.get_by_text(label, exact=True).first
                if not el.is_visible(timeout=timeout_ms):
                    continue
                own = (el.evaluate(
                    """el => Array.from(el.childNodes)
                      .filter(n => n.nodeType === 3)
                      .map(n => n.textContent).join('')
                      || el.getAttribute('aria-label')
                      || (el.childElementCount === 0 ? el.textContent : '')
                      || ''"""
                ) or "").strip().lower()
                if own in ("website", "sitio web"):
                    return True
            except Exception:
                pass
    return False


def _scroll_conversion_menu_to_single(page: Page) -> bool:
    """Scroll the open Conversion location dropdown so Single / Website appear.

    Meta shows Multiple options first; Single → Website is below the fold.
    """
    script = """
    () => {
      const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
      const own = (el) => norm(
        Array.from(el.childNodes).filter(n => n.nodeType === 3)
          .map(n => n.textContent).join('')
        || (el.childElementCount === 0 ? (el.textContent || '') : '')
        || el.getAttribute('aria-label') || ''
      );
      // Prefer an open listbox/menu
      let scrollers = [];
      for (const el of document.querySelectorAll('[role=listbox], [role=menu]')) {
        if (!el.offsetParent) continue;
        if (el.scrollHeight > el.clientHeight + 8) scrollers.push(el);
      }
      // Fallback: any scrollable ancestor of "Multiple" / "Single"
      for (const el of document.querySelectorAll('div, span, [role=heading]')) {
        if (!el.offsetParent) continue;
        const t = own(el);
        if (t !== 'single' && t !== 'multiple' && t !== 'único' && t !== 'única') continue;
        let p = el.parentElement;
        for (let i = 0; i < 10 && p; i++, p = p.parentElement) {
          const st = getComputedStyle(p);
          if (/(auto|scroll)/.test(st.overflowY) && p.scrollHeight > p.clientHeight + 8) {
            scrollers.push(p);
            break;
          }
        }
      }
      if (!scrollers.length) return false;
      let moved = false;
      for (const sc of scrollers) {
        const before = sc.scrollTop;
        sc.scrollTop = Math.min(sc.scrollHeight, sc.scrollTop + Math.max(220, sc.clientHeight * 0.7));
        if (sc.scrollTop !== before) moved = true;
      }
      // Also bring Single heading into view if present
      for (const el of document.querySelectorAll('[role=heading], div, span')) {
        if (!el.offsetParent) continue;
        if (!['single', 'único', 'única'].includes(own(el))) continue;
        el.scrollIntoView({ block: 'center', inline: 'nearest' });
        moved = true;
        break;
      }
      return moved;
    }
    """
    ok = False
    for root in _roots(page):
        try:
            if root.evaluate(script):
                ok = True
                _sleep_h(0.35)
        except Exception:
            pass
    # Playwright wheel fallback over the open menu center
    if not ok or not _is_website_option_visible(page, timeout_ms=300):
        try:
            box = None
            for root in _roots(page):
                try:
                    lb = root.locator("[role=listbox], [role=menu]").last
                    if lb.is_visible(timeout=400):
                        box = lb.bounding_box()
                        if box:
                            break
                except Exception:
                    pass
            if box:
                page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] * 0.7)
                page.mouse.wheel(0, 480)
                _sleep_h(0.35)
                ok = True
        except Exception:
            pass
    return ok


def _find_website_option_bbox(page: Page) -> dict | None:
    """Locate the Single → Website row and return click targets.

    Returns dict with cx/cy (row center) and radio_x/radio_y (left radio circle).
    Prefers a compact row (not a huge container that also contains Instant forms).
    """
    script = """
    () => {
      const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
      const viz = (el) => !!(el && el.offsetParent !== null);
      const ownText = (el) => norm(
        Array.from(el.childNodes)
          .filter(n => n.nodeType === Node.TEXT_NODE)
          .map(n => n.textContent).join('')
        || (el.childElementCount === 0 ? (el.textContent || '') : '')
        || el.getAttribute('aria-label') || ''
      );
      const isWebsiteOnly = (el) => {
        const t = ownText(el);
        return t === 'website' || t === 'sitio web';
      };
      const cands = [];
      for (const el of document.querySelectorAll(
        'div, span, label, li, [role=option], [role=radio], [role=menuitemradio]'
      )) {
        if (!viz(el) || !isWebsiteOnly(el)) continue;
        const r = el.getBoundingClientRect();
        if (r.width < 4 || r.height < 4) continue;
        // Prefer a parent row that is still Website-only (radio + label).
        let row = el;
        for (let i = 0; i < 4 && row.parentElement; i++) {
          const p = row.parentElement;
          const pr = p.getBoundingClientRect();
          const pt = norm(p.textContent || '');
          const websiteOnlyRow = (
            pt === 'website' || pt === 'sitio web'
            || (/^(website|sitio web)\\b/.test(pt)
                && !pt.includes('instant')
                && !pt.includes('calls')
                && !pt.includes('messenger')
                && !pt.includes('formularios'))
          );
          if (websiteOnlyRow && pr.width >= r.width && pr.height <= 72 && pr.height >= 16) {
            row = p;
          } else {
            break;
          }
        }
        const rr = row.getBoundingClientRect();
        if (rr.width < 40 || rr.height < 14 || rr.height > 80) continue;
        cands.push({
          x: rr.x, y: rr.y, w: rr.width, h: rr.height,
          cx: rr.x + Math.min(rr.width * 0.45, 120),
          cy: rr.y + rr.height / 2,
          radio_x: rr.x + Math.min(22, rr.width * 0.12),
          radio_y: rr.y + rr.height / 2,
        });
      }
      if (!cands.length) return null;
      // Prefer medium rows (typical option height ~28-48px).
      cands.sort((a, b) => {
        const score = (c) => {
          let s = 0;
          if (c.h >= 22 && c.h <= 56) s += 100;
          if (c.w >= 80 && c.w <= 640) s += 40;
          s -= Math.abs(c.h - 36);
          return s;
        };
        return score(b) - score(a);
      });
      return cands[0];
    }
    """
    for root in _roots(page):
        try:
            hit = root.evaluate(script)
            if hit:
                return hit
        except Exception:
            pass
    return None


def _human_jitter(base: float, spread: float = 0.3) -> float:
    """A pause near `base` with human-looking spread.

    Fixed sleeps were the most countable tell in the 2026-07-31
    detectability audit: 33 pauses of exactly 0.5s, 24 of 0.6s, 21 of 0.3s
    in one file. Real people never repeat an interval to the millisecond.
    """
    import random

    lo, hi = base * (1.0 - spread), base * (1.0 + spread)
    return random.uniform(lo, hi)


def _sleep_h(base: float, spread: float = 0.3) -> None:
    """Jittered sleep — drop-in for time.sleep() on interaction pauses."""
    time.sleep(_human_jitter(base, spread))


_UPLOADING_TEXT = re.compile(r"^Uploading (video|image)\b", re.I)
_UPLOAD_MAX_S = 20 * 60
_UPLOAD_FROZEN_S = 60   # столько неподвижной полосы считаем застоем


def _upload_in_progress(page: Page) -> bool:
    """Говорит ли страница прямо сейчас, что грузит файл.

    Единственный честный ответ на вопрос «ждать ли дальше». Размер креатива
    коду неизвестен: картинка в полмегабайта и видео на сотню — один и тот же
    шаг, и никакое фиксированное число проходов не годится для обоих.
    """
    try:
        return bool(
            page.evaluate(
                """
                () => [...document.querySelectorAll('*')]
                  .filter(e => e.offsetParent !== null && e.children.length === 0)
                  .some(e => /^Uploading (video|image)\\b/i.test(
                    (e.textContent || '').replace(/\\s+/g, ' ').trim()))
                """
            )
        )
    except Exception:
        return False


_LOADING_TEXT = re.compile(r"^\s*Loading\b", re.I)
"""Надпись загрузочной накладки Meta — и только она.

Было `re.compile(r"Loading", re.I)`: без привязки к началу и без границы
слова. На странице редактора объявления это находило постоянную видимую
фразу «Send people to a fast-loading, mobile-optimized experience» —
описание Instant Experience в разделе назначения. Скрыться она не может, и
ожидание «пока исчезнет» выбирало все тридцать секунд таймаута.

Проверка идёт перед каждой попыткой каждого клика и каждого заполнения,
поэтому платили эти тридцать секунд по нескольку раз за шаг: у `ad3_name`
плоский пол 31.7с при восьми прогонах, у `ad_name` разброс 0.7…32.6 — в
зависимости от того, есть ли на странице эта фраза.

Формулировки накладки видели разные («Loading...», «Loading creation» на
списке кампаний с тремя десятками черновиков), но все начинаются со слова
целиком — привязка к началу их сохраняет и отсекает «fast-loading».
"""


def _visible_now(locator) -> bool:
    """Виден ли элемент прямо сейчас — без ожидания (ACL-1313).

    Для проверок «есть ли оно вообще» там, где страница уже отрисована:
    `click(timeout=…)` по отсутствующему элементу стоит весь таймаут.
    """
    try:
        return bool(locator.count()) and locator.first.is_visible()
    except Exception:
        return False


def _first_visible(page: Page, locators, *, budget_s: float = 4.0, poll_s: float = 0.25):
    """Первый из локаторов, который стал видимым за отведённое время.

    Отвечает на вопрос «есть ли оно вообще», и отвечает быстро. Попытка
    кликнуть по отсутствующему элементу стоит полного таймаута ожидания
    видимости — умноженного на число попыток. У `onboarding_modal` это
    складывалось в 43 секунды на каждую кампанию: пять заходов по восемь
    секунд ради модалки, которой на обжитом аккаунте не бывает.

    Опрос частый, потому что здесь ничего не происходит — мы ждём, а не
    действуем. Человеческие паузы между действиями (`_sleep_h`) это не
    затрагивает: они нужны, чтобы наши действия выглядели человеческими, а
    ожидание никаких событий не порождает.
    """
    deadline = time.time() + budget_s
    while True:
        for loc in locators:
            try:
                if loc.count() and loc.first.is_visible(timeout=200):
                    return loc
            except Exception:
                continue
        if time.time() >= deadline:
            return None
        time.sleep(poll_s)


def _type_human(page: Page, text: str) -> None:
    """Type text as keystrokes, not as one instant insertion.

    Measured 2026-07-31: `insert_text` puts the whole string in with ZERO
    keydown events — the probe's key array stayed empty. That reads as a
    paste that never fired a paste event, which no human input produces.
    Real keypresses cost ~40ms each but look like typing. Accented/emoji
    characters still go through insert_text, because keyboard.type drops
    them (confirmed live 2026-07-26 on ad copy: "Regístrese" lost its
    accent), so those are inserted per-chunk rather than all at once.
    """
    import random

    if not text:
        return
    if "\n" in text and os.environ.get("FB_RPA_HUMAN_TYPING", "").strip() not in ("1", "true", "yes"):
        # ACL-1313, прогон 24.09 (GT FX, 7364047ef03c): остаток текста уходил
        # одной вставкой вместе с переносами строк. Каретка к этому моменту
        # уже не в начале строки, редактор Meta (Draft.js) отдаёт такую
        # вставку браузеру, и Chrome сам режет её на абзацы-копии, о которых
        # редактор не знает: семь копий на семь переносов. Повтор их не
        # стирал и дописывал текст поверх — «текст дважды». До ускорения
        # 20.09 перенос был нажатием Enter (keyboard.type) — так и делаем:
        # строка набирается быстрым путём, перенос — нажатием.
        for n, line in enumerate(text.replace("\r\n", "\n").split("\n")):
            if n:
                page.keyboard.press("Enter")
                time.sleep(_human_jitter(0.03, 0.3))
            _type_human(page, line)
        return
    # 20.09, решение пользователя: набор по символу стоил ~45 минут на круге
    # из восьми кампаний. Живые нажатия оставляем в начале строки — событий
    # keydown достаточно, чтобы ввод не выглядел мгновенной вставкой, — а
    # длинный остаток вставляем целиком. Прежнее поведение возвращает
    # FB_RPA_HUMAN_TYPING=1.
    lead = int(os.environ.get("FB_RPA_TYPING_LEAD", "8") or 8)
    if os.environ.get("FB_RPA_HUMAN_TYPING", "").strip() not in ("1", "true", "yes") and len(text) > lead:
        head, tail = text[:lead], text[lead:]
        for ch in head:
            if ch.isascii():
                page.keyboard.type(ch, delay=random.uniform(25, 70))
            else:
                page.keyboard.insert_text(ch)
        time.sleep(_human_jitter(0.08, 0.4))
        page.keyboard.insert_text(tail)
        return
    chunk = ""
    for ch in text:
        if ch.isascii():
            chunk += ch
            continue
        if chunk:
            page.keyboard.type(chunk, delay=random.uniform(25, 70))
            chunk = ""
        page.keyboard.insert_text(ch)
        time.sleep(_human_jitter(0.04, 0.5))
    if chunk:
        page.keyboard.type(chunk, delay=random.uniform(25, 70))


def _click_trusted(page: Page, selector_or_handle, *, timeout: float = 3000) -> bool:
    """Click an element so the page sees isTrusted=True.

    Measured live 2026-07-31: `el.click()` from JS produces
    `isTrusted: false` — the one unambiguous synthetic-input marker a site
    can log. CDP `Input.dispatchMouseEvent` produces `isTrusted: true`,
    indistinguishable from a real user. So: resolve the element's centre,
    click it through the trusted path, and only fall back to a DOM click
    when the element has no clickable box at all (zero-size hover-only
    icons — the media-library tiles and tree action menus, where a DOM
    click is the only thing that works).

    `selector_or_handle` may be a CSS selector string or an ElementHandle.
    Returns True if a click of either kind was dispatched.
    """
    handle = None
    try:
        if isinstance(selector_or_handle, str):
            handle = page.query_selector(selector_or_handle)
        else:
            handle = selector_or_handle
        if handle is None:
            return False
        try:
            handle.evaluate("el => el.scrollIntoView({block: 'center', behavior: 'instant'})")
        except Exception:
            pass
        _sleep_h(0.2)
        box = None
        try:
            box = handle.bounding_box()
        except Exception:
            box = None
        if box and box.get("width", 0) > 0 and box.get("height", 0) > 0:
            _cdp_mouse_click(page, box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            return True
        # No box: either a zero-size hover-only control (DOM click is the
        # only option) or a STALE handle whose node left the document — the
        # virtualised editor recycles nodes constantly, and "clicking" a
        # detached node used to report success with zero effect (run
        # 45b8ccb4, 2026-08-04). Refuse the stale case so callers re-query.
        if not handle.evaluate("el => el.isConnected"):
            return False
        handle.evaluate("el => el.click()")
        return True
    except Exception:
        return False


def _cdp_input_session(page: Page):
    """CDP-сессия для ввода мышью — одна на страницу (ACL-1313).

    Прежде каждый клик открывал новую `new_cdp_session` и отсоединял её:
    при трёх профилях это 0,1–0,3 с на клик, 4–6 с на кампанию. Сессия
    привязана к вкладке, а не к документу, и переживает переходы по
    страницам; закрытую вкладку или отвалившуюся сессию выдаёт первая же
    ошибка `send` — тогда кэш сбрасывается (`_drop_cdp_input_session`).
    """
    session = getattr(page, "_rpa_input_cdp", None)
    if session is None:
        session = page.context.new_cdp_session(page)
        try:
            page._rpa_input_cdp = session  # type: ignore[attr-defined]
        except Exception:
            pass
    return session


def _drop_cdp_input_session(page: Page) -> None:
    session = getattr(page, "_rpa_input_cdp", None)
    try:
        page._rpa_input_cdp = None  # type: ignore[attr-defined]
    except Exception:
        pass
    if session is not None:
        try:
            session.detach()
        except Exception:
            pass


def _cdp_mouse_click(page: Page, x: float, y: float) -> None:
    """Trusted click via Chrome DevTools Protocol Input domain.

    Playwright's connect_over_cdp path can drop/reorder mouse events
    (microsoft/playwright#41286): ClickAsync succeeds but the page never
    gets a click. Raw CDP mouseMoved → mousePressed → mouseReleased is the
    community workaround used by Multilogin/CDP harnesses.
    """
    for attempt in range(2):
        sent = {"any": False}
        try:
            session = _cdp_input_session(page)

            def _send(params, _session=session):
                _session.send("Input.dispatchMouseEvent", params)
                sent["any"] = True

            # Approach along a curved path instead of teleporting to the
            # target. Audit 2026-07-31: our pointer produced 2-3 points
            # from anywhere to anywhere, with the press ~8ms after the last
            # move — a shape no hand makes. Walk a few interpolated points
            # with a slight arc and human-ish gaps, then press.
            _hit_mark(page, x, y)
            for px, py in _mouse_path(page, x, y):
                _send({"type": "mouseMoved", "x": px, "y": py, "button": "none"})
                time.sleep(_human_jitter(0.02, 0.5))
            _sleep_h(0.08)
            if _hit_changed(page, x, y):
                # ACL-1313, прогон 25.09 (GT 887b2d, 8814): путь курсора шёл
                # через левое меню Ads Manager, меню раскрывалось на наведение
                # поверх кнопки Create, и нажатие попадало в «Audiences» —
                # страница уходила со списка кампаний. Уводим курсор вправо,
                # ждём, пока меню свернётся, и подходим к цели справа.
                away = (float(x) + 320.0, float(y))
                for px, py in _mouse_path(page, *away):
                    _send({"type": "mouseMoved", "x": px, "y": py, "button": "none"})
                    time.sleep(_human_jitter(0.02, 0.5))
                _sleep_h(0.9)
                for px, py in _mouse_path(page, x, y, start=away):
                    _send({"type": "mouseMoved", "x": px, "y": py, "button": "none"})
                    time.sleep(_human_jitter(0.02, 0.5))
                _sleep_h(0.08)
                _telemetry.record("click_target_covered", not _hit_changed(page, x, y),
                                  diag={"x": round(x), "y": round(y)})
            for typ, extra in (
                ("mousePressed", {"button": "left", "buttons": 1, "clickCount": 1}),
                ("mouseReleased", {"button": "left", "buttons": 0, "clickCount": 1}),
            ):
                params = {"type": typ, "x": float(x), "y": float(y), **extra}
                _send(params)
                time.sleep(_human_jitter(0.06, 0.4))
            return
        except Exception:
            # Сессия из кэша могла умереть (вкладка закрыта, профиль
            # переподключён). Ничего ещё не отправлено — одна попытка со
            # свежей сессией; иначе, как и прежде, клик через Playwright.
            _drop_cdp_input_session(page)
            if attempt == 0 and not sent["any"]:
                continue
            break
    # Fallback: Playwright mouse API
    page.mouse.move(x, y)
    _sleep_h(0.06)
    page.mouse.down()
    _sleep_h(0.07)
    page.mouse.up()


#: Что лежит в точке клика до того, как курсор к ней пошёл. Сравнение —
#: «тот же узел или его предок/потомок»: подсветка кнопки при наведении не
#: считается подменой, а раскрывшееся поверх меню — считается.
_HIT_MARK_JS = """([x, y]) => { window._rpa_hit_before = document.elementFromPoint(x, y); }"""
_HIT_CHANGED_JS = """([x, y]) => {
  const was = window._rpa_hit_before, now = document.elementFromPoint(x, y);
  if (!was || !was.isConnected || !now) return false;
  return !(now === was || was.contains(now) || now.contains(was));
}"""


def _hit_mark(page: Page, x: float, y: float) -> None:
    try:
        page.evaluate(_HIT_MARK_JS, [x, y])
    except Exception:
        pass


def _hit_changed(page: Page, x: float, y: float) -> bool:
    """Точку клика накрыло что-то другое, пока курсор шёл к ней."""
    try:
        return bool(page.evaluate(_HIT_CHANGED_JS, [x, y]))
    except Exception:
        return False


#: Левая колонка навигации Ads Manager. Меню раскрывается на наведение и
#: ложится поверх содержимого — путь курсора через неё опасен (ACL-1313).
_NAV_RAIL_X = 72.0


def _mouse_path(page: Page, x: float, y: float,
                start: tuple[float, float] | None = None) -> list[tuple[float, float]]:
    """Interpolated points from the current pointer to (x, y), with an arc.

    Starts wherever the tracker last saw the pointer (installed by
    pacing._install_mouse_tracker); falls back to a point offset from the
    target so the first move is never a teleport from (0,0).

    ACL-1313: запасная точка — справа от цели, а не слева: слева у Ads
    Manager колонка меню, и путь через неё раскрывал меню поверх кнопки
    (Create x≈120). Если курсор и так стоит в этой колонке, а цель — нет,
    путь тоже начинаем справа.
    """
    import math
    import random

    if start is not None:
        sx, sy = float(start[0]), float(start[1])
    else:
        try:
            last = page.evaluate("() => window._rpa_last_mouse || null")
        except Exception:
            last = None
        if last and last.get("x", -1) >= 0 and not (last["x"] < _NAV_RAIL_X <= x):
            sx, sy = float(last["x"]), float(last["y"])
        else:
            sx, sy = float(x) + random.uniform(120, 260), float(y) + random.uniform(-60, 60)

    dist = math.hypot(x - sx, y - sy)
    steps = max(3, min(12, int(dist / 60) + 3))
    # Perpendicular bow so the path is an arc, not a straight ruler line.
    bow = random.uniform(-1, 1) * min(28.0, dist * 0.12)
    dx, dy = x - sx, y - sy
    norm = math.hypot(dx, dy) or 1.0
    px_, py_ = -dy / norm, dx / norm

    pts: list[tuple[float, float]] = []
    for i in range(1, steps + 1):
        t = i / steps
        ease = t * t * (3 - 2 * t)  # smoothstep: slow start, slow finish
        arc = math.sin(math.pi * t) * bow
        pts.append((
            sx + dx * ease + px_ * arc + random.uniform(-1.2, 1.2),
            sy + dy * ease + py_ * arc + random.uniform(-1.2, 1.2),
        ))
    pts[-1] = (float(x), float(y))
    return pts


def _mouse_click_chain(page: Page, x: float, y: float) -> None:
    """Prefer raw CDP click; fall back to Playwright mouse chain."""
    _cdp_mouse_click(page, x, y)


def _scroll_website_option_into_view(page: Page) -> bool:
    """Scroll the Single → Website row into the open menu viewport."""
    script = """
    () => {
      const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
      const own = (el) => norm(
        Array.from(el.childNodes).filter(n => n.nodeType === 3)
          .map(n => n.textContent).join('')
        || (el.childElementCount === 0 ? (el.textContent || '') : '')
        || el.getAttribute('aria-label') || ''
      );
      for (const el of document.querySelectorAll('div, span, label, li, [role=radio]')) {
        if (!el.offsetParent) continue;
        if (own(el) !== 'website' && own(el) !== 'sitio web') continue;
        const r = el.getBoundingClientRect();
        if (r.width < 4 || r.height < 4 || r.height > 80) continue;
        el.scrollIntoView({ block: 'nearest', inline: 'nearest' });
        // Also scroll nearest scrollable ancestor
        let p = el.parentElement;
        for (let i = 0; i < 8 && p; i++, p = p.parentElement) {
          const st = getComputedStyle(p);
          if (/(auto|scroll)/.test(st.overflowY) && p.scrollHeight > p.clientHeight) {
            const er = el.getBoundingClientRect();
            const pr = p.getBoundingClientRect();
            p.scrollTop += (er.top + er.height / 2) - (pr.top + pr.height / 2);
            break;
          }
        }
        return true;
      }
      return false;
    }
    """
    for root in _roots(page):
        try:
            if root.evaluate(script):
                _sleep_h(0.35)
                return True
        except Exception:
            pass
    return False


def _turn_off_advantage_plus_leads(page: Page) -> bool:
    """Turn off 'Advantage+ leads campaign' if it is On — can block manual location picks."""
    script = """
    () => {
      const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
      for (const el of document.querySelectorAll('*')) {
        if (!el.offsetParent) continue;
        const t = norm(el.textContent || '').slice(0, 80);
        if (!t.includes('advantage+ leads') && !t.includes('advantage+ lead')) continue;
        // Find nearby toggle/switch
        let node = el;
        for (let i = 0; i < 6 && node; i++, node = node.parentElement) {
          const sw = node.querySelector(
            '[role=switch], input[type=checkbox], [aria-checked]'
          );
          if (!sw || !sw.offsetParent) continue;
          const checked = sw.getAttribute('aria-checked') === 'true'
            || sw.checked === true;
          if (checked) {
            sw.click();
            return true;
          }
          return false; // already off
        }
      }
      return false;
    }
    """
    for root in _roots(page):
        try:
            if root.evaluate(script):
                _sleep_h(0.7)
                return True
        except Exception:
            pass
    # Text fallback
    for root in _roots(page):
        try:
            label = root.get_by_text(re.compile(r"Advantage\+ leads?", re.I)).first
            if not label.is_visible(timeout=800):
                continue
            row = label.locator("xpath=ancestor::*[self::div or self::section][1]")
            sw = row.get_by_role("switch").first
            if sw.is_visible(timeout=800):
                checked = sw.get_attribute("aria-checked")
                if checked == "true":
                    sw.click(timeout=3000, force=True)
                    _sleep_h(0.7)
                    return True
        except Exception:
            pass
    return False


def _click_website_via_keyboard_focus(page: Page) -> bool:
    """Focus Website option then Space/Enter — most reliable over CDP (#41286)."""
    _scroll_website_option_into_view(page)
    focused = False
    script = """
    () => {
      const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
      const own = (el) => norm(
        Array.from(el.childNodes).filter(n => n.nodeType === 3)
          .map(n => n.textContent).join('')
        || (el.childElementCount === 0 ? (el.textContent || '') : '')
        || el.getAttribute('aria-label') || ''
      );
      for (const el of document.querySelectorAll(
        '[role=radio], [role=option], [role=menuitemradio], div, span, label, li'
      )) {
        if (!el.offsetParent) continue;
        if (own(el) !== 'website' && own(el) !== 'sitio web') continue;
        const r = el.getBoundingClientRect();
        if (r.width < 4 || r.height < 4 || r.height > 80) continue;
        el.scrollIntoView({ block: 'nearest' });
        if (typeof el.focus === 'function') el.focus();
        el.setAttribute('tabindex', '-1');
        el.focus();
        // Prefer radio ancestor
        const radio = el.closest('[role=radio], [role=menuitemradio]') || el;
        if (typeof radio.focus === 'function') radio.focus();
        return true;
      }
      return false;
    }
    """
    for root in _roots(page):
        try:
            if root.evaluate(script):
                focused = True
                break
        except Exception:
            pass
    if not focused:
        return False
    _sleep_h(0.2)
    for key in ("Space", "Enter"):
        try:
            page.keyboard.press(key)
            _sleep_h(0.55)
            if is_website_conversion_selected(page):
                return True
        except Exception:
            pass
    return False


def _click_website_via_cdp(page: Page) -> bool:
    """Raw CDP click on radio circle then row center."""
    _scroll_website_option_into_view(page)
    bbox = _find_website_option_bbox(page)
    if not bbox:
        return False
    targets = (
        (float(bbox["radio_x"]), float(bbox["radio_y"])),
        (float(bbox["cx"]), float(bbox["cy"])),
    )
    for x, y in targets:
        try:
            _cdp_mouse_click(page, x, y)
            _sleep_h(0.6)
            if is_website_conversion_selected(page):
                return True
        except Exception:
            pass
    return False


def _click_website_via_mouse(page: Page) -> bool:
    """Click Website using coordinates (radio circle, then row center)."""
    _scroll_website_option_into_view(page)
    bbox = _find_website_option_bbox(page)
    if not bbox:
        return False
    # 1) Radio circle on the left — often the real hit target
    try:
        _mouse_click_chain(page, float(bbox["radio_x"]), float(bbox["radio_y"]))
        _sleep_h(0.55)
        if is_website_conversion_selected(page):
            return True
    except Exception:
        pass
    # 2) Row center / label area
    try:
        _mouse_click_chain(page, float(bbox["cx"]), float(bbox["cy"]))
        _sleep_h(0.55)
        if is_website_conversion_selected(page):
            return True
    except Exception:
        pass
    # 3) Playwright mouse.click convenience
    try:
        page.mouse.click(float(bbox["radio_x"]), float(bbox["radio_y"]))
        _sleep_h(0.55)
        if is_website_conversion_selected(page):
            return True
    except Exception:
        pass
    return False


def _click_website_via_force_locator(page: Page) -> bool:
    """force=True click on exact Website text (skips actionability checks)."""
    WEBSITE_LABELS = ["Website", "Sitio web"]
    EXCLUDE_WORDS = ("instant", "and calls", "messenger", "formularios")
    for root in _roots(page):
        for label in WEBSITE_LABELS:
            try:
                radio = root.get_by_role("radio", name=re.compile(
                    rf"^{re.escape(label)}$", re.I
                ))
                if radio.count() > 0 and radio.first.is_visible():
                    radio.first.click(timeout=4000, force=True)
                    _sleep_h(0.45)
                    if is_website_conversion_selected(page):
                        return True
            except Exception:
                pass
            try:
                for el in root.get_by_text(label, exact=True).all()[:10]:
                    try:
                        if not el.is_visible():
                            continue
                        own_text = (el.evaluate(
                            "el => el.textContent || el.getAttribute('aria-label') || ''"
                        ) or "").lower().strip()
                        if any(x in own_text for x in EXCLUDE_WORDS):
                            continue
                        el.click(timeout=4000, force=True)
                        _sleep_h(0.45)
                        if is_website_conversion_selected(page):
                            return True
                    except Exception:
                        pass
            except Exception:
                pass
    return False


def _click_website_via_keyboard(page: Page) -> bool:
    """Navigate open Conversion Location menu with keyboard to Website."""
    # Focus is usually already in the menu after opening / expanding Single.
    for _ in range(14):
        try:
            page.keyboard.press("ArrowDown")
            _sleep_h(0.12)
        except Exception:
            break
        # Stop early if Website already selected (some UIs select on focus)
        if is_website_conversion_selected(page):
            return True
        # Detect focused option text when possible
        try:
            focused = page.evaluate("""() => {
              const el = document.activeElement;
              if (!el) return '';
              return (el.getAttribute('aria-label') || el.textContent || '')
                .replace(/\\s+/g, ' ').trim().toLowerCase();
            }""") or ""
            if focused in ("website", "sitio web") or (
                focused.startswith("website")
                and "instant" not in focused
                and "calls" not in focused
            ):
                page.keyboard.press("Enter")
                _sleep_h(0.45)
                if is_website_conversion_selected(page):
                    return True
                page.keyboard.press("Space")
                _sleep_h(0.45)
                if is_website_conversion_selected(page):
                    return True
        except Exception:
            pass
    # Bug found live 2026-07-25 (DQ204): every other strategy in this file's
    # cascade gates its click/Enter on the focused element's text actually
    # reading "website" first — this was the one exception. If none of the
    # 14 ArrowDown presses above ever landed on "website" text (e.g. this
    # account's Single list has extra options — Calls/App/WhatsApp — not
    # present on the accounts this cascade was tuned against, so 14 presses
    # overshoots), it used to press Enter unconditionally on whatever ended
    # up focused. Live evidence: this silently confirmed "WhatsApp" as the
    # Conversion location, changing the whole ad set's objective/Performance
    # goal — a much bigger side effect than "wrong radio in a submenu".
    # Re-check the focused text one last time before committing anything;
    # if it's not Website, back out with Escape instead of blindly
    # committing to whatever is focused.
    try:
        focused = page.evaluate("""() => {
          const el = document.activeElement;
          if (!el) return '';
          return (el.getAttribute('aria-label') || el.textContent || '')
            .replace(/\\s+/g, ' ').trim().toLowerCase();
        }""") or ""
    except Exception:
        focused = ""
    is_website_text = focused in ("website", "sitio web") or (
        focused.startswith("website") and "instant" not in focused and "calls" not in focused
    )
    if is_website_text:
        try:
            page.keyboard.press("Enter")
            _sleep_h(0.45)
        except Exception:
            pass
    # else: leave it alone — focus without Enter/Space never commits a radio
    # selection, so simply not pressing anything here is already safe. Do
    # NOT press Escape (documented elsewhere in this file, e.g.
    # close_open_dropdown's docstring, to sometimes close the whole wizard
    # rather than just this menu) to "clean up" — an unwanted wrong
    # selection is recoverable by retrying; a closed wizard is not.
    return is_website_conversion_selected(page)


def _click_website_via_js_row(page: Page) -> bool:
    """JS: click the Website-only row (and its nearest radio-like child)."""
    script = """
    () => {
      const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
      const viz = (el) => !!(el && el.offsetParent !== null);
      const ownText = (el) => norm(
        Array.from(el.childNodes)
          .filter(n => n.nodeType === Node.TEXT_NODE)
          .map(n => n.textContent).join('')
        || (el.childElementCount === 0 ? (el.textContent || '') : '')
        || el.getAttribute('aria-label') || ''
      );
      const cands = [];
      for (const el of document.querySelectorAll(
        'div, span, label, li, [role=option], [role=radio], [role=menuitemradio]'
      )) {
        if (!viz(el)) continue;
        if (ownText(el) !== 'website' && ownText(el) !== 'sitio web') continue;
        const r = el.getBoundingClientRect();
        if (r.width < 4 || r.height < 4 || r.height > 80) continue;
        let row = el;
        for (let i = 0; i < 3 && row.parentElement; i++) {
          const p = row.parentElement;
          const pt = norm(p.textContent || '');
          const pr = p.getBoundingClientRect();
          if ((pt === 'website' || pt === 'sitio web') && pr.height <= 72) row = p;
          else break;
        }
        cands.push({ el, row, area: r.width * r.height });
      }
      if (!cands.length) return false;
      cands.sort((a, b) => a.area - b.area);
      const { el, row } = cands[0];
      row.scrollIntoView({ block: 'nearest' });
      const radio = row.querySelector(
        '[role=radio], [role=menuitemradio], input[type=radio], [aria-checked]'
      );
      const target = radio || row || el;
      target.dispatchEvent(new MouseEvent('mousedown', {bubbles:true, cancelable:true, view:window}));
      target.dispatchEvent(new MouseEvent('mouseup', {bubbles:true, cancelable:true, view:window}));
      target.dispatchEvent(new MouseEvent('click', {bubbles:true, cancelable:true, view:window}));
      if (typeof target.click === 'function') target.click();
      return true;
    }
    """
    for root in _roots(page):
        try:
            if root.evaluate(script):
                _sleep_h(0.55)
                if is_website_conversion_selected(page):
                    return True
        except Exception:
            pass
    return False


_CONVERSION_MENU_OPEN_JS = r"""
  () => {
    // Открытое меню Conversion location — это заголовки групп «Multiple» /
    // «Single» (запись рецепта: role=heading) или подпись «Send people to one
    // location…». Текст «Website and instant forms» в счёт НЕ идёт: это
    // значение закрытого триггера (25.09, 90a8987edad9 — меню закрыто, а
    // старая проверка по этому тексту считала его открытым).
    const norm = (s) => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
    const own = (el) => norm(Array.from(el.childNodes).filter(n => n.nodeType === 3)
      .map(n => n.textContent).join('') || (el.childElementCount === 0 ? el.textContent : ''));
    const shown = (el) => {
      if (!el.offsetParent) return false;
      const r = el.getBoundingClientRect();
      return r.width > 0 && r.height > 0;
    };
    const groups = ['single', 'multiple', 'único', 'única', 'múltiple', 'múltiples'];
    for (const el of document.querySelectorAll('[role=heading], div, span')) {
      const t = own(el);
      if (!t || t.length > 80) continue;
      if (groups.includes(t) && shown(el)) return true;
      if ((t.startsWith('send people to one location') || t.startsWith('enviar personas a una ubicación'))
          && shown(el)) return true;
    }
    return false;
  }
"""


def _conversion_location_menu_open(page: Page) -> bool:
    """Открыто ли сейчас меню Conversion location (без ожиданий)."""
    for root in _roots(page):
        try:
            if root.evaluate(_CONVERSION_MENU_OPEN_JS):
                return True
        except Exception:
            continue
    return False


def _ensure_conversion_menu_open(page: Page, *, tries: int = 2) -> bool:
    """Меню Conversion location открыто к следующему способу выбора Website.

    25.09 (90a8987edad9, SA Investing; 19 полных отказов с 19.09): все восемь
    способов `_pick_website_from_open_menu` работали по уже закрытому меню —
    на снимке закрытый список «Website and instant forms», в page.html нет ни
    одного role=radio. Первый же неудачный способ (или клик мимо) закрывает
    меню, и остальные семь жмут пробел/мышь в пустоту. Поэтому перед каждым
    способом меню проверяется и при необходимости открывается заново — только
    когда оно закрыто: клик по открытому триггеру его бы закрыл.
    """
    for _ in range(max(1, tries)):
        if _conversion_location_menu_open(page):
            return True
        dismiss_publish_draft_dialog(page)
        if not click_conversion_location_dropdown(page):
            return False
        for _poll in range(8):
            if _conversion_location_menu_open(page):
                return True
            _sleep_h(0.25)
    return _conversion_location_menu_open(page)


#: Сколько ждать, пока выбранное «Website» появится в триггере после способа.
WEBSITE_VALUE_WAIT_S = 2.0


def _website_value_shows(page: Page, timeout_s: float | None = None) -> bool:
    """В триггере Conversion location стоит ровно «Website» (не «Website and
    instant forms»): читаем показанное значение, а не ответ способа клика."""
    wait = WEBSITE_VALUE_WAIT_S if timeout_s is None else timeout_s
    deadline = time.monotonic() + max(0.0, wait)
    while True:
        if is_website_conversion_selected(page):
            return True
        if time.monotonic() >= deadline:
            return False
        _sleep_h(0.25)
        # Под _sleep_h=0 в тестах цикл не должен крутиться вхолостую.
        time.sleep(0.05)


def _pick_website_from_open_menu(page: Page) -> bool:
    """Expand Single (if needed) then click Website — fully automatic.

    Research (Playwright #41286, Multilogin CDP): mouse clicks over connect_over_cdp
    often succeed in the API but never reach the page. Keyboard Space/Enter and raw
    CDP Input.dispatchMouseEvent are the reliable workarounds.

    Order:
      0. Scroll Website into menu viewport
      1. Keyboard focus + Space/Enter  (most reliable over CDP)
      2. Raw CDP mouse on radio / row
      3. Recorded golden path (Single heading → Website)
      4. Playwright mouse chain / force / JS / legacy keyboard

    25.09: перед КАЖДЫМ способом меню проверяется и при надобности открывается
    заново (`_ensure_conversion_menu_open`), а успех способа засчитывается
    только по показанному значению «Website» в триггере. Строка телеметрии
    `conversion_website_pick` — какой способ сработал и сколько раз меню
    пришлось открывать.
    """
    if is_website_conversion_selected(page):
        return True

    # Dismiss 'Publish draft items?' dialog that may be blocking the dropdown
    dismiss_publish_draft_dialog(page)

    methods = (
        ("keyboard_focus", _click_website_via_keyboard_focus),
        ("cdp", _click_website_via_cdp),
        ("recorded_recipe", _pick_website_via_recorded_recipe),
        ("mouse", _click_website_via_mouse),
        ("force_locator", _click_website_via_force_locator),
        ("js_row", _click_website_via_js_row),
        ("keyboard", _click_website_via_keyboard),
        ("aria_controls", _pick_website_from_aria_controls),
    )
    tried: list[str] = []
    reopened = 0
    menu_closed = 0

    def _done(method: str | None, ok: bool) -> bool:
        try:
            _telemetry.record(
                "conversion_website_pick", ok,
                note=method or "not_selected",
                diag={"tried": tried, "reopened": reopened, "menu_closed": menu_closed},
            )
        except Exception:
            pass
        return ok

    for name, method in methods:
        if is_website_conversion_selected(page):
            return _done(tried[-1] if tried else "already", True)
        was_open = _conversion_location_menu_open(page)
        if not was_open:
            menu_closed += 1
            if not _ensure_conversion_menu_open(page):
                # Меню не открыть — способ жал бы в пустоту; следующий
                # способ снова попробует открыть.
                tried.append(f"{name}:menu_closed")
                continue
            reopened += 1
        dismiss_publish_draft_dialog(page)
        if not _is_website_option_visible(page):
            _expand_conversion_location_single(page)
            _wait_for_single_expanded(page, timeout_s=5.0)
        dismiss_publish_draft_dialog(page)
        _scroll_website_option_into_view(page)
        tried.append(name)
        try:
            method(page)
        except Exception:
            pass
        if _website_value_shows(page):
            return _done(name, True)

    return _done(None, _website_value_shows(page, timeout_s=0))


def conversion_menu_is_open(page: Page) -> bool:
    if wait_conversion_menu_open(page):
        return True
    script = """
    () => {
      const visible = (el) => !!(el && el.offsetParent !== null);
      for (const el of document.querySelectorAll('[role=combobox], [aria-haspopup]')) {
        if (!visible(el)) continue;
        if (el.getAttribute('aria-expanded') === 'true') return true;
      }
      return false;
    }
    """
    for root in _roots(page):
        try:
            if root.evaluate(script):
                return True
        except Exception:
            pass
    return False


def wait_conversion_menu_open(page: Page) -> bool:
    """Return True when a listbox/menu appears after opening Conversion location."""
    for root in _roots(page):
        try:
            lb = root.locator("[role=listbox], [role=menu]").last
            lb.wait_for(state="visible", timeout=2500)
            return True
        except Exception:
            pass
    return False


def conversion_location_menu_visible(page: Page) -> bool:
    for root in _roots(page):
        for label in ("Multiple", "Single", "Website and instant forms"):
            try:
                if root.get_by_text(re.compile(rf"^{re.escape(label)}$", re.I)).first.is_visible():
                    return True
            except Exception:
                pass
    return False


def conversion_event_menu_visible(page: Page) -> bool:
    for root in _roots(page):
        for label in ("Active events", "Inactive events", "Lead"):
            try:
                if root.get_by_text(re.compile(label, re.I)).first.is_visible():
                    return True
            except Exception:
                pass
    return False


def pick_website_conversion_option(page: Page) -> bool:
    """Pick Website from an already-open Conversion location menu."""
    return _pick_website_from_open_menu(page)


#: Сколько «Loading…» может висеть, прежде чем считать страницу зависшей (ACL-1233).
LOADING_STUCK_S = 20.0


_ERROR_PAGE_JS = r"""
  () => {
    // 20.09 02:00 (скриншот пользователя, профиль KLD_BFL_S15): в адресной
    // строке остался обычный адрес Meta, а во вкладке — «This site can't
    // provide a secure connection / ERR_SSL_PROTOCOL_ERROR». По адресу такую
    // страницу не узнать, узнаём по самой разметке Chrome.
    if (!document.body) return '';
    const shell = document.querySelector('#main-frame-error, .neterror, #error-information-popup-container');
    const txt = document.body.innerText || '';
    const code = txt.match(/ERR_[A-Z0-9_]+/);
    if (code) return code[0];
    if (shell || /can.t provide a secure connection|site can.t be reached|ERR_/i.test(txt.slice(0, 600))) {
      return 'ERR_UNKNOWN';
    }
    return '';
  }
"""
"""Страница ошибки Chrome вместо Ads Manager — её собственная разметка."""


def _chrome_error_page(page: Page) -> str:
    """Код ошибки, если во вкладке вместо Ads Manager — страница ошибки Chrome."""
    try:
        if (page.url or "").startswith("chrome-error://"):
            got = page.evaluate("() => (document.body && document.body.innerText || '').match(/ERR_[A-Z_]+/)")
            return (got or ["ERR_UNKNOWN"])[0] if isinstance(got, list) else "ERR_UNKNOWN"
        # Адрес может остаться прежним (SSL-ошибка) — смотрим саму страницу.
        return page.evaluate(_ERROR_PAGE_JS) or ""
    except Exception:
        pass
    return ""


#: Спиннер маршрута вместо редактора (ACL-1313, 7364047ef03c/05, 24.09):
#: `role=progressbar aria-valuetext="Loading" data-is-placeholder` внутри
#: `[data-non-int-surface*="AdsPERouteLoadPlaceholder"]` — текста «Loading»
#: на экране нет, и `_loading_visible` его не видел. Только маршрутный
#: заполнитель: мелкие заполнители секций редактора живут подолгу, и считать
#: их «зависшей загрузкой» значило бы перезагружать живой редактор.
_ROUTE_PLACEHOLDER_SEL = '[data-non-int-surface*="AdsPERouteLoadPlaceholder"]'


def _route_placeholder_visible(page: Page) -> bool:
    """Виден ли маршрутный спиннер Meta на месте страницы."""
    try:
        return bool(page.evaluate(
            """(sel) => [...document.querySelectorAll(sel)].some((e) => {
                 const r = e.getBoundingClientRect();
                 if (r.width <= 0 || r.height <= 0) return false;
                 const s = getComputedStyle(e);
                 return s.visibility !== 'hidden' && s.display !== 'none';
               })""",
            _ROUTE_PLACEHOLDER_SEL,
        ))
    except Exception:
        return False


def _loading_visible(page: Page) -> bool:
    """Накладка «Loading…» или маршрутный спиннер — страница ещё грузится."""
    try:
        overlay = page.get_by_text(_LOADING_TEXT).first
        if bool(overlay.count()) and overlay.is_visible(timeout=200):
            return True
    except Exception:
        pass
    return _route_placeholder_visible(page)


#: ACL-1313, прогон 24.09: после Create Meta минутами крутит модалку
#: «Loading creation». Хвост удачных переходов — 55–79 с, ровно в наш порог
#: 75 с; перезагрузка списка поверх крутящейся модалки выбрасывала начатую
#: загрузку, и все пять отказов `objective_leads` того вечера — это цепочка
#: «75 с → reload → Create → 75 с → reload». Пока модалка на экране, ждём
#: дальше, до этого потолка.
OBJECTIVE_LOADING_MAX_S = 180.0


#: Meta сама говорит, что сеть профиля рвётся или часть запросов не проходит:
#: «Turn off ad blocker», «Your internet connection was lost» (279ead, 25.09).
#: Ждать загрузку формы в таком состоянии бессмысленно.
_NETWORK_TROUBLE = re.compile(r"Turn off ad blocker|internet connection was lost", re.I)


def _network_trouble(page: Page) -> bool:
    try:
        return page.get_by_text(_NETWORK_TROUBLE).first.is_visible(timeout=300)
    except Exception:
        return False


def _create_took(page: Page, list_url: str, timeout_s: float = 8.0) -> str:
    """Отозвалась ли страница на Create: "ok" / "left_list" / "nothing".

    ACL-1313: с 20.09 первый Create после загрузки открывал окно цели в 42%
    случаев, остальные ждали 75–180 с и перезагрузку. На снимках 25.09 у
    этих попыток не «долгая загрузка», а промах: открыта страница Audiences
    или список без модалки. Проверяем за секунды, а не за минуты.
    """
    base = (list_url or "").split("?", 1)[0]
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            if page.get_by_role("radio", name="Leads").first.is_visible(timeout=300):
                return "ok"
        except Exception:
            pass
        if _loading_visible(page):
            return "ok"
        try:
            if base and page.url.split("?", 1)[0] != base:
                return "left_list"
        except Exception:
            pass
        _sleep_h(0.5, 0.2)
    return "nothing"


#: ACL-1313: сколько ждать окно цели, когда на экране НЕТ ни радио Leads, ни
#: «Loading creation». Снимки after_create 25.09 (ce8077, 570e21): модалка
#: «Loading creation» встаёт сразу после нажатия, а `_create_took` уже
#: убедился, что радио или загрузка появились. Значит, пустой экран — это
#: промежуток между концом загрузки и отрисовкой окна, а не сама загрузка.
#: Порог выше медианы ВСЕГО перехода create→leads (18.6 с по 156 удачным
#: переходам телеметрии, с учётом ~10 с накладных шага), то есть даже
#: целиком «пустой» нормальный переход в него укладывается. Прежде без
#: загрузки ждали 75 с и только потом перезагружали.
OBJECTIVE_IDLE_S = 20.0

#: Шаг опроса окна цели. Короткий, чтобы замечать загрузку/её конец.
OBJECTIVE_POLL_S = 2.5

#: Последнее ожидание радио Leads после всех повторов Create. Раньше
#: `_click` делал пять заходов по 8 с (+8 с на накладку) — ~83 с на
#: отсутствующее окно, после чего шаг всё равно падал.
OBJECTIVE_RADIO_LAST_WAIT_S = 10.0


def _wait_objective_dialog(page: Page, *, first_s: float = 75.0,
                           idle_s: float = OBJECTIVE_IDLE_S,
                           loading_max_s: float = OBJECTIVE_LOADING_MAX_S,
                           poll_s: float = OBJECTIVE_POLL_S) -> bool:
    """Дождаться окна выбора цели.

    Пока видна «Loading…» — ждём до `loading_max_s` (сеть профиля рвётся —
    сразу сдаёмся). Пока не видно ни радио, ни загрузки — ждём не дольше
    `idle_s` подряд и не дольше `first_s` всего. Строка телеметрии
    `objective_wait` говорит, сколько ждали и видели ли загрузку — по ней
    потом подбирается `OBJECTIVE_IDLE_S`.
    """
    radio = page.get_by_role("radio", name="Leads").first
    start = time.time()
    last_busy = start
    loading_polls = 0
    outcome = "idle"
    found = False
    while True:
        try:
            radio.wait_for(state="visible", timeout=int(poll_s * 1000))
            found, outcome = True, "radio"
            break
        except Exception:
            pass
        now = time.time()
        if _loading_visible(page):
            loading_polls += 1
            last_busy = now
            if _network_trouble(page):
                outcome = "network_trouble"
                break
            if now - start >= loading_max_s:
                outcome = "loading_ceiling"
                break
            continue
        if now - start >= first_s:
            outcome = "first_s"
            break
        if now - last_busy >= idle_s:
            outcome = "idle"
            break
    try:
        _telemetry.record("objective_wait", None, note=outcome,
                          duration_s=time.time() - start,
                          diag={"loading_polls": loading_polls,
                                "idle_s": round(time.time() - last_busy, 1)})
    except Exception:
        pass
    return found


def _all_edits_saved(page: Page) -> bool:
    try:
        return page.get_by_text(re.compile(r"^All edits saved$", re.I)).first.is_visible(timeout=500)
    except Exception:
        return False


def _component_failed(page: Page) -> bool:
    """Блок редактора, который Meta не смогла загрузить.

    20.09 00:35 (CO_bnc Mix): на месте панели плейсментов стояло «Error:
    component failed to load — Something went wrong, please try reloading the
    page», и подряд отказали placements и devices_ios. Meta сама просит
    перезагрузить — это тот же случай, что и ACL-1233.
    """
    try:
        return page.get_by_text(
            re.compile(r"component failed to load", re.I)).first.is_visible(timeout=500)
    except Exception:
        return False


_PENDING_DRAFTS_JS = r"""
  () => {
    const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
    const el = [...document.querySelectorAll('*')].find(e =>
      e.children.length === 0 && /^Review and publish( \(\d+\))?$/.test(norm(e.textContent)));
    if (!el) return null;
    const m = norm(el.textContent).match(/\((\d+)\)/);
    return m ? Number(m[1]) : 0;
  }
"""
"""Сколько черновиков ждёт публикации — число в кнопке «Review and publish»."""


def _pending_drafts(page: Page) -> int | None:
    try:
        return page.evaluate(_PENDING_DRAFTS_JS)
    except Exception:
        return None


def recover_stuck_page(page: Page, where: str, *, editor: bool, force: bool = False) -> bool:
    """Перезагрузить вкладку, если она зависла (ACL-1233). True — перезагрузили.

    * Страница ошибки Chrome (ERR_SSL_PROTOCOL_ERROR, 19.09 CO_bnc Mix) —
      перезагружаем всегда: живого редактора в ней уже нет.
    * «Loading…», висящий дольше LOADING_STUCK_S: в редакторе — только при
      «All edits saved», иначе потеряются несохранённые поля; на списке
      кампаний (`editor=False`, шаг выбора цели) сохранять нечего.
    """
    err = _chrome_error_page(page)
    reason = ""
    if err:
        reason = f"error_page:{err}"
    elif _component_failed(page):
        # Блок не загрузился: перезагружаем, но в редакторе — только когда
        # всё сохранено, иначе потеряются уже заполненные поля.
        if editor and not _all_edits_saved(page):
            _telemetry.record("loading_modal_reload", False, note="unsaved_edits",
                              diag={"where": where, "reason": "component_error"})
            return False
        reason = "component_error"
    elif force and not editor:
        # Список кампаний: окно цели не открылось, а видимого «Loading…» нет
        # (19.09 CO_bnc Mix, 18:12). Терять на списке нечего — перезагружаем.
        reason = "no_objective_dialog"
    elif _loading_visible(page):
        deadline = time.time() + LOADING_STUCK_S
        while time.time() < deadline and _loading_visible(page):
            _sleep_h(1.0, 0.1)
        if not _loading_visible(page):
            return False
        if editor and not _all_edits_saved(page):
            _telemetry.record("loading_modal_reload", False, note="unsaved_edits", diag={"where": where})
            return False
        reason = "loading_modal"
    else:
        return False
    # Сначала разбудить: свёрнутое окно — из свёрнутого, вкладку — вперёд,
    # троттлинг Chrome — снять заново (19.09 вечер: перезагрузка без этого
    # снова упиралась в «Loading creation»).
    woke = {}
    try:
        from src.fb_ads_rpa.browser import wake_page
        woke = wake_page(page)
    except Exception as exc:
        woke = {"error": str(exc)[:80]}
    try:
        page.reload(timeout=60000, wait_until="domcontentloaded")
        _sleep_h(4.0, 0.2)
        try:
            from src.fb_ads_rpa.browser import _disable_background_throttling
            _disable_background_throttling(page.context, page)
        except Exception:
            pass
        ok = not _chrome_error_page(page)
    except Exception as exc:
        # 20.09 00:55 (CO_bnc GMB, дважды): сам `reload` не уложился в минуту
        # — Meta на этом профиле еле отвечает. Пробуем так, как это делает
        # человек клавишей F5: изнутри страницы, не дожидаясь её загрузки.
        reason += f" ({str(exc)[:60]})"
        ok = False
        try:
            page.evaluate("() => { location.reload(); }")
            _sleep_h(6.0, 0.2)
            page.wait_for_selector("[role=button]", timeout=30000)
            ok = not _chrome_error_page(page)
            reason += " js_reload" + ("" if ok else "_failed")
        except Exception as exc2:
            reason += f" js_reload_failed ({str(exc2)[:40]})"
    _telemetry.record("error_page_reload" if reason.startswith("error_page") else "loading_modal_reload",
                      ok, note=reason, diag={"where": where, "url": (page.url or "")[:160], "woke": woke})
    return ok


_NAME_SUGGESTIONS_JS = r"""
  () => {
    const vis = (e) => { const r = e.getBoundingClientRect(); return r.width > 40 && r.height > 20; };
    const open = [...document.querySelectorAll('[role="listbox"], [role="option"]')].filter(vis);
    if (!open.length) return null;
    const r = open[0].getBoundingClientRect();
    return { count: open.length, x: r.x, y: r.y, w: r.width, h: r.height };
  }
"""
"""Открытая подсказка истории имён — она накрывает блок под полем."""


def _dismiss_name_suggestions(page: Page) -> bool:
    """Закрыть подсказку истории имён под полем имени.

    20.09 07:53, все три аккаунта разом: после ввода имени ad set'а Meta
    показала список прежних ad set'ов с тем же именем, он накрыл «Conversion
    location», и шаг падал. Клик по заголовку «Ad set name» её не убирал.
    Escape здесь нельзя — он закрывает весь мастер. Поэтому кликаем заведомо
    пустое место панели правее списка и проверяем, что подсказка ушла.
    """
    for _ in range(4):
        try:
            box = page.evaluate(_NAME_SUGGESTIONS_JS)
        except Exception:
            return True
        if not box:
            return True
        try:
            _cdp_mouse_click(page, box["x"] + box["w"] + 60, max(box["y"] - 30, 10))
        except Exception:
            pass
        _sleep_h(0.6)
    ok = False
    try:
        ok = not page.evaluate(_NAME_SUGGESTIONS_JS)
    except Exception:
        ok = True
    _telemetry.record("name_suggestions", ok, note="dismissed" if ok else "still_open")
    return ok


def _lead_terms_not_accepted(page: Page) -> bool:
    """Страница Facebook не приняла условия Lead Generation.

    21.09 (CO_bnc, страница The Financial Observer India): Meta пишет «Terms of
    Service Not Accepted — You can't run lead ads until your Facebook Page
    accepts Facebook's Lead Generation Terms of Service». Редактор креатива при
    этом не отрисовывается, и полей текста на странице нет вовсе. Починить это
    роботом нельзя: условия принимает человек в Meta. Пишем отдельной меткой,
    чтобы не искать причину заново по снимкам.
    """
    try:
        return page.get_by_text(
            re.compile(r"Terms of Service Not Accepted|Lead Generation Terms of Service", re.I)
        ).first.is_visible(timeout=800)
    except Exception:
        return False


#: Ошибки проверки, которые чинит обход назначений / выбор «None» в Browser
#: add-ons: объявление ссылается на лид-форму, которой нет.
_LEAD_FORM_ERROR_RE = re.compile(r"must be a valid Lead Gen Data id|lead_gen_form", re.I)

#: Уже записанные «прочие» ошибки проверки — одна строка телеметрии на текст.
_OTHER_REVIEW_ERRORS_SEEN: set[str] = set()


def _review_errors_visible(page: Page) -> bool:
    """Meta показывает ошибку проверки про лид-форму / назначение объявления.

    20.09 19:29 (CO_bnc Mix): пропуск обхода назначений оставил объявление с
    «Param call_to_action[value][lead_gen_form_id] must be a valid Lead Gen
    Data id» — ровно то, что обход и чинил. Если такая ошибка висит, обход
    делаем, сколько бы времени он ни стоил.

    ACL-1313 (25.09): только ошибки лид-формы. Раньше True давало любое
    «Review N error» — и «Review 1 error — Start authentication» (аккаунт
    требует проверки, прогон ecce52f330b8 22.09) запускал бесполезную
    починку Browser add-ons. Прочие ошибки проверки записываются отдельной
    строкой `review_error_other` (`_report_other_review_errors`), а
    блокировку аккаунта ловит `_account_blocked_text`.
    """
    try:
        if page.get_by_text(_LEAD_FORM_ERROR_RE).first.is_visible(timeout=800):
            return True
    except Exception:
        pass
    _report_other_review_errors(page)
    return False


_REVIEW_ERRORS_JS = r"""
  () => {
    const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
    const shown = (el) => {
      const r = el.getBoundingClientRect();
      return r.width > 0 && r.height > 0 && getComputedStyle(el).visibility !== 'hidden';
    };
    const out = [];
    for (const el of document.querySelectorAll('body *')) {
      if (el.children.length > 3) continue;
      const t = norm(el.textContent);
      if (!/^Review \d+ errors?\b/i.test(t) || t.length > 300 || !shown(el)) continue;
      // Текст ошибки часто лежит рядом с заголовком «Review N error» —
      // берём контейнер на пару уровней выше, но не больше 300 знаков.
      let box = el, text = t;
      for (let k = 0; k < 3 && box.parentElement; k++) {
        const up = norm(box.parentElement.textContent);
        if (up.length > 300) break;
        box = box.parentElement; text = up;
      }
      if (!out.includes(text)) out.push(text);
    }
    return out;
  }
"""


def _report_other_review_errors(page: Page) -> list[str]:
    """Видимые «Review N error…», не относящиеся к лид-форме — в телеметрию.

    Одна строка `review_error_other` на каждый новый текст: это не повод
    для починки назначений, но человеку и разбору прогона их надо видеть.
    """
    found: list[str] = []
    for root in _roots(page):
        try:
            found.extend(root.evaluate(_REVIEW_ERRORS_JS) or [])
        except Exception:
            continue
    others = [t for t in found if not _LEAD_FORM_ERROR_RE.search(t)]
    for text in others:
        if text in _OTHER_REVIEW_ERRORS_SEEN:
            continue
        _OTHER_REVIEW_ERRORS_SEEN.add(text)
        try:
            _telemetry.record("review_error_other", None, note=text[:160],
                              diag={"account_blocked": _account_blocked_match(text)})
        except Exception:
            pass
    return others


#: Признаки аккаунта, в котором Meta не даёт создавать рекламу. Ни повтор,
#: ни починка тут не помогут — нужен человек в Meta.
_ACCOUNT_BLOCKED_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    ("verification_required", re.compile(r"Verification required to advertise", re.I)),
    ("account_disabled", re.compile(r"Your ad account is disabled", re.I)),
    ("account_disabled", re.compile(r"^(?:Ad )?account disabled$", re.I)),
    ("start_authentication", re.compile(r"\bStart authentication\b", re.I)),
    ("authentication_error", re.compile(r"#3858385\b")),
)


def _account_blocked_match(text: str) -> str | None:
    """Вид блокировки по тексту или None."""
    t = re.sub(r"\s+", " ", text or "").strip()
    for kind, pat in _ACCOUNT_BLOCKED_PATTERNS:
        if pat.search(t):
            return kind
    return None


_ACCOUNT_BLOCKED_JS = r"""
  () => {
    const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
    const shown = (el) => {
      const r = el.getBoundingClientRect();
      return r.width > 0 && r.height > 0 && getComputedStyle(el).visibility !== 'hidden';
    };
    const out = [];
    for (const el of document.querySelectorAll('body *')) {
      // Листья и мелкие узлы: подпись баннера, статус в колонке Delivery,
      // кнопка «Start authentication», текст ошибки с кодом (#3858385).
      if (el.children.length > 2) continue;
      const t = norm(el.textContent);
      if (!t || t.length > 300) continue;
      if (!/verification required to advertise|account (is )?disabled|start authentication|3858385/i.test(t)) continue;
      if (!shown(el)) continue;
      out.push(t);
    }
    return out;
  }
"""


def _account_blocked_text(page: Page) -> str | None:
    """Видимый текст, по которому ясно: аккаунт заблокирован Meta.

    Прогон ecce52f330b8 (22.09): красный баннер «Verification required to
    advertise — Complete verification to remove restrictions…», у кампании в
    колонке Delivery «Account disabled», кнопка Create серая. Прогон трижды
    нажимал Create, дважды перезагружал страницу и через 5 минут упал на
    `objective_leads`, а повтор кампании начал бы всё заново. В редакторе
    объявления та же блокировка выглядит как «Review 1 error — Start
    authentication» с кодом (#3858385).
    """
    for root in _roots(page):
        try:
            texts = root.evaluate(_ACCOUNT_BLOCKED_JS) or []
        except Exception:
            continue
        for t in texts:
            if _account_blocked_match(t):
                return t
    return None


#: ACL-1313, прогон 25.09: снимок экрана после каждого удачного шага стоил
#: 5–7 с при трёх профилях (Playwright замораживает анимации Meta и кодирует
#: PNG 3072×1467) — около 30 снимков, 2–3 минуты простоя на кампанию; владелец
#: видел это как паузу после «All edits saved». Телеметрия пишется по-прежнему
#: на каждом шаге; картинка — только на отказе. Оставленные было «границы
#: разделов» (after_create, after_placements, …) и метки success_/before_
#: давали ещё 12–16 снимков на кампанию — около минуты.
#: Вернуть снимок на каждом шаге: FB_RPA_SHOT_ALL=1.
def _shot_worth_a_picture(label: str) -> bool:
    """Нужен ли PNG для НЕ-отказного шага `label` (отказы снимаются всегда)."""
    from src.fb_ads_rpa.page_capture import success_captures_enabled

    return success_captures_enabled()


def run_recorded_campaign_flow(
    page: Page,
    *,
    campaign_name: str,
    budget: str,
    adset_name: str,
    country: str = "",
    age_min: int = 18,
    age_max: int = 65,
    targeting_label: str = "",
    language_names: list[str] | None = None,
    ad_name: str = "",
    creative_type: str = "",
    creative_path: str = "",
    ad_title: str = "",
    ad_body: str = "",
    destination_url: str = "",
    os_val: str = "All",
    additional_ads: list[dict[str, str]] | None = None,
    soft_fails_out: list[str] | None = None,
) -> str:
    """Create -> Website -> ... -> Ad level, using the exact locators from a
    verified human Playwright codegen recording (2026-07-21) instead of the
    label/breadcrumb/coordinate cascade the rest of this file falls back to.

    2026-07-26: this is the version live-validated end-to-end (CLI dry-run
    AND a real dashboard-triggered run) all the way to Ad level — a parallel
    version merged in from another branch (interests/language/creative_filename
    params, its own conversion-event/Locations/Languages/Placements logic) was
    NOT used as the base: its own docstring admits large parts were
    "transcribed... not live-verified", and its Interests/Languages fields
    use `.fill()` on the exact span-based pseudo-inputs this file spent
    2026-07-25/26 proving `.fill()` cannot work on (see fill_audience_location/
    fill_audience_languages/paste_targeting_interests docstrings). Reused only
    its one clearly-safe, non-overlapping addition: dismissing the one-time
    "You no longer need to choose a campaign setup" onboarding modal.

    The recording confirmed several things live evidence had only guessed at:
      - Campaign/Ad set name inputs have no real ARIA label — they match by
        placeholder text, not get_by_label.
      - The Budget input's accessible name is "Budget", not "Daily budget".
      - The footer Next control has no `role` attribute on its own text node,
        but get_by_role("button", name="Next") still resolves it (role comes
        from an ancestor) — no breadcrumb/sidebar-tree workaround needed here.

    Returns "" on success, else a short slug naming the first failed step
    (matches the vocabulary already used in RecipeRunResult step ids).
    """

    def _wait_loading() -> None:
        # Meta shows a transient full-page loading overlay, seemingly on a
        # periodic list refresh as well as after transitions (list -> Create
        # modal, Continue -> wizard) — confirmed live (2026-07-21) covering the
        # page while a click/fill silently missed underneath it, and a single
        # wait-then-click was still sometimes racing a fresh overlay. Checked
        # before every action below, with a retry loop around the action itself.
        # Wording varies ("Loading...", "Loading creation" seen live on a
        # campaign list with 30+ prior test drafts) — hence `_LOADING_TEXT`
        # matches the word at the start rather than the literal ellipsis.
        #
        # Спрашиваем сначала, висит ли накладка сейчас. Если её нет, ждать
        # нечего и возвращаться надо немедленно: прежний вариант в этом
        # случае звал `wait_for(hidden)` на элементе, который скрываться не
        # собирался, и выбирал весь таймаут. Тридцать секунд урезаны до
        # восьми — накладка, не ушедшая за восемь секунд, уже не переходная,
        # и следующая попытка действия всё равно проверит заново.
        try:
            overlay = page.get_by_text(_LOADING_TEXT).first
            if overlay.count() and overlay.is_visible(timeout=200):
                overlay.wait_for(state="hidden", timeout=8000)
                return
        except Exception:
            pass
        # ACL-1313: маршрутный спиннер без слова «Loading» — те же 8 секунд.
        if _route_placeholder_visible(page):
            try:
                page.wait_for_selector(_ROUTE_PLACEHOLDER_SEL, state="hidden", timeout=8000)
            except Exception:
                pass

    def _click(loc, *, attempts: int = 5) -> bool:
        for _ in range(attempts):
            _wait_loading()
            try:
                loc.wait_for(state="visible", timeout=8000)
                loc.scroll_into_view_if_needed(timeout=3000)
                box = loc.bounding_box()
                if box and box.get("width", 0) > 0:
                    _cdp_mouse_click(page, box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                else:
                    loc.click(timeout=4000)
                return True
            except Exception:
                _sleep_h(0.6)
        return False

    def _fill(loc, value: str, *, attempts: int = 5) -> bool:
        for _ in range(attempts):
            _wait_loading()
            try:
                loc.wait_for(state="visible", timeout=8000)
                loc.click(timeout=3000)
                loc.fill(value, timeout=4000)
                return True
            except Exception:
                _sleep_h(0.6)
        return False

    # Telemetry rides along with the screenshots. Every step in this flow
    # already calls _shot("after_<step>") or _shot("fail_<step>"), so this
    # single choke point yields a complete, machine-readable record of the
    # run — per-step outcome AND duration — with no separate instrumentation
    # to keep in sync as steps change. Until now the same information had to
    # be reconstructed by hand from screenshot filenames.
    run_id = _telemetry.new_run_id()
    # Названо один раз здесь — дальше номер прогона получают и те места,
    # которые пишут в телеметрию напрямую, мимо `_shot`.
    _telemetry.set_current_run(run_id, campaign_name)
    _shot_clock = {"t": time.time()}

    def _shot(label: str, diag: dict | None = None, note: str | None = None) -> None:
        now = time.time()
        step, ok = _telemetry.normalise_step(label)
        # Причина отказа собирается здесь, в единственной точке, а не в сорока
        # трёх местах вызова. Ни один из них её не передавал: у всех отказов
        # 9 сентября `diag` был пуст, и сказать про них было нечего — ни
        # человеку, ни будущему агенту. Собранное в одном месте не разойдётся
        # с вызовами, и новый шаг получит контекст, ничего для этого не делая.
        if ok is False:
            diag = _telemetry.merge_diag(_telemetry.page_context(page), diag)
        # Разметка упавшего шага — на диск, рядом со скриншотом. По картинке
        # селектор не напишешь: на вопрос «почему не нашлась кнопка» отвечает
        # то, что было в DOM. Тот же снимок нужен стенду `acl-340`, без
        # которого `meta_dom` не покрыть тестами.
        if ok is False:
            try:
                from src.fb_ads_rpa import page_capture as _pages

                saved = _pages.capture(
                    page, step=step, run_id=run_id, campaign=campaign_name, diag=diag
                )
                if saved:
                    diag = {**(diag or {}), "page": saved}
            except Exception:
                pass
        _telemetry.record(
            step,
            ok,
            run_id=run_id,
            campaign=campaign_name,
            duration_s=now - _shot_clock["t"],
            diag=diag,
            note=note or "",
        )
        _shot_clock["t"] = now
        if ok is not False and not _shot_worth_a_picture(label):
            return
        try:
            from src.fb_ads_rpa.screenshots import capture as _capture

            _capture(page, f"recorded_{label}")
        except Exception:
            pass

    def _stop_if_account_blocked(where: str) -> bool:
        """Аккаунт заблокирован Meta — остановить кампанию (`account_blocked`).

        Не повторяется (retry_policy.NON_RETRYABLE_FLOW_STEPS): повтор
        кампании в заблокированном аккаунте снова упрётся в тот же баннер.
        В телеметрии — найденный текст (note) и место проверки.
        """
        matched = _account_blocked_text(page)
        if not matched:
            return False
        _shot("fail_account_blocked",
              {"where": where, "kind": _account_blocked_match(matched), "matched": matched[:300]},
              note=matched[:160])
        return True

    # Dismiss any dismissible top banner (e.g. "We're evolving how we measure
    # results") before the very first click — confirmed live 2026-07-24 on a
    # fresh account (existing real campaigns, unlike the emptier test
    # accounts used so far): the banner sat at the very top of the page and
    # the flow never got past the campaigns list, suggesting it covered or
    # shifted the Create button just enough to miss it.
    # Retry the dismiss a few times with a short wait — confirmed live
    # 2026-07-24 that a single dismiss attempt can run before the banner has
    # actually rendered (intermittent: worked on one run, didn't on the next
    # against the same account), leaving it in place to block/shift Create.
    # Начинаем с вопроса, открыт ли вообще Ads Manager. 10 сентября прогон
    # встал между кампаниями на ERR_SSL_PROTOCOL_ERROR: на экране была
    # страница-ошибка самого браузера, и следующая кампания не началась
    # никогда. Шаг честно не нашёл бы на ней кнопку Create и сообщил бы «не
    # нашёл Create» — то есть назвал бы причиной разметку Meta.
    #
    # Перезагрузка здесь безопасна ровно потому, что мы **в начале**: на
    # странице-ошибке ничего не введено и терять нечего. Именно поэтому
    # правило «застряло — обнови» применяется тут, а не ко всякому отказу
    # подряд: посреди мастера создания кампании перезагрузка стёрла бы
    # несохранённые поля.
    if _recover_from_browser_error(page):
        _shot("skip_browser_error_recovered")

    from src.fb_ads_rpa.browser import ensure_editor_window

    if not ensure_editor_window(page, "campaign"):
        return "window_too_small"

    # Record geometry for diagnosing failures.
    try:
        _telemetry.record("browser_window", None, diag=_telemetry.window_state(page) or None)
    except Exception:
        pass

    # ACL-1313: окно «Turn off ad blocker», тост сети и баннеры — до Create.
    _clear_blocking_overlays(page, "campaign", editor=False)
    for _ in range(2):
        _sleep_h(0.4)
        _dismiss_floating_banners(page)
    # 20.09 01:20: «Loading creation» без конца шло на аккаунтах, где
    # накопились черновики (CO_bnc 11, CR_ait 10). Пишем их число до Create,
    # чтобы связь была видна сразу, а не по снимкам.
    _telemetry.record("drafts_pending", None, diag={"count": _pending_drafts(page)})
    # ACL-1313: заблокированный аккаунт (ecce52f330b8, 22.09) — не жать Create.
    if _stop_if_account_blocked("campaigns_list"):
        return "account_blocked"
    list_url = page.url
    if not _click(page.get_by_role("button", name="Create", exact=True)):
        if not (recover_stuck_page(page, "create", editor=False)
                and _click(page.get_by_role("button", name="Create", exact=True))):
            return "create"
    _shot("after_create")

    def _back_to_list() -> None:
        """Нажатие увело со списка кампаний — вернуться на него."""
        try:
            if list_url and page.url.split("?", 1)[0] != list_url.split("?", 1)[0]:
                page.goto(list_url, wait_until="domcontentloaded", timeout=60000)
                _sleep_h(3.0, 0.2)
        except Exception:
            pass

    # ACL-1313: промах виден за секунды — повторяем нажатие сразу, без 75 с
    # ожидания и перезагрузки. Два быстрых круга, дальше — прежний путь.
    for _quick in range(2):
        took = _create_took(page, list_url)
        _telemetry.record("create_took", took == "ok", note=took)
        if took == "ok":
            break
        if took == "left_list":
            _back_to_list()
        try:
            page.get_by_role("button", name="Create", exact=True).first.wait_for(
                state="visible", timeout=30000)
        except Exception:
            pass
        _clear_blocking_overlays(page, "create_quick_retry", editor=False)
        _click(page.get_by_role("button", name="Create", exact=True))
        _shot("after_create_quick_retry")

    # ACL-1233: окно выбора цели не появилось, а страница висит в «Loading…»
    # или стала страницей ошибки Chrome — перезагрузить список и нажать Create
    # ещё раз, вместо минут ожидания в каждой попытке клика (19.09: 10–12 мин).
    # 19.09 вечер: одного раунда мало — Meta отвечала со второго-третьего
    # раза. До двух раундов «разбудить → перезагрузить → Create».
    # 20.09 02:05, замер по 80 удачным переходам за сутки: медиана 19 с, но
    # 31 из 80 — дольше 25 с (до 151 с). Прежние 25 с обрывали нормальное
    # ожидание в 40% случаев, и перезагрузка начинала отсчёт заново.
    # ACL-1313: пока крутится «Loading creation», ждём её, а не перезагружаем.
    for attempt in range(2):
        if _wait_objective_dialog(page):
            break
        # Со списка увели (Audiences, 887b2d): перезагрузка открыла бы ту же
        # чужую страницу, где кнопки Create нет вовсе.
        _back_to_list()
        cleared = _clear_blocking_overlays(page, "objective", editor=False)
        if ("network_trouble:reloaded" not in cleared
                and not recover_stuck_page(page, "objective", editor=False, force=True)):
            break
        try:
            page.get_by_role("button", name="Create", exact=True).first.wait_for(
                state="visible", timeout=30000)
        except Exception:
            pass
        _clear_blocking_overlays(page, "create_retry", editor=False)
        _click(page.get_by_role("button", name="Create", exact=True))
        _shot("after_create_retry")

    # New Meta UI sometimes shows a Guided/Manual choice before the objective
    # picker enables Campaign name — click Manual if present, best-effort
    # (the recording's session apparently started already past this choice).
    # ACL-1313: сюда приходим, когда окно цели уже отрисовано (радио Leads
    # видно) — есть ли в нём «Manual campaign setup» и заголовок «Leads»,
    # видно сразу. Прежде клик по отсутствующему элементу ждал 3 с и 5 с на
    # каждой кампании.
    manual = page.get_by_text("Manual campaign setup", exact=False)
    if _visible_now(manual):
        try:
            manual.first.click(timeout=3000)
            _shot("after_manual")
        except Exception:
            pass

    leads_heading = page.get_by_role("heading", name="Leads")
    if _visible_now(leads_heading):
        try:
            leads_heading.first.click(timeout=5000)
        except Exception:
            pass
    # ACL-1313: окна нет — одно ограниченное ожидание, а не пять заходов
    # `_click` по 8+8 с (~83 с) ради того же отказа.
    leads_radio = page.get_by_role("radio", name="Leads")
    try:
        leads_radio.first.wait_for(state="visible", timeout=int(OBJECTIVE_RADIO_LAST_WAIT_S * 1000))
        radio_there = True
    except Exception:
        radio_there = False
    if not radio_there or not _click(leads_radio):
        _shot("fail_objective_leads")
        return "objective_leads"
    _shot("after_leads")

    if not _click(page.get_by_role("button", name="Continue")):
        _shot("fail_objective_continue")
        return "objective_continue"
    _wait_loading()
    _shot("after_continue")

    # Live evidence (2026-07-23, first run ever on a fresh account —
    # FacebookDQ204 CR): a one-time onboarding modal ("You no longer need to
    # choose a campaign setup") appears over Campaign details on accounts
    # that haven't dismissed it before, blocking the campaign-name fill
    # underneath. Never seen on the day's main test account since it had
    # already dismissed this once. Best-effort, harmless no-op elsewhere.
    # First attempt used a raw one-shot .click() with no _wait_loading()
    # guard -- it raced the transient "Loading..." overlay (same class of
    # bug documented everywhere else in this function) and silently missed,
    # while the screenshot happened to catch the overlay and looked like
    # progress. Use the same retrying _click() helper as everything else.
    # Сначала спрашиваем, есть ли она вообще. Пять попыток кликнуть по
    # отсутствующей кнопке стоили ровно 43 секунды: три захода по 8 секунд
    # ожидания видимости плюс два по 8, плюс паузы между ними. Измерено по
    # телеметрии — у шага плоский пол 43.2с при медиане 44.0с и четырнадцати
    # прогонах, то есть модалки не было ни разу. За 9 сентября — 11 минут
    # ожидания того, чего нет.
    #
    # Ждать её всё же надо: она всплывает поверх «Campaign details» и
    # закрывает поле имени кампании, а появляется не мгновенно. Но четырёх
    # секунд на «появись, если ты есть» достаточно — дальше идёт заполнение
    # имени со своими попытками, и если модалка всё же успела встать, оно
    # упадёт заметно, а не молча.
    #
    # ACL-1313: ждём не «модалку 4 с», а первое из двух — модалку или поле
    # имени. Поле видно и модалки нет — идём дальше сразу, без 4 с простоя
    # на каждой кампании. Если модалка всплывёт позже поля, ввод имени не
    # пройдёт — тогда она закрывается и ввод повторяется до перезагрузки.
    onboarding_btns = (
        page.get_by_role("button", name="Don't show again"),
        page.get_by_role("button", name="OK", exact=True),
    )
    name_field = page.get_by_placeholder("Enter your campaign name here")
    first_seen = _first_visible(page, (*onboarding_btns, name_field), budget_s=4.0)
    if first_seen is not None and first_seen is not name_field:
        _click(first_seen, attempts=3)
    _shot("after_onboarding_modal")

    def _late_onboarding_modal() -> bool:
        btn = _first_visible(page, onboarding_btns, budget_s=0.0)
        return btn is not None and _click(btn, attempts=2)

    if not (_fill(name_field, campaign_name)
            or (_late_onboarding_modal() and _fill(name_field, campaign_name))):
        # 20.09 01:12 (CL_ml TRDEZ, третий раз за сутки): после Continue
        # редактор не отрисовался — пустой экран с «Loading…», и поля имени
        # на странице нет вовсе. Вводить ещё нечего, поэтому перезагружаем
        # без оглядки на «All edits saved» и пробуем снова.
        if not (recover_stuck_page(page, "campaign_name", editor=False, force=True)
                and _fill(page.get_by_placeholder("Enter your campaign name here"), campaign_name)):
            _shot("fail_campaign_name")
            return "campaign_name"
    # Confirmed live 2026-07-28: _fill() returning True only means the type
    # succeeded, not that Meta's async autosave actually persisted it — a
    # campaign was seen to silently revert to the default "New Leads
    # Campaign" / $25 budget many steps later, with no exception anywhere
    # along the way. Give the debounced autosave a moment to actually
    # start, then wait for "Verifying your edits..." to clear before
    # trusting the value and moving on.
    wait_editor_saved(page)
    _shot("after_campaign_name")

    # 17.09 00:05 (S15, 71f6bddbcf7c): Meta открыла черновик с «Ad set
    # budget» по умолчанию — поля бюджета на уровне кампании нет, и шаг
    # 133 с искал его. Бюджет у нас кампанийный: переключить до ввода.
    if _ensure_campaign_budget(page):
        wait_editor_saved(page)

    try:
        page.get_by_role("textbox", name="Budget").click(timeout=3000)
    except Exception:
        pass
    budget_ok = _fill(page.get_by_role("spinbutton", name="Budget"), budget)
    if not budget_ok:
        budget_ok = _fill(page.get_by_role("textbox", name="Budget"), budget)
    if not budget_ok:
        # aria_snapshot evidence (2026-07-21) showed this input's accessible
        # name/placeholder is actually "Please enter amount", not "Budget" —
        # try that directly rather than the role+name guess above.
        budget_ok = _fill(page.get_by_placeholder("Please enter amount"), budget)
    if not budget_ok:
        _shot("fail_daily_budget")
        return "daily_budget"
    wait_editor_saved(page)
    _shot("after_campaign_fields")

    # Verify both fields actually still hold what we just typed before
    # clicking Next — cheaper than discovering a silent revert several
    # minutes and many screenshots later.
    try:
        name_now = page.get_by_placeholder("Enter your campaign name here").input_value(timeout=2000)
    except Exception:
        name_now = None
    if name_now is not None and name_now.strip() != campaign_name.strip():
        for _ in range(3):
            if _fill(page.get_by_placeholder("Enter your campaign name here"), campaign_name):
                wait_editor_saved(page)
                break

    if not _click(page.get_by_role("button", name="Next")):
        _shot("fail_next_to_adset")
        return "next_to_adset"
    _wait_loading()
    _shot("after_next_to_adset")
    if not ensure_editor_window(page, "adset"):
        return "window_too_small"
    _clear_blocking_overlays(page, "adset")

    if not _fill(page.get_by_placeholder("Enter your ad set name here..."), adset_name):
        # ACL-1313 (как у campaign_name): после Next вместо редактора ad
        # set'а висел маршрутный спиннер, и поля имени не было вовсе.
        # Перезагрузка — через `recover_stuck_page` в режиме редактора: только
        # при зависшей загрузке и «All edits saved», поля кампании не теряем.
        if not (recover_stuck_page(page, "adset_name", editor=True)
                and _fill(page.get_by_placeholder("Enter your ad set name here..."), adset_name)):
            _shot("fail_adset_name")
            return "adset_name"
    # Reusing the same ad set name across runs makes Meta show a name-history
    # autocomplete dropdown under this field — confirmed live (2026-07-21)
    # covering the Conversion location control below it and blocking the next
    # click. NOTE: do not press Escape here — elsewhere in this file that's
    # documented to close the whole wizard, not just a popover. Click a
    # neutral heading instead to move focus off the field.
    try:
        page.get_by_text("Ad set name", exact=True).first.click(timeout=2000)
    except Exception:
        pass
    # 20.09 07:53: клика по заголовку не хватило — список остался висеть и
    # накрыл «Conversion location» на всех трёх аккаунтах разом.
    _dismiss_name_suggestions(page)
    _shot("after_adset_name")

    if not click_conversion_location_dropdown(page):
        _shot("fail_conversion_location_open")
        return "conversion_location_open"
    # Live evidence (2026-07-21): the open menu shows Website directly, both
    # "Multiple" and "Single" already expanded — no separate accordion click
    # needed. wait_conversion_menu_open() checks [role=listbox]/[role=menu],
    # which this custom dropdown doesn't use (consistent with the rest of
    # today's findings: this UI is light on real ARIA roles) — check for the
    # "Website" radio text directly instead.
    _shot("after_conversion_menu_open")

    # Don't trust a bare radio click here: "Website" lives inside the "Single"
    # accordion, which the live run today (2026-07-24) showed is NOT always
    # already expanded despite this file's older comment — a coordinate-based
    # click can silently land on the still-visible "Website and instant forms"
    # (Multiple) option instead. pick_website_conversion_option() already
    # handles expanding Single first and tries several verified click
    # strategies, so use it directly instead of a fragile inline attempt.
    if not pick_website_conversion_option(page):
        _shot("fail_conversion_website")
        return "conversion_website"

    _sleep_h(0.6)
    _shot("after_website_click")

    try:
        page.get_by_text("Verifying your edits", exact=False).first.wait_for(state="hidden", timeout=8000)
    except Exception:
        pass

    # is_website_conversion_selected() reads ONLY the dropdown trigger's own
    # text (ignoring nested banners), so it correctly distinguishes "Website"
    # from "Website and instant forms" — a whole-page text-node scan (the
    # previous approach here) found a live false positive today (2026-07-24):
    # it matched a "Website" text node still present in the DOM inside a
    # collapsed-but-not-removed "Single" section, even though the visible
    # dropdown still showed "Website and instant forms".
    if not is_website_conversion_selected(page):
        _shot("fail_conversion_website_verify")
        return "conversion_website_verify"

    _shot("success_website_selected")

    # Next segment past Website: Conversion event = Lead. Reuses the existing
    # select_conversion_event_lead()/is_lead_conversion_event_selected() pair
    # (already used by the older recipe_player delegated-section path).
    # Soft-fail (2026-07-26, explicit user direction): confirmed live via
    # playwright-skill that a hard failure here can be the pixel/dataset
    # genuinely having no tracked events yet (both "Active events" and
    # "Inactive events" render empty) — not a click/selector bug, and not
    # something retrying fixes. Per the same "don't stop the test, keep
    # going" direction that made Locations/Age/Targeting soft-fail, log and
    # continue into Audience/Targeting/Ad-level instead of aborting the
    # whole run over one field this account's data can't currently satisfy.
    top_level_soft_fails: list[str] = []
    try:
        if not select_conversion_event_lead(page):
            _shot("fail_conversion_event")
            top_level_soft_fails.append("conversion_event")
        else:
            _shot("success_conversion_event")
    except Exception as exc:
        _shot("fail_conversion_event")
        top_level_soft_fails.append(f"conversion_event:{str(exc)[:60]}")

    # Next segment: Audience section, "Further limit the reach of your ads"
    # (gear icon, collapsed by default under Suggest an audience) — confirmed
    # live 2026-07-24 that the wizard actually reaches this screen. First
    # pass: just expand it and screenshot: what's inside hasn't been mapped
    # yet, so don't assume a shape for it — the next iteration reads this
    # screenshot to decide what (if anything) needs filling in here.
    try:
        scroll_to_section(page, "Audience")
        opened = False

        def _reach_dialog_open() -> bool:
            """Окно «Reach your preferred audiences» уже открыто?

            20.09 14:48 (SA_arc GMBL, пятый такой отказ за сутки): окно было
            открыто, а шаг падал. Та же фраза есть и на странице за окном, и
            клик уходил в перекрытую ссылку под ним — пять попыток подряд.
            """
            try:
                return page.get_by_text(
                    re.compile(r"Reach your preferred audiences", re.I)).first.is_visible(timeout=800)
            except Exception:
                return False

        if _reach_dialog_open():
            opened = True
        for _ in range(0 if opened else 5):
            try:
                el = page.get_by_text(
                    re.compile(r"further limit the reach of your ads", re.I)
                ).first
                el.scroll_into_view_if_needed(timeout=5000)
                el.click(timeout=5000)
                _sleep_h(0.6)
                opened = True
                break
            except Exception:
                # Not rendered/visible yet — nudge the page down and retry
                # rather than assuming the text is simply absent.
                if _reach_dialog_open():
                    opened = True
                    break
                try:
                    page.mouse.wheel(0, 400)
                except Exception:
                    pass
                _sleep_h(0.6)
        _shot("after_further_limit_reach" if opened else "fail_further_limit_reach_click")
        if not opened:
            return "further_limit_reach"

        # The link opens a confirmation modal ("Reach your preferred
        # audiences") with 3 radio choices; "Stay in current setup" is
        # selected by default. Pick the "Further limit the reach of your
        # ads" radio specifically (matches the same phrase as the link that
        # opened this modal, but scoped to the dialog so we don't re-match
        # the now-hidden link behind it), then Continue.
        picked_radio = False
        for root in _roots(page):
            try:
                dialog = root.get_by_role("dialog").first
                if not dialog.is_visible(timeout=1500):
                    continue
                option = dialog.get_by_text(
                    re.compile(r"further limit the reach of your ads", re.I)
                ).first
                option.scroll_into_view_if_needed(timeout=3000)
                option.click(timeout=4000)
                picked_radio = True
                break
            except Exception:
                continue
        _shot("after_pick_further_limit_radio" if picked_radio else "fail_pick_further_limit_radio")
        if not picked_radio:
            return "further_limit_reach_radio"

        continued = False
        for root in _roots(page):
            try:
                btn = root.get_by_role("button", name=re.compile(r"^continue$", re.I)).first
                if btn.is_visible(timeout=1500):
                    btn.click(timeout=4000)
                    continued = True
                    break
            except Exception:
                continue
        if continued:
            # Confirmed live 2026-07-24: the scroll position after this
            # Continue click was inconsistent between runs (sometimes
            # Locations/Age already visible, sometimes the page ended up
            # back at the top) — a fixed sleep(0.6) wasn't enough to let
            # Meta's own re-render/verification settle. Wait for the actual
            # markers instead: the "Verifying your edits..." indicator, then
            # the "You've switched audience setup" toast to appear AND clear.
            wait_editor_saved(page)
            # ACL-1313: тост ждём уйти, только если он на экране. Прежде
            # 3 с уходили на ожидание тоста, которого нет (applied_without_dialog
            # и большинство прогонов 25.09): wait_editor_saved уже дождался
            # «Verifying your edits», перерисовка к этому моменту прошла.
            toast = page.get_by_text(re.compile(r"You've switched audience setup", re.I))
            if _visible_now(toast):
                try:
                    toast.first.wait_for(state="hidden", timeout=8000)
                except Exception:
                    pass
            _sleep_h(0.5)
        if not continued and _audience_already_manual(page):
            # 17.09 (S15 06:53, Doc21 04:19/04:28): выбор радио применился
            # сразу, окна с Continue не было — на экране уже «Switch to
            # recommended setup» и живые выпадашки возраста. Прежде это был
            # отказ, и на Doc21 PF он оборвал кампанию целиком.
            _telemetry.record("further_limit_continue", None, note="applied_without_dialog")
            continued = True
        _shot("after_further_limit_continue" if continued else "fail_further_limit_continue")
        if not continued:
            return "further_limit_reach_continue"
    except Exception:
        _shot("fail_further_limit_reach")
        return "further_limit_reach"

    # Locations: confirmed live 2026-07-24 that without an explicit fill here,
    # this defaults to whatever Meta last used (seen: Hong Kong) instead of
    # the draft's actual country — a real targeting-correctness bug, not just
    # a missing nice-to-have. Reuses fill_audience_location(), which was
    # written for the older recipe_player delegated path and has NOT been
    # independently re-verified the way Website/Conversion event were (it
    # optimistically returns True after a click/Enter, without checking what
    # actually got selected) — treat its return value as a weaker signal than
    # the other steps in this function until that's tightened up.
    # Audience fill-in (Locations / Age / Targeting): best-effort from here
    # on, per explicit direction — if one field doesn't click, log it and
    # move on to the next rather than aborting the whole run. soft_fails
    # collects what didn't work so the caller/diary can see exactly which
    # pieces still need a live fix, without losing the rest of the segment.
    soft_fails: list[str] = list(top_level_soft_fails)

    # Age BEFORE Locations (reordered 2026-07-25): Age used to render as
    # live "18 [v] 65+ [v]" dropdowns directly, confirmed in screenshots
    # from before the Locations fix existed. Now that Locations reliably
    # expands into a large panel (map + chip list), it pushes everything
    # below it down and Age started rendering as a collapsed "18 - 65+"
    # text summary instead — a click-to-expand attempt modeled on Locations
    # made this worse (scrolled past Age entirely), so the working
    # hypothesis is layout-order, not Age's own mechanics: fill Age first,
    # while the page is still in its simpler pre-Locations-expansion state.
    try:
        if not fill_audience_age(page, age_min, age_max):
            _shot("fail_audience_age")
            soft_fails.append("audience_age")
        else:
            _shot("after_audience_age")
    except Exception as exc:
        _shot("fail_audience_age")
        soft_fails.append(f"audience_age:{str(exc)[:60]}")

    if country:
        _shot("before_audience_location")
        try:
            all_placeholders = page.evaluate(
                "() => Array.from(document.querySelectorAll('input[placeholder]'))"
                ".filter(el => el.offsetParent !== null)"
                ".map(el => el.placeholder)"
            )
            Path("output/debug_visible_placeholders.txt").write_text(
                "visible input placeholders before fill_audience_location:\n"
                + "\n".join(all_placeholders),
                encoding="utf-8",
            )
        except Exception:
            pass
        try:
            if not fill_audience_location(page, country):
                _shot("fail_audience_location")
                soft_fails.append("audience_location")
            else:
                _shot("after_audience_location")
        except Exception as exc:
            _shot("fail_audience_location")
            soft_fails.append(f"audience_location:{str(exc)[:60]}")

    # Languages — wired to the real per-country language from the Upload
    # FB.xlsx "Countries" sheet (see enrich_draft.py's lookup_country()),
    # not a hardcoded guess. Confirmed live (2026-07-25) fill_audience_languages
    # itself works; only the "what value to type" plumbing was missing before.
    if language_names:
        try:
            if not fill_audience_languages(page, language_names):
                _shot("fail_languages")
                soft_fails.append("languages")
            else:
                _shot("after_languages")
        except Exception as exc:
            _shot("fail_languages")
            soft_fails.append(f"languages:{str(exc)[:60]}")

    # Detailed targeting: paste each comma-separated interest term and pick
    # the first autocomplete suggestion — same pattern the old recipe_player
    # path used for this exact field.
    if targeting_label:
        try:
            if not open_detailed_targeting_field(page):
                _shot("fail_targeting_open")
                soft_fails.append("targeting_open")
            else:
                any_added = paste_targeting_groups(page, targeting_label)
                if not any_added:
                    # 20.09 13:03: интересы вставлены и на снимке стоят, но Meta
                    # рисует их дольше нашего ожидания, и шаг падал зря. Вместо
                    # всё более длинных пауз перечитываем группы ещё раз позже —
                    # к этому времени время уже прошло само.
                    _sleep_h(6.0)
                    groups = [g for g in re.split(r"\s+and\s+", targeting_label, flags=re.I) if g.strip()]
                    terms = [t.strip() for t in (groups[-1] if groups else targeting_label).split(",") if t.strip()]
                    late = _await_group_terms(page, terms, timeout_s=40.0)
                    any_added = bool(late.get("hits"))
                    probe = {}
                    if not any_added:
                        # 20.09 13:24: снимок, снятый за 48 с до этого чтения,
                        # держал все пять интересов, а чтение видит пустую
                        # группу. Замеряем, что именно видно живьём.
                        try:
                            probe = page.evaluate(_TARGETING_PROBE_JS) or {}
                        except Exception as exc:
                            probe = {"error": str(exc)[:80]}
                        try:
                            from src.fb_ads_rpa import page_capture as _pc
                            _pc.capture(page, step="targeting_recheck_empty", diag=probe)
                        except Exception:
                            pass
                    _telemetry.record("targeting_recheck", any_added,
                                      note="late_hits" if any_added else "still_empty",
                                      diag={"hits": (late.get("hits") or [])[:5],
                                            "selected": (late.get("selected") or [])[:8],
                                            "why": late.get("why", ""), "probe": probe})
                _shot("after_targeting" if any_added else "fail_targeting_none_added")
                if not any_added:
                    soft_fails.append("targeting_none_added")
        except Exception as exc:
            _shot("fail_targeting")
            soft_fails.append(f"targeting:{str(exc)[:60]}")
    else:
        # Пропуск тоже событие. Раньше пустой список интересов не оставлял в
        # телеметрии ни строки: шага в потоке просто не существовало. 9 сентября
        # на экране это выглядело как «интересы не проставились», а отличить
        # «нечего было вводить» от «не нашёл поле» было нечем — оба исхода
        # выглядели одинаково, то есть никак.
        _shot("skip_targeting", {"why": "в черновике нет интересов"})

    # ACL-1313: Advantage+ audience — итог, а не действие. Проверяется после
    # возраста, локации, языков и интересов. Включённая = возраст лишь
    # «подсказка», и Meta показывает рекламу вне него. Возраст 18–65+ —
    # не условие вовсе: там «on» ожидаем, проверять нечего.
    if not (age_min == 18 and age_max >= 65):
        try:
            if not verify_audience_advantage_off(page):
                _shot("fail_advantage_plus_audience")
                soft_fails.append("advantage_plus_audience")
        except Exception as exc:
            soft_fails.append(f"advantage_plus_audience:{str(exc)[:60]}")

    # Placements: switch off Advantage+ automatic placements and set the
    # account's real manual configuration (confirmed live 2026-07-26 via
    # user screenshot) — real checkboxes, so this should be more reliable
    # than the Audience fields above, but still soft-fail per the same
    # "don't stop the test" contract.
    try:
        placements_report: dict = {}
        ok_placements = fill_placements(page, placements_report)
        if not ok_placements and recover_stuck_page(page, "placements", editor=True):
            # 20.09 00:35 CO_bnc Mix: панель плейсментов не загрузилась, и
            # следом отказал iOS. После перезагрузки шаг делается заново.
            placements_report = {}
            ok_placements = fill_placements(page, placements_report)
        if not ok_placements:
            # Отчёт кладётся рядом с отказом: пустое «placements ✗» стоило
            # пяти прогонов вслепую — шаг падал, а что именно не нашлось,
            # приходилось выяснять заново каждый раз.
            _shot("fail_placements", placements_report)
            soft_fails.append("placements")
        else:
            _shot("after_placements")
    except Exception as exc:
        _shot("fail_placements", {"error": str(exc)[:200]})
        soft_fails.append(f"placements:{str(exc)[:60]}")

    # Devices and operating systems: lives inside the same Placements /
    # Account controls panel just expanded above (confirmed live
    # 2026-07-28), not a separate Audience field. "All" is Meta's default
    # (no-op); only "iOS" needs an explicit switch.
    if (os_val or "All").strip().lower() == "ios":
        try:
            ok_ios = select_devices_ios_only(page)
            if not ok_ios and recover_stuck_page(page, "devices_ios", editor=True):
                # 20.09 01:37 CO_bnc Mix: раздел устройств не загрузился и за
                # 25 с. Перезагрузка (при «All edits saved») и второй заход —
                # так же, как с панелью плейсментов.
                fill_placements(page, {})
                ok_ios = select_devices_ios_only(page)
            if not ok_ios:
                _shot("fail_devices_ios")
                soft_fails.append("devices_ios")
            else:
                _shot("after_devices_ios")
        except Exception as exc:
            _shot("fail_devices_ios")
            soft_fails.append(f"devices_ios:{str(exc)[:60]}")

    if soft_fails:
        try:
            Path("output/debug_audience_soft_fails.txt").write_text(
                "\n".join(soft_fails), encoding="utf-8"
            )
        except Exception:
            pass

    # Ad level: Next -> Set up creative -> Media upload -> Primary text/
    # Headline -> Destination (Website + tracking link, no Instant Form).
    # Same soft-fail contract as the Audience block above — a failure here
    # is logged and the run still ends in success (recorded_flow:reached_ad
    # instead of :reached_targeting), matching the explicit "don't stop the
    # test, keep going" direction that shaped the Audience block too.
    ad_level_fails: list[str] = []
    if creative_path:
        ad_edit_url = ""
        reached_ad_level = False
        try:
            if not _click(page.get_by_role("button", name="Next")):
                _shot("fail_next_to_ad")
                # HARD fail, not soft. Confirmed live 2026-07-29 (Test145,
                # campaign B): the editor drawer closed mid-audience-setup,
                # every later step failed against the bare ad-sets list,
                # and this soft-append still let the run report
                # success/reached_ad for a campaign with ZERO configured
                # ads. Reaching the ad level is the whole point of the
                # flow — failing to get there must fail the campaign (and
                # lets publish_campaign's whole-campaign retry re-run it).
                return "next_to_ad"
        except Exception:
            _shot("fail_next_to_ad")
            return "next_to_ad"
        _wait_loading()
        ad_edit_url = page.url
        reached_ad_level = True
        _shot("after_next_to_ad")
        if not ensure_editor_window(page, "ad"):
            return "window_too_small"
        _clear_blocking_overlays(page, "ad")
        if _stop_if_account_blocked("ad_editor"):
            return "account_blocked"

        if reached_ad_level:
            final_ad_name = ad_name or f"{campaign_name} Ad"
            if not _fill(page.get_by_placeholder("Enter your ad name here..."), final_ad_name):
                _shot("fail_ad_name")
                ad_level_fails.append("ad_name")
            else:
                _shot("after_ad_name")

        creative_uploaded = False
        if reached_ad_level:
            try:
                if not upload_ad_creative_with_retry(page, creative_type, creative_path, edit_url=ad_edit_url):
                    # След попытки уходит в телеметрию вместе с отказом.
                    # Раньше здесь было пусто, и «не загрузился» ничем не
                    # отличалось от «не нашёл кнопку» — шесть отказов 14.09
                    # не дали ни одной зацепки, и разбор свёлся к догадкам.
                    _shot("fail_ad_creative_upload", last_upload_diag())
                    ad_level_fails.append("ad_creative_upload")
                else:
                    creative_uploaded = True
                    _shot("after_ad_creative_upload")
            except Exception as exc:
                _shot("fail_ad_creative_upload")
                ad_level_fails.append(f"ad_creative_upload:{str(exc)[:60]}")

        # 21.09 11:17: порядок был имя → креатив → текст → назначение, и
        # редактор креатива блокировался ошибкой «lead_gen_form_id» ещё до
        # текста: Meta ставит в Browser add-ons «Instant form», а формы нет.
        # Назначение (внутри него и выбор «None») теперь идёт ДО текста, и
        # блокировке взяться неоткуда.
        if reached_ad_level and destination_url:
            try:
                if not set_ad_destination_website(page, destination_url):
                    _shot("fail_ad_destination")
                    ad_level_fails.append("ad_destination")
                else:
                    _shot("after_ad_destination")
            except Exception as exc:
                _shot("fail_ad_destination")
                ad_level_fails.append(f"ad_destination:{str(exc)[:60]}")

        if reached_ad_level and creative_uploaded and (ad_title or ad_body):
            try:
                copy_report: dict = {}
                ok_copy = write_ad_copy(
                    page, ad_title, ad_body,
                    ad_name=ad_name or f"{campaign_name} Ad",
                    destination_url=destination_url, report=copy_report)
                if not ok_copy:
                    if _stop_if_account_blocked("ad_copy"):
                        return "account_blocked"
                    _shot("fail_ad_copy", copy_report)
                    # Объявление без текста называем прямо: по этой строке
                    # человек решает, публиковать ли кампанию.
                    ad_level_fails.append(
                        f"ad_copy:{ad_name or 'ad 1'} has no text"
                        + (f" ({copy_report['why']})" if copy_report.get("why") else ""))
                else:
                    _shot("after_ad_copy")
            except Exception as exc:
                _shot("fail_ad_copy")
                ad_level_fails.append(f"ad_copy:{str(exc)[:60]}")

        # Additional ads (2nd, 3rd, ...) in the same ad set — each one is a
        # fresh "Create ad" (create_additional_ad()) followed by the exact
        # same per-ad fill sequence used for the first ad above. Confirmed
        # live 2026-07-28 that upload_ad_creative/fill_ad_copy/
        # set_ad_destination_website work unmodified here.
        if reached_ad_level and additional_ads:
            for i, extra in enumerate(additional_ads, start=2):
                tag = f"ad{i}"
                # ACL-1233: зависшая загрузка или страница ошибки Chrome перед
                # объявлением — перезагрузить (в редакторе — только при «All
                # edits saved»), иначе все следующие объявления падают подряд.
                recover_stuck_page(page, f"{tag}_before", editor=True)
                if _stop_if_account_blocked(f"{tag}_before"):
                    return "account_blocked"
                shells_before = _tree_new_ad_shells(page)
                try:
                    created = create_additional_ad(page, adset_name)
                    if not created and recover_stuck_page(page, f"{tag}_create", editor=True):
                        created = create_additional_ad(page, adset_name)
                    if not created:
                        # A reported failure does not mean no ad was born:
                        # Meta names every fresh ad "New Leads Ad", so if
                        # that row now exists in the tree the create DID
                        # land and only the post-create bookkeeping flaked
                        # — adopt the shell (select it, close any wizard)
                        # and let the normal naming step rename it. Only
                        # when no shell exists is a clean retry safe
                        # (otherwise a second create would duplicate ads).
                        # Confirmed need live 2026-07-29 (Test146, B ad2):
                        # create failed once, leaving the ad missing
                        # entirely — the only fill step with no retry path.
                        if _open_tree_ad(page, "New Leads Ad"):
                            _close_creative_setup_modal(page)
                            created = True
                        else:
                            created = create_additional_ad(page, adset_name)
                    if not created:
                        _shot(f"fail_{tag}_create")
                        ad_level_fails.append(f"{tag}_create")
                        continue
                    _shot(f"after_{tag}_create")
                    # 17.09 00:02 (Doc20, cbe566db70b2): один «Create ad» дал
                    # ДВА «New Leads ad» — лишнее осталось пустым в кампании.
                    # Удалять сами не берёмся; отказ шага с именем причины,
                    # чтобы черновик не ушёл в публикацию с пустым объявлением.
                    shells_after = _tree_new_ad_shells(page)
                    if shells_before >= 0 and shells_after - shells_before > 1:
                        _telemetry.record(f"{tag}_create", False, note="extra_ad_shell",
                                          diag={"before": shells_before, "after": shells_after})
                        ad_level_fails.append(f"{tag}_extra_empty_ad")
                except Exception as exc:
                    _shot(f"fail_{tag}_create")
                    ad_level_fails.append(f"{tag}_create:{str(exc)[:60]}")
                    continue

                if not ensure_editor_window(page, tag):
                    return "window_too_small"
                _clear_blocking_overlays(page, tag)

                extra_name = extra.get("ad_name") or f"{campaign_name} Ad {i}"
                if not _fill(page.get_by_placeholder("Enter your ad name here..."), extra_name):
                    _shot(f"fail_{tag}_name")
                    ad_level_fails.append(f"{tag}_name")
                else:
                    _shot(f"after_{tag}_name")

                extra_creative_path = extra.get("creative_path") or ""
                no_creative = False
                if extra_creative_path:
                    try:
                        if not upload_ad_creative_with_retry(
                            page, extra.get("creative_type") or "", extra_creative_path, edit_url=page.url
                        ):
                            _shot(f"fail_{tag}_creative_upload")
                            ad_level_fails.append(f"{tag}_creative_upload")
                            no_creative = True
                        else:
                            _shot(f"after_{tag}_creative_upload")
                    except Exception as exc:
                        _shot(f"fail_{tag}_creative_upload")
                        ad_level_fails.append(f"{tag}_creative_upload:{str(exc)[:60]}")
                        no_creative = True

                extra_title = extra.get("ad_title") or ""
                extra_body = extra.get("ad_body") or ""
                if destination_url:
                    try:
                        if not set_ad_destination_website(page, destination_url):
                            _shot(f"fail_{tag}_destination")
                            ad_level_fails.append(f"{tag}_destination")
                        else:
                            _shot(f"after_{tag}_destination")
                    except Exception as exc:
                        _shot(f"fail_{tag}_destination")
                        ad_level_fails.append(f"{tag}_destination:{str(exc)[:60]}")

                if (extra_title or extra_body) and no_creative:
                    # ACL-1313 (SA Mix, da86e97cbf8b): без медиа Meta не рисует
                    # поля текста вовсе — шаг текста лишь жёг минуты на поиск
                    # полей и прятал настоящую причину. Называем её сразу.
                    ad_level_fails.append(f"{tag}_copy:{extra_name or tag} has no text (no_creative)")
                elif extra_title or extra_body:
                    try:
                        copy_report = {}
                        ok_copy = write_ad_copy(
                            page, extra_title, extra_body, ad_name=extra_name,
                            destination_url=destination_url, report=copy_report)
                        if not ok_copy:
                            _shot(f"fail_{tag}_copy", copy_report)
                            ad_level_fails.append(
                                f"{tag}_copy:{extra_name or tag} has no text"
                                + (f" ({copy_report['why']})" if copy_report.get("why") else ""))
                        else:
                            _shot(f"after_{tag}_copy")
                    except Exception as exc:
                        _shot(f"fail_{tag}_copy")
                        ad_level_fails.append(f"{tag}_copy:{str(exc)[:60]}")

        # Final destination sweep over every ad of this campaign.
        # Confirmed live 2026-07-29 (Test143): an ad's Browser add-ons
        # radio can revert to "Instant form" AFTER its own in-flow check
        # already passed — the last ad of campaign B read back clean at
        # fill time, yet the post-run live verification found the
        # lead_gen_form_id error banner on it. Re-visit each ad once the
        # whole campaign is filled and re-fix anything that reverted.
        if reached_ad_level:
            # ACL-1233: обход должен пройти и после зависшей страницы.
            recover_stuck_page(page, "destination_sweep", editor=True)
        # 20.09, решение пользователя: обход открывает каждое объявление
        # заново и стоит около минуты на кампанию. Если назначение каждого
        # объявления подтвердилось на своём шаге, обход пропускаем. Вернуть
        # его целиком: FB_RPA_ALWAYS_SWEEP=1.
        skip_sweep = (
            reached_ad_level and destination_url
            and not any("destination" in f for f in ad_level_fails)
            and os.environ.get("FB_RPA_ALWAYS_SWEEP", "").strip() not in ("1", "true", "yes")
            and not _review_errors_visible(page)
            # 20.09 21:52: «Instant form» возвращался в Browser add-ons уже
            # после успешной проверки шага — именно это обход и ловил.
            and _browser_addon_none_checked(page) is not False
        )
        if skip_sweep:
            _telemetry.record("destination_sweep_skipped", None,
                              note="all_destinations_confirmed_in_flow")
        if reached_ad_level and destination_url and not skip_sweep:
            sweep_names = [ad_name or f"{campaign_name} Ad"]
            for i, extra in enumerate(additional_ads or [], start=2):
                sweep_names.append(extra.get("ad_name") or f"{campaign_name} Ad {i}")
            try:
                sweep_failed = verify_and_fix_ad_destinations(page, sweep_names, destination_url)
                for bad in sweep_failed:
                    ad_level_fails.append(f"destination_sweep:{bad}")
                # Проход открыл каждое объявление и подтвердил (или починил)
                # назначение. Ранний отказ того же объявления после этого —
                # устаревшая запись: 17.09 06:13 (S15 Day) ad2_destination упал в
                # узком окне, проход в конце его поправил, а сводка всё равно
                # пометила кампанию непубликуемой.
                unverified = {b.split(":", 1)[0] for b in sweep_failed}
                confirmed = {
                    ("ad" if i == 1 else f"ad{i}"): n
                    for i, n in enumerate(sweep_names, start=1)
                    if n not in unverified
                }
                cleared = [
                    f for f in ad_level_fails
                    if f.split(":", 1)[0].endswith("_destination")
                    and f.split(":", 1)[0][: -len("_destination")] in confirmed
                ]
                if cleared:
                    ad_level_fails[:] = [f for f in ad_level_fails if f not in cleared]
                    _telemetry.record("destination_sweep_cleared", None, diag={"cleared": cleared})
                _shot("after_destination_sweep")
            except Exception as exc:
                ad_level_fails.append(f"destination_sweep:{str(exc)[:60]}")

    # Was written to debug_ad_level_soft_fails.txt, which each run
    # OVERWROTE — so the previous run's evidence was gone before anyone
    # could compare. Append-only telemetry keeps the history.
    _telemetry.record(
        "ad_level_summary",
        not ad_level_fails,
        run_id=run_id,
        campaign=campaign_name,
        diag={"soft_fails": ad_level_fails} if ad_level_fails else None,
    )
    # Hand the failures back to the caller as well. Telemetry alone is not a
    # safety control: nobody reads rpa_telemetry.jsonl in the seconds between a run
    # finishing and the operator clicking Publish, so an ad that ended up with
    # no creative was indistinguishable from a clean run.
    if soft_fails_out is not None:
        # Adset-level failures travel back too. They used to go only to
        # output/debug_audience_soft_fails.txt, which means a run that failed to
        # set the target country was indistinguishable from a clean one — the
        # operator saw success and published a campaign aimed at the wrong
        # market. Severity is decided in one place, failure_policy. Every
        # currently emitted field failure blocks publication; execution only
        # continues so the operator receives a useful draft and full defect list.
        soft_fails_out.extend(soft_fails)
        soft_fails_out.extend(ad_level_fails)

    return ""


PLACEMENTS_ROW_JS = """
  const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
  const rowsFor = (root, name) => {
    const out = [];
    const leaves = [...root.querySelectorAll('*')].filter(e =>
      e.offsetParent !== null && e.children.length === 0
      && norm(e.textContent) === name);
    for (const leaf of leaves) {
      let node = leaf.parentElement;
      for (let i = 0; i < 8 && node; i++) {
        const cb = node.querySelector('input[type=checkbox]');
        if (cb) { if (!out.some(h => h.cb === cb)) out.push({ row: node, cb: cb }); break; }
        if (node.tagName === 'LI') break;
        node = node.parentElement;
      }
    }
    return out;
  };
  // Одно имя может встретиться дважды. «Search results» на DQ501 нашлась и
  // как категория, и как чужой контрол внутри `<label>` — недоступный, из-за
  // чего шаг кликал в него и отчитывался, что категория осталась включённой.
  // Берём доступную галочку; если доступных нет — первую, чтобы отказ
  // назвал настоящую причину, а не «строка не найдена».
  const rowFor = (root, name) => {
    const all = rowsFor(root, name);
    return all.find(h => !h.cb.disabled) || all[0] || null;
  };
"""
"""Поиск строки раздела площадок по её тексту — в обеих разметках Meta.

14.09, круг 11: на DQ500/DQ501 у всех пяти категорий есть настоящий
`input[type=checkbox]`, и все пять включены («Included: 21»). Нет только
обёртки `<label>`: строка — это `li`, текст и галочка в ней лежат порознь.
На DQ204/UL921 обёртка есть, и код искал галочку внутри неё; восемь кругов
ушло на «видно 0 категорий из 5» при всех пяти на экране.

Отсюда один поиск на оба случая: от листа с нужным текстом вверх, до первого
узла, в котором есть галочка. Через `<label>` подъём проходит насквозь, так
что прежняя разметка обслуживается тем же кодом. Упирается он в `li` —
дальше начинается чужая строка.

Вынесено на уровень модуля, чтобы это можно было выполнить в тесте против
сохранённой разметки: проверки стенда дёргают страницу локаторами Playwright
и наш поиск не вызывают, так что их зелёный цвет о нём ничего не говорит.
"""

PLACEMENTS_EXPAND_JS = (
    "([name]) => {"
    + PLACEMENTS_ROW_JS
    + """
      const root = document.querySelector('[data-rpa-placements-marker]') || document;
      const hit = rowFor(root, name);
      if (!hit) return { found: false };
      let node = hit.row;
      for (let i = 0; i < 12 && node; i++) {
        if (node.getAttribute('role') === 'list') break;
        if (node.getAttribute('role') === 'button' && node.hasAttribute('aria-expanded')) {
          const was = node.getAttribute('aria-expanded');
          if (was === 'false') node.click();
          return { found: true, was: was, depth: i, where: 'над строкой' };
        }
        node = node.parentElement;
      }
      // Над строкой кнопки нет — на DQ500/DQ501 все три категории дали
      // `was: null`, и подпункты не появились ни у одной. Значит
      // раскрыватель внутри самой строки: ищем его там, а не достраиваем
      // предположение о ещё одном уровне вверх. Свой же переключатель
      // категории не трогаем.
      const inside = [...hit.row.querySelectorAll('[aria-expanded]')]
        .filter(e => e.offsetParent !== null && !e.contains(hit.cb));
      if (inside.length) {
        const btn = inside[0];
        const was = btn.getAttribute('aria-expanded');
        if (was === 'false') btn.click();
        return { found: true, was: was, where: 'внутри строки', count: inside.length };
      }
      // Третий способ — кнопка с подписью для чталки, без `aria-expanded`
      // вовсе. Круг 14: в `li` каждой категории лежит
      // `button[aria-label="Expand"]`, а состояние читается по самой
      // подписи: раскрытая называется «Collapse», и жать её нельзя — это
      // свернёт категорию обратно. Ею пользовался код до сентябрьской
      // переписки; тогда её убрали, потому что в снимке 9 сентября таких
      // кнопок нет ни одной.
      const li = hit.row.closest('li') || hit.row;
      const named = [...li.querySelectorAll('button[aria-label]')]
        .filter(e => e.offsetParent !== null);
      const open = named.find(e => /collapse/i.test(e.getAttribute('aria-label')));
      if (open) return { found: true, was: 'true', where: 'уже раскрыта' };
      const btn = named.find(e => /expand/i.test(e.getAttribute('aria-label')));
      if (btn) {
        btn.click();
        return { found: true, was: 'false', where: 'кнопка Expand' };
      }
      return { found: true, was: null, where: 'не найден' };
    }
    """
)
"""Три способа раскрыть категорию — по порядку, от прежней разметки к новой.

Вынесено рядом с `PLACEMENTS_ROW_JS` и по той же причине: иначе это нельзя
выполнить в тесте, а осмотренный, но ни разу не выполненный скрипт уже стоил
нам дня.
"""

def _label_on_page(label: str, seen: list[str]) -> bool:
    """Есть ли такой подпункт среди увиденных — не глядя на регистр букв.

    15.09 живой прогон на кувейтском кабинете отчитался:

        subitems_missing: ["Instagram feed", "Facebook Notifications"]
        subitems_seen:    [… "Instagram Feed" … "Facebook notifications" …]

    Обе строки приехали в одном тикете Sentry, рядом: «что искали» и «что
    видели». Разница только в заглавной букве — и подпункт объявлялся
    отсутствующим, а плейсменты отказывали.

    Сводить регистр безопасно: названия плейсментов различаются словами, а не
    регистром. В том же списке рядом живут «Facebook Feed» и «Facebook profile
    feed» — их не спутать.
    """
    want = (label or "").strip().casefold()
    return any((x or "").strip().casefold() == want for x in seen)


PLACEMENTS_SUBITEM_READ_JS = r"""
  (label) => {
    const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
    const root = document.querySelector('[data-rpa-placements-marker]');
    if (!root) return { found: false, why: 'нет метки блока' };
    const rows = [...root.querySelectorAll('*')].filter(e =>
      e.children.length === 0 && norm(e.textContent).toLowerCase() === (label || '').toLowerCase() && e.offsetParent !== null);
    let frozen = null;
    for (const row of rows) {
      let container = row.parentElement;
      for (let i = 0; i < 4 && container; i++) {
        const cb = container.querySelector('input[type=checkbox]');
        if (cb) {
          const aria = cb.getAttribute('aria-checked');
          const checked = aria === 'true' ? true : aria === 'false' ? false : cb.checked;
          if (!cb.disabled) return { found: true, checked };
          if (frozen === null) frozen = checked;
          break;
        }
        container = container.parentElement;
      }
    }
    return frozen === null ? { found: false, why: 'строка не найдена' }
                           : { found: true, checked: frozen };
  }
"""
"""Состояние подпункта без клика — для перепроверки после паузы."""


PLACEMENTS_SUBITEM_JS = """
  ([label, wantChecked]) => {
    const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
    const root = document.querySelector('[data-rpa-placements-marker]');
    if (!root) return { ok: false, why: 'нет метки блока' };
    const rows = [...root.querySelectorAll('*')].filter(e =>
      e.children.length === 0 &&
      // Регистр не сверяем: страница пишет «Instagram Feed», рецепт —
      // «Instagram feed», и подпункт объявлялся отсутствующим (15.09).
      norm(e.textContent).toLowerCase() === (label || '').toLowerCase() &&
      e.offsetParent !== null);
    let frozen = null;
    for (const row of rows) {
      let container = row.parentElement;
      for (let i = 0; i < 4 && container; i++) {
        const cb = container.querySelector('input[type=checkbox]');
        if (cb) {
          // Состояние — по aria-checked, свойство checked у Meta отстаёт.
          // 17.09 20:00 (CR_ait FX_Y): отказ «checked: true» у пяти подпунктов,
          // а на дампе после отказа у всех aria-checked="false" — галочки
          // были сняты. Второй заход по устаревшему checked кликнул бы снова
          // и поставил их обратно.
          const state = () => {
            const aria = cb.getAttribute('aria-checked');
            return aria === 'true' ? true : aria === 'false' ? false : cb.checked;
          };
          if (!cb.disabled) {
            if (state() !== wantChecked) cb.click();
            return { ok: state() === wantChecked, checked: state() };
          }
          // У недоступной галочки правду говорит aria-checked: 17.09 (Doc20)
          // «Ads on Facebook Reels» — aria-checked="false", disabled, а
          // свойство checked осталось true от разметки.
          if (frozen === null) {
            const aria = cb.getAttribute('aria-checked');
            frozen = aria === 'true' ? true : aria === 'false' ? false : cb.checked;
          }
        }
        container = container.parentElement;
      }
    }
    if (frozen !== null) {
      return frozen === wantChecked
        ? { ok: true, why: 'недоступна, но уже как надо', checked: frozen }
        : { ok: false, why: 'недоступна и не та', checked: frozen };
    }
    return { ok: false, why: 'строка не найдена' };
  }
"""
"""Подпункт категории: найти по словам и переключить.

Подъём на четыре уровня, а не общий `rowFor`, и это не небрежность. 14.09 я
свёл подпункты на `rowFor` «для единообразия» — и вместо семнадцати из
восемнадцати круг 16 дал десять: подпункты лежат во вложенных `li` внутри
категории, и граница по `li`, которая спасает категории от чужой галочки,
здесь отрезает свою же. Работающий путь без причины не меняют.

Недоступная галочка — не всегда беда: «Facebook Business Explore» Meta
держит выключенной и трогать не даёт. Если она уже в нужном состоянии,
требовать от неё клика бессмысленно; если нет — это честный отказ.
"""

#: Пауза между подпунктами внутри одного вызова страницы (мс). Отдаёт
#: управление React между кликами — как прежде это делал обратный путь
#: Python↔браузер между отдельными `evaluate`, — и каждый следующий подпункт
#: ищется заново уже по перерисованной разметке.
PLACEMENTS_SUBITEM_PAUSE_MS = 120

PLACEMENTS_SUBITEMS_BATCH_JS = (
    "async ([items, pauseMs]) => {"
    "  const one = " + PLACEMENTS_SUBITEM_JS.strip() + ";"
    "  const out = [];"
    "  for (const [label, want] of items) {"
    "    let res;"
    "    try { res = one([label, want]); }"
    "    catch (e) { res = { ok: false, why: String(e).slice(0, 80) }; }"
    "    out.push(res);"
    "    await new Promise((r) => setTimeout(r, pauseMs));"
    "  }"
    "  return out;"
    "}"
)


def _set_placement_subitems(page: Page, items: list[tuple[str, bool]]) -> list[dict]:
    """Проставить подпункты одним вызовом страницы (ACL-1313).

    Каждый подпункт — ровно `PLACEMENTS_SUBITEM_JS`, как прежде: найти по
    словам и кликнуть в одном и том же JS, прочитать состояние. Прежде это
    был отдельный `page.evaluate` на подпункт — до 18 обратных путей
    Python↔браузер за заход, по 0,1–0,5 с каждый при трёх профилях. Ответ —
    по одному словарю на пункт, в том же порядке.
    """
    if not items:
        return []
    try:
        res = page.evaluate(
            PLACEMENTS_SUBITEMS_BATCH_JS,
            [[[label, bool(want)] for label, want in items], PLACEMENTS_SUBITEM_PAUSE_MS],
        )
    except Exception as exc:
        return [{"ok": False, "why": str(exc)[:80]} for _ in items]
    res = list(res or [])
    res += [None] * (len(items) - len(res))
    return [r or {"ok": False, "why": "пустой ответ"} for r in res[: len(items)]]

_PLACEMENT_CATEGORIES = {
    # Пять категорий раздела Placement controls и то, чем они должны стать.
    # Порядок важен только для отчёта: снимаем и ставим по одной.
    "Feeds": True,
    "Stories, Status, Reels": True,
    "In-stream ads for reels": True,
    "Search results": False,
    "Apps and sites": False,
}

_SUBITEMS_ON = (
    "Facebook Feed",
    "Instagram feed",
    "Instagram Stories",
    "Facebook Stories",
    "Instagram Reels",
    "Facebook Reels",
    "Facebook in-stream reels",
)

_SUBITEMS_OFF = (
    "Facebook profile feed",
    "Instagram profile feed",
    "Facebook Marketplace",
    "Facebook right column",
    "Instagram Explore home",
    "Facebook Business Explore",
    "Threads feed",
    "Facebook Notifications",
    "Messenger Stories",
    "WhatsApp Status",
    "Ads on Facebook Reels",
)

#: Сколько раз за категорию её можно раскрыть заново, если Meta свернула её
#: посреди простановки подпунктов (перерисовка после сохранения правки,
#: ACL-1313 25.09). Дважды: одна перерисовка на заход — то, что видно в
#: отказах; бесконечный цикл против страницы, которая сворачивает на каждый
#: клик, — хуже честного отказа.
_PLACEMENT_REOPENS = 2


def _wheel_to_placements_card(page: Page, max_ticks: int = 40) -> int:
    """Прокрутить колонку редактора колесом до карточки Placements.

    Возвращает число сделанных прокруток или -1, если карточка так и не
    показалась.
    """
    probe = r"""() => {
      const h = [...document.querySelectorAll('[role=heading]')].find(e =>
        (e.textContent || '').replace(/\s+/g, ' ').trim().startsWith('Placements'));
      if (!h) return 'absent';
      const r = h.getBoundingClientRect();
      if (r.height > 0 && r.y > 60 && r.y < window.innerHeight * 0.6) return 'on_screen';
      return r.height > 0 && r.y >= window.innerHeight * 0.6 ? 'below' : (r.height > 0 ? 'above' : 'unlaid');
    }"""
    try:
        vw = page.evaluate("() => [window.innerWidth, window.innerHeight]")
        x, y = int(vw[0] * 0.45), int(vw[1] * 0.5)
    except Exception:
        x, y = 700, 450
    for i in range(max_ticks):
        try:
            state = page.evaluate(probe)
        except Exception:
            return -1
        if state == "on_screen":
            return i
        try:
            page.mouse.move(x, y)
            page.mouse.wheel(0, -350 if state == "above" else 450)
        except Exception:
            return -1
        _sleep_h(0.4)
    return -1


def _placements_exclusion_removed(page: Page) -> bool:
    """Карточка Placements, на которой Meta убрала ручной выбор площадок.

    Два признака, любого достаточно. Объявление «Excluding placements or
    platforms will no longer be available» было в дампе Doc20 (18:51), но в
    живом Doc21 (22:35) его уже нет — там только строка «Account controls ·
    Excluded placements: …». Зовётся лишь тогда, когда категорий не видно.
    """
    try:
        return bool(page.evaluate(
            """() => {
              const t = (document.body.textContent || '').replace(/\\s+/g, ' ');
              return /Excluding placements or platforms will no longer be available/i.test(t)
                  || /Excluded placements:/i.test(t);
            }"""
        ))
    except Exception:
        return False


#: Что исключается на уровне рекламного аккаунта. Это ровно те площадки из
#: `_SUBITEMS_OFF`/«Apps and sites», которые Meta даёт исключить в окне
#: «Placement controls for this ad account» (живое чтение Doc21, 16.09 22:35:
#: Apps and sites → Audience Network native, banner and interstitial;
#: Audience Network rewarded videos; Feeds → Facebook Marketplace; Facebook
#: right column). Остальное из прежнего списка там не предлагается.
_ACCOUNT_EXCLUDED_PLACEMENTS = (
    "Audience Network native, banner and interstitial",
    "Audience Network rewarded videos",
    "Facebook Marketplace",
    "Facebook right column",
)

_ACCOUNT_CONTROLS_DIALOG = "Placement controls for this ad account"
_ACCOUNT_CONTROLS_SWITCH = "My business can only advertise on specific placements"


def _account_controls_dialog(page: Page):
    return page.locator("[role=dialog]").filter(has_text=_ACCOUNT_CONTROLS_DIALOG).last


def _account_exclusion_boxes(page: Page) -> dict:
    """Галочки окна Account controls по имени площадки: {имя: checked|None}.

    Галочка берётся в ближайшем предке подписи, где она одна: у группы
    («Apps and sites») их несколько, у строки площадки — одна.
    """
    js = r"""
    ([dialogText, names]) => {
      const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
      const dlg = [...document.querySelectorAll('[role=dialog]')]
        .filter(d => d.getBoundingClientRect().width > 0 && norm(d.textContent).includes(dialogText)).pop();
      if (!dlg) return null;
      const out = {};
      for (const name of names) {
        const leaf = [...dlg.querySelectorAll('*')].find(e => e.children.length === 0 && norm(e.textContent) === name);
        if (!leaf) { out[name] = null; continue; }
        let n = leaf, box = null;
        for (let k = 0; k < 8 && n; k++) {
          n = n.parentElement;
          const boxes = n ? n.querySelectorAll('input[type=checkbox]:not([role=switch])') : [];
          if (boxes.length === 1) { box = boxes[0]; break; }
          if (boxes.length > 1) break;
        }
        if (box) { box.setAttribute('data-rpa-exclude', name); out[name] = box.checked; }
        else out[name] = null;
      }
      return out;
    }
    """
    try:
        return page.evaluate(js, [_ACCOUNT_CONTROLS_DIALOG, list(_ACCOUNT_EXCLUDED_PLACEMENTS)]) or {}
    except Exception:
        return {}


def _excluded_placements_summary(page: Page) -> str | None:
    """Текст «Excluded placements: …» на карточке, без префикса."""
    try:
        return page.evaluate(
            r"""() => {
              const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
              // «Excluded placements:» — отдельный жирный узел, список идёт
              // соседним текстом (скриншот S15, 23:27). Берём самый короткий
              // элемент, чей текст начинается с метки: это абзац целиком.
              const els = [...document.querySelectorAll('*')].filter(e =>
                /^Excluded placements:/i.test(norm(e.textContent)));
              if (!els.length) return null;
              els.sort((a, b) => norm(a.textContent).length - norm(b.textContent).length);
              const own = els.find(e => norm(e.textContent).replace(/^Excluded placements:\s*/i, '').length) || els[0];
              let t = norm(own.textContent).replace(/^Excluded placements:\s*/i, '');
              return t.replace(/\s*Account controls let you.*$/i, '');
            }"""
        )
    except Exception:
        return None


def ensure_account_placement_exclusions(page: Page, report: dict | None = None) -> bool:
    """Исключить площадки на уровне рекламного аккаунта (Account controls).

    Нужен там, где Meta убрала ручной выбор площадок у ad set (16.09): единственное
    место, где их ещё можно исключить, — окно «Placement controls for this ad
    account». Настройка общая для всех кампаний аккаунта; решение владельца —
    ставить её автоматически (16.09, «давай автоматом, делай»).

    Идемпотентен: если переключатель включён и все четыре площадки уже сняты,
    окно закрывается через Cancel, ничего не меняя.
    """
    report = report if report is not None else {}

    def _record(ok, note, **diag):
        report.update({"account_exclusions": note, **diag})
        _telemetry.record("account_placement_exclusions", ok, note=note, diag=diag or None)
        return bool(ok)

    report["summary_before"] = _excluded_placements_summary(page)

    # 1. Строка «Account controls» — раскрывашка; внутри кнопка «Edit placement controls».
    #
    # Живой прогон 0054550ac608 (22:49): `row.click` — TimeoutError на обоих
    # аккаунтах, хотя после прогона тот же клик в том же окне проходил. Во
    # время шага карточка ещё не прокручена в видимую часть редактора, и
    # Playwright ждёт «видимости», которой без прокрутки нет. Поэтому: сперва
    # прокрутить сам узел, затем честный клик, а если он всё равно отказал —
    # клик DOM по тому же узлу.
    row_state = _scroll_and_mark(
        page,
        r"[...document.querySelectorAll('[role=button][aria-expanded]')]"
        r".find(e => /^Account controls/i.test(norm(e.textContent)))",
        "account-controls-row",
    )
    if row_state is None:
        return _record(False, "account_controls_row_not_found")
    if row_state.get("expanded") != "true":
        how = _click_marked(page, "account-controls-row")
        if not how:
            return _record(False, "account_controls_row_click_failed", row=row_state)
        _sleep_h(1.2)
    edit_state = _scroll_and_mark(
        page,
        r"[...document.querySelectorAll('[role=button]')]"
        r".find(e => norm(e.textContent) === 'Edit placement controls')",
        "edit-placement-controls",
    )
    if edit_state is None:
        return _record(False, "edit_placement_controls_not_found", row=row_state)
    if not _click_marked(page, "edit-placement-controls"):
        return _record(False, "edit_placement_controls_not_clickable", row=row_state)

    dialog = _account_controls_dialog(page)
    try:
        dialog.wait_for(state="visible", timeout=8000)
    except Exception:
        return _record(False, "dialog_did_not_open")

    # 2. Переключатель «My business can only advertise on specific placements».
    switch = dialog.get_by_label(_ACCOUNT_CONTROLS_SWITCH)
    try:
        if not switch.first.is_checked():
            switch.first.click(force=True, timeout=3000)
            _sleep_h(1.5)
    except Exception as exc:
        return _record(False, "switch_not_clickable", error=type(exc).__name__)

    # 3. Снять четыре площадки.
    before = _account_exclusion_boxes(page)
    missing = [n for n, v in before.items() if v is None]
    changed = []
    for name, checked in before.items():
        if checked:
            try:
                page.locator(f'[data-rpa-exclude="{name}"]').first.click(force=True, timeout=3000)
                changed.append(name)
                _sleep_h(0.5)
            except Exception:
                pass
    after = _account_exclusion_boxes(page)
    still_on = [n for n, v in after.items() if v]
    if still_on or missing:
        _cancel_account_controls(page)
        return _record(False, "placements_not_unchecked", before=before, after=after, missing=missing)

    if not changed:
        _cancel_account_controls(page)
        return _record(True, "already_excluded", after=after)

    # 4. Review changes → подтверждение.
    try:
        dialog.get_by_role("button", name=re.compile(r"^Review changes$", re.I)).first.click(timeout=5000)
    except Exception as exc:
        _cancel_account_controls(page)
        return _record(False, "review_not_clickable", error=type(exc).__name__, changed=changed)
    _sleep_h(2.0)
    try:
        from src.fb_ads_rpa import page_capture as _pages

        _pages.capture_on_success(page, step="account_controls_review", diag={"changed": changed})
    except Exception:
        pass
    confirmed = None
    for label in ("Confirm", "Save changes", "Save", "Apply", "Publish", "Update", "Done"):
        btn = page.locator("[role=dialog]").get_by_role("button", name=re.compile(rf"^{label}$", re.I))
        try:
            if btn.count() and btn.last.is_visible():
                btn.last.click(timeout=5000)
                confirmed = label
                break
        except Exception:
            continue
    if not confirmed:
        texts = _visible_dialog_texts(page)
        return _record(False, "confirm_button_not_found", changed=changed, dialogs=texts)
    _sleep_h(3.0)

    # Уведомление «Account controls updating» ложится в правый нижний угол —
    # ровно на кнопку Next. Doc20, 23:27: «next_to_ad ok» кликнул в него,
    # редактор остался на ad set, и `ad_name` упал. Закрываем крестиком.
    _close_account_controls_toast(page)

    # 5. Проверка: карточка больше не говорит «None».
    summary = _excluded_placements_summary(page)
    if summary is not None and summary.strip().lower() in ("", "none"):
        return _record(False, "summary_still_none", changed=changed, confirmed=confirmed, summary=summary)
    if summary:
        absent = [n for n in _ACCOUNT_EXCLUDED_PLACEMENTS if n.lower() not in summary.lower()]
        if absent:
            return _record(False, "summary_missing_placements", changed=changed,
                           confirmed=confirmed, summary=summary, absent=absent)
    return _record(True, "excluded", changed=changed, confirmed=confirmed, summary=summary)


def _scroll_and_mark(page: Page, finder_js: str, mark: str) -> dict | None:
    """Найти узел выражением `finder_js`, прокрутить к нему и пометить.

    Возвращает {expanded, w, h} после прокрутки или None, если узла нет.
    """
    js = (
        "(mark) => { const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();"
        f" const el = {finder_js};"
        " if (!el) return null;"
        " document.querySelectorAll(`[data-rpa-mark=\"${mark}\"]`).forEach(e => e.removeAttribute('data-rpa-mark'));"
        " el.setAttribute('data-rpa-mark', mark);"
        " el.scrollIntoView({block: 'center', behavior: 'instant'});"
        " const r = el.getBoundingClientRect();"
        " return {expanded: el.getAttribute('aria-expanded'), w: Math.round(r.width), h: Math.round(r.height)}; }"
    )
    try:
        state = page.evaluate(js, mark)
    except Exception:
        return None
    _sleep_h(0.6)
    return state


def _click_marked(page: Page, mark: str) -> str:
    """Клик по помеченному узлу: честный, иначе DOM. Возвращает способ или ""."""
    loc = page.locator(f'[data-rpa-mark="{mark}"]').first
    try:
        loc.click(timeout=4000)
        return "trusted"
    except Exception:
        pass
    try:
        if page.evaluate(
            "(mark) => { const el = document.querySelector(`[data-rpa-mark=\"${mark}\"]`);"
            " if (!el) return false; el.click(); return true; }",
            mark,
        ):
            return "dom"
    except Exception:
        pass
    return ""


def _close_account_controls_toast(page: Page) -> bool:
    """Закрыть «Account controls updating» (✕ в самом уведомлении)."""
    js = r"""
    () => {
      const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
      const title = [...document.querySelectorAll('*')].find(e =>
        e.children.length === 0 && norm(e.textContent) === 'Account controls updating'
        && e.getBoundingClientRect().width > 0);
      if (!title) return 'absent';
      let box = title;
      for (let i = 0; i < 8 && box.parentElement; i++) {
        box = box.parentElement;
        const btn = [...box.querySelectorAll('[role=button], button, [aria-label]')].find(b =>
          /^(close|dismiss)$/i.test(norm(b.getAttribute('aria-label') || b.textContent)));
        if (btn) { btn.click(); return 'closed'; }
      }
      return 'no_button';
    }
    """
    for _ in range(3):
        try:
            state = page.evaluate(js)
        except Exception:
            return False
        if state == "absent":
            return True
        _sleep_h(0.8)
    _telemetry.record("account_controls_toast", None, note="still_open")
    return False


def _cancel_account_controls(page: Page) -> None:
    try:
        _account_controls_dialog(page).get_by_role("button", name=re.compile(r"^Cancel$", re.I)).first.click(timeout=3000)
        _sleep_h(0.8)
    except Exception:
        pass


def _visible_dialog_texts(page: Page) -> list[str]:
    try:
        return page.evaluate(
            r"""() => [...document.querySelectorAll('[role=dialog]')]
                .filter(d => d.getBoundingClientRect().width > 0)
                .map(d => (d.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 400))"""
        )
    except Exception:
        return []


def fill_placements(page: Page, report: dict | None = None) -> bool:
    """Выставить ручные площадки вместо автоматических Advantage+.

    Переписано 2026-09-09 по настоящей разметке — снимку прогона, в котором
    этот шаг упал пятый раз подряд. Разметка сохранена стендом:
    `tests/fixtures/ads_manager/placements_2026_09_09.html`.

    **Чего на странице больше нет** (проверено по полному снимку в 4 МБ, не
    по догадке): формулировок «Advantage+ placements» и «Manual placements»
    — ноль совпадений; идентификаторов
    `ads_placement_expandable_list_item_{feed,story,stream}` — их Meta
    заменила на порождаемые (`js_14p`, `_r_d2_`), меняющиеся от отрисовки к
    отрисовке, так что цепляться за них нельзя больше никогда; кнопок с
    подписью «Expand» — ни одной; галочки «Allow limited spending» — ни
    одной. Наш прежний код искал все четыре и падал на первом же.

    **Что на странице есть.** Раздел приходит уже раскрытым («Hide
    settings»), внутри — пять категорий, каждая строкой `role="button"` с
    `aria-expanded`, и в каждой строке `<label>` со своей галочкой. Опора
    осталась одна — слова рядом с контролом, то есть то же, по чему ищет
    человек.

    Порядок: убедиться, что пять категорий видны (а не жать «показать» на
    уже открытом — этот клик его закрывал); выставить пять категорийных
    галочек; раскрыть три оставленные категории и проставить в них
    подпункты.

    Имена подпунктов взяты из прогона в июле, и по свёрнутой разметке их не
    проверить. Поэтому код переписывает в `report` то, что увидел на месте:
    одного упавшего прогона хватит, чтобы узнать настоящий список вместо
    гадания. Пустое `False` в телеметрии стоило пяти прогонов вслепую.

    **Разметок две, и зависят они от аккаунта.** 14.09 те же шаги на новых
    DQ500/DQ501 дали ноль категорий и ноль `Placement controls` — при том что
    фикстура 9 сентября (DQ204/UL921) содержит и то, и другое, и стенд на ней
    зелёный. Значит код не «сломался»: Meta отдаёт новым аккаунтам другой
    вариант раздела, где пять категорий спрятаны за свёрнутой створкой
    «Placements» со сводкой в подписи («Included: 21»). Поэтому ниже сначала
    пробуется прежний путь, а створка раскрывается только если категорий не
    видно. Вывод «Meta переделала раздел для всех» из одного аккаунта делать
    нельзя — оба варианта живы одновременно.

    Найти и кликнуть — в одном вызове `page.evaluate`. Правая колонка
    («Audience definition», «Campaign score») пересчитывается сама и двигает
    строки, поэтому координата, снятая даже на доли секунды раньше, к
    моменту клика указывает на соседнюю строку. Ссылка на узел этим не
    страдает.
    """
    report = report if report is not None else {}
    _dbg: list[str] = []

    def _dump_dbg() -> None:
        try:
            Path("output/debug_placements_fill.txt").write_text(
                "\n".join(_dbg), encoding="utf-8"
            )
        except Exception:
            pass

    def _text_y(text: str, *, exact: bool = True, max_children: int = 0) -> float | None:
        try:
            return page.evaluate(
                """
                ([text, exact, maxChildren]) => {
                  const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                  const el = [...document.querySelectorAll('*')].find(e => {
                    if (e.offsetParent === null || e.children.length > maxChildren) return false;
                    const t = norm(e.textContent);
                    return exact ? t === text : t.toLowerCase().includes(text.toLowerCase());
                  });
                  return el ? el.getBoundingClientRect().y : null;
                }
                """,
                [text, exact, max_children],
            )
        except Exception:
            return None

    # Confirmed live 2026-07-27: the "Placements" heading isn't reliably in
    # the DOM at all right after the previous step (Detailed targeting) —
    # this page mounts sections progressively as the main panel scrolls
    # (same pattern already seen for the Ad-level creative fields). Force-
    # scroll toward increasing fractions of the container's scrollHeight
    # and retry instead of searching the DOM as-is.
    # 17.09 00:40, живьём на Doc20: редактор не раскладывает карточку
    # Placements, пока она за экраном — у неё и у «Show more settings» размер
    # 0×0, и весь путь ниже промахивался. `_text_y` при этом находил строку
    # «Placements» в правой панели Campaign score и прокрутку пропускал.
    # Колесо над колонкой редактора, пока заголовок карточки
    # (`role=heading`) не окажется на экране с настоящим размером. Под «Show
    # more settings» ручной выбор цел: «Placements · Included: 17», пять
    # категорий с живыми галочками.
    _dbg.append(f"wheel_to_card={_wheel_to_placements_card(page)}")
    placements_y = _text_y("Placements")
    for frac in (0.5, 0.6, 0.7, 0.8, 0.9, 1.0):
        if placements_y is not None:
            break
        try:
            page.evaluate(
                """
                (f) => {
                  const all = [...document.querySelectorAll('*')];
                  const container = all.find(e => {
                    const style = getComputedStyle(e);
                    return (style.overflowY === 'auto' || style.overflowY === 'scroll')
                      && e.scrollHeight > e.clientHeight + 50 && e.scrollHeight > 1500;
                  });
                  if (container) container.scrollTop = container.scrollHeight * f;
                }
                """,
                frac,
            )
        except Exception:
            pass
        _sleep_h(0.5)
        placements_y = _text_y("Placements")
    _dbg.append(f"placements_y={placements_y}")
    if placements_y is None:
        _dump_dbg()
        return False

    # Confirmed live 2026-07-27: Y-coordinate disambiguation between
    # Budget & schedule's "Show more settings" and Placements' own kept
    # breaking — el.scrollIntoView() on click shifts the whole viewport,
    # which moves the "Placements" heading's own y between ticks, and a
    # re-measured floor can then wrongly admit/exclude either link
    # depending on scroll drift. Tag the Placements card's own container
    # with a marker attribute once, then scope every subsequent search to
    # its subtree — DOM containment instead of screen-space Y, immune to
    # scroll/reflow entirely.
    tag_info = page.evaluate(
        """
        () => {
          const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
          // Every leaf reading "Placements", not the first visible one. The
          // right-hand "Campaign score" panel has its own "Placements" row;
          // taking the first by document order + offsetParent picked that
          // one or skipped the real card depending on which panel is
          // position:fixed on the account's layout (tag_info tagged:false on
          // three runs of 2026-09-16). The real card is the one with
          // "Account controls" among its ancestors — ask that of each.
          const headings = [...document.querySelectorAll('*')].filter(e =>
            e.children.length === 0 && norm(e.textContent) === 'Placements');
          if (!headings.length) return { tagged: false, headings: 0 };
          for (const heading of headings) {
            let node = heading;
            for (let i = 0; i < 10 && node; i++) {
              const t = norm(node.textContent);
              if (t.includes('Account controls')) {
                node.setAttribute('data-rpa-placements-marker', '1');
                const showMoreCount = [...node.querySelectorAll('*')].filter(e =>
                  norm(e.textContent).toLowerCase().includes('show more settings')
                  && e.getBoundingClientRect().width > 0).length;
                return { tagged: true, depth: i, textLen: t.length, showMoreCount,
                         headings: headings.length, rect: node.getBoundingClientRect() };
              }
              node = node.parentElement;
            }
          }
          return { tagged: false, headings: headings.length };
        }
        """
    )
    _dbg.append(f"tag_info={tag_info}")
    if not tag_info or not tag_info.get("tagged"):
        _dump_dbg()
        return False

    def _click_in_placements(text: str, *, exact: bool = True, max_children: int = 0) -> bool:
        # Confirmed live 2026-07-27: querySelectorAll('*') returns ancestors
        # BEFORE their own descendants (document order) — a nested chain of
        # wrappers all sharing the same visible text (e.g. <div><div><a>
        # text</a></div></div>) means the FIRST match is the outermost
        # wrapper, not the actual clickable <a>. Calling .click() on that
        # outer element does not invoke a React onClick handler attached
        # only to the inner <a> (native click doesn't propagate DOWN to
        # descendants) — 12 nested "show more settings" matches were found
        # this way and repeatedly "clicked" without ever expanding
        # anything. Prefer the deepest (most children-specific, i.e. an
        # actual leaf-ish <a>/<span>) match instead of the first one found.
        try:
            return bool(
                page.evaluate(
                    """
                    ([text, exact, maxChildren]) => {
                      const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                      const container = document.querySelector('[data-rpa-placements-marker]');
                      if (!container) return false;
                      const matches = [...container.querySelectorAll('*')].filter(e => {
                        if (e.offsetParent === null || e.children.length > maxChildren) return false;
                        const t = norm(e.textContent);
                        return exact ? t === text : t.toLowerCase().includes(text.toLowerCase());
                      });
                      matches.sort((a, b) => {
                        const aTag = a.tagName === 'A' ? 0 : 1;
                        const bTag = b.tagName === 'A' ? 0 : 1;
                        if (aTag !== bTag) return aTag - bTag;
                        return a.children.length - b.children.length;
                      });
                      for (const el of matches) {
                        const r = el.getBoundingClientRect();
                        if (r.width > 0 && r.height > 0) {
                          el.scrollIntoView({ block: 'center', behavior: 'instant' });
                          el.click();
                          return true;
                        }
                      }
                      return false;
                    }
                    """,
                    [text, exact, max_children],
                )
            )
        except Exception:
            return False

    def _present_in_placements(text: str, *, exact: bool = True) -> bool:
        try:
            return bool(
                page.evaluate(
                    """
                    ([text, exact]) => {
                      const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                      const container = document.querySelector('[data-rpa-placements-marker]');
                      if (!container) return false;
                      return [...container.querySelectorAll('*')].some(e => {
                        if (e.offsetParent === null) return false;
                        const t = norm(e.textContent);
                        return exact ? t === text : t.toLowerCase().includes(text.toLowerCase());
                      });
                    }
                    """,
                    [text, exact],
                )
            )
        except Exception:
            return False

    def _categories_visible() -> int:
        """Сколько из пяти категорий доступно прямо сейчас.

        Считаем не подписи, а строки с галочкой: подпись бывает и без
        `<label>`, а решение шага зависит от того, есть ли до чего дотянуться.
        Прежний счёт по `<label>` на DQ500/DQ501 давал ноль при всех пяти
        категориях на экране — восемь кругов ушло на этот ноль.
        """
        try:
            return int(
                page.evaluate(
                    "(names) => {"
                    + PLACEMENTS_ROW_JS
                    + """
                      const root = document.querySelector('[data-rpa-placements-marker]') || document;
                      return names.filter(n => rowFor(root, n)).length;
                    }
                    """,
                    list(_PLACEMENT_CATEGORIES),
                )
                or 0
            )
        except Exception:
            return 0

    def _what_is_in_the_block() -> dict:
        """Что шаг ВИДИТ в помеченном блоке — подписи, а не только их число.

        14.09: шесть прогонов подряд дали «видно 0 категорий из 5», и по этому
        числу нельзя понять главного — помечен ли вообще нужный блок. Отладка
        показывала `textLen: 759` и размер 600×501: слишком мало для раздела с
        пятью раскрывающимися категориями, слишком похоже на сводку
        («Included: 21 · Allowed with limited spend: 0 · Excluded: 0»).

        Две версии причины уже оказались неверны — про переименование категорий
        и про включённый Advantage+, — и обе строились на догадке по
        посмертному снимку. Поэтому здесь не догадка, а то, что видно шагу в
        момент решения: сколько в блоке подписей, какие они и есть ли рядом
        переключатели.
        """
        try:
            return page.evaluate(
                """
                () => {
                  const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                  const marked = document.querySelector('[data-rpa-placements-marker]');
                  const root = marked || document;
                  const vis = (e) => e.offsetParent !== null;
                  const labels = [...root.querySelectorAll('label')].filter(vis)
                    .map(l => norm(l.textContent)).filter(t => t && t.length < 60);
                  const texts = [...root.querySelectorAll('*')].filter(e =>
                      vis(e) && e.children.length === 0)
                    .map(e => norm(e.textContent))
                    .filter(t => t && t.length < 60);
                  const seen = [];
                  for (const t of texts) if (!seen.includes(t)) seen.push(t);
                  // Что здесь вообще можно нажать. Переключателя Advantage+ в
                  // блоке нет ни одного (`switches: 0`), а сводка вместо
                  // категорий показывается именно из-за него — значит режим
                  // меняется чем-то другим, и гадать чем не надо: пусть
                  // страница сама перечислит свои нажимаемые места.
                  const clickable = [...root.querySelectorAll(
                      '[role="button"],[role="link"],button,a,[aria-expanded],[tabindex="0"]')]
                    .filter(vis)
                    .map(e => ({
                      t: norm(e.textContent).slice(0, 44),
                      role: e.getAttribute('role') || e.tagName.toLowerCase(),
                      exp: e.getAttribute('aria-expanded'),
                    }))
                    .filter(x => x.t);
                  const uniq = [];
                  for (const c of clickable)
                    if (!uniq.some(u => u.t === c.t && u.role === c.role)) uniq.push(c);
                  return {
                    marked: !!marked,
                    rect: marked ? (({width, height}) => ({width, height}))(marked.getBoundingClientRect()) : null,
                    labels: labels.slice(0, 25),
                    switches: root.querySelectorAll('[role="switch"],[role="checkbox"]').length,
                    texts: seen.slice(0, 40),
                    clickable: uniq.slice(0, 30),
                  };
                }
                """
            )
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {exc}"[:120]}

    def _expand_button(text: str) -> dict:
        """Раскрыть свёрнутый узел по началу его текста.

        Жмём только при `aria-expanded="false"`: тот же клик по уже
        раскрытому закрывает его.
        """
        try:
            return page.evaluate(
                """
                ([text]) => {
                  const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                  const root = document.querySelector('[data-rpa-placements-marker]') || document;
                  const btn = [...root.querySelectorAll('[role="button"][aria-expanded]')].find(e =>
                    e.offsetParent !== null && norm(e.textContent).startsWith(text));
                  if (!btn) return { found: false };
                  const was = btn.getAttribute('aria-expanded');
                  if (was === 'false') btn.click();
                  return { found: true, was: was };
                }
                """,
                [text],
            ) or {"found": False}
        except Exception as exc:
            return {"found": False, "error": str(exc)[:80]}

    def _click_category(name: str, want_checked: bool) -> dict:
        """Один клик по галочке категории и её состояние сразу после него."""
        try:
            return page.evaluate(
                "([name, want]) => {"
                + PLACEMENTS_ROW_JS
                + """
                  const root = document.querySelector('[data-rpa-placements-marker]') || document;
                  const hit = rowFor(root, name);
                  if (!hit) return { found: false };
                  if (hit.cb.disabled) return { found: true, checkbox: false, why: 'недоступна' };
                  const state = () => {
                    const a = hit.cb.getAttribute('aria-checked');
                    return a === 'true' ? true : a === 'false' ? false : hit.cb.checked;
                  };
                  const before = state();
                  if (before !== want) hit.cb.click();
                  return { found: true, checkbox: true, before: before, checked: state() };
                }
                """,
                [name, want_checked],
            ) or {"found": False}
        except Exception as exc:
            return {"found": False, "error": str(exc)[:80]}

    def _read_category(name: str) -> dict:
        """Состояние строки категории — без единого клика.

        Отдельно от нажатия: значение, прочитанное в том же вызове, что и
        клик, говорит лишь о том, что произошло в эту миллисекунду. React
        может вернуть галочку обратно при следующей отрисовке, и именно так
        «Search results» осталась включённой, отчитавшись об успехе.
        """
        try:
            return page.evaluate(
                "(name) => {"
                + PLACEMENTS_ROW_JS
                + """
                  const norm2 = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                  const root = document.querySelector('[data-rpa-placements-marker]') || document;
                  const hit = rowFor(root, name);
                  if (!hit) return { found: false };
                  const cb = hit.cb;
                  const clickable = [...hit.row.querySelectorAll(
                      '[role="button"],[role="checkbox"],button,label,[aria-expanded]')]
                    .slice(0, 5)
                    .map(e => ({
                      t: norm2(e.textContent).slice(0, 28),
                      role: e.getAttribute('role') || e.tagName.toLowerCase(),
                      exp: e.getAttribute('aria-expanded'),
                    }));
                  const aria = cb.getAttribute('aria-checked');
                  return {
                    found: true,
                    checked: aria === 'true' ? true : aria === 'false' ? false : cb.checked,
                    cbVisible: cb.offsetParent !== null,
                    inLabel: !!cb.closest('label'),
                    row: hit.row.tagName.toLowerCase(),
                    clickable: clickable,
                  };
                }
                """,
                name,
            ) or {"found": False}
        except Exception as exc:
            return {"found": False, "error": str(exc)[:80]}

    def _set_category(name: str, want_checked: bool) -> dict:
        """Поставить галочку категории и убедиться, что она такой осталась.

        14.09, круг 12: «Apps and sites» снялась, а «Search results» тем же
        кодом — нет, и шаг отчитался об успехе, потому что читал состояние в
        том же вызове, что и кликал. Теперь после клика выжидаем отрисовку и
        спрашиваем страницу заново; если не взялось — один повтор, и в отчёт
        идёт итоговое значение, а не мгновенное.
        """
        res = _click_category(name, want_checked)
        if not res.get("checkbox"):
            return res
        _sleep_h(0.5)
        now = _read_category(name)
        res["checked"] = now.get("checked")
        if now.get("checked") is not want_checked:
            again = _click_category(name, want_checked)
            _sleep_h(0.7)
            settled = _read_category(name)
            res["retry"] = {"clicked": again.get("checkbox"), "checked": settled.get("checked")}
            res["checked"] = settled.get("checked")
            res["row"] = {k: settled.get(k) for k in ("cbVisible", "inLabel", "row", "clickable")}
        return res

    def _expand_category(name: str) -> dict:
        """Раскрыватель категории — её собственная строка, а не кнопка рядом.

        До неё от подписи семь уровней вверх, и это число менять нельзя на
        глазок: остановка идёт по `role="list"` — общему списку всех пяти
        категорий. Уйти выше значило бы схватить чужую кнопку и свернуть
        весь раздел.
        """
        try:
            return page.evaluate(PLACEMENTS_EXPAND_JS, [name]) or {"found": False}
        except Exception as exc:
            return {"found": False, "error": str(exc)[:80]}

    def _subitem_labels() -> list:
        """Какие подпункты страница показала после раскрытия категорий.

        Это ответ страницы на вопрос, на который наш июльский список
        ответить не может: как эти строки называются сегодня.

        Подпункт здесь — строка, у которой есть галочка, а не элемент
        `<label>`: на DQ500/DQ501 обёртки нет ни у одной строки, и прежний
        список возвращал единственную подпись раздела («Value rules»).
        """
        try:
            return (
                page.evaluate(
                    "(skip) => {"
                    + PLACEMENTS_ROW_JS
                    + """
                      const root = document.querySelector('[data-rpa-placements-marker]') || document;
                      const out = [];
                      // Одна галочка — одна строка. Описание категории лежит в
                      // той же строке и упирается в ту же галочку, поэтому
                      // круг 12 назвал подпунктами «Get high visibility for
                      // your business with ads in feeds» и ещё восемнадцать
                      // соседних надписей. Считаем по контролам: первым идёт
                      // само название, остальное к этой галочке уже не
                      // относится.
                      const claimed = new Set();
                      for (const leaf of root.querySelectorAll('*')) {
                        if (leaf.offsetParent === null || leaf.children.length) continue;
                        const t = norm(leaf.textContent);
                        if (!t || t.length > 60) continue;
                        const hit = rowFor(root, t);
                        if (!hit || claimed.has(hit.cb)) continue;
                        claimed.add(hit.cb);
                        if (skip.includes(t) || out.includes(t)) continue;
                        out.push(t);
                      }
                      return out;
                    }
                    """,
                    list(_PLACEMENT_CATEGORIES),
                )
                or []
            )
        except Exception:
            return []

    def _category_rows() -> dict:
        """Чем управляется строка категории в новом варианте раздела.

        14.09, круг 10: створка «Placements» открылась (`стало=true`), и за
        ней оказались все пять июльских имён — «Feeds», «Stories, Status,
        Reels», «In-stream ads for reels», «Search results», «Apps and
        sites». То есть ни имена не менялись, ни раздел не перестраивался.

        Но `<label>` в блоке остался один («Value rules»), а
        `input[type=checkbox]` — ноль. Значит строки здесь не «подпись с
        галочкой», как на DQ204/UL921, и `_set_category` до контрола не
        дотянется. Чем они управляются, спрашиваем у страницы, а не
        достраиваем по аналогии: три версии подряд, построенные по аналогии,
        оказались неверны.
        """
        try:
            return page.evaluate(
                """
                (names) => {
                  const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                  const root = document.querySelector('[data-rpa-placements-marker]') || document;
                  const vis = (e) => e.offsetParent !== null;
                  const out = {};
                  for (const name of names) {
                    const leaf = [...root.querySelectorAll('*')].find(e =>
                      vis(e) && e.children.length === 0 && norm(e.textContent) === name);
                    if (!leaf) { out[name] = { leaf: false }; continue; }
                    const chain = [];
                    let node = leaf.parentElement;
                    for (let i = 0; i < 6 && node; i++) {
                      chain.push({
                        tag: node.tagName.toLowerCase(),
                        role: node.getAttribute('role'),
                        checked: node.getAttribute('aria-checked'),
                        exp: node.getAttribute('aria-expanded'),
                        kids: node.children.length,
                      });
                      node = node.parentElement;
                    }
                    const box = leaf.closest('[role="listitem"],li,[role="row"]')
                      || (leaf.parentElement && leaf.parentElement.parentElement
                          && leaf.parentElement.parentElement.parentElement);
                    const ctrls = box ? [...box.querySelectorAll(
                        'input,[role="checkbox"],[role="switch"],[role="button"]')]
                      .filter(vis)
                      .slice(0, 6)
                      .map(e => ({
                        tag: e.tagName.toLowerCase(),
                        type: e.getAttribute('type'),
                        role: e.getAttribute('role'),
                        checked: e.tagName === 'INPUT' ? e.checked : e.getAttribute('aria-checked'),
                        label: norm(e.getAttribute('aria-label') || '').slice(0, 36),
                      })) : [];
                    out[name] = { leaf: true, chain: chain, ctrls: ctrls };
                  }
                  return out;
                }
                """,
                list(_PLACEMENT_CATEGORIES),
            ) or {}
        except Exception as exc:
            return {"error": str(exc)[:120]}

    def _category_candidates() -> dict:
        """Все совпадения имени категории, а не только первое.

        14.09, круг 13: «Search results» осталась включённой, повтор вернул
        «недоступна», а форма строки отличалась от остальных четырёх
        (`inLabel: true`). То есть имя на странице не одно, и шаг всё это
        время целился в чужой контрол. Отказ должен показывать это сам, а не
        выясняться ещё одним кругом.

        Заодно перечисляем, что лежит в объемлющем `li`: раскрывателя
        подпунктов не нашлось ни над строкой, ни внутри неё, и следующий
        вопрос — есть ли он вообще в разметке.
        """
        try:
            return page.evaluate(
                "(names) => {"
                + PLACEMENTS_ROW_JS
                + """
                  const root = document.querySelector('[data-rpa-placements-marker]') || document;
                  const out = {};
                  for (const name of names) {
                    out[name] = rowsFor(root, name).map((h, i) => {
                      const li = h.row.closest('li') || h.row;
                      const parts = [...li.querySelectorAll('[role],button,svg,i')]
                        .filter(e => e.offsetParent !== null)
                        .slice(0, 8)
                        .map(e => (e.getAttribute('role') || e.tagName.toLowerCase())
                          + (e.getAttribute('aria-label') ? ':' + e.getAttribute('aria-label').slice(0, 20) : '')
                          + (e.hasAttribute('aria-expanded') ? '[exp=' + e.getAttribute('aria-expanded') + ']' : ''));
                      return {
                        i: i,
                        checked: h.cb.getAttribute('aria-checked') === 'true' ? true
                          : h.cb.getAttribute('aria-checked') === 'false' ? false : h.cb.checked,
                        disabled: h.cb.disabled,
                        inLabel: !!h.cb.closest('label'),
                        row: h.row.tagName.toLowerCase(),
                        liText: norm(li.textContent).slice(0, 60),
                        parts: parts,
                      };
                    });
                  }
                  return out;
                }
                """,
                list(_PLACEMENT_CATEGORIES),
            ) or {}
        except Exception as exc:
            return {"error": str(exc)[:120]}

    def _node_candidates() -> str:
        """JS-кусок: створки раздела, чья подпись начинается с `prefix`.

        `prefix` здесь — имя параметра самого скрипта, а не подстановка из
        Python: текст уезжает в страницу аргументом, не склейкой строк.

        Одно место на оба помощника ниже, чтобы «найти» и «проверить»
        не разошлись в том, какой именно узел они имеют в виду.
        """
        return """
          const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
          const root = document.querySelector('[data-rpa-placements-marker]');
          if (!root) return null;
          const cands = [...root.querySelectorAll('[role="button"][aria-expanded]')]
            .filter(e => e.offsetParent !== null && norm(e.textContent).startsWith(prefix));
          cands.sort((a, b) => a.querySelectorAll('*').length - b.querySelectorAll('*').length);
        """

    def _expand_node(prefix: str) -> dict:
        """Раскрыть створку раздела по началу её подписи.

        Из нескольких вложенных совпадений берём самое внутреннее — июльский
        урок: `querySelectorAll` отдаёт предков раньше потомков, а обработчик
        висит на потомке, и native-клик по обёртке до него не доходит.
        """
        try:
            return page.evaluate(
                "(prefix) => {"
                + _node_candidates()
                + """
                  if (cands === null) return { found: false, why: 'нет метки блока' };
                  if (!cands.length) return { found: false };
                  const el = cands[0];
                  const was = el.getAttribute('aria-expanded');
                  el.scrollIntoView({ block: 'center', behavior: 'instant' });
                  if (was === 'false') el.click();
                  return {
                    found: true,
                    was: was,
                    text: norm(el.textContent).slice(0, 60),
                    candidates: cands.length,
                  };
                }
                """,
                prefix,
            ) or {"found": False}
        except Exception as exc:
            return {"found": False, "error": str(exc)[:80]}

    def _node_expanded(prefix: str) -> str | None:
        """Открыта ли створка прямо сейчас — по мнению самой страницы."""
        try:
            return page.evaluate(
                "(prefix) => {"
                + _node_candidates()
                + """
                  if (cands === null || !cands.length) return null;
                  return cands[0].getAttribute('aria-expanded');
                }
                """,
                prefix,
            )
        except Exception:
            return None

    # ── раскрытие ────────────────────────────────────────────────────────
    #
    # Смотрим не на надпись, а на то, что нужно на самом деле: видны ли пять
    # категорий. Прежний код начинал с клика по «Show more settings» — а
    # раздел приходит раскрытым, и этот клик его закрывал.
    seen = _categories_visible()
    if seen < len(_PLACEMENT_CATEGORIES):
        for i in range(6):
            if _present_in_placements("Hide settings", exact=False):
                break
            clicked = _click_in_placements("show more settings", exact=False, max_children=2)
            _dbg.append(f"tick {i}: show_more_clicked={clicked}")
            _sleep_h(0.6 if clicked else 0.4)
        seen = _categories_visible()

    if seen < len(_PLACEMENT_CATEGORIES):
        pc = _expand_button("Placement controls")
        _dbg.append(f"placement_controls={pc}")
        report["placement_controls"] = pc
        _sleep_h(0.8)
        seen = _categories_visible()

    # Раздел раскрыт, а узел внутри него — нет.
    #
    # 14.09: восемь отказов «видно 0 категорий из 5», и шаг научили
    # перечислять, что в блоке можно нажать. Ответ пришёл первым же прогоном:
    #
    #   [button] PlacementsIncluded: 21Allowed with limited s  aria-expanded=false
    #   [button] Platforms                                     aria-expanded=true
    #   [a]      Hide settings
    #
    # Пять категорий лежат за свёрнутой кнопкой «Placements» — вложенной, со
    # своей сводкой в подписи. `Placement controls`, который искал шаг, на
    # странице отсутствует (`found: False`), а `Show more settings` уже нажат:
    # рядом стоит `Hide settings`.
    #
    # Трогаем только «Placements». Соседний «Platforms» — другая настройка, и
    # раскрытие соседа закрывает створку обратно: это гармошка, как у
    # категорий (дневник, 9 сентября). Первый заход жал оба подряд, и в дампе
    # «Placements» оказывался снова свёрнут — отсюда взялись площадки,
    # принятые за новую разбивку раздела.
    #
    # Раскрываем по началу подписи — в ней сводка («Included: 21»), которая
    # меняется от прогона к прогону, — и убеждаемся, что створка открылась.
    # Сам факт клика ничего не значит: июльская хроника (пункт 6) — клик по
    # внешней обёртке не долетает до React-обработчика на потомке.
    if seen < len(_PLACEMENT_CATEGORIES):
        node = _expand_node("Placements")
        _dbg.append(f"expand[Placements]={node}")
        report["expand_placements"] = node
        if node.get("found"):
            _sleep_h(1.0)
            now = _node_expanded("Placements")
            if now == "false":
                kicked = _click_in_placements(
                    "Placements", exact=False, max_children=2
                )
                _dbg.append(f"expand[Placements] повтор={kicked}")
                _sleep_h(1.0)
                now = _node_expanded("Placements")
            _dbg.append(f"expand[Placements] стало={now}")
            report["expand_placements_now"] = now
            seen = _categories_visible()

    report["categories_visible"] = seen
    _dbg.append(f"categories_visible={seen}")
    if seen < len(_PLACEMENT_CATEGORIES):
        # Не «сколько не нашли», а «что вместо них». Число ничего не говорит о
        # главном: помечен ли нужный блок. Без этого разбор шесть раз свёлся к
        # догадкам по посмертным снимкам.
        inside = _what_is_in_the_block()
        report["block"] = inside
        _dbg.append("block=" + json.dumps(inside, ensure_ascii=False)[:2500])
        rows = _category_rows()
        report["category_rows"] = rows
        _dbg.append("rows=" + json.dumps(rows, ensure_ascii=False)[:4000])
        report["why"] = f"видно {seen} категорий из {len(_PLACEMENT_CATEGORIES)}"
        # 16.09, 17:26–18:53: карточка без единого контрола — «Advantage+ on»,
        # «Placement value rules», «Account controls · Excluded placements:
        # None» и объявление Meta «Excluding placements or platforms will no
        # longer be available for your ad sets». Кликать там нечего; в том же
        # кабинете соседнее ad set ещё получало старую карточку (17:41 PF ok).
        # Отличаем это от промаха селектора, чтобы оператор видел причину.
        if _placements_exclusion_removed(page):
            # Решение владельца 16.09: исключения ставим сами — на уровне
            # аккаунта, единственном, где Meta их ещё принимает.
            _dump_dbg()
            # Исключения аккаунта — только страховка (четыре площадки из
            # списка «выключить»), а не успех шага: 17.09 Doc21 так ушёл с 17
            # площадками вместо 7. Успех — только пять категорий по списку.
            report["account_exclusions_ok"] = ensure_account_placement_exclusions(page, report)
            report["why"] = "placement_categories_not_reached"
            report["notice"] = (
                "The placement categories under Show more settings could not be "
                "set, so the ad set would run on more placements than requested."
            )
            return False
        _dump_dbg()
        return False

    ok = True

    # ── категории ────────────────────────────────────────────────────────
    report["categories"] = {}
    locked_ok: list[str] = []
    for name, want in _PLACEMENT_CATEGORIES.items():
        res = _set_category(name, want)
        report["categories"][name] = res
        if not res.get("checkbox") or res.get("checked") is not want:
            # Недоступная категория в нужном состоянии — не отказ. 17.09
            # 04:22 (S15): «Apps and sites» заблокирована, потому что Audience
            # Network исключён в Account controls, и стоит снятой — ровно
            # как надо. Тот же принцип, что у подпунктов («Facebook Business
            # Explore»). Состояние берём у самой страницы, не из `res`.
            try:
                cands = (_category_candidates() or {}).get(name) or []
            except Exception:
                cands = []
            if cands and all(c.get("disabled") for c in cands) and all(
                bool(c.get("checked")) is want for c in cands
            ):
                locked_ok.append(name)
            else:
                ok = False
        _sleep_h(0.3)
    if locked_ok:
        report["categories_locked_as_wanted"] = locked_ok

    # ── подпункты ────────────────────────────────────────────────────────
    report["expanded"] = {}
    report["subitems_seen"] = []

    def _set_many(items: list[tuple[str, bool]]) -> set[str]:
        # Найдено И нажато одним вызовом: снятие галочки перекладывает
        # строки под ней, и координата, снятая отдельным шагом, к моменту
        # клика указывает уже на соседнюю.
        # Подъём на четыре уровня, а не общий `rowFor`, и это не небрежность.
        # 14.09: я свёл подпункты на `rowFor` «для единообразия» — и вместо
        # семнадцати из восемнадцати круг 16 дал десять. Подпункты лежат во
        # вложенных `li` внутри категории, и граница по `li`, которая спасает
        # категории от чужой галочки, здесь отрезает свою же. Работающий путь
        # без причины не меняют; правка откачена.
        #
        # Недоступная галочка — не всегда беда: «Facebook Business Explore»
        # Meta держит выключенной и не даёт трогать. Если она уже в нужном
        # состоянии, требовать от неё клика бессмысленно.
        #
        # ACL-1313: все подпункты захода — одним вызовом страницы
        # (`_set_placement_subitems`), каждый тем же JS, что и прежде.
        # Возвращает подписи, которые встали.
        done: set[str] = set()
        for (label, _want), res in zip(items, _set_placement_subitems(page, items)):
            if not res.get("ok"):
                _dbg.append(f"подпункт[{label}]={res}")
                report.setdefault("subitems_failed", {})[label] = res
            else:
                done.add(label)
                # Встал на повторном заходе (после перераскрытия категории) —
                # прежний отказ в отчёте больше не правда.
                report.get("subitems_failed", {}).pop(label, None)
        return done

    def _pass(on_page: list[str]) -> None:
        """Один заход по видимым подпунктам: сначала «включить», потом «выключить»."""
        items = ([(x, True) for x in remaining_on if _label_on_page(x, on_page)]
                 + [(x, False) for x in remaining_off if _label_on_page(x, on_page)])
        done = _set_many(items)
        for label, want in items:
            if label in done:
                (remaining_on if want else remaining_off).remove(label)

    # Категории ведут себя как гармошка: раскрытие следующей закрывает
    # предыдущую. Проверено живым прогоном 9 сентября — код раскрыл все три
    # подряд, а подпункты нашлись только у последней («Facebook in-stream
    # reels», «Ads on Facebook Reels» — дети In-stream). Поэтому раскрываем
    # и заполняем по одной: пока категория открыта, ставим её подпункты.
    #
    # Какой подпункт к какой категории относится, не задаём списком —
    # спрашиваем страницу. Наш список имён от июля, и угадывать
    # принадлежность значило бы добавить второе предположение к первому.
    remaining_on = list(_SUBITEMS_ON)
    remaining_off = list(_SUBITEMS_OFF)
    def _settle_visible(here: list[str]) -> None:
        # Meta обновляет галочку не сразу: 17.09 20:59 (CR_ait Day) семь
        # подпунктов отчитались «ещё включены», а на дампе, снятом через пару
        # секунд, все были в нужном состоянии. Читаем, пока эта категория ещё
        # раскрыта: открытие следующей скрывает её подпункты.
        still = ([(x, True) for x in remaining_on if _label_on_page(x, here)]
                 + [(x, False) for x in remaining_off if _label_on_page(x, here)])
        if still:
            _sleep_h(2.0)
            settled = {}
            for label, want in still:
                try:
                    res = page.evaluate(PLACEMENTS_SUBITEM_READ_JS, label) or {}
                except Exception as exc:
                    res = {"found": False, "why": str(exc)[:80]}
                if res.get("found") and res.get("checked") is want:
                    settled[label] = res
                    (remaining_on if want else remaining_off).remove(label)
                    report.get("subitems_failed", {}).pop(label, None)
            if settled:
                report.setdefault("subitems_settled", {}).update(settled)


    for name, want in _PLACEMENT_CATEGORIES.items():
        if not want:
            continue
        report["expanded"][name] = _expand_category(name)
        _sleep_h(0.8)
        here = _subitem_labels()
        report["subitems_seen"].extend(x for x in here if x not in report["subitems_seen"])
        # Два захода, пока категория ещё открыта. Первый клик по подпункту
        # иногда не успевает отразиться: `_set` читает галочку в том же
        # вызове, где кликает, и возвращает старое значение. У подпунктов
        # первых двух категорий это сходило с рук — их добирал заход
        # следующей категории. У последней такого захода нет, и 14.09
        # «Ads on Facebook Reels» осталась единственной причиной отказа
        # шага на прогоне пользователя.
        for _ in range(2):
            _pass(here)
            if not [x for x in remaining_on + remaining_off if _label_on_page(x, here)]:
                break
            _sleep_h(0.6)

        _settle_visible(here)

        # ACL-1313, 25.09 09:43 (CL_ML Day, run 10402d9c2daf): «Messenger
        # Stories» и «WhatsApp Status» были в `subitems_seen` этой же
        # категории, а оба захода и перечитка через 2 с ответили «строка не
        # найдена». Не разметка: в 921824496d79 (19.09) «Messenger Stories»
        # тем же JS снялась, а отказала только следующая за ней «WhatsApp
        # Status». Не bf02e5a: ровно этот отказ был 19.09 (921824496d79),
        # 21.09 (ca89176131c2, af0204b30dd4) и 22.09
        # (8587f7668f86 — хвост «Feeds» после трёх снятых галочек), до пакетного
        # вызова. Общее у всех: отказывает хвост категории, идущий ПОСЛЕ
        # кликов, — Meta перерисовывает раздел после сохранения правки, и
        # раскрытая категория сворачивается (строки её подпунктов удаляются
        # из DOM). Прежний код раскрывал категорию один раз и дальше искал
        # строки в свёрнутой.
        #
        # Поэтому: пока видимые здесь подпункты не встали, проверяем, открыта
        # ли категория. Раскрыватель жмётся только у свёрнутой (`was:
        # "false"`); открытую повтор не трогает, и тогда перераскрытие не
        # поможет — выходим. Не больше `_PLACEMENT_REOPENS` раз на категорию.
        for _ in range(_PLACEMENT_REOPENS):
            lost = [x for x in remaining_on + remaining_off if _label_on_page(x, here)]
            if not lost:
                break
            again = _expand_category(name)
            report.setdefault("reopened", {}).setdefault(name, []).append(
                {"was": again.get("was"), "lost": lost})
            if again.get("was") != "false":
                break
            _sleep_h(0.8)
            fresh = [x for x in _subitem_labels() if not _label_on_page(x, here)]
            here.extend(fresh)
            report["subitems_seen"].extend(
                x for x in fresh if not _label_on_page(x, report["subitems_seen"]))
            for _ in range(2):
                _pass(here)
                if not [x for x in remaining_on + remaining_off if _label_on_page(x, here)]:
                    break
                _sleep_h(0.6)
            _settle_visible(here)

        # Source 8e9acb2/5b16886: lower placement rows are virtualized until
        # scrolling. Aim at this card, not wherever the operator left the
        # pointer. Settle current rows BEFORE scrolling can unmount them.
        for _ in range(3):
            if not any(not _label_on_page(x, here) for x in remaining_on + remaining_off):
                break
            try:
                page.locator('[data-rpa-placements-marker]').first.hover(timeout=2000)
                page.mouse.wheel(0, 400)
            except Exception as exc:
                report["subitems_scroll_error"] = type(exc).__name__
                break
            _sleep_h(0.6)
            more = [x for x in _subitem_labels() if not _label_on_page(x, here)]
            if not more:
                continue
            here.extend(more)
            report["subitems_seen"].extend(x for x in more if not _label_on_page(x, report["subitems_seen"]))
            _pass(more)
            _settle_visible(more)

    report["subitems_missing"] = remaining_on + remaining_off
    _dbg.append(f"subitems_seen={report['subitems_seen']}")
    if report["subitems_missing"]:
        ok = False

    # Пока хоть что-то не сошлось, отказ должен принести разметку строк, а не
    # одно число. Круг 13 стоил целого прогона ровно потому, что «Search
    # results» осталась включённой, а чем она отличается от соседних — в
    # отчёте не было.
    if not ok:
        cands = _category_candidates()
        report["category_candidates"] = cands
        _dbg.append("candidates=" + json.dumps(cands, ensure_ascii=False)[:3500])

    # «Allow limited spending to excluded placements»: в снимке 2026-09-09
    # её нет ни разу. Раньше она появлялась после исключения площадок, так
    # что шаг оставлен — но её отсутствие больше не беда, отчёт скажет,
    # нашлась она или нет.
    try:
        report["limited_spending"] = page.evaluate(
            """
            () => {
              const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
              const row = [...document.querySelectorAll('*')].find(e =>
                norm(e.textContent).startsWith('Allow limited spending to excluded placements')
                && e.offsetParent !== null);
              if (!row) return { found: false };
              let container = row;
              for (let i = 0; i < 6 && container; i++) {
                const cb = container.querySelector('input[type=checkbox]');
                if (cb) {
                  if (cb.checked) cb.click();
                  return { found: true, checked: cb.checked };
                }
                container = container.parentElement;
              }
              return { found: true, checkbox: false };
            }
            """
        )
    except Exception:
        report["limited_spending"] = {"found": False}

    _dump_dbg()
    return ok


def select_devices_ios_only(page: Page) -> bool:
    """Set 'Devices and operating systems' (inside the same Placements /
    Account controls panel fill_placements() expands — not a separate
    section) to 'iOS devices only'.

    Confirmed live 2026-07-28. Two things that look like the same bug class
    fixed elsewhere in this file turned out to have a different cause here:
      - The "Devices and operating systems" heading toggle IS a real
        ancestor of a [role=button][aria-expanded] div, ~10 levels up from
        the leaf text — a plain ancestor walk works for opening it.
      - But for the "All mobile devices" trigger below it, walking UP from
        the leaf text node to find a [role=button] ancestor fails, because
        querySelectorAll('*') returns that DIV[role=button] itself (2
        children: two <span>s, one holding the label text, one empty)
        BEFORE its own leaf descendants in document order. A naive
        `find()` on exact-text-match therefore returns that wrapper's
        outer non-interactive parent, and walking further UP from there
        never reaches the button (it's a sibling/descendant, not an
        ancestor). Fix: select `[role=button]` elements directly and
        match on text, no ancestor walk needed.
    """
    expanded = page.evaluate(
        """() => {
          const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
          const already = [...document.querySelectorAll('[role=button]')].some(e =>
            e.offsetParent !== null && /^(All mobile devices|iOS devices only|Android devices only)$/.test(norm(e.textContent)));
          if (already) return true;
          const heading = [...document.querySelectorAll('*')].find(e =>
            e.offsetParent !== null && norm(e.textContent) === 'Devices and operating systems' && e.children.length === 0);
          if (!heading) return false;
          let cur = heading;
          for (let i = 0; i < 16 && cur; i++) {
            if (cur.getAttribute('role') === 'button' && cur.hasAttribute('aria-expanded')) {
              cur.scrollIntoView({block: 'center', behavior: 'instant'});
              cur.click();
              return true;
            }
            cur = cur.parentElement;
          }
          return false;
        }"""
    )
    if not expanded:
        return False

    # 20.09 00:50 (CO_bnc GMB): раздел раскрыт, а внутри ещё крутилась
    # загрузка — пустая рамка. Прежние три секунды ожидания её не
    # перекрывали, и iOS молча не вставал. Ждём до 25 с.
    for _ in range(60):
        present = page.evaluate(
            """() => [...document.querySelectorAll('[role=button]')].some(e =>
              e.offsetParent !== null && (e.textContent || '').trim() === 'All mobile devices')"""
        )
        if present:
            break
        _sleep_h(0.4)
    else:
        _telemetry.record("devices_ios", False, note="devices_panel_not_loaded")
        return False

    clicked = page.evaluate(
        """() => {
          const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
          const el = [...document.querySelectorAll('[role=button]')].find(e =>
            e.offsetParent !== null && norm(e.textContent) === 'All mobile devices');
          if (!el) return false;
          el.scrollIntoView({block: 'center', behavior: 'instant'});
          el.click();
          return true;
        }"""
    )
    if not clicked:
        return False
    _sleep_h(0.5)

    picked = page.evaluate(
        """() => {
          const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
          const listbox = document.querySelector('[role=listbox]');
          const scope = listbox || document;
          const el = [...scope.querySelectorAll('[role=menuitemradio], [role=radio], [role=option]')].find(e =>
            e.offsetParent !== null && norm(e.textContent) === 'iOS devices only');
          if (!el) return false;
          el.scrollIntoView({block: 'center', behavior: 'instant'});
          el.click();
          return true;
        }"""
    )
    if not picked:
        return False
    _sleep_h(0.5)

    return bool(
        page.evaluate(
            """() => [...document.querySelectorAll('[role=button]')].some(e =>
              e.offsetParent !== null && (e.textContent || '').trim() === 'iOS devices only')"""
        )
    )


def create_additional_ad(page: Page, adset_name: str) -> bool:
    """Add another ad to the ad set that's already open (for campaigns with
    more than one ad per ad set), landing on the same "Set up creative"
    button state that upload_ad_creative() expects for the first ad.

    Confirmed live 2026-07-28: the left tree's ad-set row has an "Action
    menu" icon at its far right — but finding it via `[role=button]` + exact
    text "Action menu" (with an `offsetParent !== null` visibility check)
    is unreliable, intermittently finding 0 matches even with a real mouse
    hover over the row first (retried up to 10x live, still 0). A raw
    coordinate click bypasses this entirely: `elementFromPoint()` at that
    same spot always resolves to the icon's div regardless of what the
    text/role search reports, and a real click there opens the menu 100%
    of the time in live testing. The x offset is computed from the row's
    own *wide* ancestor (walk up until width > 350px — the row's own
    bounding box is only as wide as its text, not the full tree panel)
    rather than a hardcoded pixel, so it isn't tied to one fixed sidebar
    width.

    Its "Create ad" menu item is reliably identified by
    `data-surface="/am/editor_drawer/lib:campaign-structure-item-action-
    menu-create"` (text-matching "Create ad" is fragile for the same
    ancestor-vs-descendant reason fixed elsewhere in this file for Devices/
    Placements: querySelectorAll('*') returns the non-clickable wrapper
    before the real `[role=menuitem]`).

    "Create ad" sometimes opens a "Set up your creative" wizard modal
    directly (skipping the "Set up creative" button state that
    upload_ad_creative() expects), sometimes lands straight on the ad-level
    page — confirmed live both ways happen across otherwise-identical
    calls. Cancel out of the modal if present (best-effort, not an error if
    it never appeared); either way the real completion signal is the
    "Set up creative" button becoming visible, so the rest of the ad fill
    sequence (upload_ad_creative / fill_ad_copy / set_ad_destination_
    website) can run completely unmodified for every additional ad.
    """
    # Preferred path: click the ad-set row's action-menu button by its own
    # stable data-surface, no coordinates at all.
    #
    # Found 2026-07-30 via the new telemetry. Every tree level exposes one:
    #   editor_tree:campaign/...action-menu-item/CAMPAIGN_GROUP
    #   editor_tree:adset/...action-menu-item/CAMPAIGN     <- ad set row
    #   editor_tree:ad/...action-menu-item/ADGROUP         <- each ad row
    # (the level prefix is what identifies the row; the trailing type name
    # is offset by one and must NOT be used for matching).
    #
    # They report zero size and offsetParent === null because the icon only
    # renders on hover — which is exactly why every coordinate-based attempt
    # kept missing, and why a DOM .click() (which ignores visibility, same
    # as the "Create ad" menu-item click below already relies on) is the
    # right tool. The coordinate path is kept as a fallback in case Meta
    # renames these surfaces.
    #
    # 24.09 (снимки rpa_pages/7364047ef03c/05, 7d2ce3ab1161/05–08,
    # d3e690189a6e/08): шаг стартовал на странице, где дерева ещё нет —
    # `AdsPERouteLoadPlaceholder` после смены маршрута или сразу после
    # перезагрузки (есть только шапка nav-header-*). Сначала ждём дерево.
    click_point = None
    tree_ready = _wait_tree_ready(page)
    shells_before = _tree_new_ad_shells(page)
    surface_clicked = _click_adset_action_menu(page) if tree_ready else False
    if surface_clicked:
        _sleep_h(0.6)

    click_point = page.evaluate(
        """(adsetName) => {
          const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
          // Confirmed live 2026-07-29 (Test142): when the browser window is
          // in NARROW restored bounds (e.g. it was un-maximized before
          // being minimised), the tree renders truncated names — the leaf
          // text is then a shortened PREFIX of the full ad-set name, so an
          // exact === match finds < 2 rows and this whole function bails
          // in ~3s. Accept a long-enough prefix as a match too.
          const nameMatches = (raw) => {
            const t = norm(raw).replace(/[…\\u2026]+$/, '');
            if (!t) return false;
            if (t === adsetName) return true;
            return t.length >= 10 && adsetName.startsWith(t);
          };
          // Confirmed live 2026-07-29 (real dashboard run,
          // CR_AIT_All_Video_FX_35_29.07): the campaign/ad-set name appears
          // BOTH in the left structure tree and in the top breadcrumb, so a
          // page-wide search mixes them and "rows[1]" can be a breadcrumb
          // item instead of the ad-set row — the click point then lands
          // nowhere and one ad per campaign is silently skipped. Keep only
          // candidates in the left tree panel (leftmost third of the
          // window) and order them top-to-bottom, so rows[0] is reliably
          // the campaign row and rows[1] the ad-set row.
          const inTree = (el) => {
            const r = el.getBoundingClientRect();
            return r.width > 0 && r.x < window.innerWidth * 0.34;
          };
          const candidates = [...document.querySelectorAll('*')].filter(e =>
            e.offsetParent !== null && nameMatches(e.textContent) && e.children.length === 0
            && inTree(e))
            .sort((a, b) => a.getBoundingClientRect().y - b.getBoundingClientRect().y);
          const rows = [];
          const seen = new Set();
          for (const leaf of candidates) {
            let row = leaf;
            for (let i = 0; i < 5 && row; i++) {
              if (row.getAttribute('role') === 'button' || row.tagName === 'A' || row.onclick) break;
              row = row.parentElement;
            }
            row = row || leaf;
            if (!seen.has(row)) { seen.add(row); rows.push(row); }
          }
          // rows[0] is the campaign row, rows[1] is the ad-set row (both
          // share the same name in this recorded flow).
          if (rows.length < 2) {
            const wide = [...document.querySelectorAll('*')].filter(e =>
              e.offsetParent !== null && nameMatches(e.textContent) && e.children.length === 0);
            return {_diag: 'rows_lt_2', rows: rows.length, pageWide: wide.length,
                    xs: wide.slice(0, 8).map(e => Math.round(e.getBoundingClientRect().x)),
                    innerWidth: window.innerWidth};
          }
          // No width THRESHOLD at all — thresholds keep breaking at
          // window sizes nobody tested (fixed 350px failed at ~1000px,
          // 0.22*innerWidth failed at ~1500px where the tree panel is
          // ~310px — confirmed live 2026-07-29, Test147: 1-second create
          // failures). The tree panel is simply the WIDEST ancestor that
          // is still clearly narrower than the page, so take the widest
          // ancestor below 60% of the window and be done with it.
          // X and Y come from DIFFERENT elements, deliberately.
          //
          // The "..." action-menu icon sits at the right edge of the tree
          // PANEL, so x has to come from a wide ancestor. But y must come
          // from the ad-set ROW itself. Confirmed live 2026-07-30 via the
          // new telemetry (`underPoint: DIV//ES Ai Trader Text 2 ... Action
          // menu`): taking y from that same wide ancestor aimed at the
          // panel's vertical centre, which lands on whatever ad row happens
          // to sit in the middle of the list — so every additional-ad create
          // clicked the wrong row and no menu ever opened. Three earlier
          // attempts kept re-tuning the WIDTH threshold; the bug was on the
          // vertical axis the whole time.
          let node = rows[1];
          let panel = null, panelW = 0;
          for (let i = 0; i < 8 && node; i++) {
            const w = node.getBoundingClientRect().width;
            if (w < window.innerWidth * 0.6 && w > panelW) { panel = node; panelW = w; }
            node = node.parentElement;
          }
          if (!panel) return {_diag: 'no_wide_ancestor', rows: rows.length,
                              innerWidth: window.innerWidth};

          // Row rect for y: prefer the walked-up row, but if that ancestor
          // is tall it is a container rather than a row — use the bare text
          // leaf then, whose box is always the row's own line.
          // 19.09 15:12 (CR_ait IPO, 302c/…): панель дерева была прокручена,
          // строка ad set'а стояла у самого верха (y=23) под липкой шапкой —
          // клик попадал в шапку, меню не открывалось, ad 2 не создавался.
          // Для ad 3 та же строка стояла ниже (y=84) и всё сработало.
          try { rows[1].scrollIntoView({block: 'center'}); } catch (e) {}
          let rowRect = rows[1].getBoundingClientRect();
          if (rowRect.height > 80) rowRect = candidates[1].getBoundingClientRect();

          const p = panel.getBoundingClientRect();
          return {x: p.right - 22, y: rowRect.y + rowRect.height / 2,
                  _diag: 'ok', rows: rows.length,
                  panelW: Math.round(panelW), rowH: Math.round(rowRect.height),
                  innerWidth: window.innerWidth};
        }""",
        adset_name,
    )
    def _diag(stage: str, extra: dict | None = None) -> None:
        # Same approach that cracked the ad-copy bug: record WHICH stage
        # failed instead of collapsing every cause into one opaque False.
        # Added 2026-07-29 after `fail_ad3_create` recurred on both
        # campaigns of a real dashboard run and two rounds of guessing at
        # the geometry heuristics failed to fix it.
        info = {
            "adset": adset_name,
            "point": click_point if isinstance(click_point, dict) else None,
            **(extra or {}),
        }
        # Разметка этого шага на диск, как у всех остальных отказов. 9 сентября
        # он единственный упал без снимка — писал в телеметрию напрямую, мимо
        # общей точки, — и `pageWide: 0` пришлось толковать на глаз: имени
        # ад-сета нет на странице нигде, а почему, сказать нечем.
        try:
            from src.fb_ads_rpa import page_capture as _pages

            saved = _pages.capture(page, step="create_additional_ad", diag=info)
            if saved:
                info["page"] = saved
        except Exception:
            pass
        _telemetry.record(
            "create_additional_ad",
            False,
            note=stage,
            diag={**(_telemetry.page_context(page) or {}), **info},
        )

    if not surface_clicked:
        if not click_point or not isinstance(click_point, dict) or "x" not in click_point:
            _diag("no_click_point" if tree_ready else "tree_not_ready")
            return False

        # A click with no prior hover dwell can land on the row's own (wider)
        # click target — navigating into the ad set instead of opening the
        # menu — confirmed live 2026-07-28: the icon only becomes the active
        # hit-target after a brief hover.
        page.mouse.move(click_point["x"], click_point["y"])
        _sleep_h(0.4)
        page.mouse.click(click_point["x"], click_point["y"])
        _sleep_h(0.6)

    create_clicked = _click_create_menu_item(page)
    if not create_clicked and surface_clicked:
        # DOM-клик по «...» ад-сета заодно ВЫБИРАЕТ ад-сет: редактор уходит
        # с объявления на ад-сет (d3e690189a6e 20:52: адрес ads/edit →
        # adsets/edit&selected_adset_ids, снимок 09 — editor:ad_set), дерево
        # перемонтируется и открытое меню пропадает. Ждём дерево и жмём ещё
        # раз: ад-сет уже выбран, второй клик только открывает меню.
        if _wait_tree_ready(page):
            wait_editor_saved(page)
            if _click_adset_action_menu(page):
                _sleep_h(0.6)
                create_clicked = _click_create_menu_item(page)
    if not create_clicked:
        # What DID the click open, if anything? Record the menu surfaces on
        # the page and what sits under the click point.
        try:
            probe = page.evaluate(
                """(pt) => {
                  const surfaces = [...document.querySelectorAll('[data-surface]')]
                    .filter(e => e.offsetParent !== null)
                    .map(e => e.getAttribute('data-surface'))
                    .filter(s => s && s.indexOf('action-menu') !== -1);
                  const under = pt ? document.elementFromPoint(pt.x, pt.y) : null;
                  return {
                    menuSurfaces: [...new Set(surfaces)].slice(0, 10),
                    anyMenuOpen: !!document.querySelector('[role=menu],[role=menuitem]'),
                    underPoint: under ? (under.tagName + '/' + (under.getAttribute('role') || '') +
                                         '/' + (under.textContent || '').trim().slice(0, 40)) : null,
                  };
                }""",
                # Без точки (путь через data-surface) probe падал KeyError 'x'
                # и терял диагностику — 24.09 так дважды.
                ({"x": click_point["x"], "y": click_point["y"]}
                 if isinstance(click_point, dict) and "x" in click_point else None),
            )
        except Exception as exc:
            probe = {"probe_error": str(exc)[:80]}
        _diag("create_menu_item_not_found", probe)
        return False
    _sleep_h(1.5)

    # "Create ad" sometimes opens the "Set up your creative" wizard modal
    # directly, sometimes not. Close it, then prove it is actually gone.
    #
    # The previous version clicked the first "Cancel" it could find, broke
    # out of the loop on the first click that didn't raise, and accepted the
    # underlying "Set up creative" button being "visible" as proof it had
    # worked. Confirmed live 2026-07-29 (Test138): that reports success
    # while the modal is still wide open, because Playwright's visibility
    # check does not consider occlusion — a button covered by a fullscreen
    # modal is still "visible". The second ad then failed on every single
    # following step, its fields being behind that modal.
    # Each round: try the modal's close icon, re-check, and if it is still
    # up fall back to its own Cancel button.
    #
    # Confirmed live 2026-07-29 (Test141): the previous version gated the
    # Cancel fallback on `_close_creative_setup_modal()` returning False —
    # but that helper reports whether it CLICKED, not whether the modal
    # closed. When the icon hunt landed on the wrong small element near the
    # top-right corner it returned True every round, so Cancel was never
    # tried at all and all six rounds re-clicked the same dead spot.
    for _ in range(6):
        if not _creative_modal_open(page):
            break
        _close_creative_setup_modal(page)
        _sleep_h(0.6)
        if not _creative_modal_open(page):
            break
        try:
            page.get_by_role("button", name="Cancel").first.click(timeout=2000)
        except Exception:
            pass
        _sleep_h(0.6)

    if _creative_modal_open(page):
        _diag("creative_modal_left_open")
        return False

    # Readiness is the ad-name field specifically — that is what the caller
    # writes to next, so it is the only signal worth gating on.
    #
    # 24.09 (снимки 7d2ce3ab1161/04, d3e690189a6e/04): через 10 с после
    # «Create ad» редактор стоит на АД-СЕТЕ (editor:ad_set), в дереве всё ещё
    # одно объявление, поля имени нет. Заготовка «New Leads ad» появляется
    # позже: с 19.09 после ad_name_field_not_visible вызывающий усыновлял её
    # за 2–6 с в 86 случаях из 111. Ждём дольше и усыновляем сами — до
    # перезагрузки в recover_stuck_page, которая эту заготовку и губила.
    name_box = page.get_by_placeholder("Enter your ad name here...").first
    deadline = time.time() + 45
    adopted = False
    while time.time() < deadline:
        try:
            if name_box.is_visible(timeout=500):
                return True
        except Exception:
            pass
        if not adopted and shells_before >= 0 and _tree_new_ad_shells(page) > shells_before:
            adopted = _open_tree_ad(page, "New Leads Ad")
            _close_creative_setup_modal(page)
        _sleep_h(1.0, 0.1)
    # Объявление к этому моменту уже создано — отказ здесь значит «поле
    # имени не показалось», а не «ничего не случилось». Doc20 17.09:
    # молчаливый False, повторный Create ad, два пустых объявления.
    _diag("ad_name_field_not_visible", {"adopted_shell": adopted})
    return False


_TREE_READY_JS = r"""() => {
  // Маршрут ещё грузится: вместо редактора один спиннер (снимки 24.09).
  if (document.querySelector('[data-non-int-surface*="AdsPERouteLoadPlaceholder"]')) return false;
  return !!document.querySelector(
    '[data-surface*="editor_tree:adset"][data-surface*="campaign-structure-action-menu-item"]');
}"""


def _wait_tree_ready(page: Page, timeout_s: float = 30.0) -> bool:
    """Левое дерево смонтировано и маршрут не в загрузке. False — не дождались.

    Свёрнутое окно Chrome придерживает (7d2ce3ab1161 24.09: minimized=true,
    спиннер маршрута 11 с подряд) — поэтому вкладку сначала будим.
    """
    try:
        if page.evaluate(_TREE_READY_JS):
            return True
    except Exception:
        pass
    try:
        from src.fb_ads_rpa.browser import wake_page

        wake_page(page)
    except Exception:
        pass
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            if page.evaluate(_TREE_READY_JS):
                _sleep_h(0.8)  # дать дереву дорисовать строки
                return True
        except Exception:
            pass
        _sleep_h(1.0, 0.1)
    return False


def _click_adset_action_menu(page: Page) -> bool:
    """DOM-клик по скрытой кнопке «...» строки ад-сета (см. create_additional_ad)."""
    try:
        return bool(page.evaluate(
            """() => {
              const el = document.querySelector(
                '[data-surface*="editor_tree:adset"][data-surface*="campaign-structure-action-menu-item"]');
              if (!el) return false;
              el.click();
              return true;
            }"""
        ))
    except Exception:
        return False


def _click_create_menu_item(page: Page, rounds: int = 5) -> bool:
    """Пункт «Create ad» открытого меню строки; False — меню не открылось."""
    for _ in range(rounds):
        try:
            if page.evaluate(
                """() => {
                  const el = document.querySelector(
                    '[data-surface="/am/editor_drawer/lib:campaign-structure-item-action-menu-create"]');
                  if (!el) return false;
                  el.click();
                  return true;
                }"""
            ):
                return True
        except Exception:
            pass
        _sleep_h(0.3)
    return False


def _creative_modal_open(page: Page) -> bool:
    """Is the "Set up your creative" wizard modal currently covering the page?"""
    try:
        return page.get_by_text("Set up your creative", exact=True).first.is_visible(timeout=1000)
    except Exception:
        return False


def _library_selected_count(page: Page) -> int | None:
    """The visible library counter, or None when its state is unavailable."""
    try:
        value = page.evaluate(
            r"""() => {
              let total = 0, found = false;
              for (const el of document.querySelectorAll('*')) {
                const r = el.getBoundingClientRect();
                const style = getComputedStyle(el);
                if (!r.width || !r.height || el.children.length ||
                    style.visibility === 'hidden' || el.closest('[hidden],[inert],[aria-hidden="true"]')) continue;
                // 18.09 (CR_ait Mix): в режиме «до 10 файлов» счётчик пишется
                // «1 of 10 selected»; читали только «1 selected» и видели 0.
                const m = (el.textContent || '').trim().match(/^(\d+)(?: of \d+)? selected$/);
                if (m) { found = true; total += Number(m[1]); }
              }
              return found ? total : null;
            }"""
        )
        return int(value) if value is not None else None
    except Exception:
        return None


def _library_tiles(page: Page, filename: str):
    """Locator for the clickable library tiles captioned `filename`.

    Ground truth from a live DOM walk (2026-07-29): the caption is a plain
    text div, and the real interactive element is the `[role=button]`
    exactly two levels above it. The grid itself is a
    `ReactVirtualized__Grid`, so tiles mount and unmount as it scrolls —
    which is why this returns a locator (re-resolved on every use, and
    auto-scrolled by Playwright before clicking) rather than precomputed
    coordinates. An earlier coordinate-based attempt failed live: clicks
    landed on stale positions and the grid stayed at "0 selected".
    """
    return page.get_by_text(filename, exact=True).locator(
        "xpath=ancestor::*[@role='button'][1]"
    )


#: Сколько раз переспросить сетку, прежде чем поверить, что копии нет.
#: Секунда на попытку: 15.09 файл не находился и через полторы минуты после
#: собственной загрузки, так что ждать дольше бессмысленно — если задержка
#: индексации такая, повторное использование всё равно не поможет.
_LIBRARY_APPEAR_TRIES = 5

#: Сколько подходящих плиток пробовать. Было 6: 17.09 00:03 (S15) копий
#: `2046_1.mp4` было семь — шесть старых не брались, а свежая, загруженная в
#: этом же прогоне, так и не получила клика.
_LIBRARY_MAX_PICKS = 12


def _library_sight(page: Page, filename: str) -> dict:
    """Что шаг видел в медиатеке в момент, когда решил, что копии нет.

    Пишется только при отказе найти. Причина появления этой функции проста:
    15.09 поиск трижды ответил «копии нет» на файл, который сам же загрузил
    минутой раньше, и разобрать это было нечем — ни числа плиток, ни
    содержимого поиска, ни подписей соседей. Весь вечер ушёл на догадки о
    том, что можно было прочитать.
    """
    sight: dict = {"looked_for": filename}
    try:
        box = page.get_by_placeholder(re.compile(r"Search media", re.I)).first
        sight["search_box"] = box.input_value(timeout=1500) if box.count() else None
    except Exception as exc:
        sight["search_box_error"] = f"{type(exc).__name__}"[:40]
    try:
        # Подписи соседних плиток: если файл там под другим именем, это
        # видно сразу, а если сетка пуста — тоже.
        sight["captions"] = page.evaluate(
            """
            () => [...document.querySelectorAll('[role="button"]')]
              .map(b => (b.textContent || '').replace(/\\s+/g, ' ').trim())
              .filter(t => /\\.(mp4|mov|jpg|jpeg|png|gif)\\b/i.test(t))
              .slice(0, 12)
            """
        )
    except Exception as exc:
        sight["captions_error"] = f"{type(exc).__name__}"[:40]
    try:
        # На какой вкладке стоим: Images | Videos | Upload | Account videos.
        # Поиск по чужой вкладке не найдёт ничего, и это объяснило бы всё.
        sight["tabs"] = page.evaluate(
            """
            () => [...document.querySelectorAll('[role="tab"],[role="radio"]')]
              .filter(e => e.offsetParent !== null)
              .map(e => ((e.textContent || '').trim() + (e.getAttribute('aria-selected') === 'true' ? ' *' : '')))
              .slice(0, 8)
            """
        )
    except Exception as exc:
        sight["tabs_error"] = f"{type(exc).__name__}"[:40]
    return sight


_LIBRARY_DIALOG_JS = (
    "(() => { const d = [...document.querySelectorAll('[role=dialog]')].filter(e =>"
    " e.getBoundingClientRect().width > 0); return d[d.length - 1] || null; })()"
)
"""Верхний открытый диалог — сетка медиатеки, для снимка целиком."""

_AD_MEDIA_BLOCK_JS = (
    "(() => { const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();"
    " const mark = [...document.querySelectorAll('*')].find(e => e.children.length === 0"
    " && /^(Edit media|Change selections|Uploaded media|Customize media|Media)$/.test(norm(e.textContent))"
    " && e.getBoundingClientRect().width > 0); if (!mark) return null; let el = mark;"
    " for (let i = 0; i < 14 && el.parentElement; i++) { el = el.parentElement;"
    " if (el.querySelector('img[src*=\"t15.5256-10\"], [src*=\"t15.5256-10\"]')) break; }"
    " return el; })()"
)
"""Блок «Media» объявления, для снимка целиком."""

_MEDIA_FINGERPRINT_JS = r"""
  (root) => {
    // Отпечаток медиа: адреса картинок/видео и числовые идентификаторы в
    // data-атрибутах. Какой из них стабилен у Meta — решим по живым снимкам;
    // пока собираем и сравниваем, но не отказываем по нему (ревью ACL-1171).
    const out = new Set();
    const add = (v) => { if (!v) return; const m = String(v).match(/[0-9]{8,}|[A-Za-z0-9_-]{20,}(?=\.(jpg|png|mp4|webp))/g);
      (m || []).forEach(x => out.add(x)); };
    const nodes = [root, ...root.querySelectorAll('img, video, source, [style*="background-image"], [data-id], [data-video-id], [data-asset-id]')];
    for (const n of nodes) {
      add(n.getAttribute && n.getAttribute('src'));
      add(n.getAttribute && n.getAttribute('poster'));
      add(n.style && n.style.backgroundImage);
      for (const a of (n.attributes ? [...n.attributes] : [])) if (a.name.startsWith('data-')) add(a.value);
    }
    return [...out].slice(0, 40);
  }
"""


_THUMB_ID_JS = r"""
  (scope) => {
    // Миниатюра видео на CDN Meta: <...>/813948786_38351125907864671_2841079256059848182_n.jpg.
    // Живые снимки 18.09 (CO_bnc Mix2): у выбранной плитки медиатеки и у
    // строк размещения в блоке Media объявления — один и тот же идентификатор.
    const re = /\d{6,}_\d{10,}_\d{10,}_n/g;
    const out = new Set();
    const root = scope || document;
    const nodes = [root, ...root.querySelectorAll('img, [src], [style*="background-image"]')];
    for (const n of nodes) {
      if (!n || !n.getAttribute) continue;
      if (!scope && n.closest && n.closest('[role=dialog]')) continue;
      for (const v of [n.getAttribute('src'), n.getAttribute('style')]) {
        (String(v || '').match(re) || []).forEach(x => out.add(x));
      }
    }
    return [...out];
  }
"""
"""Идентификаторы миниатюр видео — внутри элемента или по странице вне диалогов."""


def _tile_thumb_ids(tile) -> list[str]:
    try:
        return list(tile.evaluate(_THUMB_ID_JS) or [])
    except Exception:
        return []


def _ad_media_thumb_ids(page: Page) -> list[str]:
    """Миниатюры видео в редакторе объявления (открытые диалоги не считаются)."""
    try:
        return list(page.evaluate("(js) => (new Function('return (' + js + ')'))()(null)",
                                  _THUMB_ID_JS) or [])
    except Exception:
        return []


def _tile_fingerprint(tile) -> list[str]:
    try:
        return list(tile.evaluate(_MEDIA_FINGERPRINT_JS) or [])
    except Exception:
        return []


def _ad_media_fingerprint(page: Page) -> list[str]:
    try:
        return list(page.evaluate(
            "(fp) => { const el = " + _AD_MEDIA_BLOCK_JS + "; if (!el) return [];"
            " return (new Function('return (' + fp + ')'))()(el); }",
            _MEDIA_FINGERPRINT_JS,
        ) or [])
    except Exception:
        return []


def _library_any_tile_marked(page: Page) -> bool | None:
    """Осталась ли в сетке хоть одна отмеченная плитка (None — сетку не прочесть)."""
    try:
        return bool(page.evaluate(
            "() => { const d = " + _LIBRARY_DIALOG_JS + "; if (!d) return null;"
            " return !!d.querySelector('[data-test-decoration=\"selected\"],"
            " [aria-checked=\"true\"], [aria-selected=\"true\"], input[type=checkbox]:checked'); }"
        ))
    except Exception:
        return None


def _library_undo_selection(page: Page, tiles, clicked: list[int], before: int) -> bool:
    """Undo while the grid is observable. A count cannot restore item identity."""
    if before != 0:
        # An exclusive replacement can keep count=1 while changing the item.
        # Without the original item identity, do not authorize a fresh upload.
        return False
    for _ in range(10):
        count = _library_selected_count(page)
        if count is None:
            return False
        if count > 0:
            break
        _sleep_h(0.3)
    for i in reversed(clicked):
        count = _library_selected_count(page)
        if count is None:
            return False
        if count == 0:
            break
        if _click_library_tile(page, tiles.nth(i)):
            return False
        for _ in range(10):
            _sleep_h(0.3)
            count = _library_selected_count(page)
            if count is None:
                return False
            if count == 0:
                break
    if _library_selected_count(page) != 0:
        return False
    # Счётчик «0 selected» мог отстать от сетки: ревью 18.09 просит
    # подтверждать исходное состояние, а не одно число. Ни одной отмеченной
    # плитки — вот это состояние, при котором загрузка поверх безопасна.
    marked = _library_any_tile_marked(page)
    return marked is False


def _library_tile_shape(page: Page, filename: str) -> dict:
    """Короткое описание плитки медиатеки для разбора — без полного снимка."""
    js = r"""
    (name) => {
      const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
      const cap = [...document.querySelectorAll('*')].find(e => e.children.length === 0 && norm(e.textContent) === name);
      if (!cap) return {caption: false};
      const attrs = (e) => {
        const o = {tag: e.tagName};
        for (const a of ['role', 'aria-checked', 'aria-selected', 'aria-pressed', 'aria-label', 'tabindex', 'type', 'data-testid']) {
          const v = e.getAttribute(a); if (v !== null) o[a] = v.slice(0, 60);
        }
        const r = e.getBoundingClientRect(); o.box = [Math.round(r.width), Math.round(r.height)];
        return o;
      };
      const chain = []; let n = cap;
      for (let i = 0; i < 9 && n; i++) { chain.push(attrs(n)); n = n.parentElement; }
      let tile = cap; for (let i = 0; i < 6 && tile.parentElement; i++) tile = tile.parentElement;
      const marks = [...tile.querySelectorAll('input, [role=checkbox], [aria-checked], [aria-selected], [aria-pressed], [role=button]')]
        .slice(0, 12).map(attrs);
      const dialog = cap.closest('[role=dialog]');
      const counters = dialog ? [...dialog.querySelectorAll('*')].filter(e => e.children.length === 0 && /selected/i.test(e.textContent))
        .slice(0, 5).map(e => norm(e.textContent).slice(0, 60)) : [];
      const buttons = dialog ? [...dialog.querySelectorAll('[role=button]')].map(e => norm(e.textContent)).filter(Boolean).slice(0, 15) : [];
      return {caption: true, chain, marks, html: tile.outerHTML.replace(/class="[^"]*"/g, '').slice(0, 2500), counters, buttons};
    }
    """
    try:
        return page.evaluate(js, filename)
    except Exception as exc:
        return {"error": type(exc).__name__}


#: Точка клика по плитке: миниатюра, не подпись (29.07), и что в этой точке
#: лежит на самом деле — сама плитка или что-то поверх неё.
_TILE_POINT_JS = r"""el => { const r = el.getBoundingClientRect(); if (!r.width || !r.height) return null;
  const x = r.x + r.width / 2, y = r.y + Math.min(r.height / 2, 48);
  const hit = document.elementFromPoint(x, y);
  return {x, y, ok: !!hit && (hit === el || el.contains(hit)), hit: hit ? hit.tagName : null}; }"""


def _click_library_tile(page: Page, tile, *, before: int | None = None) -> str:
    """Щёлкнуть плитку медиатеки. `""` — щёлкнули; иначе причина отказа.

    ACL-1313, прогон 24.09 (SA Mix, da86e97cbf8b): копия в медиатеке нашлась,
    а `tile.click` упал по таймауту — Playwright ждёт, пока плитка перестанет
    двигаться, а виртуальная сетка под нагрузкой ему этого не даёт (то же
    «not stable» записано здесь 04.08). Защита от повторной заливки после
    этого оставила объявление без медиа. Тогда — настоящий CDP-клик по
    точке, но только если в точке сама плитка и выбор ещё не сдвинулся:
    второй клик по уже выбранной плитке снял бы выбор.
    """
    try:
        tile.scroll_into_view_if_needed(timeout=3000)
        tile.click(timeout=3000)
        return ""
    except Exception as exc:
        first = type(exc).__name__
    if before is not None:
        now = _library_selected_count(page)
        if (now is not None and now > before) or _library_tile_selected(tile):
            return ""
    try:
        pt = tile.evaluate(_TILE_POINT_JS)
    except Exception:
        return first[:40]
    if not pt or not pt.get("ok"):
        return f"{first}+blocked:{(pt or {}).get('hit')}"[:40]
    _cdp_mouse_click(page, pt["x"], pt["y"])
    return ""


def _library_tile_selected(tile) -> bool:
    """Does this tile say it is selected — by its own ARIA state, a checked
    box inside it, or Meta's selection decoration on its wrapper? False when
    it cannot say (a double without attributes, a detached handle), so the
    counter test still decides on its own. Each check stands alone: one that
    cannot run must not hide the others."""
    try:
        for attr in ("aria-pressed", "aria-checked", "aria-selected"):
            if (tile.get_attribute(attr) or "").strip().lower() == "true":
                return True
    except Exception:
        pass
    try:
        box = tile.locator("input[type=checkbox]").first
        if box.count() and box.is_checked():
            return True
    except Exception:
        pass
    # Живая разметка 16.09 23:28 (S15, tile_shape): у выбранной плитки нет
    # ни aria-*, ни чекбокса — признак висит на обёртке над `role=button`:
    # `data-test-decoration="selected"`.
    try:
        return bool(tile.evaluate(
            "el => { for (let n = el, i = 0; n && i < 3; n = n.parentElement, i++)"
            " if (n.getAttribute && n.getAttribute('data-test-decoration') === 'selected') return true;"
            " return false; }"
        ))
    except Exception:
        return False


def _library_search(page: Page, filename: str) -> bool:
    """Filter the media library grid down to `filename` before picking one.

    Without this the code picks from whatever the grid happens to be
    showing. On this account that is 60+ near-identical entries for the
    same file left by past test runs, most of them scrolled out of view —
    so the pick is a lottery and usually lands on nothing. Searching first
    is both what a human does and what makes the choice deterministic.
    """
    try:
        box = page.get_by_placeholder(re.compile(r"Search media", re.I)).first
        if not box.count():
            return False
    except Exception:
        return False

    # Same focus trap as every other text field today (playwright#41286):
    # a plain .click() can "succeed" without the page getting focus, after
    # which keyboard input goes nowhere — or worse, Control+A lands on the
    # page itself. Use the shared CDP-click helper, keep the pinned handle,
    # and read the value back before claiming the search happened.
    handle = _focus_via_cdp_click(page, box)
    if handle is None:
        return False
    try:
        box.fill(filename, timeout=3000)
    except Exception:
        # Meta builds several of these search fields as span-based fake
        # inputs where .fill() throws — type via keyboard instead. Focus
        # was established above, so Control+A stays inside the field.
        try:
            page.keyboard.press("Control+A")
            _type_human(page, filename)
        except Exception:
            return False

    try:
        typed_ok = bool(handle.evaluate(
            "(el, name) => (el.value !== undefined ? el.value : el.textContent || '').includes(name)",
            filename,
        ))
    except Exception:
        typed_ok = False
    if not typed_ok:
        return False

    # Let the grid re-query and re-render before anything reads it.
    _sleep_h(1.5)
    wait_editor_saved(page, appear_s=0.5, timeout_s=5)
    return True


#: Исходы поиска в медиатеке. Их три, а не два, и это главное в этом шаге.
#:
#: `absent` и `dirty` оба означают «не прикрепили», но требуют
#: противоположного от вызывающего: после `absent` грузить файл нужно, после
#: `dirty` — категорически нельзя, иначе получится объявление с несколькими
#: креативами. Пока исход был булевым, эти два случая были неразличимы, и
#: возможность повторного использования пришлось выключить целиком.
REUSE_ATTACHED = "attached"
REUSE_ABSENT = "absent"
REUSE_DIRTY = "dirty"


def _try_reuse_library_media(page: Page, filename: str) -> str:
    """Прикрепить уже загруженную копию `filename` из медиатеки аккаунта.

    Экономит полную переброску файла: сегодняшний креатив весит 247 МБ, и за
    круг из шести кампаний по три объявления тот же самый файл уезжает в Meta
    восемнадцать раз.

    Возвращает один из трёх исходов:

    * `REUSE_ATTACHED` — медиа прикреплено, шаг закончен;
    * `REUSE_ABSENT` — копии нет либо мы отступили, ничего не изменив;
      сетка ровно в том состоянии, в котором мы её застали, и вызывающий
      спокойно грузит файл;
    * `REUSE_DIRTY` — мы выбрали плитки и **не смогли снять выбор**. Грузить
      поверх нельзя: объявление выйдет с несколькими креативами. Это отказ
      шага, а не повод для запасного пути.

    Third state существует из-за конкретной поломки: 24.08 повторное
    использование выключили, потому что отказ «после клика» возвращал то же
    самое `False`, что и «ничего не нашли», вызывающий уходил грузить поверх
    выбранного, и объявления выходили с 3-6 креативами (прогоны 5bff134ea745,
    770bd7e5fb7b). Meta делает из лишних отдельные вариации — публикуется не
    то, что собрал оператор, и видно это только глазами в кабинете.

    Two things make this harder than it looks, both confirmed live:

    * Repeated test runs leave DOZENS of identical entries for the same
      filename, most flagged with a warning badge. Clicking one is a
      lottery, so this tries several and verifies the grid's own "N
      selected" counter actually moved instead of assuming the click took.
      A run on 2026-07-29 failed exactly here: the click landed on the
      caption, the counter stayed at "0 selected", Next stayed disabled,
      and the ad ended up with no media at all.
    * Picking an EXISTING item (unlike a fresh upload) always forces the
      Trim -> Crop -> Text -> Image generation -> Enhancements ->
      Translation wizard. Rather than click through each step's
      differently-labelled button, force-close the modal — that keeps the
      selection applied rather than discarding it.
    """
    before = _library_selected_count(page)
    if before is None:
        _telemetry.record("ad_creative_reuse", False, note="selection_state_unavailable")
        return REUSE_DIRTY
    _library_search(page, filename)

    # Свежезагруженный файл появляется в сетке не мгновенно. 15.09 на живом
    # прогоне поиск трижды подряд ответил «копии нет» — в том числе через
    # полторы минуты после того, как сам же её загрузил. Ждём, но недолго и
    # по состоянию, а не вслепую: если файла нет и через это время, значит
    # дело не в задержке индексации, и тянуть незачем.
    tiles = _library_tiles(page, filename)
    available = 0
    for _ in range(_LIBRARY_APPEAR_TRIES):
        try:
            available = tiles.count()
        except Exception:
            available = 0
        if available:
            break
        _sleep_h(1.0)
        tiles = _library_tiles(page, filename)

    if not available:
        # Молчаливое «не нашли» — это то, из-за чего мы весь вечер гадали,
        # виновата ли медиатека. Шаг обязан сказать, ЧТО он видел: сколько
        # вообще плиток в сетке, что стоит в поиске и как подписаны соседи.
        # Без этого следующая попытка починки снова будет догадкой.
        _telemetry.record("ad_creative_reuse", None, note="not_in_library",
                          diag=_library_sight(page, filename))
        return REUSE_ABSENT if before == 0 else REUSE_DIRTY

    # Poll the counter for seconds, not milliseconds, before giving up on a
    # click. The old 0.5s check raced Meta's own render: the click HAD
    # landed, the counter simply had not caught up, so the loop moved on and
    # clicked the next duplicate — and the next. Every one of them
    # eventually registered. Measured run 5bff134ea745 (24.08): ads ended up
    # with "6 of 10 selected" / four preview variations instead of the one
    # creative they were built from, because repeated test runs had left
    # three identical CCblk.mp4 tiles in this account's library.
    picked = False
    clicked: list[int] = []
    # Дневник выбора: по какой плитке кликнули, сработал ли клик вообще и что
    # показывал счётчик после него. 15.09 второй прогон застрял именно здесь —
    # копию нашёл, взять не смог, — и разобрать было нечем: наружу уходило
    # одно слово `library_pick_left_selection`.
    log: list[dict] = []
    # Копия может быть уже выбрана до нас: «1 selected» и плитка нужного файла
    # с отметкой. 16.09 23:28 (S15) шаг щёлкал по ней — клик снимал выбор, —
    # и потом решал, что копии нет. Уже выбранная плитка — это наш выбор.
    picked_fingerprint: list[str] = []
    picked_thumbs: list[str] = []
    for i in range(min(available, _LIBRARY_MAX_PICKS)):
        try:
            if _library_tile_selected(tiles.nth(i)):
                log.append({"tile": i, "before": before, "already_selected": True, "took": True})
                picked = True
                break
        except Exception:
            continue
    for i in ([] if picked else range(min(available, _LIBRARY_MAX_PICKS))):
        note = {"tile": i, "before": before}
        tile = tiles.nth(i)
        err = _click_library_tile(page, tile, before=before)
        if err:
            note["click_error"] = err
            log.append(note)
            continue
        clicked.append(i)
        for _ in range(10):  # ~3s
            _sleep_h(0.3)
            # The counter growing is the old test, written for an empty
            # grid. On a live page it already reads 1 before we arrive
            # (acl-1192: six clicks, `before: 1, after: 1, took: false`,
            # every one of them), and a choice is exclusive — it cannot
            # grow. So the tile's own word counts too: a tile that reports
            # itself selected after our click is our pick.
            count = _library_selected_count(page)
            if (count is not None and count > before) or _library_tile_selected(tile):
                picked = True
                break
        note["after"] = _library_selected_count(page)
        note["tile_selected"] = _library_tile_selected(tile)
        note["took"] = picked
        try:
            # Есть ли на миниатюре значок (кружок с минусом) — кандидат в
            # «копия недоступна»; проверяется по накопленным прогонам.
            note["icon"] = bool(tile.evaluate("el => !!el.querySelector('svg')"))
        except Exception:
            pass
        log.append(note)
        if picked:
            picked_fingerprint = _tile_fingerprint(tile)
            picked_thumbs = _tile_thumb_ids(tile)
            try:
                from src.fb_ads_rpa import page_capture as _pages

                _pages.capture_on_success(page, step="library_pick_selected",
                               diag={"tile": i, "file": filename, "picks": log,
                                     "fingerprint": _tile_fingerprint(tile)},
                               blocks={"library": _LIBRARY_DIALOG_JS})
            except Exception:
                pass
            break
    if not picked:
        # Разметка медиатеки — до отката, пока сетка такая, какой её видел
        # клик. 16.09 вечером на S15 и Doc20 каждая плитка давала `before 0,
        # after 0, tile_selected false`, а на Doc21 в те же минуты копия
        # бралась; без снимка отличие не увидеть.
        saved_page = None
        try:
            from src.fb_ads_rpa import page_capture as _pages

            saved_page = _pages.capture(page, step="ad_creative_reuse_pick", diag={"picks": log})
        except Exception:
            pass
        # Кликали, но счётчик не двинулся. «Не двинулся» — это то, что мы
        # видим, а не то, что произошло: клик мог и зарегистрироваться, просто
        # позже. Поэтому не уходим молча, а снимаем за собой.
        clean = (before == 0 and _library_selected_count(page) == 0) if not clicked else _library_undo_selection(page, tiles, clicked, before)
        sight = _library_sight(page, filename)
        if saved_page:
            sight["page"] = saved_page
        # Снимок страницы режется на 4 МБ раньше модалки (Doc20, 22:54), так
        # что устройство плитки пишется прямо сюда: сама плитка, её предки и
        # всё отмечаемое рядом — чекбоксы, aria-checked/selected/pressed.
        sight["tile_shape"] = _library_tile_shape(page, filename)
        sight.update({"tiles_found": available, "picks": log, "undone": clean})
        _telemetry.record(
            "ad_creative_reuse", False if not clean else None,
            note="library_pick_left_selection" if not clean else "pick_did_not_take",
            diag=sight,
        )
        return REUSE_ABSENT if clean else REUSE_DIRTY

    # One ad, one creative. If a slow render still let two clicks through,
    # toggle the surplus back off — an over-selected ad silently becomes a
    # multi-media ad Meta then spins into extra variations, which is not the
    # campaign the operator built on the dashboard.
    for i in reversed(clicked[:-1]):
        count = _library_selected_count(page)
        if count is None:
            _telemetry.record("ad_creative_reuse", False, note="count_unreadable_after_pick",
                              diag={"picks": log[-3:]})
            return REUSE_DIRTY
        if count <= 1:
            break
        if _click_library_tile(page, tiles.nth(i)):
            continue
        _sleep_h(0.8)

    after_pick = _library_selected_count(page)
    if after_pick != 1:
        # Раньше — молчаливый отказ: 18.09 CR_ait Mix оставил отмеченную
        # плитку и ни одной записи, почему. Теперь причина видна.
        _telemetry.record("ad_creative_reuse", False, note="count_after_pick",
                          diag={"count": after_pick, "picks": log[-3:]})
        return REUSE_DIRTY

    # Дальше сетка уже изменена. Любой отказ здесь — либо чистое отступление
    # (мы сняли выбор и сетка как была), либо `dirty`. Третьего не дано, и
    # молча возвращать «не получилось» больше нельзя.
    try:
        advanced = False
        for root in _roots(page):
            dialog = root.get_by_role("dialog").first
            if not dialog.is_visible(timeout=1000):
                continue
            next_btn = dialog.get_by_role("button", name="Next").first
            if next_btn.count() and not next_btn.is_disabled():
                next_btn.click(timeout=5000)
                _sleep_h(1.0)
                advanced = True
            break
        if not advanced:
            if _wizard_past_media(page):
                return _attached_at_text(page, filename, log, "no_next_but_at_text")
            _telemetry.record("ad_creative_reuse", None, note="next_not_pressed",
                              diag={"picks": log[-3:]})
            return _retreat(page, tiles, clicked, before)
        # Next после нашего выбора перевёл мастер на Text — значит, Meta
        # приняла медиа: без него шаг Text не открывается.
        at_text = _wizard_past_media(page)
        _close_creative_setup_modal(page)
        _sleep_h(0.5)
        if "/ads/edit/" in page.url and _verify_ad_media_present(page):
            right, detail = _verify_ad_media_is_the_file(page, filename)
            ad_thumbs = _ad_media_thumb_ids(page)
            if picked_thumbs and ad_thumbs:
                if set(picked_thumbs) & set(ad_thumbs):
                    detail["thumb"] = "match"
                else:
                    # Миниатюра на объявлении не та, что у выбранной плитки:
                    # прикрепилось другое видео, как бы ни совпало имя.
                    right = False
                    detail["why"] = "other_file_attached"
                    detail["thumb"] = "mismatch"
            else:
                detail["thumb"] = "unconfirmed"
            detail["thumbs"] = {"picked": picked_thumbs[:5], "ad": ad_thumbs[:5]}
            media_fp = _ad_media_fingerprint(page)
            detail["identity"] = {
                "tile": picked_fingerprint[:10], "ad": media_fp[:10],
                "overlap": sorted(set(picked_fingerprint) & set(media_fp))[:10],
            }
            if not right:
                # Не тот файл или файл не обработался — и он уже стоит в
                # объявлении, а сетки медиатеки на экране нет. «Откатывать» тут
                # нечего: загрузка поверх дала бы объявление с чужим или битым
                # креативом. Отказ шага, без загрузки (ревью ACL-1192).
                try:
                    from src.fb_ads_rpa import page_capture as _pages

                    detail["page"] = _pages.capture(
                        page, step="ad_media_" + str(detail.get("why")), diag=detail,
                        blocks={"media": _AD_MEDIA_BLOCK_JS})
                except Exception:
                    pass
                _telemetry.record("ad_creative_reuse", False,
                                  note=str(detail.get("why")), diag=detail)
                return REUSE_DIRTY
            try:
                from src.fb_ads_rpa import page_capture as _pages

                _pages.capture_on_success(page, step="ad_media_ready", diag=detail,
                               blocks={"media": _AD_MEDIA_BLOCK_JS})
            except Exception:
                pass
            return REUSE_ATTACHED
        if at_text:
            # Мастер дошёл до Text после НАШЕГО выбора, а страница объявления
            # медиа (ещё) не показывает — превью у Meta догружается позже.
            # Отступать некуда и незачем: выбор принят.
            return _attached_at_text(page, filename, log, "next_to_text")
        # Мастер продвинулся, а медиа на объявлении нет. Выбор при этом
        # где-то остался, и снять его отсюда уже нечем: сетки на экране нет.
        _telemetry.record("ad_creative_reuse", False, note="dirty_after_advance",
                          diag={"picks": log[-3:], "url_is_ad": "/ads/edit/" in (page.url or "")})
        return REUSE_DIRTY
    except Exception as exc:
        # Next перерисовал диалог прямо под кликом — частый случай, когда
        # мастер ушёл на Text. Тогда медиа уже принято, откатывать нечего.
        if _wizard_past_media(page):
            return _attached_at_text(page, filename, log, "exception_but_at_text")
        _telemetry.record("ad_creative_reuse", None, note="exception:" + type(exc).__name__,
                          diag={"error": str(exc)[:300], "picks": log[-3:]})
        return _retreat(page, tiles, clicked, before)


def _attached_at_text(page: Page, filename: str, log: list[dict], how: str) -> str:
    """Мастер на шаге Text после нашего выбора: медиа прикреплено.

    ACL-1313: раньше в этом состоянии поток «отступал» — пытался снять выбор
    в сетке, которой на экране уже нет, и получал `dirty`, то есть отказ
    шага при медиа, стоящем в объявлении.
    """
    try:
        _close_creative_setup_modal(page)
    except Exception:
        pass
    _telemetry.record("ad_creative_reuse", True, note="wizard_at_text_after_pick",
                      diag={"file": filename, "via": how, "picks": log[-3:],
                            "page_shows_media": _media_block_filled_now(page)})
    return REUSE_ATTACHED


def _retreat(page: Page, tiles, clicked: list[int], before: int) -> str:
    """Отступить так, чтобы за нами можно было грузить файл.

    Отдельной функцией, потому что зовётся из трёх мест, и в каждом соблазн
    написать `return False` — ровно та ошибка, которая стоила отключения всей
    возможности 24.08.
    """
    clean = (before == 0 and _library_selected_count(page) == 0) if not clicked else _library_undo_selection(page, tiles, clicked, before)
    if not clean:
        _telemetry.record("ad_creative_reuse", False, note="retreat_left_selection",
                          diag={"clicked": clicked, "before": before})
        return REUSE_DIRTY
    try:
        _close_creative_setup_modal(page)
    except Exception as exc:
        _telemetry.record("ad_creative_reuse", False, note="retreat_close_failed:" + type(exc).__name__,
                          diag={"error": str(exc)[:300]})
        return REUSE_DIRTY
    return REUSE_ABSENT


def reuse_before_upload(page: Page, creative_path: str) -> bool | None:
    """Брать ли копию из медиатеки, и что делать, если взять не вышло.

    Возвращает `True` — прикрепили, шаг закончен; `False` — шаг обязан
    отказать; `None` — грузить файл как обычно.

    Отдельной функцией, а не тремя `if` посреди двухсотстрочной загрузки,
    потому что решается здесь ровно тот вопрос, из-за которого возможность
    выключали 24.08: можно ли после неудачи класть файл поверх. Проверить это
    внутри `upload_ad_creative` нельзя — до неё сто восемьдесят строк работы
    с живым мастером, и без браузера туда не добраться. Ошибка, которую
    стоило отключения, жила именно в такой непроверяемой ветке.
    """
    if not get_reuse_library_media():
        return None
    outcome = _try_reuse_library_media(page, Path(creative_path).name)
    if outcome == REUSE_ATTACHED:
        _telemetry.record("ad_creative_reuse", True, note="attached_from_library")
        return True
    if outcome == REUSE_DIRTY:
        # Сетка осталась с нашим выбором. Загрузка поверх дала бы объявление
        # с несколькими креативами — то самое, из-за чего возможность
        # выключили 24.08. Отказ шага хуже для прогона, но его видно, а
        # лишний креатив не видно нигде, кроме глаз в кабинете.
        #
        # Запись об этом уже сделана внутри, вместе с дневником выбора: по
        # какой плитке кликали, сработал ли клик, что показывал счётчик.
        # Здесь — только решение, без второй строки в телеметрии.
        return False
    # `REUSE_ABSENT` о себе уже отчитался — и подробно: `_try_reuse_library_media`
    # пишет, что именно видел в сетке, когда решил, что копии нет. Вторая запись
    # здесь была бы дублем, а в телеметрии дубль хуже молчания: он делает вид,
    # что событий два.
    return None


def upload_ad_creative_with_retry(
    page: Page, creative_type: str, creative_path: str, *, edit_url: str = "", attempts: int = 2
) -> bool:
    """upload_ad_creative plus one clean retry.

    Confirmed live 2026-07-29 (Test145): the step can fail on a
    nondeterministic render — the "Set up creative" button simply never
    became actionable within its wait on one ad, while the identical
    sequence worked on the ads before and after it. Between attempts:
    close any half-open creative wizard, then check whether the media in
    fact landed despite the reported failure (a retry would otherwise
    report False forever — once media is attached, the "Set up creative"
    button this function waits for no longer exists).
    """
    # Повторно тот же файл не заливаем — ни после неудачи, ни после удачи.
    #
    # После неудачи очевидно. После УДАЧИ — потому что Meta не принимает
    # вторую заливку того же файла в тот же кабинет: первое объявление
    # загружается, все следующие висят на `progress: 0` и отказывают
    # (проверено на двух кампаниях 15.09). Замер того же дня:
    # `ad2_creative_upload` — 130 секунд в среднем и сто процентов впустую,
    # а всего на заведомо безнадёжные заливки ушло 35 минут из 83.
    #
    # Медиатеку спрашиваем в обоих случаях и ДО отказа: копия могла появиться
    # от соседнего объявления, и тогда файл вообще никуда не поедет — это
    # единственный работающий путь для объявлений 2 и 3.
    known = upload_already_failed(creative_path) or upload_already_succeeded(creative_path)
    if known:
        # Through the wizard, not straight at the library. The library lives
        # on the wizard's Media step; asked on the bare ad page it has no
        # search box and no tiles, and answered "not_in_library" for every
        # second ad of 2026-09-16 (three batches, run 26f3ab66f66f among
        # them) — the copy was there, the dialog was not.
        try:
            attached = upload_ad_creative(
                page, creative_type, creative_path, edit_url=edit_url, reuse_only=True
            )
        except Exception as exc:
            # Молча глотать нельзя: 25.09 GT FX ad3 отказал за 15 с, а в
            # телеметрии осталась одна строка «уже загружали» — без причины.
            _telemetry.record("ad_creative_upload", False, note="exception:" + type(exc).__name__,
                              diag={"file": Path(creative_path).name, "error": str(exc)[:300],
                                    "path": "known_file_reuse"})
            attached = False
        if attached:
            return True
        # 17.09 04:30 (S15, 3-е объявление): медиа уже стояло — Meta перенесла
        # ролик в новое объявление, мастер не открылся, и шаг объявил
        # объявление пустым, хотя на экране «Uploaded media · 1 selected».
        # Файл здесь тот же, что уже поднят в этом прогоне, — медиа на месте.
        if _media_block_filled_now(page):
            _telemetry.record("ad_creative_upload", True, note="media_already_on_ad",
                              diag={"file": Path(creative_path).name})
            return True
        if _wizard_past_media(page):
            _close_creative_setup_modal(page)
            _telemetry.record("ad_creative_upload", True, note="media_already_on_ad_wizard_at_text",
                              diag={"file": Path(creative_path).name,
                                    "page_shows_media": _media_block_filled_now(page)})
            return True
        why = ("same_file_already_uploaded_this_run"
               if upload_already_succeeded(creative_path)
               else "same_file_already_failed_this_run")
        _telemetry.record(
            "ad_creative_upload", False, note=why,
            diag={"file": Path(creative_path).name},
        )
        return False

    for attempt in range(attempts):
        try:
            if upload_ad_creative(page, creative_type, creative_path, edit_url=edit_url):
                remember_upload_succeeded(creative_path)
                return True
        except Exception:
            pass
        _close_creative_setup_modal(page)
        _sleep_h(0.8)
        if _verify_ad_media_present(page):
            remember_upload_succeeded(creative_path)
            return True
        # Застрявшая загрузка — отдельная беда, и повтор на той же странице
        # её не лечит: состояние висит в этой же вкладке. Перезагружаем
        # редактор и пробуем заново. Объявление при этом не теряется — Meta
        # хранит черновик у себя (в снимке отказа 10 сентября видно «All
        # edits saved», имя объявления в поле и в дереве, статус «In draft»).
        if attempt + 1 < attempts and _upload_stalled(page):
            if not _upload_retry_can_reload(page, edit_url):
                _telemetry.record("creative_upload_stalled", False, note="reload_refused_unconfirmed_saved_draft")
                return False
            _telemetry.record("creative_upload_stalled", None, note="reload_and_retry")
            _reload_editor(page, edit_url)
    # Сдались — но убрать за собой обязаны. Незакрытый диалог перехватывает
    # ВСЕ последующие шаги: 15.09 один отказ загрузки превратился в четыре
    # (`ad_destination`, `ad2_name`, ...), у каждого в диагностике `dialogs: 1`,
    # и каждый стоил ещё 15-25 секунд ожидания вхолостую. Настоящая причина при
    # этом тонет среди одинаковых «не нашёл элемент».
    try:
        _close_creative_setup_modal(page)
    except Exception:
        pass
    _remember_upload_failed(creative_path)
    return False


#: Файлы, которые в этом прогоне уже не смогли загрузиться.
#:
#: Ключ — прогон и путь: у одной кампании три объявления с одним и тем же
#: креативом, и после провала на первом остальные два платили полную цену без
#: единого шанса. Замер 15.09: `ad2_creative_upload` — 178 секунд в среднем,
#: почти всё впустую; за день это 10 минут из 48.
_failed_uploads: set[tuple[str, str]] = set()


def _upload_memo_key(creative_path: str) -> tuple[str, str]:
    return (_telemetry._current.get("run_id") or "", str(creative_path or ""))


def _remember_upload_failed(creative_path: str) -> None:
    _failed_uploads.add(_upload_memo_key(creative_path))


#: Файлы, которые в этом прогоне уже успешно загрузились.
#:
#: Замер 15.09 по восьми живым прогонам: 83 минуты всего, **39 (47%) впустую**,
#: и почти всё — объявления 2 и 3, заливающие файл, который Meta уже не
#: примет. `ad2_creative_upload` — 130 секунд в среднем, **сто процентов
#: впустую**.
#:
#: Meta не берёт повторную заливку того же файла в тот же кабинет: первое
#: объявление загружается, все следующие висят на `progress: 0` и отказывают.
#: Проверено на двух кампаниях подряд. Значит второй раз пытаться незачем — не
#: ради экономии трафика, а потому что шанса нет.
_uploaded_ok: set[tuple[str, str]] = set()


def remember_upload_succeeded(creative_path: str) -> None:
    _uploaded_ok.add(_upload_memo_key(creative_path))


def upload_already_succeeded(creative_path: str) -> bool:
    """Этот файл уже уехал в этом прогоне?

    Как и у неудач, память живёт в пределах прогона: в новом прогоне кабинет,
    сеть и профиль другие, и переносить туда вывод нельзя.
    """
    return _upload_memo_key(creative_path) in _uploaded_ok


def upload_already_failed(creative_path: str) -> bool:
    """Этот файл уже провалился в этом прогоне?

    Память намеренно живёт в пределах прогона: между прогонами условия
    меняются (другой кабинет, другая сеть, перезапущенный профиль), и
    переносить туда приговор нельзя.
    """
    return _upload_memo_key(creative_path) in _failed_uploads


def _upload_retry_can_reload(page: Page, edit_url: str = "") -> bool:
    """Reload only the same confirmed, saved draft. An old screenshot saying
    'All edits saved' does not establish that the current upload can be retried.
    """
    from urllib.parse import parse_qs, urlsplit
    from src.fb_ads_rpa.wizard_guard import is_ads_manager_url

    try:
        current = str(page.url)
        target = edit_url or current
        if not is_ads_manager_url(current) or not is_ads_manager_url(target):
            return False
        current_url, target_url = urlsplit(current), urlsplit(target)
        # Never navigate to an earlier ad/campaign while retrying this upload.
        if current_url.path != target_url.path or parse_qs(current_url.query) != parse_qs(target_url.query):
            return False
        if not parse_qs(current_url.query).get('act'):
            return False
        for attempt in range(2):
            if attempt:
                _sleep_h(1.5, spread=0)
            if str(page.url) != current or _editor_is_busy_saving(page):
                return False
            saved = page.get_by_text(re.compile(r"^All edits saved$", re.I))
            if saved.count() != 1 or not saved.first.is_visible():
                return False
        return True
    except Exception:
        return False


def _upload_progress(page: Page) -> int | None:
    """Сколько процентов загрузки показывает страница, или `None`.

    Meta рисует настоящий индикатор:
    `<div role="progressbar" aria-valuenow="0" aria-valuemax="100">` — рядом с
    надписью «Uploading video …» и кнопкой Cancel.
    """
    try:
        val = page.evaluate(
            """
            () => {
              const bars = [...document.querySelectorAll('[role="progressbar"][aria-valuenow]')].filter(el => {
                const r = el.getBoundingClientRect();
                return r.width > 0 && r.height > 0 && !el.closest('[aria-hidden="true"]')
                  && getComputedStyle(el).visibility !== 'hidden';
              });
              return bars.length === 1 ? bars[0].getAttribute('aria-valuenow') : null;
            }
            """
        )
        return int(val) if val is not None else None
    except Exception:
        return None


def _upload_stalled(page: Page, *, watch_s: float = 4.0) -> bool:
    """Висит ли загрузка на месте — измеряем, а не предполагаем.

    10 сентября два объявления подряд простояли 537 и 327 секунд с надписью
    «Uploading video 2_ar_2221_story.mp4» на экране, и в обоих снимках отказа
    `aria-valuenow="0"`. Файл 25.8 МБ. То есть загрузка не шла медленно — она
    не начиналась вовсе, а надпись создавала видимость работы.
    Это же объясняет разброс, который вес объяснить не мог: те же 25 МБ в
    других попытках уходили за 62 и 90 секунд.

    Одного нуля мало: ноль бывает и в первую секунду настоящей загрузки.
    Поэтому смотрим дважды и считаем застоем только неподвижность.
    """
    if not _upload_in_progress(page):
        return False
    before = _upload_progress(page)
    if before is None:
        return False
    time.sleep(watch_s)
    if not _upload_in_progress(page):
        return False
    return _upload_progress(page) == before


def _recover_from_browser_error(page: Page, *, attempts: int = 2) -> bool:
    """Открыта страница-ошибка браузера — обновить и посмотреть, ушла ли она.

    Правило «застряло — попробуй обновить» применяется здесь, а не ко всякому
    отказу подряд, и разница существенная. На странице-ошибке ничего не
    введено: это не страница Meta, а экран Chrome, и терять при перезагрузке
    нечего. Посреди мастера создания кампании та же перезагрузка стёрла бы
    несохранённые поля и превратила бы один отказ в потерянную кампанию.

    Возвращает True, если ошибка была (и, возможно, ушла) — чтобы это попало
    в телеметрию: связь рвалась, и знать об этом важнее, чем о том, что
    следующий шаг потом прошёл.

    10 сентября прогон встал между кампаниями на ERR_SSL_PROTOCOL_ERROR и
    вторая кампания не началась вовсе. На проксированном профиле такая ошибка
    почти всегда означает сам прокси; перезагрузка лечит разовый разрыв, а
    если прокси лежит — отказ придёт честно и с названной причиной.
    """
    seen = ""
    for _ in range(attempts):
        code = _telemetry.browser_error(page)
        if not code:
            return bool(seen)
        seen = code
        _telemetry.record("browser_error", None, note=code)
        _reload_editor(page)
        _sleep_h(1.0)
    return bool(seen) and not _telemetry.browser_error(page)


def _reload_editor(page: Page, edit_url: str = "") -> None:
    """Перезагрузить редактор объявления, сбросив зависшее состояние вкладки.

    Это перезагрузка страницы внутри того же профиля, а не перезапуск
    браузера: сессия, профиль и прокси остаются на месте.
    """
    try:
        if edit_url:
            page.goto(edit_url, wait_until="domcontentloaded", timeout=60000)
        else:
            page.reload(wait_until="domcontentloaded", timeout=60000)
    except Exception:
        return
    _sleep_h(1.5)


def _click_xpath(page: Page, xpath: str, *, scroll: bool = True) -> bool:
    """Measure an element's centre and CDP-click it in one shot.

    The ad editor re-renders continuously: a handle obtained from
    query_selector goes stale within milliseconds, so `bounding_box()` was
    observed returning None *after* `_clickable` had already verified a box
    on the very same element (measured 2026-08-04). Holding any handle
    across the measure→click gap is therefore unsafe. This resolves the
    element, scrolls it into view and reads its centre inside a SINGLE
    page.evaluate, then dispatches a trusted CDP click at those
    coordinates — nothing survives the round trip except two numbers.

    Picks the smallest matching box, since `contains()` XPaths also match
    layout wrappers. Pass a bare XPath (no "xpath=" prefix).
    """
    js = """
    (args) => {
      const [xp, doScroll] = args;
      const it = document.evaluate(xp, document, null,
        XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
      // Visibility by computed style, not by `offsetParent`: an element
      // inside a `position: fixed` container has offsetParent === null
      // while being perfectly visible. The Advantage+ row sits in the
      // fixed right-hand panel of the ad set editor — measured 2026-09-16:
      // the same XPath clicked it on the saved page (no site CSS, nothing
      // fixed) and matched nothing live, four runs in a row.
      const shown = (e) => {
        for (let n = e; n; n = n.parentElement) {
          const s = getComputedStyle(n);
          if (s.display === 'none' || s.visibility === 'hidden') return false;
        }
        return true;
      };
      let best = null, bestArea = Infinity;
      for (let i = 0; i < it.snapshotLength; i++) {
        const el = it.snapshotItem(i);
        if (!el || !shown(el)) continue;
        const r = el.getBoundingClientRect();
        if (r.width <= 0 || r.height <= 0) continue;
        const area = r.width * r.height;
        if (area < bestArea) { best = el; bestArea = area; }
      }
      if (!best) return null;
      if (doScroll) best.scrollIntoView({ block: 'center', behavior: 'instant' });
      const r = best.getBoundingClientRect();
      if (r.width <= 0 || r.height <= 0) return null;
      if (r.bottom < 0 || r.top > innerHeight) return null;
      return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
    }
    """
    try:
        pt = page.evaluate(js, [xpath, scroll])
    except Exception:
        return False
    if not pt:
        return False
    try:
        _cdp_mouse_click(page, pt["x"], pt["y"])
        return True
    except Exception:
        return False


def _clickable(page: Page, xpath: str):
    """First handle matching `xpath` that has a real, on-screen click box.

    `contains(normalize-space(.), 'X')` matches every ancestor whose subtree
    contains the text, and `query_selector` hands back the OUTERMOST one —
    typically a layout wrapper with no box at all. Measured live 2026-08-04
    on the "Set up creative" trigger: handle found, isConnected true,
    bounding_box() None, so _click_trusted fell through to a DOM click that
    reported success and did nothing. Pick the smallest boxed candidate
    instead — that is the actual control.
    """
    try:
        handles = page.query_selector_all(xpath)
    except Exception:
        return None
    best = None
    best_area = None
    for h in handles:
        try:
            box = h.bounding_box()
        except Exception:
            box = None
        if not box:
            continue
        w, hgt = box.get("width", 0), box.get("height", 0)
        if w <= 0 or hgt <= 0:
            continue
        area = w * hgt
        if best_area is None or area < best_area:
            best, best_area = h, area
    return best


_MOUNT_FRACS = (0.4, 0.55, 0.7, 0.85, 0.3, 0.6, 1.0)
_MOUNT_POLL_S = 0.15


def _scroll_until_mounted(
    page: Page,
    present_js: str,
    *,
    fracs: tuple[float, ...] = _MOUNT_FRACS,
    settle_s: float = 0.9,
) -> bool:
    """Scroll the editor's main container until `present_js` reports True.

    Meta's ad editor virtualises its sections: a control is absent from the
    DOM (not merely hidden) unless the container sits near it. Established
    live three separate times on 2026-08-02/03 — the Call to action
    combobox, the Browser add-ons radio group, and the "Set up creative"
    menu trigger all behaved this way, and in every case a blind
    `scrollTop = scrollHeight` was what hid them: the bottom of the page
    unmounts the middle. Stepping fractions and checking after each is the
    pattern that works. Returns True if the control mounted.
    """
    scroll_js = """
    (frac) => {
      const c = [...document.querySelectorAll('*')].find((e) => {
        const st = getComputedStyle(e);
        return (st.overflowY === 'auto' || st.overflowY === 'scroll')
          && e.scrollHeight > e.clientHeight + 50 && e.scrollHeight > 1500;
      });
      if (!c) return false;
      c.scrollTop = Math.floor(c.scrollHeight * frac);
      return true;
    }
    """
    for frac in fracs:
        try:
            if not page.evaluate(scroll_js, frac):
                return False  # no scroll container — nothing to do
            # ACL-1313: не «подождать settle_s и посмотреть», а смотреть всё
            # это время. Прокрутка — присваивание scrollTop, не событие ввода,
            # так что человеческая пауза тут ничего не изображает; секция,
            # смонтировавшаяся за 0,15 с, больше не ждёт остаток 0,9 с.
            settle_until = time.time() + settle_s
            while True:
                time.sleep(_MOUNT_POLL_S)
                if page.evaluate(present_js):
                    return True
                if time.time() >= settle_until:
                    break
        except Exception:
            continue
    return False


def _creative_wizard_open(page: Page) -> bool:
    """Is the creative wizard on screen?

    Meta reshaped this modal on 2026-08-24: the "Set up your creative"
    title is GONE, the panel became a narrow centred modal, and the step
    list moved from a vertical column on the left to a horizontal row
    across the top. Keying on the old title made the code open the wizard
    and then fail to notice it had — every creative upload in the runs of
    24.08 died that way while the screenshot clearly showed it open.

    "Creative setup" alone is NOT enough: it also appears on the plain ad
    page ("Creative setup (0/1)"). Require several sibling step labels
    that exist only inside the wizard, plus one of its own buttons.

    Module-level since 2026-09-16 so the force-close can ask the same
    question the walk asks; it used to be a closure inside
    `upload_ad_creative`, and the force-close still looked for the title
    that was gone — which is how a wizard on its Trim step stayed open
    through the whole rest of the ad (run b31b45bc8599).
    """
    try:
        return bool(page.evaluate(
            r"""
            () => {
              const norm = (x) => (x || '').replace(/\s+/g, ' ').trim();
              const vis = [...document.querySelectorAll('*')]
                .filter(e => e.offsetParent !== null && e.children.length === 0)
                .map(e => norm(e.textContent));
              if (vis.includes('Set up your creative')) return true;  // legacy shape
              // «Creative generation» — новое имя шага «Image generation»
              // (строка шагов 25.09: Creative setup · Media · Text ·
              // Creative generation · Enhancements · Translation).
              const steps = ['Creative setup', 'Media', 'Text', 'Image generation',
                             'Creative generation', 'Enhancements', 'Translation'];
              const hits = steps.filter(t => vis.includes(t)).length;
              const nav = ['Next', 'Cancel', 'Skip and continue', 'Continue', 'Done', 'Save'];
              const hasNav = [...document.querySelectorAll('[role=button],button')]
                .filter(e => e.offsetParent !== null)
                .some(e => nav.includes(norm(e.textContent)));
              return hits >= 3 && hasNav;
            }
            """
        ))
    except Exception:
        return False


def _creative_post_media_step(page: Page) -> bool:
    """The observed Trim/Crop screen with Skip and continue (ACL-1207).

    This can also be left open from a previous attempt. Callers must observe
    a transition after feeding THIS upload, not treat its presence as identity.
    """
    if not _creative_wizard_open(page):
        return False
    try:
        return bool(page.evaluate(r"""() => {
          const norm = s => (s || '').replace(/\s+/g, ' ').trim();
          const visible = [...document.querySelectorAll('*')]
            .filter(e => e.offsetParent !== null && e.children.length === 0)
            .map(e => norm(e.textContent));
          return visible.some(t => t === 'Trim' || t === 'Crop')
            && [...document.querySelectorAll('button,[role=button]')].some(e =>
              e.offsetParent !== null && !e.disabled && e.getAttribute('aria-disabled') !== 'true'
              && norm(e.textContent) === 'Skip and continue');
        }"""))
    except Exception:
        return False


#: What the wizard's forward button reads, step by step. "Done"/"Save"/"Next"
#: were the whole list until 2026-09-16; the Trim step that a fresh video
#: upload now opens reads **"Skip and continue"**, and the walk pressed
#: nothing four rounds in a row, gave up, and left the modal on screen
#: (screenshot 094000_recorded_fail_ad_creative_upload.png: "Media ✓ · Trim",
#: Skip and continue). Order matters only as a tie-break within one step.
_WIZARD_ADVANCE_LABELS = ("Done", "Save", "Next", "Skip and continue", "Continue", "Skip")


#: Кнопка «вперёд» мастера — одним вызовом страницы (ACL-1313).
#:
#: Было: по одному `_click_xpath` на каждую подпись из шести, и каждый —
#: XPath `//*[...][normalize-space(.)='…']` по ВСЕМУ документу редактора
#: (строковое значение каждого элемента). На шаге, где кнопка читается
#: «Skip and continue», это четыре пустых прохода по DOM до нужного на
#: каждом круге обхода. Теперь один проход: те же кандидаты
#: (`[role=button]`, `button`, без `aria-disabled="true"`), та же нормализация
#: пробелов, что у XPath `normalize-space`, тот же порядок подписей и та же
#: самая маленькая видимая рамка; клик — тем же CDP-кликом.
_WIZARD_FORWARD_JS = r"""
(labels) => {
  const norm = (s) => (s || '').replace(/[ \t\r\n]+/g, ' ').trim();
  const shown = (e) => {
    for (let n = e; n; n = n.parentElement) {
      const s = getComputedStyle(n);
      if (s.display === 'none' || s.visibility === 'hidden') return false;
    }
    return true;
  };
  const cands = [...document.querySelectorAll('[role=button],button')]
    .filter(e => e.getAttribute('aria-disabled') !== 'true');
  for (const label of labels) {
    let best = null, bestArea = Infinity;
    for (const el of cands) {
      if (norm(el.textContent) !== label || !shown(el)) continue;
      const r = el.getBoundingClientRect();
      if (r.width <= 0 || r.height <= 0) continue;
      const area = r.width * r.height;
      if (area < bestArea) { best = el; bestArea = area; }
    }
    if (!best) continue;
    best.scrollIntoView({ block: 'center', behavior: 'instant' });
    const r = best.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) continue;
    if (r.bottom < 0 || r.top > innerHeight) continue;
    return { label: label, x: r.x + r.width / 2, y: r.y + r.height / 2 };
  }
  return null;
}
"""

#: Сколько после нажатия «вперёд» ждать следующего шага. Прежняя пауза
#: 1,4 с сохранена как потолок; мастер, закрывшийся раньше, замечается
#: опросом, и обход кончается сразу, а не через полторы секунды.
_WIZARD_STEP_SETTLE_S = 1.4
_WIZARD_CLOSE_POLL_S = 0.35


def _press_wizard_forward(page: Page) -> str | None:
    """Нажать кнопку «вперёд» мастера. Подпись нажатой или None."""
    try:
        pt = page.evaluate(_WIZARD_FORWARD_JS, list(_WIZARD_ADVANCE_LABELS))
    except Exception:
        return None
    if not pt:
        return None
    try:
        _cdp_mouse_click(page, pt["x"], pt["y"])
    except Exception:
        return None
    return pt.get("label") or ""


def _walk_creative_wizard(page: Page, *, rounds: int = 16) -> bool:
    """Press the wizard forward until it closes. True when it is gone.

    Each round clicks the one enabled forward button this step shows, then
    asks the SAME detector that opened the wizard whether it is still there.
    A transient render can leave no enabled button for a beat — measured
    live 2026-08-04: rounds where the selection counter briefly vanished
    still had Next a moment later — so a miss is counted, not acted on, and
    only four in a row end the walk.
    """
    misses = 0
    for _ in range(rounds):
        if not _creative_wizard_open(page):
            return True
        if _press_wizard_forward(page) is None:
            misses += 1
            if misses >= 4:
                break
            _sleep_h(1.2)
            continue
        misses = 0
        # Следующий шаг отрисовывается до 1,4 с; закрытие видно раньше.
        settle_until = time.time() + _WIZARD_STEP_SETTLE_S
        while time.time() < settle_until:
            time.sleep(_WIZARD_CLOSE_POLL_S)   # опрос, не действие: без «человеческой» паузы
            if not _creative_wizard_open(page):
                return True
    return not _creative_wizard_open(page)


def _click_modal_corner_icon(page: Page, anchor_text: str) -> bool:
    """CDP-click the small icon nearest the top-right corner of the modal
    that contains a visible leaf reading `anchor_text`.

    The modal is not a role=dialog; it is found by walking up from the
    anchor to the ancestor whose box covers most of the viewport. A
    page-wide `[aria-label="Close"]` search was seen (2026-07-26) hitting the
    whole ad editor's close button instead, so the search stays inside.
    """
    try:
        box = page.evaluate(
            """
            (anchor) => {
              const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
              const all = [...document.querySelectorAll('*')];
              const heading = all.find(e => e.children.length === 0
                && norm(e.textContent) === anchor && e.offsetParent !== null);
              if (!heading) return null;
              let modal = heading;
              for (let i = 0; i < 12 && modal; i++) {
                const r = modal.getBoundingClientRect();
                if (r.width > window.innerWidth * 0.5 && r.height > window.innerHeight * 0.5) break;
                modal = modal.parentElement;
              }
              if (!modal) return null;
              const mr = modal.getBoundingClientRect();
              let best = null, bestDist = Infinity;
              for (const el of modal.querySelectorAll('div, span, svg, button, [role=button]')) {
                if (!el.offsetParent || el.children.length > 1) continue;
                const r = el.getBoundingClientRect();
                if (r.width < 8 || r.width > 40 || r.height < 8 || r.height > 40) continue;
                const dist = Math.hypot(r.x - mr.right, r.y - mr.top);
                if (dist < bestDist) { bestDist = dist; best = r; }
              }
              if (!best) return null;
              return { x: best.x + best.width / 2, y: best.y + best.height / 2 };
            }
            """,
            anchor_text,
        )
    except Exception:
        return False
    if not box:
        return False
    _cdp_mouse_click(page, box["x"], box["y"])
    _sleep_h(0.8)
    return True


_SITE_LINKS_MODAL_JS = r"""
  () => {
    const norm = (s) => (s || '').replace(/[\u200b-\u200d\ufeff]/g, '').replace(/\s+/g, ' ').trim();
    const shown = (e) => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
    const drawerClose = (b) => b.getAttribute('aria-label') === 'Close' && (() => {
      const s = b.closest('[data-surface]');
      return !!s && s.getAttribute('data-surface') === '/am/editor_drawer';
    })();
    const anchor = [...document.querySelectorAll('*')].find((e) =>
      e.children.length === 0 && /^(Site links|Extensions)$/.test(norm(e.textContent)) && shown(e));
    if (!anchor) return '';
    const btn = (box, label) => [...box.querySelectorAll('[role=button], button')]
      .find((b) => shown(b) && norm(b.textContent) === label);
    let box = anchor;
    for (let i = 0; i < 12 && box.parentElement; i++) {
      box = box.parentElement;
      if ([...box.querySelectorAll('[aria-label="Close"]')].some(drawerClose)) return '';
      const cancel = btn(box, 'Cancel');
      if (cancel && btn(box, 'Save')) {
        // Мастер креатива со своими Next/Done — это не то окно.
        if (btn(box, 'Next') || btn(box, 'Done')) return '';
        cancel.setAttribute('data-acl-close', 'sitelinks');
        return 'cancel';
      }
    }
    return '';
  }
"""
"""Окно «Creative setup → Extensions / Site links» (Cancel/Save): пометить Cancel."""


def _cancel_site_links_modal(page: Page) -> bool:
    """Закрыть окно «Extensions / Site links» кнопкой Cancel. True — закрыли.

    21.09, ad2_copy: поверх объявления стояло окно «Creative setup» на
    разделе Extensions / Site links с кнопками Cancel и Save; мастер его не
    узнавал, и шаг текста упирался в модалку. Save не жмём никогда — он
    сохранил бы пустые ссылки в объявление.
    """
    try:
        found = page.evaluate(_SITE_LINKS_MODAL_JS)
    except Exception:
        return False
    if found != "cancel":
        return False
    clicked = _click_close_mark(page, '[data-acl-close="sitelinks"]')
    try:
        page.evaluate("() => document.querySelectorAll('[data-acl-close]')"
                      ".forEach(e => e.removeAttribute('data-acl-close'))")
    except Exception:
        pass
    _sleep_h(0.6)
    _telemetry.record("creative_setup_modal", clicked, note="site_links_cancelled")
    return clicked


def _close_creative_setup_modal(page: Page) -> bool:
    """Force-close the creative wizard, whichever shape and step it is on.

    Two shapes. The legacy one has a "Set up your creative" heading and is
    closed by its own top-right icon. The 2026-08-24 shape has no such
    heading — it is recognised by its step row — and is first walked
    forward (the forward button keeps the media applied), then, if it
    still stands, closed by its corner icon the same way.

    Until 2026-09-16 only the legacy branch existed. On the new shape this
    returned False without touching anything, so the retry between upload
    attempts never closed the wizard, `_verify_ad_media_present` could not
    see "Edit media" behind it, and every step after the upload failed
    under a modal that the log recorded only as `dialogs: 1`.

    Confirmed live (2026-07-29) for the legacy shape: closing mid-wizard
    (e.g. on the Enhancements step, after picking an existing media-library
    thumbnail) keeps the current media selection applied to the ad rather
    than discarding it.

    This modal isn't a role=dialog, so it's scoped by walking up from the
    "Set up your creative" heading to its own modal container (the
    ancestor whose bounding box covers most of the viewport) rather than a
    page-wide `[aria-label="Close"]` search — confirmed live (2026-07-26)
    that a page-wide search can hit the whole ad editor's close button
    instead of a toast/modal-local one. Confirmed live (2026-07-29): doing
    this even mid-wizard (e.g. on the Enhancements step, after picking an
    existing media-library thumbnail) keeps the current media selection
    applied to the ad rather than discarding it.
    """
    legacy = False
    try:
        modal_heading = page.get_by_text("Set up your creative", exact=True).first
        legacy = bool(modal_heading.count()) and modal_heading.is_visible(timeout=1500)
    except Exception:
        legacy = False
    if legacy:
        return _click_modal_corner_icon(page, "Set up your creative")

    # Окно Extensions / Site links: только Cancel, до обхода мастера — обход
    # жал бы в нём Save.
    if _cancel_site_links_modal(page):
        return True

    if not _creative_wizard_open(page):
        return False
    if _walk_creative_wizard(page):
        return True
    # Walking did not get it closed — the step shows a button we do not know.
    # The corner icon is the way out that keeps the media; the step row's
    # first label anchors the search since the heading is gone.
    if _click_modal_corner_icon(page, "Creative setup"):
        return not _creative_wizard_open(page)
    return False


def _scroll_xpath_into_view(page: Page, xpath: str) -> dict | None:
    """Прокрутить к самому маленькому видимому совпадению `xpath`.

    Возвращает, что увидели: доступна ли кнопка и попала ли она в окно, —
    или None, если совпадения нет (секция редактора размонтирована).
    """
    try:
        return page.evaluate(
            """
            (xp) => {
              const it = document.evaluate(xp, document, null,
                XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
              let best = null, bestArea = Infinity;
              for (let i = 0; i < it.snapshotLength; i++) {
                const el = it.snapshotItem(i);
                const r = el.getBoundingClientRect();
                if (r.width <= 0 || r.height <= 0) continue;
                if (r.width * r.height < bestArea) { best = el; bestArea = r.width * r.height; }
              }
              if (!best) return null;
              best.scrollIntoView({ block: 'center', behavior: 'instant' });
              const r = best.getBoundingClientRect();
              return {
                enabled: best.getAttribute('aria-disabled') !== 'true' && !best.disabled,
                in_view: r.bottom > 0 && r.top < innerHeight,
              };
            }
            """,
            xpath,
        )
    except Exception:
        return None


_SET_UP_CREATIVE_PRESENT_JS = r"""
() => [...document.querySelectorAll('[role=button],button')]
  .some(e => e.offsetParent !== null
        && /Set up creative/i.test((e.textContent || '').replace(/\s+/g, ' ').trim()))
"""


def _open_creative_wizard(page: Page, trigger_xpath: str, item_xpath: str, *,
                          attempts: int = 4) -> tuple[bool, list[dict]]:
    """Открыть мастер креатива: «Set up creative» → «Video ad»/«Image ad».

    ACL-1313, 25.09 GT FX (5b223701b271, ad3): кнопка была на месте и
    доступна, а мастер за четыре попытки (~15 с) так и не открылся. На
    экране в тот момент висел «Verifying your edits…», превью крутилось:
    редактор ещё сохранял предыдущий шаг, и клики по кнопке уходили в
    перерисовку. Поэтому:

    * до первой попытки ждём автосейв (`wait_editor_saved`);
    * перед каждой попыткой прокручиваем к кнопке — секции виртуальные, и
      кнопка могла уехать или размонтироваться;
    * между попытками ждём, пока погаснет занятый футер;
    * если клик по координатам из XPath меню не открыл — второй клик через
      CDP по рамке самого элемента (`_clickable` + `_click_trusted`).

    Возвращает (открыт ли мастер, дневник попыток) — дневник уходит в
    телеметрию, если открыть не вышло.
    """
    log: list[dict] = []
    remounted = False
    try:
        wait_editor_saved(page, timeout_s=15)
    except Exception:
        pass
    for attempt in range(attempts):
        if _creative_wizard_open(page):
            return True, log
        note: dict = {"try": attempt}
        if attempt:
            try:
                wait_editor_saved(page, timeout_s=10, appear_s=0)
            except Exception:
                pass
        note["busy"] = _editor_is_busy_saving(page)
        seen = _scroll_xpath_into_view(page, trigger_xpath)
        if seen is None and not remounted:
            # Прокрутка по долям — дорогая (до ~6 с), поэтому один раз.
            remounted = True
            _scroll_until_mounted(page, _SET_UP_CREATIVE_PRESENT_JS)
            seen = _scroll_xpath_into_view(page, trigger_xpath)
        note["button"] = seen
        if not seen:
            log.append(note)
            if _media_block_filled_now(page):
                # Кнопки нет, потому что медиа уже стоит (Meta перенесла его
                # сама, 17.09) — долбить дальше незачем.
                note["media_on_ad"] = True
                break
            _sleep_h(0.8)
            continue
        _sleep_h(0.3)
        menu = False
        for how in ("xpath", "cdp_handle"):
            if how == "xpath":
                sent = _click_xpath(page, trigger_xpath)
            else:
                sent = _click_trusted(page, _clickable(page, trigger_xpath))
            note[how] = sent
            if not sent:
                continue
            for _ in range(8):  # ~2.4s на отрисовку меню
                if page.query_selector("xpath=" + item_xpath) is not None:
                    menu = True
                    break
                if _creative_wizard_open(page):
                    note["opened_by"] = how
                    log.append(note)
                    return True, log
                _sleep_h(0.3)
            if menu:
                note["menu_by"] = how
                break
        if not menu:
            log.append(note)
            continue
        _click_xpath(page, item_xpath)
        for _ in range(10):  # ~3s на монтирование мастера
            if _creative_wizard_open(page):
                note["opened_by"] = note.get("menu_by")
                log.append(note)
                return True, log
            _sleep_h(0.3)
        note["wizard"] = False
        log.append(note)
    return _creative_wizard_open(page), log


def _reuse_upload_decision(page: Page, filename: str) -> bool | None:
    """Normalize the old boolean and pending explicit media-reuse contract.

    True means attached, None permits upload, False blocks it. Unknown outcomes
    fail closed; nonempty strings must never be interpreted as success.
    """
    result = _try_reuse_library_media(page, filename)
    if result is True or result == "attached":
        return True
    if result is False or result == "absent":
        return None
    return False


def _feed_mounted_file(page: Page, handle, creative_path: str, size_mb: float, diag: dict) -> bool:
    """Use the same size/timeout policy even when the input is already mounted."""
    if size_mb > _PW_FILE_LIMIT_MB:
        return _upload_heavy_via_chooser_event(
            page, creative_path, diag, lambda: handle.evaluate("el => el.click()") or True
        )
    handle.set_input_files(creative_path, timeout=_PW_SET_FILES_MS)
    return True


def upload_ad_creative(
    page: Page, creative_type: str, creative_path: str, *, edit_url: str = "",
    reuse_only: bool = False,
) -> bool:
    """Set up creative -> Video/Image ad -> Media -> upload the real file.

    Confirmed live (2026-07-25/26) via playwright-skill: the "Set up
    creative" modal's first step varies run to run — sometimes "Creative
    setup" (Source URL/Extensions) first, sometimes it opens straight on
    Media — so this checks for the Upload button rather than assuming a
    fixed number of clicks to get there. Also confirmed live: the first
    `[aria-label="Close"]` match after upload can be the WHOLE ad editor's
    close button, not a toast dismiss — clicking it blindly bounced the
    whole run back to the ads list. Recover via `edit_url` (the page's own
    /ads/edit/ URL captured right after the Next-to-ad-level click) instead
    of guessing a re-open control.
    """
    _set_last_upload_diag({"file": Path(creative_path).name, "attempts": []})
    is_video = (creative_type or "").strip().lower() == "video"
    option_label = "Video ad" if is_video else "Image ad"

    # The editor is virtualised: sections mount/unmount as the container
    # scrolls. The old code slammed scrollTop to scrollHeight ("scroll past
    # the Ad setup section", 2026-07-26) — but confirmed live 2026-08-03
    # (run 038fc1b6/b0ed965f, all 6 creative uploads soft-failed) that
    # bottom-scrolling now UNMOUNTS the Ad creative section entirely, so the
    # button is not merely invisible, it is absent from the DOM and the
    # 15s wait_for burns out. Same trap as the Call to action combobox and
    # the Browser add-ons group. Step through fractions and stop at the
    # first one where the control actually mounts.
    _scroll_until_mounted(
        page,
        """
        () => [...document.querySelectorAll('[role=button],button')]
          .some(e => e.offsetParent !== null
                && /Set up creative/i.test((e.textContent || '').replace(/\\s+/g, ' ').trim()))
        """,
    )

    # "Set up creative" is a MENU trigger on this UI variant (verified live
    # 2026-08-03: aria-haspopup="menu", items "Image ad" / "Video ad"), and
    # clicks must go through the trusted-coordinate path — Playwright's
    # actionability gate rejects the re-rendering control ("element is not
    # stable"), while a real mouse click at its centre works.
    #
    # CRITICAL (run 45b8ccb4/baee53d9, 2026-08-04): the handle must be
    # RE-QUERIED before every click. The panel re-renders continuously, a
    # handle captured once goes stale, and _click_trusted's fallback then
    # "clicks" a detached node — reported success, zero effect. The fail
    # screenshots show exactly that: menu closed, trigger untouched. So:
    # fresh query each attempt, and advance only on OBSERVED effect (menu
    # item painted, then wizard actually open) — never on "click sent".
    trigger_xpath = (
        "//*[@role='button' or self::button]"
        "[contains(normalize-space(.), 'Set up creative')]"
    )
    # Zero-width spaces infest this UI's labels ('Open​') — match on
    # contains, not equality.
    item_xpath = (
        "//*[@role='menuitem' or @role='option']"
        f"[contains(normalize-space(.), '{option_label}')]"
    )

    opened, tries = _open_creative_wizard(page, trigger_xpath, item_xpath)
    if not opened:
        # Раньше — молчаливое `return False`: 25.09 GT FX (5b223701b271, ad3)
        # четыре попытки за 15 с, и в телеметрии ни слова, почему. Теперь
        # видно, была ли кнопка, чем по ней били и не висел ли автосейв.
        _telemetry.record("ad_creative_upload", False, note="wizard_not_opened",
                          diag={"file": Path(creative_path).name, "tries": tries,
                                "busy": _editor_is_busy_saving(page)})
        return False
    _sleep_h(0.7)

    # Advance to the wizard's Media step.
    #
    # The wizard ("Set up your creative": Creative setup / Media / Trim /
    # Crop / Text / …) opens on Creative setup and is NOT a role="dialog"
    # any more — measured live 2026-08-03, it renders as a plain full-panel
    # overlay. The old loop scoped its Next lookup to role="dialog", found
    # nothing, and fell through to a page-wide Playwright click that the
    # re-rendering panel rejected; the step then failed at the Upload check.
    #
    # Prefer the wizard's own left-hand step list — clicking "Media"
    # directly is one action instead of an unknown number of Nexts — and
    # keep trusted Next clicks as the fallback.
    for _ in range(6):
        if page.get_by_role("button", name="Upload").count() > 0:
            break
        if _click_xpath(
            page,
            "//*[@role='tab' or @role='button' or @role='listitem' or @role='menuitem']"
            "[normalize-space(.)='Media']",
        ):
            _sleep_h(1.2)
            continue
        _click_xpath(
            page,
            "//*[@role='button' or self::button]"
            "[normalize-space(.)='Next' and not(@aria-disabled='true')]",
        )
        _sleep_h(1.2)

    # The advance loop above polls on a fixed cadence; the wizard's Media
    # step can finish painting just after the last round. Measured live
    # 2026-08-03: the function returned False at 19.1s while the Upload
    # button was present on the page moments later. Give it a real wait
    # before declaring failure instead of trusting the poll's last tick.
    upload_btn = page.get_by_role("button", name="Upload")
    if upload_btn.count() < 1:
        try:
            upload_btn.first.wait_for(state="attached", timeout=8000)
        except Exception:
            pass
    if upload_btn.count() < 1:
        # Мастер открыт, а шага Media с кнопкой Upload нет. Тоже было молча.
        _telemetry.record("ad_creative_upload", False, note="upload_button_missing",
                          diag={"file": Path(creative_path).name,
                                "past_media": _wizard_past_media(page)})
        return False

    # Media-reuse: confirmed live (2026-07-29) that the Media step's own
    # library grid (Account/Business/Video URL/Page tabs + existing
    # thumbnails) is already visible right next to the Upload button, even
    # on a brand-new ad — no extra "Edit media -> Change Video" navigation
    # needed. An earlier attempt (2026-07-26) reverted this over a
    # suspected "restricted" duplicate-thumbnail trap, but live retesting
    # (2026-07-28 diary, Test135 follow-up) showed clicking ANY thumbnail
    # for the filename, warning icon or not, produces a clean "1 selected"
    # — that concern doesn't hold. Only commit to this path if a thumbnail
    # for the exact filename is actually visible; otherwise fall straight
    # through to the real upload below, unchanged.
    # Note the `.name`, not `.stem`: the library captions each tile with the
    # full filename including extension ("2046_2.mp4"), so matching on the
    # stem finds nothing.
    verdict = reuse_before_upload(page, creative_path)
    if verdict is not None:
        return verdict
    if reuse_only:
        # Память прогона говорит, что файл уже уходил наверх: второй раз Meta
        # его не берёт, медиатека была единственным путём, и копии там нет.
        # Объявление оставляем как есть и называем причину.
        _close_creative_setup_modal(page)
        _telemetry.record("ad_creative_reuse", None, note="library_has_no_copy",
                          diag={"file": Path(creative_path).name})
        return False

    # Clear the library search box before uploading. The reuse attempt just
    # above types the filename into it; when reuse fails the filter STAYS,
    # and the freshly uploaded file lands in a grid that is still filtered
    # to a term matching nothing — measured live 2026-08-04: "No images
    # match your search", 0 tiles, "0 selected", Next aria-disabled.
    try:
        search_box = page.get_by_placeholder(re.compile(r"Search media", re.I)).first
        if search_box.count() and (search_box.input_value(timeout=1500) or "").strip():
            handle = _focus_via_cdp_click(page, search_box)
            if handle is not None:
                page.keyboard.press("ControlOrMeta+A")
                page.keyboard.press("Delete")
                _sleep_h(1.2)
    except Exception:
        pass

    # Trusted click here too: a Playwright locator click on this panel gets
    # rejected as "not stable" the same way the trigger did (2026-08-04), so
    # the file chooser never opens and the whole step fails after the wizard
    # had already been driven all the way to Media.
    #
    # RETRY the whole upload, not just the click: measured live 2026-08-04
    # that the identical code path sometimes lands the file in the library
    # and sometimes does not (chooser opens, set_files returns, file never
    # appears as a tile). Treating set_files as success is what let those
    # runs continue into a wizard that could never be advanced. Verify the
    # observable result — a tile captioned with the filename — and re-upload
    # if it never shows.
    _tile_present_js = r"""
        (name) => [...document.querySelectorAll('*')]
          .filter(e => e.offsetParent !== null && e.children.length === 0)
          .some(e => (e.textContent || '').replace(/\s+/g, ' ').trim() === name)
    """
    # ── след попытки ─────────────────────────────────────────────────────
    #
    # 14.09: шесть отказов подряд на двух аккаунтах, и по телеметрии о них
    # известно ровно ничего — шаг возвращал False без единого слова. Три
    # разбора подряд свелись к догадкам по посмертным снимкам, и все три
    # оказались неверны (переименованные категории, Advantage+, вес файла).
    #
    # По снимкам установлено твёрдо только это: `input[type=file]` на странице
    # нет, «Uploading» нет, индикатора нет — то есть загрузка не начиналась.
    # Почему окно медиа оказалось не в том состоянии, посмертный снимок не
    # говорит: он снят ПОСЛЕ всех трёх попыток, когда след уже простыл.
    #
    # Поэтому шаг теперь записывает, что видел и делал на каждом шагу. Это не
    # меняет поведения — только перестаёт молчать там, где принимается решение.
    _diag: dict = {
        "file": Path(creative_path).name,
        "size_mb": 0.0,
        "attempts": [],
    }
    # Тяжёлый ролик сжимается ДО того, как мы что-то решаем про пути загрузки
    # (acl-1189). Замер 15.09: 247 МБ уходят в Meta порциями 2.8 МБ в минуту,
    # то есть полтора часа при бюджете шага в девять минут. Тот же ролик в 24
    # МБ едет обычной дорогой за минуту. Оригинал при этом не трогается —
    # сжатое живёт в машинном кэше.
    try:
        from src.fb_ads_rpa import creative_compress

        prepared = (creative_compress.ensure_uploadable(creative_path) if is_video
                    else creative_compress.Prepared(creative_path,
                         creative_compress.size_mb(creative_path),
                         creative_compress.size_mb(creative_path)))
        _diag["prepared"] = {
            "original_mb": round(prepared.original_mb, 1),
            "sent_mb": round(prepared.sent_mb, 1),
            "compressed": prepared.compressed,
            "seconds": round(prepared.seconds, 3),
            "note": prepared.note,
        }
        if prepared.compressed:
            # Оператор собирал кампанию из своего файла. Если уехала копия, он
            # узнаёт об этом здесь, а не постфактум из кабинета.
            _telemetry.record("ad_creative_compressed", True, note=prepared.note,
                              diag=_diag["prepared"])
            creative_compress.report_preparation(creative_path, prepared)
            creative_path = prepared.path
        elif prepared.reason:
            _telemetry.record("ad_creative_compressed", None, note=prepared.reason,
                              diag=_diag["prepared"])
            creative_compress.report_preparation(creative_path, prepared)
    except Exception as exc:  # сжатие — удобство, а не условие работы шага
        _diag["prepared_error"] = f"{type(exc).__name__}: {exc}"[:160]

    _file_size_mb = float("inf")  # unknown sizes must not enter the byte-transfer path
    try:
        _file_size_mb = Path(creative_path).stat().st_size / (1024 * 1024)
        _diag["size_mb"] = round(_file_size_mb, 1)
    except Exception:
        pass

    # Compression changes the library caption. Keep original-name reuse above,
    # then also look for an already uploaded derived copy when reuse is enabled.
    if Path(creative_path).name != _diag["file"] and get_reuse_library_media():
        decision = _reuse_upload_decision(page, Path(creative_path).name)
        if decision is not None:
            _diag["reused"] = decision
            _set_last_upload_diag(_diag)
            return decision

    # Clear the library search box before uploading. The reuse attempt just
    # above types the filename into it; when reuse fails the filter STAYS,
    # and the freshly uploaded file lands in a grid that is still filtered
    # to a term matching nothing — measured live 2026-08-04: "No images
    # match your search", 0 tiles, "0 selected", Next aria-disabled, so the
    # wizard could never be advanced and the media never attached.
    try:
        search_box = page.get_by_placeholder(re.compile(r"Search media", re.I)).first
        if search_box.count() and (search_box.input_value(timeout=1500) or "").strip():
            handle = _focus_via_cdp_click(page, search_box)
            if handle is not None:
                page.keyboard.press("ControlOrMeta+A")
                page.keyboard.press("Delete")
                _sleep_h(1.2)
    except Exception:
        pass

    def _remember(**kw) -> None:
        _diag["attempts"].append(kw)
        _set_last_upload_diag(_diag)
        try:
            # One file per run: two runs at once would otherwise overwrite
            # each other's record mid-step (acl-1210).
            run = _telemetry.current_run_id() or "run"
            Path(f"output/debug_creative_upload.{run}.txt").write_text(
                json.dumps(_diag, ensure_ascii=False, indent=1), encoding="utf-8"
            )
        except Exception:
            pass

    def _finish_upload() -> bool:
        # Advance until the wizard closes (or we run out of patience). Said in
        # the diagnostic either way: a wizard left open is what breaks every
        # step after this one, and until 2026-09-16 the only trace of it was
        # `dialogs: 1` on the NEXT step's failure.
        _diag["wizard"] = "closed" if _walk_creative_wizard(page) else "left_open"
        _set_last_upload_diag(_diag)
        if _diag["wizard"] != "closed":
            return False

        # Confirm we're still on the ad editor, not bounced back to the ads
        # list by some other in-between click — recover via the saved edit URL
        # rather than guessing a re-open control.
        if edit_url and "/ads/edit/" not in page.url:
            try:
                page.goto(edit_url)
                _sleep_h(1.0)
            except Exception:
                return False

        if "/ads/edit/" not in page.url:
            return False

        # Confirmed live (2026-07-29): being on the right URL says nothing about
        # whether the media actually landed on the ad. A run reported
        # `after_ad_creative_upload` success for all three ads, yet two of them
        # were later found in Ads Manager still showing the empty-media state —
        # the same async-save-race class already confirmed for campaign
        # name/budget and ad destination. Wait for Meta's own save indicator to
        # clear, then assert the Ad creative section really left the empty
        # state before reporting success.
        return _verify_ad_media_present(page)

    _upload_name = Path(creative_path).name
    landed = False
    for _attempt in range(3):
        # Hand the file straight to the hidden <input type=file> instead of
        # clicking Upload and catching a chooser. In the 2026-08-24 modal the
        # Upload control became one item in a tab strip
        # (Images | Videos | Upload | Account videos) and stopped matching an
        # exact-text button lookup — the user watching the screen confirmed
        # the click never lands. set_input_files needs no click at all, so the
        # control's shape stops mattering. Clicking is kept as a fallback for
        # renders that only expose a chooser.
        _step: dict = {"n": _attempt + 1}
        post_media_before = _creative_post_media_step(page)
        advanced_after_upload = False
        try:
            # 1) If a file input is already mounted, feed it — no click, so the
            #    control's shape cannot matter.
            fed = False
            _inputs = page.query_selector_all("input[type=file]")
            _step["file_inputs"] = len(_inputs)
            for h in _inputs:
                try:
                    fed = _feed_mounted_file(page, h, creative_path, _file_size_mb, _diag)
                    if fed:
                        break
                except Exception as exc:
                    _step["set_files_error"] = f"{type(exc).__name__}: {exc}"[:120]
                    continue
            _step["fed"] = fed

            # 2) Otherwise click Upload INSIDE expect_file_chooser. It must be
            #    inside: the button opens a real OS file dialog, and a bare
            #    click leaves that dialog with nobody to answer it — the page
            #    then hangs (measured 24.08 on v89: 139s on a single step).
            #    The button is a normal toolbar button reading "+ Upload", so
            #    match on *contains*; the old exact-'Upload' lookup missed it
            #    and that is why uploads failed from 24.08 onward.
            if not fed:
                def _press_upload() -> bool:
                    clicked = _click_xpath(
                        page,
                        "//*[@role='button' or self::button]"
                        "[contains(normalize-space(.), 'Upload')]",
                    )
                    _step["upload_xpath_clicked"] = clicked
                    if not clicked:
                        upload_btn.first.click(timeout=5000)
                        _step["upload_fallback_clicked"] = True
                        return True
                    return clicked

                # Тяжёлый файл идёт своей дорогой ДО перехватчика Playwright.
                #
                # `expect_file_chooser` перехватывает то же окно, что и мы, и
                # два перехватчика на одно событие — заведомая драка. Поэтому
                # здесь либо один путь, либо другой, но не оба сразу.
                #
                # Почему не через поиск поля: 14.09 обход всего дерева нашёл
                # ноль полей при открытом окне выбора. Окно медиа живёт в
                # отдельном процессе, и `DOM.getDocument` туда не дотягивается.
                # Событие и backendNodeId обрабатываются в CDP-сессии
                # того фрейма, который открыл диалог.
                if _file_size_mb > _PW_FILE_LIMIT_MB:
                    _step["heavy"] = _upload_heavy_via_chooser_event(
                        page, creative_path, _diag, _press_upload
                    )
                    if not _step["heavy"]:
                        raise RuntimeError("Browser did not accept the file by path; byte-transfer fallback is disabled for large files")
                else:
                    with page.expect_file_chooser(timeout=25000) as fc_info:
                        _press_upload()
                    _step["chooser"] = True
                    fc_info.value.set_files(creative_path, timeout=_PW_SET_FILES_MS)
        except Exception as exc:
            # Здесь и была слепота: исключение глоталось молча, и отличить
            # «кнопки нет» от «окно выбора файла не открылось» было нечем.
            _step["error"] = f"{type(exc).__name__}: {exc}"[:160]
            _remember(**_step)
            _sleep_h(1.5)
            continue
        # Give the grid time to show the new tile before judging.
        _ticks = 0
        for _ in range(30):
            # The file can leave the library immediately: a NEW Trim/Crop
            # screen after set_files is positive progress even without a tile.
            if not post_media_before and _creative_post_media_step(page):
                advanced_after_upload = True
                break
            try:
                if page.evaluate(_tile_present_js, _upload_name):
                    landed = True
                    break
            except Exception:
                pass
            _ticks += 1
            _sleep_h(1.0)
        # Что показывала страница, пока мы ждали плитку: шла ли загрузка
        # вообще. 10.09 было доказано, что «медленно» и «не начиналось» —
        # разные вещи, и по одному лишь отсутствию плитки их не различить.
        _step["waited_s"] = _ticks
        _step["tile"] = landed
        _step["state"] = ("wizard_after_upload" if advanced_after_upload
                          else "tile" if landed else "neither")
        try:
            _step["uploading"] = _upload_in_progress(page)
            _step["progress"] = _upload_progress(page)
        except Exception:
            pass
        if not advanced_after_upload and not landed and _step.get("uploading"):
            _step["state"] = "progress"
        _remember(**_step)
        if advanced_after_upload:
            return _finish_upload()
        if landed:
            break
    if not landed:
        return False

    # Wait for the upload to actually finish (confirmed live 2026-07-27: a
    # 14MB file over the Multilogin CDP connection can take long enough that
    # the "Uploading ..." toast hasn't even rendered yet at the moment this
    # code runs — `wait_for(state="hidden")` on a locator matching ZERO
    # elements succeeds immediately (vacuously "hidden"), so the old check
    # could report done before the upload even started. Wait for the
    # positive "upload is complete" toast instead of the absence of
    # "Uploading" — confirmed live this is the actual text Meta shows
    # ("Your video X.mp4 upload is complete").
    # Wait on the OBSERVABLE result — the library's "N selected" counter
    # going non-zero — not on a toast. Measured live 2026-08-04: the upload
    # completes and the tile shows "1 selected" while no "upload is
    # complete" toast is ever visible to us, so the old 45s wait (plus a 40s
    # fallback) burned the step's whole budget for nothing.
    # Counter format changed with the 2026-08-24 modal: it now reads
    # "0 of 10 selected" where it used to read "0 selected". The old regex
    # anchored straight after the number, so a real selection went unnoticed,
    # the tile-click loop kept retrying and Next stayed disabled. Accept both.
    sel_js = r"""
        () => [...document.querySelectorAll('*')]
          .filter(e => e.offsetParent !== null && e.children.length === 0)
          .some(e => /^[1-9]\d*( of \d+)? selected$/.test(
            (e.textContent || '').replace(/\s+/g, ' ').trim()))
    """
    tile_js = """
        (name) => [...document.querySelectorAll('*')]
          .filter(e => e.offsetParent !== null && e.children.length === 0)
          .some(e => (e.textContent || '').replace(/\\s+/g, ' ').trim() === name)
    """
    fname = Path(creative_path).name

    # Wait for the uploaded tile to appear in the grid (observable result of
    # the upload — measured 2026-08-04 that no "upload is complete" toast is
    # ever visible to us, so the old 45s+40s toast waits burned the budget).
    # Ждём по состоянию, а не по счёту итераций. Шестьдесят проходов — это
    # около семидесяти секунд, и на тяжёлом видео их не хватает: 10 сентября
    # файл грузился девять минут, код досчитал своё и ушёл, а на экране в этот
    # момент было «Uploading video 2_ar_2221_story.mp4» и кнопка Cancel. Объём
    # креатива нам заранее неизвестен, и никакое фиксированное число проходов
    # не будет верным для всех.
    #
    # Поэтому спрашиваем страницу: пока она говорит, что грузит, ждём. Счётчик
    # простоя идёт только тогда, когда ни результата, ни признака загрузки нет
    # — тогда семидесяти секунд по-прежнему достаточно, и застрявший шаг не
    # висит вечно. Общий потолок нужен на случай, если надпись останется
    # висеть навсегда: прогон должен кончиться отказом, а не сутками ожидания.
    #
    # Застой распознаётся ЗДЕСЬ, а не после выхода из цикла. Сначала эта
    # проверка стояла между попытками — то есть включалась только после того,
    # как ожидание выберет свой потолок. 10 сентября это стоило сорока двух
    # минут: два потолка по двадцать подряд, и лишь потом перезагрузка.
    # Неподвижная полоса видна за минуту, и решать надо в этих единицах.
    idle = 0
    frozen = 0
    last_progress = _upload_progress(page)
    deadline = time.time() + _UPLOAD_MAX_S
    while idle < 60 and frozen < _UPLOAD_FROZEN_S and time.time() < deadline:
        if not post_media_before and _creative_post_media_step(page):
            _diag["state"] = "wizard_after_upload"
            return _finish_upload()
        try:
            if page.evaluate(sel_js) or page.evaluate(tile_js, fname):
                break
        except Exception:
            pass
        uploading = _upload_in_progress(page)
        idle = 0 if uploading else idle + 1
        now = _upload_progress(page)
        # Счётчик неподвижности идёт только пока страница говорит, что грузит:
        # вне загрузки «прогресс не менялся» ничего не значит.
        if uploading and now is not None and now == last_progress:
            frozen += 1
        else:
            frozen = 0
        last_progress = now
        _sleep_h(1.0)

    # Uploading does NOT always select the tile: measured live 2026-08-04
    # sitting on "0 selected" with Next aria-disabled, i.e. the wizard
    # refuses to advance. Click the tile until the counter moves.
    for _ in range(3):
        try:
            if page.evaluate(sel_js):
                break
        except Exception:
            pass
        # The caption is a plain text div; the interactive element is the
        # [role=button] two levels above it (ground truth 2026-07-29 — an
        # earlier version clicked the caption and sat at "0 selected").
        # _clickable would pick the caption, being the smallest match, so go
        # through the ancestor explicitly.
        if not _click_xpath(
            page,
            f'//*[normalize-space(text())="{fname}"]'
            "/ancestor::*[@role='button'][1]",
        ):
            break
        _sleep_h(1.2)
    selected = False
    try:
        selected = page.evaluate(sel_js)
    except Exception:
        pass
    # Trimmed from 1.0s (2026-07-28): this runs only after the positive
    # "upload is complete" signal already fired above, unlike most other
    # sleeps in this file which were added to cover a specific documented
    # race — this one is plain padding on top of an already-confirmed state.
    _sleep_h(0.4)

    # Make sure the uploaded tile is actually selected, then walk the wizard
    # out. Both used to go through role="dialog" / Playwright locator clicks
    # — neither works on this UI (the wizard is not a dialog, and the panel
    # rejects locator clicks as unstable), so the ad was left sitting inside
    # an open wizard with the media never committed.
    if not selected:
        tile = _clickable(
            page,
            "xpath=//*[@role='button' or @role='option' or self::div]"
            f"[contains(normalize-space(.), '{Path(creative_path).name}')]",
        )
        if tile is not None:
            _click_trusted(page, tile, timeout=3000)
            _sleep_h(0.8)

    return _finish_upload()


def _media_block_filled_now(page: Page) -> bool:
    """Мгновенно: показывает ли блок Media прикреплённое медиа (без ожиданий)."""
    try:
        return bool(page.evaluate(
            r"""() => {
              const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
              const visible = [...document.querySelectorAll('*')]
                .filter(e => e.children.length === 0 && e.getBoundingClientRect().width > 0)
                .map(e => norm(e.textContent));
              if (visible.some(t => t.includes('Please specify the media'))) return false;
              return visible.some(t => t === 'Edit media' || t === 'Remove media'
                || t === 'Change selections' || t === 'Customize media');
            }"""
        ))
    except Exception:
        return False


def _wizard_past_media(page: Page) -> bool:
    """Мастер «Set up your creative» уже на шаге Text — значит, медиа выбрано.

    ACL-1313, прогон 25.09 (CL Day, 956d0d, 2-е объявление): Meta перенесла
    ролик в новое объявление сама и открыла мастер сразу на Text, с зелёной
    галочкой у Media. Проверка по странице объявления этого не видит — она
    под модалкой, — и шаг записал «нет медиа». На Text мастер не пускает без
    выбранного медиа, так что поле Primary text внутри открытого мастера —
    достаточное доказательство.
    """
    if not _creative_wizard_open(page):
        return False
    try:
        return bool(page.evaluate(
            r"""() => {
              const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
              const leaves = (root) => [...root.querySelectorAll('*')]
                .filter(e => e.children.length === 0 && e.getBoundingClientRect().width > 0)
                .map(e => norm(e.textContent));
              // Подзаголовок шага Text мастера; на странице объявления его нет.
              const atText = (ls) => ls.some(t => t.startsWith('Add multiple text options'))
                && !ls.some(t => t.includes('Please specify the media'));
              const dialogs = [...document.querySelectorAll('[role=dialog]')]
                .filter(d => d.getBoundingClientRect().width > 0);
              if (dialogs.length) return dialogs.some(d => atText(leaves(d)));
              return atText(leaves(document));
            }"""
        ))
    except Exception:
        return False


_AD_MEDIA_STATE_JS = r"""
  (stem) => {
    const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
    const visible = [...document.querySelectorAll('*')]
      .filter(e => e.offsetParent !== null && e.children.length === 0)
      .map(e => norm(e.textContent))
      .filter(Boolean);
    const low = visible.map(t => t.toLowerCase());
    const want = (stem || '').toLowerCase();
    // Имя файла Meta показывает по-разному: «2012.mp4», «2012», иногда с
    // суффиксом копии. Совпадением считаем вхождение основы имени.
    const named = want ? low.filter(t => t.includes(want)) : [];
    const processing = low.some(t => t.includes('processing') || t.includes('being processed'));
    const failed = low.some(t => t.includes("couldn't be processed") || t.includes('could not be processed')
      || t.includes('failed to process') || t.includes('processing failed')
      || t.includes("media couldn't") || t.includes('upload failed'));
    // Имена ДРУГИХ файлов рядом с медиа: нашего нет, а чужой есть — взяли не то.
    const others = low.filter(t => /\.(mp4|mov|jpg|jpeg|png|gif|webm)\b/.test(t) && (!want || !t.includes(want)));
    return { named: named.slice(0, 5), others: others.slice(0, 5),
             processing: processing, failed: failed };
  }
"""
"""Что сейчас в медиа-блоке объявления: наше имя, чужие имена, обработка, ошибка."""


def _ad_media_state(page: Page, filename: str) -> dict:
    stem = Path(filename or "").stem.lower()
    try:
        return page.evaluate(_AD_MEDIA_STATE_JS, stem) or {}
    except Exception as exc:
        return {"error": str(exc)[:80]}


_MEDIA_PROCESSING_MAX_S = 180


def _verify_ad_media_is_the_file(page: Page, filename: str) -> tuple[bool, dict]:
    """Прикрепился ли ИМЕННО наш файл и пригоден ли он (ACL-1171/1192).

    Проверка «медиа есть» пропускает две беды: прикрепилась чужая плитка
    (в медиатеке несколько похожих имён) и файл, который Meta не смогла
    обработать — объявление с таким медиа не опубликуется.
    """
    deadline = time.time() + _MEDIA_PROCESSING_MAX_S
    state: dict = {}
    while True:
        state = _ad_media_state(page, filename)
        if state.get("failed"):
            return False, {"state": state, "why": "processing_failed"}
        if not state.get("processing"):
            break
        if time.time() >= deadline:
            return False, {"state": state, "why": "still_processing"}
        _sleep_h(2.0)
    if state.get("named"):
        return True, {"state": state}
    if state.get("others"):
        return False, {"state": state, "why": "other_file_attached"}
    # Имя видно не всегда — Meta подписывает не каждую плитку. Само наличие
    # медиа уже проверено отдельно, поэтому это не отказ.
    return True, {"state": state, "why": "name_not_shown"}


def _verify_ad_media_present(page: Page) -> bool:
    """True once the ad's creative section is out of its empty state.

    Meta shows "Please specify the media to run with this ad." while no
    media is attached, and swaps in "Edit media"/"Remove media" controls
    once one is — confirmed live (2026-07-29) on both a genuinely empty ad
    and one with media applied. Checks visible text only: the empty-state
    string stays in the DOM (hidden) after media is attached, so a raw
    `body.textContent` check reports a false negative.
    """
    wait_editor_saved(page)
    # Never call an ad empty while its video is still going up. The wizard
    # closes as soon as the tile is selected, but Meta keeps uploading in the
    # background behind an "Uploading video X.mp4" toast; this check used to
    # give up ~3s later and report the ad media-less. Measured run
    # 58c3cb9b53c6 (24.08): ad3 failed with that toast still mid-progress on
    # screen, while the three ads whose uploads happened to finish sooner all
    # passed — a stopwatch race, not a broken step.
    # Потолок здесь был `for _ in range(150)` с припиской «тратится только
    # пока идёт работа». Приписка неверна: счёт шёл всегда, и на тяжёлом
    # видео эти две с половиной минуты кончались задолго до загрузки.
    # 10 сентября объявление объявили без креатива на 537-й секунде, а на
    # экране в этот момент было «Uploading video 2_ar_2221_story.mp4».
    #
    # Теперь так, как обещала приписка: проходы считаются только когда
    # страница молчит. Пока она говорит, что грузит, ждём — до общего потолка
    # по часам, который есть на случай навсегда зависшей надписи.
    grace = 0
    idle = 0
    frozen = 0
    last_progress = _upload_progress(page)
    deadline = time.time() + _UPLOAD_MAX_S
    while idle < 150 and frozen < _UPLOAD_FROZEN_S and time.time() < deadline:
        try:
            state = page.evaluate(
                """
                () => {
                  const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                  const visible = [...document.querySelectorAll('*')]
                    .filter(e => e.offsetParent !== null && e.children.length === 0)
                    .map(e => norm(e.textContent));
                  return {
                    empty: visible.some(t => t.includes('Please specify the media')),
                    // 17.09: «Uploaded media · 1 selected», «Change selections»,
                    // «Customize media» — нынешние подписи заполненного блока.
                    filled: visible.some(t => t === 'Edit media' || t === 'Remove media'
                      || t === 'Change selections' || t === 'Customize media'
                      || /^\\d+ selected$/.test(t) && visible.includes('Uploaded media')),
                    uploading: visible.some(t => /^Uploading (video|image)\\b/.test(t)),
                  };
                }
                """
            )
        except Exception:
            _sleep_h(0.5)
            continue
        if state.get("filled") and not state.get("empty"):
            return True
        if state.get("uploading"):
            grace = 0
            # Неподвижная полоса — застой, и ждать до потолка незачем: тот же
            # счёт, что и в самом ожидании загрузки. Сорок две минуты
            # 10 сентября сложились именно здесь, во втором потолке подряд.
            now = _upload_progress(page)
            frozen = frozen + 1 if now is not None and now == last_progress else 0
            last_progress = now
            _sleep_h(1.0)
            continue
        if state.get("empty"):
            # Attaching lands a beat after the toast clears — confirm the
            # empty state persists before failing the step.
            grace += 1
            if grace >= 6:  # ~3s of settled, upload-free empty state
                return False
        idle += 1
        _sleep_h(0.5)
    return False


def _reveal_ad_copy_fields(page: Page) -> bool:
    """Make the Primary text/Headline inputs visible.

    Confirmed live (2026-07-26): these fields exist in the DOM as soon as a
    creative is attached, but their container (`#creativeEditorComponent`)
    can render with `display: none !important` regardless — expanding then
    collapsing any placement row (e.g. "Feeds, In-stream ads for reels")
    was observed to trigger the re-render that makes it visible. Not a
    principled fix (root cause of the hidden state isn't understood), but a
    reliable-enough fallback: check visibility first and only do the
    expand/collapse dance if it's actually needed.
    """
    def _primary_text_visible() -> bool:
        """Готово ли поле основного текста к вводу.

        Переписано 2026-09-09. Прежняя проверка искала внутри
        `#creativeEditorComponent` элемент с ролью `combobox` и именем «Tell
        people what your ad is about». В разметке Meta на эту дату таких
        **ноль**: поле стало голой `<textarea>` без роли и без имени.
        Значит проверка не могла ответить «да» ни разу — и `fill_ad_copy`
        выходил ещё до полей, не оставив в телеметрии ни строки о том,
        почему. Прогон 9 сентября: шесть отказов, ни одной записи о стадиях.

        Спрашиваем ровно то, что делает сам шаг: находится ли поле и
        разложено ли оно. Прежняя забота про подставные поля (на странице
        бывает до семи похожих) решается сама: `data-surface$="/message_field"`
        на странице ровно один.
        """
        return mark_ad_copy_field(page, "message_field", _AD_COPY_FIELDS["message_field"], "probe")

    # 20.09 20:30 (SA_arc, CL_ml, CO_bnc подряд): после ускорения мы доходим
    # до объявления раньше, чем Meta его дорисовывает, и поля текста «ещё не
    # раскрыты» — за ними падали и текст, и назначение. Даём блоку успокоиться
    # и ждём поле до 12 с, прежде чем трогать разметку.
    # ACL-1281: заблокированный редактор ждать бессмысленно — полей текста на
    # странице нет вовсе, пока висит ошибка Meta. Раньше мы честно отстаивали
    # здесь 12 секунд, потом разворачивали блок, и только после этого шаг
    # сообщал о неудаче: 22.09 на четырёх объявлениях так ушло 6–13 минут.
    for i in range(24):
        if _primary_text_visible():
            return True
        if i == 2 and _review_errors_visible(page):
            _telemetry.record("ad_copy_fields", False, note="blocked_by_review_error")
            return False
        _sleep_h(0.5)

    # Mount the section before trying to un-hide it. Every fallback below
    # assumes #creativeEditorComponent is in the DOM and merely painted
    # `display: none` — but the editor is virtualised, and when the section
    # is scrolled away it is ABSENT, so all of them no-op and the step burns
    # through to the page reload. Ground truth from this step's own dump
    # (debug_ad_copy_reveal_dump.json, run 66b8e5ff5bdd): creativeEditorFound
    # false, primaryLabelCount 0 — while the failure screenshot taken moments
    # later shows the Primary text field plainly on screen. Same trap already
    # documented here for the CTA combobox, the Browser add-ons radio group
    # and the "Set up creative" button.
    _scroll_until_mounted(
        page,
        """
        () => !!document.getElementById('creativeEditorComponent')
              || [...document.querySelectorAll('*')]
                   .some(e => e.children.length === 0
                         && (e.textContent || '').replace(/\\s+/g, ' ').trim() === 'Primary text')
        """,
    )
    if _primary_text_visible():
        return True

    # Cheap first attempt — confirmed live (2026-07-26) that the hidden
    # state is a `display: none !important` on `#creativeEditorComponent`
    # itself (the field elements are fully mounted underneath, just not
    # painted) — force it visible directly via JS instead of waiting on
    # Meta's own re-render to happen to fix it. Near-instant; only fall
    # through to the slower dances below if this container isn't even
    # in the DOM yet.
    try:
        forced = page.evaluate(
            "() => { const el = document.getElementById('creativeEditorComponent');"
            " if (!el) return false;"
            " el.style.setProperty('display', 'block', 'important');"
            " let cur = el;"
            " for (let i = 0; i < 8 && cur; i++) {"
            "   if (cur.getAttribute && cur.getAttribute('aria-hidden') === 'true') cur.removeAttribute('aria-hidden');"
            "   cur = cur.parentElement;"
            " }"
            " return true; }"
        )
        if forced and _primary_text_visible():
            return True
    except Exception:
        pass

    # Retry a few times with waits — confirmed live (2026-07-26) that this
    # can fail on the first attempt right after the upload-complete toast
    # appears, before the placement list has actually rendered/settled;
    # the same expand/collapse click a moment later succeeds.
    for _ in range(3):
        try:
            row = page.get_by_text("Feeds, In-stream ads for reels", exact=False).first
            if row.count():
                row.scroll_into_view_if_needed(timeout=3000)
                row.click(timeout=3000)
                _sleep_h(0.5)
                row.click(timeout=3000)
                _sleep_h(0.5)
        except Exception:
            pass
        if _primary_text_visible():
            return True
        _sleep_h(1.0)

    # Last resort — confirmed live (2026-07-26): a full page reload reliably
    # clears the stuck `display: none !important` state (the ad draft
    # autosaves continuously, so the creative/ad-name/etc already entered
    # survive the reload). Slower than the expand/collapse dance above, so
    # only used once that's been tried and failed.
    #
    # Confirmed live (2026-07-26): the SPA takes noticeably longer than 3s
    # to repaint after reload — a flat sleep(3.0) caught the page still
    # blank, which then also broke the NEXT step (destination) since it
    # ran against the same still-loading page. Poll for the sidebar nav
    # (a stable early marker) plus extra settle time instead of guessing
    # a fixed duration.
    try:
        page.reload(wait_until="domcontentloaded", timeout=20000)
        page.wait_for_load_state("networkidle", timeout=15000)
        _sleep_h(2.0)
    except Exception:
        time.sleep(5.0)

    # ACL-1313: перезагрузка возвращает в Browser add-ons «Instant form», а с
    # ней — ошибку Lead Gen и баннер условий, которые потом читались как
    # «страница не приняла условия». Вернуть «None» сразу.
    try:
        if _browser_addon_none_checked(page) is False:
            _telemetry.record("browser_addon_after_reload", _select_browser_addon_none(page))
    except Exception:
        pass

    for _ in range(10):
        if _primary_text_visible():
            return True
        _sleep_h(1.0)

    # Diagnostic dump (2026-07-27) — every fallback above has now failed.
    # Capture enough live DOM/React state to actually root-cause this
    # instead of guessing at another CSS-level workaround.
    try:
        dump = page.evaluate(
            """
            () => {
              const el = document.getElementById('creativeEditorComponent');
              const dup = document.querySelectorAll('#creativeEditorComponent').length;
              const primaryLabels = [...document.querySelectorAll('*')]
                .filter(e => e.textContent.trim() === 'Primary text' && e.children.length === 0);
              const describe = (node) => {
                if (!node) return null;
                const style = getComputedStyle(node);
                const reactKey = Object.keys(node).find(k => k.startsWith('__reactFiber$') || k.startsWith('__reactInternalInstance$'));
                let fiberInfo = null;
                if (reactKey) {
                  const fiber = node[reactKey];
                  fiberInfo = {
                    type: fiber.type ? (fiber.type.name || fiber.type.displayName || String(fiber.type)) : null,
                    key: fiber.key,
                    memoizedPropsKeys: fiber.memoizedProps ? Object.keys(fiber.memoizedProps) : null,
                    memoizedStateExists: !!fiber.memoizedState,
                  };
                }
                return {
                  tag: node.tagName,
                  id: node.id,
                  className: node.className,
                  inlineStyle: node.getAttribute('style'),
                  computedDisplay: style.display,
                  computedVisibility: style.visibility,
                  offsetParent: node.offsetParent !== null,
                  childCount: node.children.length,
                  fiberInfo,
                };
              };
              const ancestorChain = [];
              let cur = el;
              for (let i = 0; i < 6 && cur; i++) {
                ancestorChain.push(describe(cur));
                cur = cur.parentElement;
              }
              return {
                creativeEditorFound: !!el,
                duplicateCount: dup,
                ancestorChain,
                primaryLabelCount: primaryLabels.length,
                primaryLabelsInfo: primaryLabels.map(describe),
                url: window.location.href,
              };
            }
            """
        )
        with open("output/debug_ad_copy_reveal_dump.json", "w", encoding="utf-8") as f:
            json.dump(dump, f, indent=2, ensure_ascii=False)
    except Exception as exc:
        try:
            with open("output/debug_ad_copy_reveal_dump.json", "w", encoding="utf-8") as f:
                f.write(f"dump failed: {exc}")
        except Exception:
            pass

    return False


def _focus_via_cdp_click(page: Page, locator):
    """Pin an ElementHandle and give it real keyboard focus; None on failure.

    Shared by every "click a field, then insert_text into it" step. Uses
    the raw CDP mouse-event click (_mouse_click_chain), not
    locator.click() — confirmed live (2026-07-26): Playwright's own click
    can report success under connect_over_cdp without the page actually
    receiving focus (microsoft/playwright#41286), silently leaving
    insert_text() with nowhere to go. Confirmed live again 2026-07-29
    (Test141): the Destination -> Website URL field hit the exact same
    trap — locator.click() "succeeded", insert_text went nowhere, the ad
    was left on the lead_gen_form_id validation error.

    Confirmed live (2026-07-27): with multiple ad-copy text variation
    slots on the page, several role=combobox elements can share the SAME
    accessible name — re-resolving a bare Locator's `.first` after the
    click is not guaranteed to hit the same DOM node if anything reflows
    in between. Pin one concrete ElementHandle up front and use it for
    both the click and the later verification — no re-resolution.
    """
    try:
        handle = locator.element_handle(timeout=3000)
    except Exception:
        return None
    if handle is None:
        return None
    try:
        handle.evaluate("el => el.scrollIntoView({block: 'center', behavior: 'instant'})")
    except Exception:
        pass
    _sleep_h(0.3)
    try:
        box = handle.bounding_box()
    except Exception:
        box = None

    # Confirmed live 2026-07-29 (Test140, via debug_ad_copy_locator_miss
    # .json): the Headline field repeatedly reached here with the locator
    # matched (count == 1) and its container visible (display: block,
    # offsetParent set), yet bounding_box() still returned None — so the
    # whole step bailed even though the field was plainly on screen in the
    # screenshot. Fall back to the element's own rect, which is reported
    # even when Playwright's visibility heuristics decline to produce a
    # box, and then to a plain DOM focus(): insert_text() targets whatever
    # holds focus, so focus alone is enough to fill the field. Callers
    # must still verify the field's content afterwards, so a fallback that
    # quietly does nothing is caught rather than trusted.
    if not box:
        try:
            rect = handle.evaluate(
                "el => { const r = el.getBoundingClientRect();"
                " return {x: r.x, y: r.y, width: r.width, height: r.height}; }"
            )
            if rect and rect.get("width", 0) > 0 and rect.get("height", 0) > 0:
                box = rect
        except Exception:
            box = None

    if box:
        _mouse_click_chain(page, box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        _sleep_h(0.3)
        return handle

    try:
        handle.evaluate("el => el.focus()")
        _sleep_h(0.2)
        return handle
    except Exception:
        return None


_AD_COPY_FIELDS = {
    # Разметочное имя Meta -> подпись над полем, на случай если имя уедет.
    "message_field": "Tell people what your ad is about",
    "headline_field": "Write a short headline",
    "link_description_field": "Include additional details",
}


def mark_ad_copy_field(
    page: Page,
    key: str,
    label: str | None,
    mark: str,
    placeholder: str | None = None,
) -> bool:
    """Пометить поле объявления атрибутом, за который его возьмёт локатор.

    Написано 2026-09-09 по настоящей разметке (снимок прогона, фикстура
    `tests/fixtures/ads_manager/ad_copy_2026_09_09.html`). Прежний код искал
    заголовок по `placeholder="Write a short headline"`, а основной текст —
    по роли `combobox` с именем: на странице нет ни одного и ни другого,
    отсюда три отказа из шести 9 сентября.

    Что там на самом деле:

        <div class="_9vo4">Write a short headline</div>
        <textarea dir="auto"></textarea>
        <div aria-hidden="true">Write a short headline</div>

    Подпись — соседний div, поле — голая `<textarea>` без единого атрибута.
    Зато весь блок обёрнут в `data-surface=".../headline_field"`: это
    собственная разметка Meta для её логов — осмысленная и, в отличие от
    классов и идентификаторов, не порождаемая заново при каждой отрисовке.

    Ищем сначала по ней, потом по подписи. Атрибут служебный, обещаний нам
    не давал, и один способ поиска на такое поле — это тот же самый отказ,
    отложенный на несколько месяцев.

    Видно ли поле, считаем по размеру его прямоугольника. В этом же снимке
    рядом лежит поле ссылки внутри контейнера с `display: none !important` —
    свёрнутый раздел назначения; по размеру такое отсеивается, и в поле,
    которого человек не видит, мы не напечатаем.

    Помечаем атрибутом, а не возвращаем координату: правая колонка
    пересчитывается сама и двигает строки, а ссылка на узел этого не
    замечает.
    """
    try:
        return bool(
            page.evaluate(
                """
                ([key, label, mark, placeholder]) => {
                  const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                  const seen = (el) => {
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0 && getComputedStyle(el).visibility !== 'hidden'
                      && !el.closest('[aria-hidden="true"], [inert]') && !el.disabled && !el.readOnly;
                  };
                  const controls = 'textarea,input[type=text],input:not([type]),[contenteditable="true"]';
                  for (const old of document.querySelectorAll('[data-rpa-field="' + mark + '"]')) {
                    old.removeAttribute('data-rpa-field');
                  }
                  let candidates = [];
                  if (key) {
                    for (const host of document.querySelectorAll('[data-surface$="/' + key + '"]')) {
                      candidates.push(...[...host.querySelectorAll(controls)].filter(seen));
                    }
                  }
                  if (!candidates.length && placeholder) {
                    candidates = [...document.querySelectorAll(controls)]
                      .filter(e => e.getAttribute('placeholder') === placeholder && seen(e));
                  }
                  if (!candidates.length && label) {
                    // Keep the old accessible/placeholder layouts as well as the
                    // recorded adjacent-label layout. Refuse ambiguous matches.
                    candidates = [...document.querySelectorAll(controls)].filter(e =>
                      seen(e) && (e.getAttribute('aria-label') === label || e.getAttribute('placeholder') === label));
                    if (!candidates.length) {
                      for (const tag of document.querySelectorAll('div,span,label')) {
                        if (tag.children.length || norm(tag.textContent) !== label || !seen(tag)) continue;
                        let host = tag.parentElement;
                        for (let i = 0; i < 3 && host; i++, host = host.parentElement) {
                          const found = [...host.querySelectorAll(controls)].filter(seen);
                          if (found.length) { candidates.push(...found); break; }
                        }
                      }
                    }
                  }
                  candidates = [...new Set(candidates)];
                  if (candidates.length !== 1) return false;
                  const el = candidates[0];
                  el.setAttribute('data-rpa-field', mark);
                  return true;
                }
                """,
                [key, label, mark, placeholder],
            )
        )
    except Exception:
        return False


#: Draft.js держит в каждом блоке `[data-block="true"]` ровно одного ребёнка
#: — обёртку листа. Всё, что лежит за ней, — абзацы, которые Chrome нарезал
#: сам мимо редактора (ACL-1313, 24.09). React ими не управляет, поэтому
#: удалить их безопасно; а `Ctrl+A` + `Backspace` их не трогает.
_DRAFT_ORPHANS_JS = """(el, strip) => { let n = 0;
  for (const b of el.querySelectorAll('[data-block="true"]'))
    for (const x of [...b.children].slice(1)) { n++; if (strip) x.remove(); }
  return n; }"""


def _draft_orphans(handle, strip: bool = False) -> int:
    """Сколько в поле абзацев-копий вне редактора; с `strip` — убрать их."""
    try:
        return int(handle.evaluate(_DRAFT_ORPHANS_JS, strip) or 0)
    except Exception:
        return -1


def _is_draft_editor(handle) -> bool:
    try:
        return bool(handle.evaluate("el => !!el.querySelector('[data-contents]') || !!el.closest('[data-contents]')"))
    except Exception:
        return False


def _insert_lines(page: Page, text: str) -> None:
    """Вставить текст целиком, но переносы — нажатиями, а не внутри вставки."""
    for n, line in enumerate(text.replace("\r\n", "\n").split("\n")):
        if n:
            page.keyboard.press("Enter")
        if line:
            page.keyboard.insert_text(line)


def _handle_text(handle) -> str:
    try:
        return str(handle.evaluate(
            "el => el.value !== undefined ? el.value : (el.innerText || el.textContent || '')"
        ) or "")
    except Exception:
        return ""


def _copy_matches(seen: str, expected: str) -> bool:
    """Сравнение текста объявления без учёта пробелов, переносов и U+200B."""
    squash = lambda s: re.sub(r"[\s​﻿]+", "", s or "")
    return squash(seen) == squash(expected)


def _await_creative_settled(page: Page, timeout_s: float = 20.0) -> bool:
    """Дождаться, пока блок креатива перестанет перерисовываться.

    20.09 02:45, разбор 16 отказов ввода текста за сутки: печатаем — поле
    читается пустым, затем на заголовке число полей на странице падает с 5
    до 1 и поле исчезает (`locator_count_zero`). Meta в этот момент
    перерисовывает блок (обычно когда дообработалось видео) и стирает
    набранное. Ждём двух одинаковых замеров подряд.
    """
    last = None
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            now = page.evaluate(
                "() => document.querySelectorAll('[placeholder], [role=textbox]').length")
        except Exception:
            return False
        if last is not None and now == last and now > 0:
            return True
        last = now
        _sleep_h(1.0)
    return False


_TEXT_VARIATIONS_JS = r"""
  () => {
    const norm = (s) => (s || '').replace(/[\s​]+/g, ' ').trim();
    const vis = (e) => e.getClientRects().length > 0;
    const boxes = [...document.querySelectorAll('[role=checkbox], input[type=checkbox]')];
    const on = boxes.filter(b => {
      const state = b.getAttribute('aria-checked') || String(b.checked);
      if (state !== 'true') return false;
      let ctx = b.parentElement, txt = '';
      for (let i = 0; i < 5 && ctx; i++, ctx = ctx.parentElement) {
        const x = norm(ctx.textContent);
        if (x.length > 8) { txt = x; break; }
      }
      return /check all text variations|text variations/i.test(txt);
    });
    const b = on.find(vis);
    if (!b) return {found: boxes.length > 0, on: on.length, point: null};
    b.scrollIntoView({block: 'center'});
    const r = b.getBoundingClientRect();
    return {found: true, on: on.length,
            point: {x: r.x + r.width / 2, y: r.y + r.height / 2}};
  }
"""
"""Галочка «Check all text variations» — сочинённые Meta варианты текста."""


def disable_text_variations(page: Page) -> bool | None:
    """Снять отметку со сгенерированных Meta вариантов текста.

    21.09, решение пользователя: в объявлении включён Advantage+ creative, и
    Meta подставляет собственные варианты основного текста рядом с нашим.
    Снимаем «Check all text variations», чтобы в показ шёл только
    утверждённый текст. None — галочки на странице нет (ничего и не надо).
    """
    try:
        state = page.evaluate(_TEXT_VARIATIONS_JS) or {}
    except Exception as exc:
        _telemetry.record("text_variations_off", False, note=str(exc)[:60])
        return False
    if not state.get("point"):
        # ACL-1313: молчаливый None прятал, что галочки на странице 24.09 не
        # было вовсе — строка в телеметрии отличает «нечего снимать» от сбоя.
        _telemetry.record("text_variations_off", None, note="label_not_found")
        return None
    _cdp_mouse_click(page, state["point"]["x"], state["point"]["y"])
    _sleep_h(0.8)
    try:
        after = page.evaluate(_TEXT_VARIATIONS_JS) or {}
    except Exception:
        after = {}
    ok = not after.get("point")
    _telemetry.record("text_variations_off", ok,
                      diag={"was_on": state.get("on"), "still_on": after.get("on")})
    return ok


_TEXT_GENERATION_JS = r"""
  () => {
    const norm = (s) => (s || '').replace(/[\s\u200b]+/g, ' ').trim();
    const vis = (e) => e.getClientRects().length > 0;
    const leaf = [...document.querySelectorAll('*')].find(e =>
      e.children.length === 0 && /creative text generation/i.test(norm(e.textContent)));
    if (!leaf) return {found: false};
    // Раздел бывает свёрнут: сначала раскрываем его собственной створкой.
    let box = leaf, collapsed = null;
    for (let i = 0; i < 7 && box.parentElement; i++) {
      box = box.parentElement;
      const shut = [...box.querySelectorAll('[aria-expanded="false"]')].filter(vis);
      if (shut.length) { collapsed = shut[0]; break; }
    }
    if (collapsed) {
      collapsed.scrollIntoView({block: 'center'});
      const r = collapsed.getBoundingClientRect();
      return {found: true, expand: {x: r.x + r.width / 2, y: r.y + r.height / 2}};
    }
    // Раскрыт: ищем включённый переключатель внутри раздела.
    let holder = leaf;
    for (let i = 0; i < 7 && holder.parentElement; i++) {
      holder = holder.parentElement;
      const on = [...holder.querySelectorAll('[role=switch], input[type=checkbox], [role=checkbox]')]
        .filter(e => vis(e) && (e.getAttribute('aria-checked') || String(e.checked)) === 'true');
      if (on.length) {
        const b = on[0];
        b.scrollIntoView({block: 'center'});
        const r = b.getBoundingClientRect();
        return {found: true, on: on.length,
                point: {x: r.x + r.width / 2, y: r.y + r.height / 2}};
      }
    }
    return {found: true, on: 0, point: null};
  }
"""
"""Раздел «Advantage+ creative text generation» и его переключатель."""


def disable_text_generation(page: Page) -> bool | None:
    """Выключить генерацию текста Meta в объявлении.

    22.09, решение пользователя: рядом с нашим текстом Meta держит свои
    варианты («Primary text (1 of 5)»), а снизу — раздел Advantage+ creative
    text generation. Снятой галочки «Check all text variations» мало: сам
    раздел остаётся включённым. Раскрываем его и выключаем. None — раздела на
    странице нет, выключать нечего.
    """
    state = {}
    for _ in range(3):
        try:
            state = page.evaluate(_TEXT_GENERATION_JS) or {}
        except Exception as exc:
            _telemetry.record("text_generation_off", False, note=str(exc)[:60])
            return False
        if not state.get("found"):
            _telemetry.record("text_generation_off", None, note="label_not_found")
            return None
        if state.get("expand"):
            _cdp_mouse_click(page, state["expand"]["x"], state["expand"]["y"])
            _sleep_h(0.8)
            continue
        break
    if not state.get("point"):
        # Раздел есть, включённого переключателя не нашли — записываем как есть.
        _telemetry.record("text_generation_off", None,
                          note="no_switch", diag={"state": state})
        return None
    _cdp_mouse_click(page, state["point"]["x"], state["point"]["y"])
    _sleep_h(0.9)
    try:
        after = page.evaluate(_TEXT_GENERATION_JS) or {}
    except Exception:
        after = {}
    ok = not after.get("point")
    _telemetry.record("text_generation_off", ok,
                      diag={"was_on": state.get("on"), "still_on": after.get("on")})
    return ok


def _repair_blocked_copy_editor(page: Page, ad_name: str, destination_url: str) -> bool:
    """Снять блокировку редактора креатива. Один раз, без второго круга.

    Meta подставляет в Browser add-ons «Instant form (suggested)»: объявление
    получает ошибку про Lead Gen форму, правая панель грузится бесконечно, а
    редактор креатива не рисуется — полей текста на странице нет. Лечение:
    поставить «None», убедиться, что встало, и открыть объявление заново.

    Возвращает True, если после лечения поля текста появились. Дальше звать
    нечего: 22.09 повторные круги давали те же 6–13 минут и тот же отказ.
    """
    if _lead_terms_not_accepted(page) and _browser_addon_none_checked(page) is not False:
        # Чинить нечего: условия Lead Generation принимает человек в Meta.
        _telemetry.record("lead_terms_not_accepted", False,
                          note="page_must_accept_lead_terms")
        return False
    # ACL-1313, прогон 24.09 (GT Day, d3e690189a6e): после перезагрузки в
    # `_reveal_ad_copy_fields` Meta снова ставит «Instant form», и баннер про
    # условия Lead Generation относится к этой форме, а не к странице — у
    # этой же страницы первое объявление прошло. Если «None» не стоит, баннер
    # не приговор: ставим «None», как при любой другой блокировке.
    try:
        fixed = _select_browser_addon_none(page)
        _sleep_h(1.5)
        checked = _browser_addon_none_checked(page)
        if not fixed or checked is not True:
            # Блока Browser add-ons на странице может не оказаться вовсе:
            # редактор висит на «Verifying your changes». Снимаем зависание
            # перезагрузкой и чиним ещё раз — это часть одного лечения.
            recover_stuck_page(page, "ad_copy_blocked", editor=True, force=True)
            _sleep_h(3.0)
            fixed = _select_browser_addon_none(page)
            _sleep_h(1.5)
            checked = _browser_addon_none_checked(page)
        _telemetry.record("browser_addon_repair", bool(fixed) and checked is True,
                          diag={"none_checked": checked})
        if ad_name:
            # После перезагрузки мы стоим на уровне ad set'а, а объявление
            # закрыто: открываем его тем же обходом, что чинит назначения.
            try:
                verify_and_fix_ad_destinations(page, [ad_name], destination_url)
                _sleep_h(2.0)
            except Exception as exc:
                _telemetry.record("ad_copy_blocked", False,
                                  note=f"reopen_failed:{str(exc)[:40]}")
                return False
    except Exception as exc:
        _telemetry.record("browser_addon_repair", False, note=str(exc)[:60])
        return False
    return not _review_errors_visible(page)


def _back_on_the_ad(page: Page, ad_name: str, report: dict) -> None:
    """Вернуть редактор на объявление перед второй попыткой текста.

    ACL-1313, прогон 25.09 (GT FX, 5b2237, объявление 1): поля текста не
    раскрылись, последний ход `_reveal_ad_copy_fields` — перезагрузка — открыл
    редактор на уровне ad set (адрес `adsets/edit`, справа «Ad set name»), а
    окно к тому же было свёрнуто. Вторая попытка искала поля там, где их нет.
    Окно — развернуть, объявление — открыть в дереве по имени.
    """
    try:
        from src.fb_ads_rpa.browser import ensure_editor_window

        report["window_ok"] = bool(ensure_editor_window(page, "ad_copy_retry"))
    except Exception as exc:
        report["window_ok"] = f"error:{str(exc)[:40]}"
    try:
        on_adset = "/adsets/edit" in (page.url or "")
    except Exception:
        on_adset = False
    if on_adset and ad_name:
        opened = False
        try:
            opened = _open_tree_ad(page, ad_name)
            _sleep_h(2.0)
            _close_creative_setup_modal(page)
        except Exception:
            pass
        report["reopened_ad"] = opened
        _telemetry.record("ad_copy_reopen_ad", opened, diag={"ad": ad_name[:60]})


def _heal_failed_component(page: Page, ad_name: str, report: dict, where: str) -> bool:
    """«Error: component failed to load» на объявлении: перезагрузить и вернуться.

    d3e690189a6e/07_ad2_copy: блок редактора не загрузился, и попытка текста
    шла в пустоту. Перезагрузка — `recover_stuck_page` в режиме редактора
    (только при «All edits saved»); после неё Meta может открыть уровень ad
    set — объявление открываем снова (`_back_on_the_ad`). True — перезагрузили.
    """
    if not _component_failed(page):
        return False
    reloaded = recover_stuck_page(page, where, editor=True)
    report["component_failed"] = {"where": where, "reloaded": reloaded}
    _telemetry.record("ad_copy_component_failed", reloaded,
                      note="reloaded" if reloaded else "not_reloaded", diag={"where": where})
    if reloaded:
        _back_on_the_ad(page, ad_name, report)
    return reloaded


def write_ad_copy(page: Page, title: str, body: str, *, ad_name: str = "",
                  destination_url: str = "", report: dict | None = None) -> bool:
    """Написать текст объявления: лечим блокировку заранее, чиним один раз.

    ACL-1281 (решение владельца 22.09). Прежний порядок — «пробуем, ждём поля,
    разворачиваем блок, обнаруживаем ошибку Meta, чиним, пробуем всё заново» —
    стоил 6–13 минут на объявление и всё равно часто кончался без текста.
    Теперь блокировка проверяется до первой попытки, лечение одно, а если
    текст всё равно не встал — шаг отказывает сразу и называет объявление.
    """
    report = report if report is not None else {}
    # ACL-1313 (5b2237): к тексту окно пришло свёрнутым — проверка окна стоит
    # только на границах разделов, а в свёрнутое окно клики по координатам не
    # попадают, и поля текста «не раскрываются». Проверяем и здесь.
    try:
        from src.fb_ads_rpa.browser import ensure_editor_window

        ensure_editor_window(page, "ad_copy")
    except Exception:
        pass
    _heal_failed_component(page, ad_name, report, "ad_copy_before")
    if _review_errors_visible(page):
        _telemetry.record("ad_copy_blocked", False, note="before_first_try")
        report["blocked_before"] = True
        if not _repair_blocked_copy_editor(page, ad_name, destination_url):
            report["why"] = "editor_blocked"
            return False
    if fill_ad_copy(page, title, body, report):
        return True
    if not _review_errors_visible(page):
        # Не блокировка — обычная осечка ввода: одна честная вторая попытка.
        # Блок, который Meta не смогла загрузить, лечится перезагрузкой — до
        # повтора, иначе повтор идёт в тот же пустой блок.
        healed = _heal_failed_component(page, ad_name, report, "ad_copy_retry")
        if not healed and report.get("why") == "copy_fields_not_revealed":
            _back_on_the_ad(page, ad_name, report)
        _await_creative_settled(page)
        report["retry"] = True
        return fill_ad_copy(page, title, body, report)
    if report.get("blocked_before"):
        # Уже лечили в этом объявлении — второй круг тех же минут не стоит.
        _telemetry.record("ad_copy_blocked", False, note="still_blocked_after_repair")
        report["why"] = "editor_blocked_after_repair"
        return False
    _telemetry.record("ad_copy_blocked", False, note="review_error")
    report["blocked_before"] = True
    if not _repair_blocked_copy_editor(page, ad_name, destination_url):
        report["why"] = "editor_blocked"
        return False
    report["retry"] = True
    return fill_ad_copy(page, title, body, report)


def fill_ad_copy(
    page: Page, ad_title: str, ad_body: str, report: dict | None = None
) -> bool:
    """Заполнить основной текст и заголовок объявления.

    `page.keyboard.type()` теряет диакритику на этом поле («Regístrese» →
    «Registrese») — проверено живьём 2026-07-26. Поэтому `insert_text()`:
    вставляет строку целиком и сохраняет ударения, эмодзи и маркеры.

    Первый клик через CDP регулярно рапортует успех, не передав фокус
    странице: текст уходит в никуда, поле остаётся пустым, исключения нет.
    Ловится это только чтением поля после ввода, поэтому и вторая попытка.
    Проверено живьём 2026-09-09: заголовок ложился со второй попытки во
    всех трёх объявлениях подряд.

    Неудачная попытка — не отказ шага, и в `report` она кладётся именно
    попыткой. До 9 сентября каждая писалась отдельной строкой `ok=False`, и
    шаг, который на деле не падал ни разу, имел в статистике надёжности
    сто процентов отказов.
    """
    report = report if report is not None else {}
    report.setdefault("attempts", [])
    if not _reveal_ad_copy_fields(page):
        # 20.09 19:29 (CO_bnc Mix): в предпросмотре объявления текст стоял, а
        # шаг отказал с пустым списком попыток — поля просто не раскрылись.
        report["why"] = "copy_fields_not_revealed"
        _telemetry.record("ad_copy_fields", False, note="not_revealed")
        return False
    settled = _await_creative_settled(page)
    report["settled"] = settled
    # Сначала убираем варианты Meta, потом вводим свой текст: иначе наш текст
    # соседствует с сочинёнными и в показ идёт что попало.
    report["variations_off"] = disable_text_variations(page)
    report["generation_off"] = disable_text_generation(page)

    def _click_via_cdp_handle(locator):
        return _focus_via_cdp_click(page, locator)

    def _handle_has_content(handle) -> bool:
        try:
            return bool(handle.evaluate(
                "el => (el.value !== undefined ? el.value : el.textContent || '').trim().length > 0"
            ))
        except Exception:
            return False

    def _handle_shows(handle, expected: str) -> bool:
        """Поле показывает ровно тот текст, что мы вводили (пробелы не в счёт).

        «Непусто» — не проверка: 16.09 18:53, три браузера разом, в заголовок
        легло «★★★★☆.6k» вместо заголовка целиком, часть нажатий пропала, а
        шаг принял это за успех. Основной текст в том же прогоне — пустой.
        """
        return _copy_matches(_handle_text(handle), expected)

    def _type_and_confirm(handle, text: str) -> bool:
        """Набрать; если экран показывает не то — стереть и вставить целиком.

        Вставка `insert_text` не теряет символов (одна операция, без
        отдельных нажатий), поэтому она — запасной путь, когда нажатия
        потерялись.
        """
        # 21.09 15:10 (скриншот пользователя): в поле лежал наш текст,
        # повторённый подряд много раз, со склейкой посреди слова — каждая
        # попытка ДОПИСЫВАЛА его к прежнему. Поле всегда чистим до ввода.
        try:
            handle.focus()
            _draft_orphans(handle, strip=True)
            page.keyboard.press("ControlOrMeta+A")
            page.keyboard.press("Backspace")
            _sleep_h(0.25)
            left = _handle_text(handle)
            # Корень редактора Draft.js не обнулять никогда: `textContent = ''`
            # сносит узлы, которыми управляет React, и редактор ломается
            # (ACL-1313). Для обычного поля — прежняя страховка.
            if left.strip() and not _is_draft_editor(handle):
                # Select-all не сработал (поле отдало фокус) — чистим начисто.
                handle.evaluate(
                    "el => { if ('value' in el) { el.value = ''; } else { el.textContent = ''; }"
                    " el.dispatchEvent(new Event('input', {bubbles: true})); }")
                _sleep_h(0.25)
        except Exception:
            pass
        _type_human(page, text)
        _sleep_h(0.4)
        if _handle_shows(handle, text):
            return True
        # 22.09 01:05 (скриншот пользователя, CR_ait IPO): дописывание
        # потерянного хвоста задвоило текст — поле ещё дорисовывало вставку,
        # чтение увидело только первую строку, и остаток дописался поверх уже
        # пришедшего. Задвоенный текст хуже неполного, поэтому дописывания
        # больше нет: ниже поле стирается и набирается заново целиком.
        # 21.09 11:33 (CL_ml): «видим» выглядело правильным текстом, но шаг
        # считал его несовпавшим — по обрезанной записи не понять, где
        # разошлось. Пишем оба текста и их длины.
        seen_now = _handle_text(handle)
        report["attempts"].append({
            "stage": "typed_text_mismatch",
            "seen": seen_now[:120], "seen_len": len(seen_now),
            "want": text[:120], "want_len": len(text),
            "tail_seen": seen_now[-40:], "tail_want": text[-40:],
            "orphans": _draft_orphans(handle),
        })
        try:
            handle.focus()
            _draft_orphans(handle, strip=True)
            page.keyboard.press("ControlOrMeta+A")
            page.keyboard.press("Backspace")
            _insert_lines(page, text)
        except Exception:
            return False
        _sleep_h(0.4)
        return _handle_shows(handle, text)

    # Both fields verified post-insert — Playwright's own click can report
    # success under connect_over_cdp without the page actually receiving
    # focus (see _click_via_cdp docstring above), silently leaving the
    # field empty even though no exception was raised. Don't trust a clean
    # run of insert_text() alone; check the field's real content after.
    def _dump_stage(what: str, stage: str, extra: dict | None = None) -> None:
        # Diagnostic (2026-07-27): pin down EXACTLY which stage fails —
        # locator not found, click/bounding-box failed, or content
        # verification failed after a clean click+insert — instead of
        # collapsing all of these into one opaque False.
        try:
            info = page.evaluate(
                "() => { const el = document.getElementById('creativeEditorComponent');"
                " const style = el ? getComputedStyle(el) : null;"
                " let ariaHiddenAncestor = null; let cur = el;"
                " while (cur) { if (cur.getAttribute && cur.getAttribute('aria-hidden') === 'true') {"
                "   ariaHiddenAncestor = cur.tagName + '.' + (cur.className||''); break; } cur = cur.parentElement; }"
                " return { containerFound: !!el, containerDisplay: style ? style.display : null,"
                " containerOffsetParent: el ? el.offsetParent !== null : null, ariaHiddenAncestor,"
                " comboboxCount: document.querySelectorAll('[role=combobox]').length,"
                " placeholderFieldCount: document.querySelectorAll('[placeholder]').length }; }"
            )
            report["attempts"].append(
                {"field": what, "stage": stage, **info, **(extra or {})}
            )
        except Exception:
            pass

    def _locate(key: str, mark: str):
        """Локатор поля — по разметочному имени Meta, иначе по подписи."""
        if mark_ad_copy_field(page, key, _AD_COPY_FIELDS.get(key), mark):
            return page.locator(f'[data-rpa-field="{mark}"]').first
        return None

    def _fill_body() -> bool:
        try:
            primary_box = _locate("message_field", "body")
            count = primary_box.count() if primary_box is not None else 0
            if count:
                handle = _click_via_cdp_handle(primary_box)
                if handle is not None:
                    if _type_and_confirm(handle, ad_body):
                        return True
                    _dump_stage("primary_text", "content_check_failed_after_insert", {"count": count})
                else:
                    _dump_stage("primary_text", "click_via_cdp_failed", {"count": count})
            else:
                _dump_stage("primary_text", "locator_count_zero", {"count": count})
        except Exception as exc:
            _dump_stage("primary_text", f"exception:{str(exc)[:80]}")
        return False

    def _fill_title() -> bool:
        try:
            headline_box = _locate("headline_field", "title")
            count = headline_box.count() if headline_box is not None else 0
            if count:
                handle = _click_via_cdp_handle(headline_box)
                if handle is not None:
                    page.keyboard.press("ControlOrMeta+A")
                    if _type_and_confirm(handle, ad_title):
                        return True
                    _dump_stage("headline", "content_check_failed_after_insert", {"count": count})
                else:
                    _dump_stage("headline", "click_via_cdp_failed", {"count": count})
            else:
                _dump_stage("headline", "locator_count_zero", {"count": count})
        except Exception as exc:
            _dump_stage("headline", f"exception:{str(exc)[:80]}")
        return False

    # Confirmed live 2026-07-29 (Test143, via debug_ad_copy_locator_miss
    # .json): `#creativeEditorComponent` can flip BACK to display:none
    # after _reveal_ad_copy_fields() already succeeded — the dump showed
    # content_check_failed_after_insert with containerDisplay:'none' for
    # both fields, i.e. Meta re-hid the container between the reveal and
    # the typing, and the (correct) focus-fallback typed into a hidden
    # field. One re-reveal + retry cycle covers that re-hide.
    body_ok = not ad_body
    title_ok = not ad_title
    for attempt in range(2):
        if not body_ok:
            body_ok = _fill_body()
        if not title_ok:
            title_ok = _fill_title()
        if body_ok and title_ok:
            break
        if attempt == 0:
            _reveal_ad_copy_fields(page)
            _sleep_h(0.5)

    return body_ok and title_ok


def _browser_addon_none_checked(page: Page) -> bool | None:
    """Is Destination -> Browser add-ons -> "None" actually selected?

    Returns None when the control can't be located at all, so callers can
    tell "definitely wrong" apart from "couldn't tell".

    Scoped to the "Browser add-ons" radio group when it can be found: a
    full-editor page has several other exact-"None" leaves ("Excluded
    placements: None" etc.) — an unscoped first-match probe was observed
    (2026-07-29, Test143 verification) reporting false on ads whose actual
    Browser add-ons radio was fine.
    """
    try:
        return page.evaluate(
            """
            () => {
              const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
              const leaves = [...document.querySelectorAll('*')].filter(e =>
                e.offsetParent !== null && e.children.length === 0);
              const scopes = [];
              const bao = leaves.find(e => norm(e.textContent) === 'Browser add-ons');
              if (bao) {
                let n = bao;
                for (let i = 0; i < 8 && n; i++) {
                  n = n.parentElement;
                  if (n && n.querySelectorAll('[role=radio], input[type=radio]').length >= 3) {
                    scopes.push(n);
                    break;
                  }
                }
              }
              scopes.push(document);
              for (const scope of scopes) {
              const labels = [...scope.querySelectorAll('*')].filter(e =>
                e.offsetParent !== null && e.children.length === 0 && norm(e.textContent) === 'None');
              for (const label of labels) {
                let node = label;
                for (let i = 0; i < 6 && node; i++) {
                  const radio = node.querySelector('[role=radio], input[type=radio]');
                  if (radio) {
                    const v = radio.getAttribute('aria-checked');
                    if (v !== null) return v === 'true';
                    return !!radio.checked;
                  }
                  node = node.parentElement;
                }
              }
              }
              return null;
            }
            """
        )
    except Exception:
        return None


def _select_browser_addon_none(page: Page) -> bool:
    """Select Destination -> Browser add-ons -> "None", and verify it took.

    Confirmed live (2026-07-29, user screenshot): clicking the "None" *text
    label* is not enough — the radio stayed on Meta's default "Instant form
    (suggested)" while the Website URL beside it filled in correctly and
    the footer read "All edits saved". The ad then failed validation with
    "Param call_to_action[value][lead_gen_form_id] must be a valid Lead Gen
    Data id". Same trap as the media-library tiles: the handler lives on
    the control, not on its caption.

    So: aim at the radio itself first, fall back to the label, and confirm
    `aria-checked` afterwards rather than trusting the click.

    Confirmed live (2026-08-02, ad 120250948999160213): on this render the
    group is FOUR native `input[type=radio]` (None / Call / WhatsApp /
    Instant form (suggested)) and a real mouse click dead-center on the
    None input changes NOTHING — not even for 300ms (watched at t+0.3s,
    t+1.5s, t+4s, t+10s; input enabled, pointer-events auto, hit-target
    confirmed via elementFromPoint). What DOES work is the native radio
    group keyboard protocol: focus the currently-checked radio and press
    ArrowUp until index 0 ("None") is checked — selection follows focus.
    So keyboard is the PRIMARY path now, clicks are the fallback.

    Also established the same day: "Instant form (suggested)" is a UI-side
    suggestion Meta re-applies every time the editor opens; it only
    corrupts the ad (call_to_action -> lead_gen) if an autosave fires
    while it is checked. Selecting None + letting autosave settle wipes
    the lead_gen state server-side.
    """
    for attempt in range(3):
        if _browser_addon_none_checked(page) is True:
            return True

        # PRIMARY: keyboard. Focus whichever radio in the group is checked
        # (or the last one) and walk ArrowUp — native groups move selection
        # together with focus, and key events via CDP are trusted input.
        try:
            focused = page.evaluate(
                """
                () => {
                  const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                  const leaves = [...document.querySelectorAll('*')].filter(e =>
                    e.offsetParent !== null && e.children.length === 0);
                  const bao = leaves.find(e => norm(e.textContent) === 'Browser add-ons');
                  if (!bao) return false;
                  let scope = null, n = bao;
                  for (let i = 0; i < 10 && n; i++) {
                    n = n.parentElement;
                    if (n && n.querySelectorAll('[role=radio], input[type=radio]').length >= 2) {
                      scope = n; break;
                    }
                  }
                  if (!scope) return false;
                  const radios = [...scope.querySelectorAll('[role=radio], input[type=radio]')]
                    .filter(r => r.offsetParent !== null);
                  if (!radios.length) return false;
                  const cur = radios.find(r =>
                    r.getAttribute('aria-checked') === 'true' || r.checked) || radios[radios.length - 1];
                  cur.scrollIntoView({ block: 'center' });
                  cur.focus();
                  return document.activeElement === cur;
                }
                """
            )
        except Exception:
            focused = False

        if focused:
            for _ in range(5):
                try:
                    page.keyboard.press("ArrowUp")
                except Exception:
                    break
                _sleep_h(0.5)
                if _browser_addon_none_checked(page) is True:
                    return True

        # FALLBACK: the old click paths (kept for renders where the group
        # exposes role=radio without native inputs).
        clicked = False
        try:
            radio = page.get_by_role("radio", name=re.compile(r"^\s*None\b", re.I)).first
            if radio.count():
                radio.scroll_into_view_if_needed(timeout=3000)
                radio.click(timeout=3000)
                clicked = True
        except Exception:
            clicked = False

        if not clicked:
            try:
                label = page.get_by_text("None", exact=True).first
                if not label.count():
                    return False
                label.scroll_into_view_if_needed(timeout=3000)
                label.click(timeout=3000)
                clicked = True
            except Exception:
                return False

        _sleep_h(0.5)
        if _browser_addon_none_checked(page) is True:
            return True

    # Unreadable state is not treated as failure — some renders expose no
    # radio role at all, and the caller still cross-checks the URL field.
    return _browser_addon_none_checked(page) is not False


_DESTINATION_PLACEHOLDER = "Enter the URL people visit after your ad"


def destination_url_box(page: Page):
    """Поле «Website URL» объявления — или `None`, если его нет на странице.

    Одно место на весь файл. До 9 сентября 2026 поле искали в четырёх
    местах по `placeholder="http://www.example.com/page"`, которого на
    странице нет ни разу: Meta переписала подсказку. Отсюда не только отказ
    самого шага — молча врала и проверка «записалось ли», потому что она
    искала то же самое и всегда отвечала «нет», после чего запускался
    ремонтный цикл поверх правильно заполненного поля.
    """
    if not mark_ad_copy_field(
        page,
        "pce_website_url_field",
        None,
        "dest",
        placeholder=_DESTINATION_PLACEHOLDER,
    ):
        return None
    box = page.locator('[data-rpa-field="dest"]').first
    return box if box.count() else None


def set_ad_destination_website(page: Page, destination_url: str) -> bool:
    """Set the ad's destination to Website + Browser add-ons: None, with the
    real account tracking link — instead of the Instant Form Meta suggests
    by default for Leads objective ads.

    Confirmed live (2026-07-26) by explicit user direction: creating an
    Instant Form is a whole separate sub-wizard (Intro/Questions/Privacy
    policy) that isn't part of this account's real publish process — the
    real flow uses Website with the account's own tracking link instead,
    which also clears the "lead_gen_form_id must be valid" verification
    error Meta shows while an Instant Form is required but missing.

    Confirmed live (2026-07-26): the "Set up your creative" modal doesn't
    always auto-close after Media/Text like it did on other runs — some
    runs leave it open on its own Trim/Crop/Text steps, hiding the main Ad
    page's Destination section underneath. Close it first if still open
    via the shared `_close_creative_setup_modal()` helper.
    """
    _close_creative_setup_modal(page)

    # Confirmed live (2026-07-27): the Destination section's container
    # (`#adgroupAdDestinationGeneric`) can render with the exact same
    # `display: none !important` Meta applies to the Ad creative section's
    # `#creativeEditorComponent` — same underlying quirk, different section.
    # Force it visible the same way before touching "None"/the URL field.
    try:
        page.evaluate(
            "() => { const el = document.getElementById('adgroupAdDestinationGeneric');"
            " if (el) el.style.setProperty('display', 'block', 'important'); }"
        )
    except Exception:
        pass

    # ORDER MATTERS — URL first, "None" LAST. Confirmed live 2026-07-30
    # after a day of chasing "the radio keeps reverting": editing the
    # Website URL field RESETS Browser add-ons back to Meta's suggested
    # "Instant form". The old order (None, then URL) therefore undid its
    # own selection on every single call — which is what fed the repair
    # loops (and, before v70's Ctrl+A, the url+url+url appends).
    # Confirmed live 2026-07-29 (Test141, campaign B ads 2/3): a plain
    # url_box.click() here reported success while the page never received
    # focus (playwright#41286 again), so insert_text() went nowhere and
    # the URL stayed on its placeholder — caught only by the read-back
    # check below. Use the shared CDP-click helper instead, same as the
    # ad-copy fields.
    # Подсказка в поле переписана Meta: «http://www.example.com/page» на
    # странице 9 сентября 2026 не встречается ни разу, теперь там «Enter the
    # URL people visit after your ad». Третий шаг подряд, упавший из-за
    # переписанной формулировки, — поэтому ищем не по ней, а по разметочному
    # имени Meta `pce_website_url_field`, и только запасным ходом по
    # подсказке. Проверено стендом на настоящей разметке из снимка отказа.
    try:
        if not mark_ad_copy_field(
            page,
            "pce_website_url_field",
            None,
            "dest",
            placeholder="Enter the URL people visit after your ad",
        ):
            return False
        url_box = page.locator('[data-rpa-field="dest"]').first
        if not url_box.count():
            return False
        if _focus_via_cdp_click(page, url_box) is None:
            return False
        # Select-all BEFORE inserting: insert_text APPENDS at the caret, it
        # does not replace. Confirmed live 2026-07-30: every destination
        # retry and every sweep re-fix appended another copy of the URL, so
        # the field grew url+url+url... until Meta's "Your website URL
        # exceeds 1024 characters" error — which is also why the read-back
        # equality check kept failing and the repair loops never converged.
        page.keyboard.press("Control+A")
        # Fast insert here, NOT _type_human. This is a 255-char tracking
        # URL — a technical value nobody types by hand, so there is nothing
        # to disguise, and typing it took ~70s during which Meta re-rendered
        # the Destination section and reset the "None" radio we set right
        # afterwards (confirmed live 2026-07-31, Test155: destination failed
        # in BOTH campaigns, server showed the lead_gen banner on campaign
        # B). Human-paced typing stays where it matters — the ad copy a real
        # person would actually write.
        page.keyboard.insert_text(destination_url)
        _sleep_h(0.4)
    except Exception:
        return False

    if not _select_browser_addon_none(page):
        return False

    # Confirmed live (2026-07-28): the same async-save-race class as the
    # campaign name/budget fields — Meta's debounced autosave can silently
    # revert the ad's Destination back to Instant Form/Lead Gen minutes
    # later with no exception raised anywhere, surfacing only much later as
    # a real validation error ("Param call_to_action[value][lead_gen_form_id]
    # must be a valid Lead Gen Data id"). Give the debounced autosave a
    # moment to actually start, wait for "Verifying your edits..." to
    # clear, then read the URL field back before trusting it — retrying the
    # None+URL selection if it didn't stick.
    wait_editor_saved(page)

    def _url_matches() -> bool:
        try:
            box = destination_url_box(page)
            if box is None:
                return False
            return box.input_value(timeout=2000).strip() == destination_url.strip()
        except Exception:
            return False

    def _both_ok() -> bool:
        if not _url_matches():
            return False
        # None (unreadable state) must not fail the whole step — but a
        # definite False must, since that is the exact broken state seen
        # live: URL correctly filled and "All edits saved" showing, while
        # Browser add-ons had silently snapped back to "Instant form".
        return _browser_addon_none_checked(page) is not False

    if not _both_ok():
        for _ in range(2):
            try:
                retry_box = destination_url_box(page)
                if retry_box is not None and _focus_via_cdp_click(page, retry_box) is not None:
                    page.keyboard.press("Control+A")  # replace, never append
                    page.keyboard.insert_text(destination_url)  # fast: see note above
                # None AFTER the URL — see the ordering note above.
                _select_browser_addon_none(page)
                wait_editor_saved(page)
            except Exception:
                pass
            if _both_ok():
                break

    return _both_ok()


def _audience_already_manual(page: Page) -> bool:
    """Аудитория уже в ручном режиме: открытого диалога нет, а на экране
    «Switch to recommended setup» (кнопка возврата к рекомендованному)."""
    try:
        return bool(page.evaluate(
            r"""() => {
              const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
              const dialogOpen = [...document.querySelectorAll('[role=dialog]')]
                .some(d => d.getBoundingClientRect().width > 0 && /further limit the reach/i.test(d.textContent || ''));
              if (dialogOpen) return false;
              return [...document.querySelectorAll('*')].some(e =>
                e.children.length === 0 && norm(e.textContent) === 'Switch to recommended setup'
                && e.getBoundingClientRect().width > 0);
            }"""
        ))
    except Exception:
        return False


def _tree_new_ad_shells(page: Page) -> int:
    """Сколько строк «New Leads ad» в левом дереве (-1 — не прочитать)."""
    try:
        return int(page.evaluate(
            r"""() => {
              const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
              return [...document.querySelectorAll('*')].filter(e => {
                if (e.children.length !== 0 || !/^New Leads ad$/i.test(norm(e.textContent))) return false;
                const r = e.getBoundingClientRect();
                return r.width > 0 && r.x < window.innerWidth * 0.34;
              }).length;
            }"""
        ))
    except Exception:
        return -1


def _open_tree_ad(page: Page, ad_name: str) -> bool:
    """Select an ad in the left campaign-structure tree by name.

    Prefix-tolerant (narrow layouts truncate row names — same finding as
    create_additional_ad, 2026-07-29). The current ad's name also appears
    in the top breadcrumb, so among all matches the LEFTMOST one is taken:
    the tree panel hugs the left edge, the breadcrumb does not.
    """
    point = page.evaluate(
        """(adName) => {
          const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
          // Без учёта регистра: Doc20 называет заготовку «New Leads ad»,
          // S15/Doc21 — «New Leads Ad». Точное сравнение не находило
          // заготовку, и вызывающий создавал второе объявление (17.09).
          const want = adName.toLowerCase();
          const nameMatches = (raw) => {
            const t = norm(raw).replace(/[…\\u2026]+$/, '').toLowerCase();
            if (!t) return false;
            if (t === want) return true;
            return t.length >= 10 && want.startsWith(t);
          };
          const cands = [...document.querySelectorAll('*')].filter(e =>
            e.offsetParent !== null && e.children.length === 0 && nameMatches(e.textContent));
          let best = null, bx = Infinity;
          for (const c of cands) {
            const r = c.getBoundingClientRect();
            if (r.width > 0 && r.x < bx) { bx = r.x; best = r; }
          }
          return best ? {x: best.x + best.width / 2, y: best.y + best.height / 2} : null;
        }""",
        ad_name,
    )
    if not point:
        return False
    try:
        _cdp_mouse_click(page, point["x"], point["y"])
    except Exception:
        return False
    _sleep_h(1.5)
    wait_editor_saved(page)
    return True


def _ad_destination_persisted(page: Page, destination_url: str) -> bool:
    """Does the currently open ad still hold Website URL + add-ons None?

    The editor mounts its sections lazily — scroll toward the bottom and
    re-check a few times before concluding the field isn't there.
    """
    for _ in range(4):
        try:
            if page.get_by_text(
                re.compile(r"must be a valid Lead Gen", re.I)
            ).first.is_visible(timeout=500):
                return False
        except Exception:
            pass
        try:
            box = destination_url_box(page)
            if box is not None:
                if (box.input_value(timeout=2000) or "").strip() == destination_url.strip():
                    return _browser_addon_none_checked(page) is not False
                return False
        except Exception:
            pass
        try:
            page.evaluate(
                """
                () => {
                  const all = [...document.querySelectorAll('*')];
                  const c = all.find(e => {
                    const s = getComputedStyle(e);
                    return (s.overflowY === 'auto' || s.overflowY === 'scroll')
                      && e.scrollHeight > e.clientHeight + 50 && e.scrollHeight > 1500;
                  });
                  if (c) c.scrollTop = c.scrollHeight;
                }
                """
            )
        except Exception:
            pass
        _sleep_h(1.0)
    return False


def verify_and_fix_ad_destinations(
    page: Page, ad_names: list[str], destination_url: str
) -> list[str]:
    """Re-visit every ad of the campaign and re-fix reverted destinations.

    Confirmed live 2026-07-29 (Test143): Meta can revert an ad's Browser
    add-ons radio back to "Instant form" AFTER the in-flow save-wait and
    read-back already passed — the revert lands once the flow has moved on
    to the next ad. A single end-of-campaign sweep catches those. Returns
    the names that could not be verified or repaired (soft-fail contract,
    same as the per-ad steps).
    """
    failed: list[str] = []
    for name in ad_names:
        if not _open_tree_ad(page, name):
            failed.append(f"{name}:row_not_found")
            continue
        ok = _ad_destination_persisted(page, destination_url)
        if not ok:
            try:
                ok = set_ad_destination_website(page, destination_url)
            except Exception:
                ok = False
        if not ok:
            failed.append(name)

    # Tail stability watch. Confirmed live twice (Test143 AND Test144,
    # 2026-07-29): the ad left open when the flow ends — always the LAST
    # one filled/swept — gets its Browser add-ons radio reverted to
    # "Instant form" AFTER its check passed and the run moved on. A single
    # point-in-time pass can therefore never certify the final ad. Sit on
    # it until its destination survives two consecutive checks ~6s apart,
    # re-fixing in between; give up (soft-fail) after 4 rounds.
    if ad_names:
        last = ad_names[-1]
        stable = 0
        for _ in range(4):
            time.sleep(6.0)
            if _ad_destination_persisted(page, destination_url):
                stable += 1
                if stable >= 2:
                    break
            else:
                stable = 0
                try:
                    set_ad_destination_website(page, destination_url)
                except Exception:
                    pass
        else:
            failed.append(f"{last}:unstable_after_sweep")
    return failed


#: Найти крестики баннеров и пометить их `data-acl-close`; вернуть имена.
_BANNERS_JS = r"""
  (args) => {
    const [titles, aymt] = args;
    const norm = (s) => (s || '').replace(/[\u200b-\u200d\ufeff]/g, '').replace(/\s+/g, ' ').trim();
    const shown = (e) => {
      if (!e) return false;
      const r = e.getBoundingClientRect();
      if (r.width <= 0 || r.height <= 0) return false;
      for (let n = e; n; n = n.parentElement) {
        const s = getComputedStyle(n);
        if (s.visibility === 'hidden' || s.display === 'none') return false;
      }
      return true;
    };
    // Крестик всего редактора: единственный aria-label="Close" в редакторе,
    // прямо под data-surface="/am/editor_drawer". Его не трогаем никогда.
    const drawerClose = (b) => b.getAttribute('aria-label') === 'Close' && (() => {
      const s = b.closest('[data-surface]');
      return !!s && s.getAttribute('data-surface') === '/am/editor_drawer';
    })();
    const drawerCloses = [...document.querySelectorAll('[aria-label="Close"]')].filter(drawerClose);
    // Кнопки, которые нажимать нельзя: они не закрывают, а ведут дальше.
    const FORBIDDEN = /^(Add\b|Learn more|Start verification|Remind me later|Apply now|Get started|View Terms|Review terms|Go to\b|Use recommended|Create a rule)/i;
    const closers = (box) => [...box.querySelectorAll('[role=button], button')].filter((b) => {
      if (drawerClose(b)) return false;
      const text = norm(b.textContent);
      if (FORBIDDEN.test(text)) return false;
      const label = norm(b.getAttribute('aria-label'));
      return /^(Close|Cerrar)$/.test(text)
        || ((!text || /^[\u00d7\u2715]$/.test(text)) && /^(Close|Dismiss|Cerrar)$/i.test(label));
    });
    // Контейнер баннера: ближайший предок заголовка с СОБСТВЕННЫМ крестиком —
    // ровно одним. Два крестика — это уже общий родитель нескольких карточек;
    // предок, внутри которого крестик всего редактора, — это сам редактор.
    const containerOf = (leaf) => {
      let box = leaf;
      for (let i = 0; i < 9 && box.parentElement; i++) {
        box = box.parentElement;
        if (drawerCloses.some((d) => box.contains(d))) return null;
        const c = closers(box);
        if (c.length === 1) return { box, btn: c[0] };
        if (c.length > 1) return null;
      }
      return null;
    };
    const found = [];
    const seen = new Set();
    const take = (name, btn) => {
      if (seen.has(btn) || !shown(btn)) return;
      seen.add(btn);
      btn.setAttribute('data-acl-close', String(found.length));
      found.push(name);
    };
    // Заголовок — лист с текстом или элемент role=heading (у карточек
    // редактора это div[role=heading][aria-level=4]).
    const leaves = [...document.querySelectorAll('*')]
      .filter((e) => (e.children.length === 0 || e.getAttribute('role') === 'heading'));
    for (const leaf of leaves) {
      const text = norm(leaf.textContent);
      if (!text || text.length > 200) continue;
      const title = titles.find((t) => text.startsWith(t));
      if (!title || !shown(leaf)) continue;
      const got = containerOf(leaf);
      if (got) take(title, got.btn);
    }
    if (aymt) {
      // Синяя полоса вверху: у каждого сообщения свой data-surface.
      for (const msg of document.querySelectorAll('[data-surface*="AYMTAMGlobalBannerContainer/"]')) {
        const c = closers(msg);
        if (c.length !== 1) continue;
        const first = norm(msg.textContent).slice(0, 60);
        take('aymt:' + first, c[0]);
      }
    }
    return found;
  }
"""


#: Баннеры и карточки Meta, которые закрываются своим крестиком (ACL-1313).
#: Совпадение — по началу текста заголовка.
_BANNER_TITLES = (
    "Add your contact information",
    "Add your phone number",
    "Updated business terms",
    "Act now",
    "Verification may be required",
    "WhatsApp Status included",
    "Use filters to verify Instant form leads",
    "Set up the Conversions API",
    "Add products or site links",
    "Value rule creation is changing",
)


def _click_close_mark(page: Page, selector: str) -> bool:
    """Нажать помеченную кнопку: доверенным кликом, если центр открыт.

    Баннер бывает прикрыт модалкой «Loading…» (279ead, 25.09): тогда клик по
    координатам попал бы в модалку, и нажатие уходит DOM-кликом прямо в
    кнопку. Прокрутки нет — баннеры на своих местах, а прокрутка редактора
    сбивает текущий шаг.
    """
    try:
        spot = page.evaluate(
            """
            (sel) => {
              const b = document.querySelector(sel);
              if (!b) return null;
              const r = b.getBoundingClientRect();
              const x = r.x + r.width / 2, y = r.y + r.height / 2;
              const inView = x > 0 && y > 0 && x < innerWidth && y < innerHeight;
              const hit = inView ? document.elementFromPoint(x, y) : null;
              return { x, y, open: !!hit && (hit === b || b.contains(hit)) };
            }
            """,
            selector,
        )
    except Exception:
        return False
    if not spot:
        return False
    if spot.get("open"):
        try:
            _cdp_mouse_click(page, spot["x"], spot["y"])
            return True
        except Exception:
            pass
    try:
        return bool(page.evaluate(
            "(sel) => { const b = document.querySelector(sel); if (!b) return false; b.click(); return true; }",
            selector))
    except Exception:
        return False


def _close_banners(page: Page, titles: tuple[str, ...] = _BANNER_TITLES, *, aymt: bool = True) -> list[str]:
    """Закрыть баннеры/карточки Meta их собственным крестиком. Что закрыли.

    По образцу `_close_account_controls_toast`: заголовок → его контейнер →
    `[role=button]` с текстом «Close» внутри этого контейнера и только там.
    Крестик всего редактора (`aria-label="Close"` под
    `data-surface="/am/editor_drawer"`) не нажимается никогда, как и кнопки
    «Add…», «Learn more», «Start verification», «Remind me later», «Apply
    now».
    """
    closed: list[str] = []
    for _ in range(3):
        try:
            found = page.evaluate(_BANNERS_JS, [list(titles), aymt]) or []
        except Exception:
            break
        if not found:
            break
        progress = False
        for i, name in enumerate(found):
            if _click_close_mark(page, f'[data-acl-close="{i}"]'):
                progress = True
                if name not in closed:
                    closed.append(name)
        try:
            page.evaluate("() => document.querySelectorAll('[data-acl-close]')"
                          ".forEach(e => e.removeAttribute('data-acl-close'))")
        except Exception:
            pass
        if not progress:
            break
        _sleep_h(0.5)
    return closed

_AD_BLOCKER_JS = r"""
  () => {
    const norm = (s) => (s || '').replace(/[\u200b-\u200d\ufeff]/g, '').replace(/\s+/g, ' ').trim();
    const shown = (e) => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
    const title = [...document.querySelectorAll('*')].find((e) =>
      (e.children.length === 0 || e.getAttribute('role') === 'heading')
      && /^Turn off ad blocker/i.test(norm(e.textContent)) && shown(e));
    if (!title) return '';
    const drawerClose = (b) => b.getAttribute('aria-label') === 'Close' && (() => {
      const s = b.closest('[data-surface]');
      return !!s && s.getAttribute('data-surface') === '/am/editor_drawer';
    })();
    const buttons = (box) => [...box.querySelectorAll('[role=button], button')]
      .filter((b) => shown(b) && !drawerClose(b));
    // Своё окно: ближайший role=dialog, иначе ближайший предок с кнопкой OK.
    let box = title.closest('[role=dialog]');
    if (!box) {
      box = title;
      for (let i = 0; i < 8 && box.parentElement; i++) {
        box = box.parentElement;
        if (buttons(box).some((b) => norm(b.textContent) === 'OK')) break;
      }
    }
    const ok = buttons(box).find((b) => norm(b.textContent) === 'OK');
    const x = ok ? null : buttons(box).find((b) => {
      const t = norm(b.textContent);
      return t === 'Close' || (!t && /^(Close|Dismiss)$/i.test(b.getAttribute('aria-label') || ''));
    });
    const btn = ok || x;
    if (!btn) return 'no_button';
    btn.setAttribute('data-acl-close', 'adblock');
    return ok ? 'ok' : 'x';
  }
"""
"""Окно «Turn off ad blocker»: пометить его OK (или крестик) для нажатия."""


def _close_ad_blocker_modal(page: Page) -> str:
    """Закрыть «Turn off ad blocker». Чем закрыли: "ok" / "x" / "" (окна нет).

    279ead80503a, 25.09: после Create поверх списка встало это окно (и тост
    «Your internet connection was lost»), под ним — вечная «Loading…». Meta
    так говорит, что часть её запросов не проходит: сеть или прокси профиля.
    """
    try:
        how = page.evaluate(_AD_BLOCKER_JS) or ""
    except Exception:
        return ""
    if how in ("ok", "x"):
        _click_close_mark(page, '[data-acl-close="adblock"]')
        _sleep_h(0.6)
        try:
            page.evaluate("() => document.querySelectorAll('[data-acl-close]')"
                          ".forEach(e => e.removeAttribute('data-acl-close'))")
        except Exception:
            pass
    return how


#: Тост «Your internet connection was lost. Some of your work may not be
#: available.» — закрывается своим крестиком, как баннеры.
_NETWORK_TOAST_TITLES = ("Your internet connection was lost",)


def _clear_blocking_overlays(page: Page, where: str, *, editor: bool = True) -> list[str]:
    """Убрать всё, что перекрывает клики: окно ad blocker, тост сети, баннеры.

    Зовётся в начале каждого раздела (кампания, ad set, объявления) и перед
    каждым Create. Возвращает, что закрыли, — это же уходит в телеметрию.

    Окно «Turn off ad blocker» — отдельный случай (просьба владельца): его
    закрываем OK или крестиком и перезагружаем страницу — на списке кампаний
    всегда, в редакторе только при «All edits saved». В телеметрию —
    `overlays_cleared` с пометкой `network_trouble`: это сигнал о сети или
    прокси профиля, а не о разметке Meta.
    """
    closed: list[str] = []
    trouble = _network_trouble(page)
    how = _close_ad_blocker_modal(page)
    if how:
        closed.append(f"ad_blocker_modal:{how}")
    closed += _close_banners(page, _BANNER_TITLES + _NETWORK_TOAST_TITLES)
    if how or trouble:
        reloaded = False
        why = ""
        if how or not editor:
            if editor and not _all_edits_saved(page):
                why = "unsaved_edits"
            else:
                _reload_editor(page)
                reloaded = True
        closed.append("network_trouble:" + ("reloaded" if reloaded else (why or "not_reloaded")))
        _telemetry.record("overlays_cleared", None, note="network_trouble",
                          diag={"where": where, "closed": closed, "reloaded": reloaded,
                                "url": (page.url or "")[:160]})
    elif closed:
        _telemetry.record("overlays_cleared", None, note="closed",
                          diag={"where": where, "closed": closed})
    return closed


def _dismiss_advantage_banner(page: Page) -> list[str]:
    """Закрыть рекомендацию Advantage+ после выбора Website — её крестиком.

    Баннер «You could get 14-24% lower cost per lead by choosing website and
    instant forms» с кнопкой «Apply now»: «Apply now» НЕ нажимаем (вернёт
    website+forms). Раньше здесь искали `[aria-label="Close"]` по всей
    странице — а это крестик ВСЕГО редактора (ACL-1313): у крестиков баннеров
    такого атрибута нет. Теперь — тот же поиск «заголовок → контейнер →
    Close», что и у прочих баннеров, плюс собственная метка рекомендации
    «Close recommendation», если Meta её поставила.
    """
    closed: list[str] = []
    try:
        rec = page.locator('[aria-label="Close recommendation"]').first
        if rec.count() and rec.is_visible(timeout=300):
            rec.evaluate("el => el.setAttribute('data-acl-close', 'rec')")
            if _click_close_mark(page, '[data-acl-close="rec"]'):
                closed.append("close_recommendation")
    except Exception:
        pass
    closed += _close_banners(page, ("You could get",) + _BANNER_TITLES, aymt=False)
    return closed

def dismiss_discard_drafts_dialog(page: Page) -> bool:
    """Dismiss the 'Discard drafts' confirmation — always Cancel, NEVER Discard.

    Distinct from dismiss_publish_draft_dialog: this is the manage-list toolbar's
    'Discard drafts' button confirmation ("Any changes ... will be discarded."),
    triggered by a stray click landing on that toolbar button (e.g. a stale
    coordinate fallback). Left un-dismissed it blocks every subsequent step; if
    ever auto-clicked wrong it would delete the campaign. Only Cancel is safe.
    """
    for root in _roots(page):
        try:
            dialog = root.locator(
                "[role=dialog]:has-text('Discard drafts'), "
                "[role=dialog]:has-text('will be discarded')"
            ).first
            if not dialog.is_visible(timeout=600):
                continue
            btn = dialog.get_by_role("button", name=re.compile(r"^(Cancel|Cancelar)$", re.I)).first
            if btn.is_visible(timeout=600):
                btn.click(timeout=3000, force=True)
                _sleep_h(0.4)
                return True
        except Exception:
            pass
    return False


def dismiss_publish_draft_dialog(page: Page) -> bool:
    """Dismiss draft review modals that block Ads Manager clicks.

    Handles both:
      - 'Publish draft items?' → Close
      - 'Review draft items' (with errors) → Cancel  (Close is often absent)

    Never clicks Publish / Fix error — RPA must keep editing.
    """
    dismiss_re = re.compile(r"^(Close|Cancel|Cerrar|Cancelar)$", re.I)
    # The campaign wizard itself is often role=dialog with an "In draft" badge and
    # a bottom Close button — matching it here and clicking Close CLOSES THE WIZARD,
    # not a popup (this reproduced live: "draft closes after every step", because
    # this function runs before every single step). Never treat the wizard's own
    # chrome as a dismissible draft-review modal.
    _WIZARD_CHROME_MARKERS = (
        "campaign name", "daily budget", "ad set name", "conversion location",
        "nombre de la campaña", "campaign score", "campaign details",
    )
    for root in _roots(page):
        # Strategy 1: dialog with draft wording → Close or Cancel
        try:
            dialog = root.locator(
                "[role=dialog]:has-text('Publish draft'), "
                "[role=dialog]:has-text('Review draft'), "
                "[role=dialog]:has-text('draft items')"
            ).first
            if dialog.is_visible(timeout=1000):
                try:
                    dialog_text = (dialog.inner_text(timeout=800) or "").lower()
                except Exception:
                    dialog_text = ""
                if any(marker in dialog_text for marker in _WIZARD_CHROME_MARKERS):
                    continue  # this is the wizard, not a confirm popup — skip to Strategy 2's own guard
                for name_re in (
                    re.compile(r"^Cancel$", re.I),
                    re.compile(r"^Close$", re.I),
                    re.compile(r"^Cancelar$", re.I),
                    re.compile(r"^Cerrar$", re.I),
                ):
                    try:
                        btn = dialog.get_by_role("button", name=name_re).first
                        if btn.is_visible(timeout=400):
                            btn.click(timeout=3000, force=True)
                            _sleep_h(0.45)
                            return True
                    except Exception:
                        pass
                # Fallback: any dismiss-looking button in the dialog
                try:
                    btn = dialog.get_by_role("button", name=dismiss_re).first
                    if btn.is_visible(timeout=400):
                        btn.click(timeout=3000, force=True)
                        _sleep_h(0.45)
                        return True
                except Exception:
                    pass
        except Exception:
            pass

        # Strategy 2: JS scan — Cancel preferred over Close; never Publish.
        # Must NOT match the campaign wizard itself: it is often role=dialog with
        # an "In draft" badge + bottom Close — clicking that closes the wizard
        # ("Lost campaign wizard" after player_wizard_ready).
        try:
            script = """
            () => {
              const prefer = ['cancel', 'cancelar', 'close', 'cerrar'];
              for (const dialog of document.querySelectorAll('[role=dialog]')) {
                if (!dialog.offsetParent) continue;
                const txt = (dialog.textContent || '').toLowerCase();
                // Wizard chrome — never dismiss
                if (
                  txt.includes('campaign name') ||
                  txt.includes('daily budget') ||
                  txt.includes('ad set name') ||
                  txt.includes('conversion location') ||
                  txt.includes('nombre de la campaña') ||
                  txt.includes('campaign score') ||
                  txt.includes('campaign details')
                ) continue;
                // Only real draft-review modals (not bare "In draft" / "Publish")
                const isDraftModal =
                  txt.includes('publish draft') ||
                  txt.includes('review draft') ||
                  txt.includes('draft items') ||
                  (txt.includes('publish') && txt.includes('draft item'));
                if (!isDraftModal) continue;
                const buttons = Array.from(dialog.querySelectorAll('button, [role=button]'))
                  .filter(b => b.offsetParent);
                for (const want of prefer) {
                  for (const btn of buttons) {
                    const t = (btn.textContent || btn.innerText || '').trim().toLowerCase();
                    if (t === want) { btn.click(); return true; }
                  }
                }
              }
              return false;
            }
            """
            if root.evaluate(script):
                _sleep_h(0.45)
                return True
        except Exception:
            pass
    return False


def _dismiss_floating_banners(page: Page) -> list[str]:
    """Закрыть баннеры и карточки Meta, мешающие кликам. Что закрыли.

    До ACL-1313 здесь искали `[aria-label="Close"]` — у крестиков баннеров
    Meta такого атрибута нет (это `div[role=button]` со спрятанным текстом
    «Close»), зато он есть у крестика всего редактора
    (`data-surface="/am/editor_drawer"`). Теперь — `_close_banners`:
    заголовок → контейнер → «Close» внутри контейнера.
    """
    return _close_banners(page)

def select_conversion_location_website(page: Page, *, skip_open: bool = False) -> bool:
    """Pick 'Website' under Conversion → Conversion location on Ad Set level.

    Fully automatic. Uses keyboard + raw CDP clicks to work around Playwright's
    connect_over_cdp mouse-drop bug (#41286).
    """
    scroll_to_section(page, "Conversion location", "Ubicación de la conversión")
    # Dismiss 'Publish draft items?' modal first — it blocks all clicks if visible
    dismiss_publish_draft_dialog(page)
    _dismiss_floating_banners(page)
    # Advantage+ On can keep forcing "Website and instant forms"
    # NOTE: only do this when no dropdown is open, to avoid triggering a draft-save dialog
    _turn_off_advantage_plus_leads(page)
    # Re-dismiss in case Advantage+ toggle triggered the draft dialog
    dismiss_publish_draft_dialog(page)

    # Fast-path: already correct
    if is_website_conversion_selected(page):
        return True

    # If menu is already open on the correct section, try to pick immediately
    if skip_open or conversion_menu_is_open(page):
        dismiss_publish_draft_dialog(page)
        if _is_conversion_event_menu(page):
            # Wrong menu open — close safely (never use Escape: it may close the wizard)
            close_open_dropdown(page)
            page.wait_for_timeout(300)
        else:
            if _pick_website_from_open_menu(page) and is_website_conversion_selected(page):
                return True

    for attempt in range(3):
        # 0. Dismiss any blocking modal first
        dismiss_publish_draft_dialog(page)

        # 1. Close any leftover open dropdown
        close_open_dropdown(page)
        _dismiss_floating_banners(page)
        if attempt == 1:
            _turn_off_advantage_plus_leads(page)
            dismiss_publish_draft_dialog(page)

        # 2. Open the Conversion Location dropdown
        if not click_conversion_location_dropdown(page):
            break  # can't find the dropdown trigger — abort

        # 3. Wait for menu to actually open (not a fixed delay)
        if not wait_conversion_menu_open(page):
            # Menu may still show options without role=listbox
            if not conversion_location_menu_visible(page):
                continue

        dismiss_publish_draft_dialog(page)
        _dismiss_floating_banners(page)

        # 4. Guard: verify it's the Conversion Location menu, not Conversion Event
        if _is_conversion_event_menu(page):
            # Close safely — never press Escape (could close the entire wizard)
            close_open_dropdown(page)
            page.wait_for_timeout(300)
            continue

        # 5. Pick Website (keyboard → CDP → recipe → fallbacks)
        if _pick_website_from_open_menu(page):
            page.wait_for_timeout(600)
            _dismiss_advantage_banner(page)
            if is_website_conversion_selected(page):
                return True

        # Step failed — close and retry
        close_open_dropdown(page)

    return is_website_conversion_selected(page)


def close_open_dropdown(page: Page) -> None:
    """Collapse an open dropdown without closing the Ads Manager wizard."""
    anchors = (
        "Conversion location",
        "Performance goal",
        "Ad set name",
        "Dataset",
        "Conversion",
    )
    for anchor in anchors:
        pattern = re.compile(rf"^{re.escape(anchor)}$", re.I)
        for root in _roots(page):
            try:
                loc = root.get_by_text(pattern).first
                loc.scroll_into_view_if_needed(timeout=3000)
                loc.click(timeout=3000)
                _sleep_h(0.45)
                return
            except Exception:
                pass


_SAVE_BUSY_RE = re.compile(r"Verifying your edits|Saving\b|Saving…|Saving\.\.\.", re.I)


def _editor_is_busy_saving(page: Page) -> bool:
    """True while Meta shows any of its in-flight autosave indicators."""
    for root in _roots(page):
        try:
            if root.get_by_text(_SAVE_BUSY_RE).first.is_visible():
                return True
        except Exception:
            pass
    return False


def _ensure_campaign_budget(page: Page) -> bool:
    """Если выбран «Ad set budget», выбрать «Campaign budget». True — переключили.

    Радио ищется по подписи; клик — по самой строке с подписью, затем
    проверка `checked`. Ничего не нашли или уже кампанийный — False, без
    кликов.
    """
    try:
        state = page.evaluate(
            r"""() => {
              const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
              const radioFor = (label) => {
                const leaf = [...document.querySelectorAll('*')].find(e =>
                  e.children.length === 0 && norm(e.textContent) === label && e.getBoundingClientRect().width > 0);
                if (!leaf) return null;
                let n = leaf;
                for (let i = 0; i < 6 && n; i++) {
                  n = n.parentElement;
                  const r = n && n.querySelector('input[type=radio], [role=radio]');
                  if (r) { return {radio: r, row: n}; }
                }
                return null;
              };
              const checked = (r) => r.checked === true || r.getAttribute('aria-checked') === 'true';
              const camp = radioFor('Campaign budget');
              const adset = radioFor('Ad set budget');
              if (!camp || !adset) return 'absent';
              if (checked(camp.radio)) return 'campaign';
              if (!checked(adset.radio)) return 'unknown';
              camp.radio.setAttribute('data-rpa-mark', 'campaign-budget-radio');
              return 'adset';
            }"""
        )
    except Exception:
        return False
    if state != "adset":
        return False
    if not _click_marked(page, "campaign-budget-radio"):
        _telemetry.record("campaign_budget_strategy", False, note="radio_not_clickable")
        return False
    _sleep_h(1.0)
    _telemetry.record("campaign_budget_strategy", None, note="switched_from_ad_set_budget")
    return True


def wait_editor_saved(page: Page, *, timeout_s: float = 15, appear_s: float = 1.5) -> None:
    """Wait until Meta's debounced autosave has actually completed.

    Two phases, deliberately:

    1. Wait (up to `appear_s`) for a busy indicator to APPEAR. Meta's save
       is debounced, so right after a keystroke or click there is nothing
       to wait on yet — the old single-phase version returned instantly in
       that window and reported "saved" before the save had even been
       scheduled. That false confidence is the direct cause of the silent
       revert bugs confirmed live on campaign name, daily budget, ad
       destination and ad media (2026-07-28/29): every one of them typed
       correctly, "verified" instantly, then reverted many steps later.
       Callers previously worked around this with a hardcoded
       `_sleep_h(0.6)` guess; the wait belongs here instead.
    2. Wait (up to `timeout_s`) for it to CLEAR.

    Matches every in-flight wording seen live — "Verifying your edits…"
    and the footer's "Saving…" — rather than only the first. Returning
    without ever seeing an indicator is normal and fine: it means the
    field was already persisted (or never dirty).
    """
    import time as _time

    appear_deadline = _time.time() + appear_s
    while _time.time() < appear_deadline:
        if _editor_is_busy_saving(page):
            break
        # Meta's footer flips to a positive "All edits saved" once nothing
        # is pending. Seeing it means the debounce has already fired and
        # settled, so there is no point sitting out the rest of the
        # appearance window — this keeps the common already-saved case
        # cheap instead of costing the full `appear_s` at every call site.
        try:
            if page.get_by_text(re.compile(r"All edits saved", re.I)).first.is_visible():
                return
        except Exception:
            pass
        _sleep_h(0.15)

    deadline = _time.time() + timeout_s
    while _time.time() < deadline:
        if not _editor_is_busy_saving(page):
            return
        _sleep_h(0.35)


def dismiss_open_popovers(page: Page) -> None:
    """Backward-compatible alias — never press Escape (closes whole wizard)."""
    close_open_dropdown(page)


def _section_field_display_value(page: Page, section_labels: list[str]) -> str:
    script = """
    ([sections]) => {
      const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
      // Strip leading asterisk / required-field markers before comparing
      const clean = (s) => norm(s).replace(/^[*✱•·\\s]+/, '').trim();
      const visible = (el) => !!(el && el.offsetParent !== null);
      const targets = sections.map(norm);
      let marker = null;
      for (const el of document.querySelectorAll('label, span, div, h2, h3, [role=heading]')) {
        if (!visible(el) || el.children.length > 4) continue;
        const t = norm(el.textContent);
        const tc = clean(el.textContent);
        if (!targets.some((s) => t === s || tc === s)) continue;
        marker = el;
        break;
      }
      if (!marker) return '';
      let node = marker.parentElement;
      for (let depth = 0; depth < 10 && node; depth++) {
        for (const ctrl of node.querySelectorAll(
          '[role=combobox], [role=button][aria-haspopup], button[aria-haspopup], select, [role=button], span, div'
        )) {
          if (!visible(ctrl)) continue;
          const raw = (ctrl.textContent || ctrl.value || ctrl.getAttribute('aria-label') || '').trim();
          const val = norm(raw);
          if (!val || val.length > 80) continue;
          if (val === norm(marker.textContent)) continue;
          return raw;
        }
        node = node.parentElement;
      }
      return '';
    }
    """
    for root in _roots(page):
        try:
            val = root.evaluate(script, section_labels)
            if isinstance(val, str) and val.strip():
                return val.strip()
        except Exception:
            pass
    return ""


def _click_section_dropdown(page: Page, section_labels: list[str]) -> bool:
    scrolled = scroll_to_section(page, *section_labels)
    # Use partial-match patterns so "* Conversion event" matches "Conversion event"
    patterns = [
        re.compile(re.escape(label), re.I)
        for label in section_labels
        if label.strip()
    ]
    _dbg = []
    _dbg.append(f"scroll_to_section({section_labels}) -> {scrolled}")
    for root in _roots(page):
        for pattern in patterns:
            try:
                match_count = root.get_by_text(pattern).count()
                marker = root.get_by_text(pattern).first
                try:
                    mbox = marker.bounding_box()
                    mtext = marker.evaluate("el => (el.textContent||'').trim().slice(0,80)")
                except Exception as exc:
                    mbox, mtext = None, f"<err:{exc}>"
                _dbg.append(f"pattern={pattern.pattern!r} match_count={match_count} box={mbox} text={mtext!r}")
                marker.scroll_into_view_if_needed(timeout=5000)
                # scroll_into_view_if_needed aligns to the nearest edge, which
                # can leave the marker right at the bottom of the viewport with
                # its actual dropdown control (rendered below the label) still
                # off-screen — confirmed live 2026-07-24 for "Conversion event"
                # (screenshot showed just the label text at the very bottom
                # edge, control not visible). Force a centered scroll instead
                # so there's room below for whatever control follows the label.
                try:
                    marker.evaluate("el => el.scrollIntoView({block: 'center', behavior: 'instant'})")
                    page.wait_for_timeout(300)
                except Exception:
                    pass
                for depth in range(1, 14):
                    row = marker.locator(f"xpath=ancestor::*[{depth}]")
                    for sel in (
                        "[role=combobox]",
                        "[aria-haspopup='listbox']",
                        "[aria-haspopup='menu']",
                        "[role=button]",
                        "button",
                        # Same class of bug fixed today for conversion location:
                        # Meta's trigger here is very likely a role-less plain
                        # div/span too, so the role-only selectors above never
                        # matched anything and this function silently gave up
                        # every time (confirmed live 2026-07-24 — 2 identical
                        # failure screenshots). Restrict to elements with a
                        # short own-text (a value/placeholder chip) so we don't
                        # accidentally click a large wrapping container.
                        "div",
                        "span",
                    ):
                        try:
                            # Use .first (not .last) — the field's own button is always
                            # the first/nearest control relative to its label; using .last
                            # can accidentally pick a button from a LOWER section.
                            ctrl = row.locator(sel).first
                            if sel in ("div", "span"):
                                own = (ctrl.evaluate(
                                    "el => Array.from(el.childNodes)"
                                    ".filter(n => n.nodeType === 3)"
                                    ".map(n => n.textContent).join('').trim()"
                                ) if ctrl.count() else "")
                                if not own or len(own) > 60:
                                    continue
                                # Reject the label's own text (e.g. "Conversion
                                # event") and anything positioned ABOVE the
                                # marker — confirmed live 2026-07-24: an
                                # unfiltered depth=1 span click "succeeded"
                                # (no exception) but didn't open anything,
                                # because it landed on a wrapper right next to
                                # the label itself, not the control below it.
                                own_norm = re.sub(r"\s+", " ", own).strip().lower()
                                label_norm = re.sub(r"\s+", " ", pattern.pattern.replace("\\", "")).strip().lower()
                                if own_norm == label_norm or own_norm.lstrip("*• ") == label_norm:
                                    continue
                                if mbox:
                                    try:
                                        cbox = ctrl.bounding_box()
                                    except Exception:
                                        cbox = None
                                    if cbox and cbox["y"] < mbox["y"] - 2:
                                        continue
                            if ctrl.count() and ctrl.is_visible():
                                ctrl.click(timeout=5000)
                                _sleep_h(0.6)
                                _dbg.append(f"CLICKED depth={depth} sel={sel!r} own={own if sel in ('div','span') else ''!r}")
                                try:
                                    Path("output/debug_conversion_event.txt").write_text(
                                        "\n".join(_dbg), encoding="utf-8"
                                    )
                                except Exception:
                                    pass
                                return True
                            elif ctrl.count():
                                _dbg.append(
                                    f"depth={depth} sel={sel!r} count={ctrl.count()} visible=False"
                                )
                        except Exception as exc:
                            _dbg.append(f"depth={depth} sel={sel!r} exc={exc}")
                            continue
            except Exception as exc:
                _dbg.append(f"outer exc pattern={pattern.pattern!r}: {exc}")
    try:
        Path("output/debug_conversion_event.txt").write_text("\n".join(_dbg), encoding="utf-8")
    except Exception:
        pass
    return False


def _pick_menu_option(page: Page, option_labels: list[str]) -> bool:
    targets = {_norm_label(v) for v in option_labels if v.strip()}
    if not targets:
        return False
    _sleep_h(0.4)
    for root in _roots(page):
        for listbox_sel in ("[role=listbox]", "[role=menu]", "[role=dialog]"):
            try:
                listboxes = root.locator(listbox_sel)
                for lb_idx in range(listboxes.count() - 1, -1, -1):
                    lb = listboxes.nth(lb_idx)
                    if not lb.is_visible():
                        continue
                    options = lb.locator(
                        "[role=option], [role=menuitemradio], [role=menuitem], [role=radio], li, label"
                    )
                    for opt_idx in range(options.count()):
                        opt = options.nth(opt_idx)
                        try:
                            if not opt.is_visible():
                                continue
                            first = _menu_first_line(opt.inner_text(timeout=1500))
                            if first not in targets:
                                continue
                            opt.click(timeout=5000)
                            _sleep_h(0.6)
                            return True
                        except Exception:
                            continue
                    for label in option_labels:
                        try:
                            item = lb.get_by_text(
                                re.compile(rf"^{re.escape(label)}$", re.I)
                            ).first
                            if item.is_visible():
                                item.click(timeout=5000)
                                _sleep_h(0.6)
                                return True
                        except Exception:
                            continue
            except Exception:
                pass
    # Fallback: Meta's custom dropdowns repeatedly turn out not to use
    # role=listbox/menu/option (confirmed today for conversion location's
    # own trigger and menu) — if no role-based container matched at all,
    # search the whole visible page for a small element whose own text is
    # exactly one of the target labels (e.g. "Lead"), same own-text-only
    # discipline used by is_website_conversion_selected's fix.
    try:
        for root in _roots(page):
            found = root.evaluate(
                """
                (targets) => {
                  const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                  for (const el of document.querySelectorAll('div, span, li, [role]')) {
                    if (!el.offsetParent) continue;
                    const own = norm(Array.from(el.childNodes)
                      .filter(n => n.nodeType === 3).map(n => n.textContent).join(''));
                    if (own && targets.includes(own)) {
                      el.scrollIntoView({block: 'center'});
                      return true;
                    }
                  }
                  return false;
                }
                """,
                list(targets),
            )
            if found:
                for label in option_labels:
                    try:
                        item = root.get_by_text(re.compile(rf"^{re.escape(label)}$", re.I)).first
                        if item.is_visible():
                            item.click(timeout=5000)
                            _sleep_h(0.6)
                            return True
                    except Exception:
                        continue
    except Exception:
        pass
    return False


def is_lead_conversion_event_selected(page: Page) -> bool:
    # _section_field_display_value's ancestor-walk-then-wide-descendant-search
    # pattern produced a live false NEGATIVE today (2026-07-24) — a user
    # watching the real browser confirmed "Lead" was genuinely selected and
    # the wizard had already moved on to the Audience section three times,
    # while this check kept reporting failure. Reuse the TreeWalker,
    # own-text-only, forward-from-marker approach that fixed the equivalent
    # false POSITIVE for is_website_conversion_selected — same underlying
    # class of bug (over-broad or mismatched DOM traversal), different sign.
    script = """
    () => {
      const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
      const ownText = (el) => norm(
        Array.from(el.childNodes)
          .filter(n => n.nodeType === Node.TEXT_NODE)
          .map(n => n.textContent).join('')
      );
      let marker = null;
      for (const el of document.querySelectorAll('label, [role=label], span, div, h2, h3, [role=heading]')) {
        if (el.offsetParent === null || el.children.length > 2) continue;
        const t = norm(el.textContent).replace(/^[*✱•\\s]+/, '');
        if (t === 'conversion event' || t === 'evento de conversión' ||
            t.startsWith('conversion event') || t.startsWith('evento de conversión')) {
          marker = el;
          break;
        }
      }
      if (!marker) return false;
      const targets = ['lead', 'leads', 'cliente potencial', 'prospecto'];
      const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT);
      walker.currentNode = marker;
      let steps = 0;
      while (walker.nextNode() && steps < 400) {
        steps++;
        const el = walker.currentNode;
        if (el.offsetParent === null) continue;
        const t = ownText(el);
        if (!t || t.length > 40) continue;
        if (targets.includes(t)) return true;
        if (t === 'select an event or search by name' || t.includes('search')) {
          // Empty/placeholder state — not selected, but this IS the field;
          // no point scanning further past it for this section.
          return false;
        }
        if (t === 'cost per result goal' || t === 'costo por resultado') break;
      }
      return false;
    }
    """
    for root in _roots(page):
        try:
            if root.evaluate(script):
                return True
        except Exception:
            pass
    return False


def click_conversion_event_dropdown(page: Page) -> bool:
    return _click_section_dropdown(page, ["Conversion event", "Evento de conversión"])


_DATASET_PLACEHOLDER = "Select a dataset or search by name or ID"


def _select_leads_dataset(page: Page) -> bool:
    """Fill the ad set's required "* Dataset" field (2026-08-24 layout).

    Meta rebuilt the ad set's Conversion block on 24.08: "Conversion event ->
    Lead" is gone, replaced by a Performance goal ("Maximize number of leads",
    already correct by default) plus a required "* Dataset" combobox holding
    the account's pixel. The old lookup searched for a "Conversion event"
    section that no longer exists and burned its whole budget waiting —
    measured 17-21s and True on every run through 17.08, then exactly 130.5s
    and False on eight consecutive runs from 24.08, this one included.

    Picks the first offered dataset: these accounts carry a single pixel
    (the ad-level Tracking block auto-checks that same one, e.g.
    "FacebookELF303 / Pixel ID: 1912109486413769"), so there is nothing to
    choose between. Returns False rather than guessing if the menu offers
    nothing — an empty required field is visible to the operator on the
    draft, a wrong pixel would not be.
    """
    _scroll_until_mounted(
        page,
        """
        () => [...document.querySelectorAll('*')]
          .some(e => e.offsetParent !== null && e.children.length === 0
                && /^\\*?\\s*Dataset$/.test((e.textContent || '').replace(/\\s+/g, ' ').trim()))
        """,
    )

    def _empty() -> bool:
        try:
            return page.evaluate(
                """
                (ph) => [...document.querySelectorAll('*')]
                  .filter(e => e.offsetParent !== null && e.children.length === 0)
                  .some(e => (e.textContent || '').replace(/\\s+/g, ' ').trim() === ph)
                """,
                _DATASET_PLACEHOLDER,
            )
        except Exception:
            return False

    if not _empty():
        return True  # already carries a dataset

    for _ in range(3):
        if not _click_xpath(
            page,
            "//*[@role='combobox' or @role='button' or self::div]"
            f"[normalize-space(.)='{_DATASET_PLACEHOLDER}']",
        ):
            _sleep_h(0.8)
            continue
        # Wait for the menu to paint, then take the first real option.
        for _ in range(12):  # ~3.6s
            _sleep_h(0.3)
            if page.query_selector("xpath=//*[@role='option']") is not None:
                break
        if _click_xpath(page, "(//*[@role='option'])[1]"):
            wait_editor_saved(page)
            _sleep_h(0.8)
            if not _empty():
                return True
        close_open_dropdown(page)
        _sleep_h(0.6)
    return not _empty()


def conversion_event_field_present(page: Page) -> bool:
    """Есть ли на странице поле «Conversion event» вообще.

    От этого зависит, что считать успехом шага. В августе поле пропадало, и
    его заменял «Dataset»; 9-10 сентября в снимках ад-сета видны **оба** — и
    метка «Conversion event», и её пустое состояние «Select an event or
    search by name».
    """
    try:
        return bool(
            page.evaluate(
                """
                () => {
                  const norm = (s) => (s || '').replace(/\\s+/g, ' ')
                    .replace(/^[*✱•\\s]+/, '').trim().toLowerCase();
                  return [...document.querySelectorAll('label, span, div, h2, h3, [role=heading]')]
                    .some(e => e.children.length <= 2 &&
                      norm(e.textContent).startsWith('conversion event'));
                }
                """
            )
        )
    except Exception:
        return False


def select_conversion_event_lead(page: Page) -> bool:
    """Выбрать событие конверсии «Lead».

    Успех засчитывается по самому полю события, а не по соседнему.
    `_select_leads_dataset` отвечает True, как только заполнен «Dataset», —
    а пиксель у этих аккаунтов один, Meta подставляет его сама, и поле
    непусто **всегда**. Шаг рапортовал успех, ни разу не тронув Conversion
    event: 10 сентября обе кампании ушли в черновик без события, при
    зелёном `conversion_event` в телеметрии.

    Посылка «с 24.08 поля события нет, его заменил Dataset» была верна
    когда-то, но проверка осталась на ней. Датасет засчитывается в одиночку
    только там, где поля события действительно нет.
    """
    scroll_to_section(page, "Conversion event", "Evento de conversión")
    if is_lead_conversion_event_selected(page):
        return True
    # Датасет всё равно нужен: он обязателен и в новой раскладке. Но сам по
    # себе он событие конверсии не заменяет — см. докстринг.
    dataset_ok = _select_leads_dataset(page)
    if not conversion_event_field_present(page):
        return dataset_ok
    for _ in range(2):
        if not click_conversion_event_dropdown(page):
            break
        wait_conversion_menu_open(page)
        if _pick_menu_option(page, ["Lead", "Leads", "Cliente potencial", "Prospecto"]):
            wait_editor_saved(page)
            if is_lead_conversion_event_selected(page):
                close_open_dropdown(page)
                return True
        close_open_dropdown(page)
    return is_lead_conversion_event_selected(page)


def open_detailed_targeting_field(page: Page) -> bool:
    """Scroll to Audience and focus the Detailed targeting interests input."""
    # Same reasoning as fill_audience_location/fill_audience_age: skip the
    # scroll_to_section("Audience", ...) pre-scroll — "Audience" as the FIRST
    # label tried is exactly the fallback confirmed live 2026-07-24 to jump
    # all the way back up to that section's top-level heading instead of the
    # specific Detailed targeting input further down. get_by_placeholder(...)
    # below targets the actual input directly and auto-scrolls it into view.
    placeholders = [
        re.compile(r"Add interests", re.I),
        re.compile(r"interests, behaviors", re.I),
        re.compile(r"Añadir intereses", re.I),
        re.compile(r"demographics", re.I),
    ]

    # Instant single-shot check — NO wait_for. The input either exists right
    # now (section already expanded) or the section is collapsed and no
    # amount of waiting will make it appear. The previous version burned
    # 4 placeholders x 5s + 2 labels x 5s per root (page + every ad-preview
    # iframe) on wait_for timeouts before reaching the collapsed-row click
    # that actually works — measured live 2026-07-29 as a consistent
    # 33-65s dead pause right after the Languages step, every campaign.
    def _click_input_now() -> bool:
        for root in _roots(page):
            for ph in placeholders:
                try:
                    inp = root.get_by_placeholder(ph).first
                    if inp.count() and inp.is_visible():
                        inp.scroll_into_view_if_needed(timeout=3000)
                        inp.click(timeout=3000)
                        _sleep_h(0.3)
                        return True
                except Exception:
                    continue
            for label_re in (
                re.compile(r"Detailed targeting", re.I),
                re.compile(r"Segmentación detallada", re.I),
            ):
                try:
                    inp = root.get_by_label(label_re).first
                    if inp.count() and inp.is_visible():
                        inp.scroll_into_view_if_needed(timeout=3000)
                        inp.click(timeout=3000)
                        _sleep_h(0.3)
                        return True
                except Exception:
                    continue
        return False

    if _click_input_now():
        return True
    # "Detailed targeting" renders as a collapsed "All demographics,
    # interests and behaviors" summary in every screenshot captured this
    # session — same collapsed-summary family as Locations/Age/Languages —
    # so none of the placeholder/label searches above would ever find
    # anything in that state. A live dump (2026-07-25, DQ204 Test37) showed
    # a SIMPLER structure than Locations/Age: header text + value text, no
    # info-icon div at all — click the header/row via CDP (the proven
    # pattern from Locations/Languages) then poll for the real input.
    targeting_placeholders = placeholders  # reuse the list already defined above
    # Три захода, а не один. 16.09 22:49 (S15, 4b296e9963e1): клик пришёлся
    # на момент, когда раскрытый Languages ещё перестраивал колонку, строка
    # «Detailed targeting» осталась свёрнутой, и шаг сдался через 3 с.
    for _attempt in range(3):
        # Строка уже раскрылась, просто поздно — второй клик её свернул бы.
        for root in ([] if _attempt and targeting_input_count(page) else _roots(page)):
            try:
                heading = root.get_by_text(re.compile(r"^Detailed targeting$|^Segmentación detallada$", re.I)).first
                heading.wait_for(state="visible", timeout=2000)
                heading.scroll_into_view_if_needed(timeout=2000)
                header_block = heading.locator("xpath=ancestor::*[2]")
                row = heading.locator("xpath=ancestor::*[3]")
                for candidate in (header_block, row):
                    try:
                        box = candidate.bounding_box(timeout=1500)
                        if box and box.get("width", 0) > 0 and box.get("height", 0) > 0:
                            _cdp_mouse_click(page, box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                            break
                    except Exception:
                        continue
                break
            except Exception:
                continue

        for _ in range(15):
            _sleep_h(0.2)
            for root in _roots(page):
                for ph in targeting_placeholders:
                    try:
                        inp = root.get_by_placeholder(ph).first
                        if inp.count() and inp.is_visible():
                            inp.click(timeout=3000)
                            _sleep_h(0.3)
                            return True
                    except Exception:
                        continue
    return False


_DEFINE_FURTHER_XPATH = (
    "//*[@role='button' or self::button]"
    "[normalize-space(.)='Define further' or normalize-space(.)='Narrow further'"
    " or normalize-space(.)='Narrow audience' or normalize-space(.)='Definir más']"
)


def targeting_input_count(page: Page) -> int:
    """How many Detailed targeting inputs are on the page (one per AND group)."""
    try:
        return int(page.evaluate(
            """
            () => [...document.querySelectorAll('input, textarea')].filter((inp) => {
              const r = inp.getBoundingClientRect();
              if (r.width <= 2 || r.height <= 2 ||
                  getComputedStyle(inp).visibility === 'hidden' ||
                  inp.closest('[hidden], [inert], [aria-hidden="true"]')) return false;
              const blob = ((inp.getAttribute('placeholder') || '') + ' ' +
                            (inp.getAttribute('aria-label') || '')).toLowerCase();
              if (['existing audience', 'audiencia existente', 'saved audience'].some(b => blob.includes(b))) return false;
              return ['add interests', 'interests, behaviors', 'interests or behaviours',
                      'interests or behaviors', 'añadir intereses', 'demographics', 'demografía']
                .some(g => blob.includes(g));
            }).length
            """
        ))
    except Exception:
        return 0


def click_define_further(page: Page) -> bool:
    """Press "Define further" and confirm a new targeting group opened.

    The Mix targets are two groups joined by AND: finance interests, and
    people who ALSO match travel/luxury. Meta expresses that with this
    button; without it both lists land in one OR group and the audience is
    the opposite of what the target means (Doc20, KW_trl_All_Video_Mix,
    2026-09-16: all sixteen interests in one "Include people who match",
    the button untouched). Success is a new input appearing, not the click.
    """
    before = targeting_input_count(page)
    for _ in range(3):
        if not _click_xpath(page, _DEFINE_FURTHER_XPATH):
            _sleep_h(0.8)
            continue
        for _ in range(10):
            _sleep_h(0.3)
            if targeting_input_count(page) > before:
                return True
    return False


def fill_targeting_interest(page: Page, text: str, *, last: bool = False) -> bool:
    """Type into Detailed targeting interests field — not 'Search existing audiences'.

    `last=True` types into the last such field on the page — the one
    "Define further" just opened for the next AND group.
    """
    script = """
    ([text, last]) => {
      const visible = (el) => {
        if (!el || el.offsetParent === null) return false;
        const r = el.getBoundingClientRect();
        return r.width > 2 && r.height > 2 &&
          getComputedStyle(el).visibility !== 'hidden' &&
          !el.closest('[hidden], [inert], [aria-hidden="true"]');
      };
      const bad = ['existing audience', 'audiencia existente', 'saved audience'];
      const good = [
        'add interests', 'interests, behaviors', 'añadir intereses',
        'detailed targeting', 'segmentación detallada', 'include people',
        'incluir personas', 'demographics', 'demografía'
      ];
      const inputs = [...document.querySelectorAll('input, textarea')].filter(visible);
      if (last) inputs.reverse();
      for (const inp of inputs) {
        const ph = (inp.getAttribute('placeholder') || '').toLowerCase();
        const aria = (inp.getAttribute('aria-label') || '').toLowerCase();
        const blob = ph + ' ' + aria;
        if (bad.some((b) => blob.includes(b))) continue;
        if (!good.some((g) => blob.includes(g))) continue;
        const proto = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value');
        inp.focus();
        inp.click();
        if (proto && proto.set) proto.set.call(inp, text);
        else inp.value = text;
        inp.dispatchEvent(new InputEvent('input', { bubbles: true, data: text }));
        return true;
      }
      return false;
    }
    """
    for root in _roots(page):
        try:
            if root.evaluate(script, [text, last]):
                return True
        except Exception:
            pass
    return False


def fill_targeting_search(page: Page, text: str) -> bool:
    """Alias — prefer interest field, not saved-audience search."""
    return fill_targeting_interest(page, text)


def pick_first_suggestion(page: Page) -> bool:
    script = """
    () => {
      const opts = [...document.querySelectorAll('[role="option"], [role="listbox"] li, ul li')]
        .filter((el) => el.offsetParent !== null);
      if (opts.length) {
        opts[0].click();
        return true;
      }
      return false;
    }
    """
    for root in _roots(page):
        try:
            if root.evaluate(script):
                return True
        except Exception:
            pass
    return False


def _focus_last_targeting_input(page: Page) -> bool:
    """Trusted-click the last Detailed targeting input — the one "Define
    further" just opened — so a paste lands in the new AND group."""
    try:
        pt = page.evaluate(
            """
            () => {
              const inputs = [...document.querySelectorAll('input, textarea')].filter((inp) => {
                const r = inp.getBoundingClientRect();
                if (r.width <= 2 || r.height <= 2 ||
                  getComputedStyle(inp).visibility === 'hidden' ||
                  inp.closest('[hidden], [inert], [aria-hidden="true"]')) return false;
                const blob = ((inp.getAttribute('placeholder') || '') + ' ' +
                              (inp.getAttribute('aria-label') || '')).toLowerCase();
                if (['existing audience', 'audiencia existente', 'saved audience'].some(b => blob.includes(b))) return false;
                return ['add interests', 'interests, behaviors', 'interests or behaviours',
                        'interests or behaviors', 'añadir intereses', 'demographics', 'demografía']
                  .some(g => blob.includes(g));
              });
              const el = inputs[inputs.length - 1];
              if (!el) return null;
              el.scrollIntoView({ block: 'center', behavior: 'instant' });
              const r = el.getBoundingClientRect();
              return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
            }
            """
        )
    except Exception:
        return False
    if not pt:
        return False
    _cdp_mouse_click(page, pt["x"], pt["y"])
    _sleep_h(0.4)
    return True


def targeting_groups(targeting_label: str) -> list[str]:
    """Split a library target into its AND groups.

    `[AND]`, `|` and `\\` are one instruction written three ways — the same
    reading `ads_manager_flow` has always applied. Each group is returned as
    the comma-separated text Meta's field parses on paste.
    """
    raw = (targeting_label or "").replace("\\", "|")
    groups = []
    for chunk in re.split(r"\[AND\]|\|", raw, flags=re.IGNORECASE):
        terms = [t.strip() for t in chunk.split(",") if t.strip()]
        if terms:
            groups.append(", ".join(terms))
    return groups


_TARGETING_PROBE_JS = r"""
  () => {
    const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
    const isT = (i) => {
      const b = norm((i.getAttribute('placeholder') || '') + ' ' + (i.getAttribute('aria-label') || '')).toLowerCase();
      return ['add interests', 'interests, behaviors', 'interests or behaviours',
              'interests or behaviors', 'demographics'].some(g => b.includes(g));
    };
    const vis = (e) => e.getClientRects().length > 0;
    const ins = [...document.querySelectorAll('input, textarea')].filter(isT);
    const out = ins.map((i) => {
      const w = i.closest('[role="presentation"]');
      const lis = w ? [...w.querySelectorAll('li')].filter(li => !li.querySelector('li')) : [];
      return {visible: vis(i), wrapper: !!w, li: lis.length,
              liVisible: lis.filter(vis).length,
              sample: lis.slice(0, 4).map(li => norm(li.textContent).slice(0, 40))};
    });
    const body = norm(document.body.innerText || '').toLowerCase();
    return {inputs: out, hasAdditional: body.includes('additional interests'),
            hasCombined: body.includes('have been combined')};
  }
"""
"""Что видно на месте детального таргетинга прямо сейчас."""


_TARGETING_GROUP_JS = r"""
  (terms) => {
    const norm = (s) => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
    const visible = el => !!el && el.getClientRects().length > 0
      && getComputedStyle(el).visibility !== 'hidden'
      && !el.closest('[hidden], [inert], [aria-hidden="true"]');
    const isTargetingInput = inp => {
      const blob = norm((inp.getAttribute('placeholder') || '') + ' ' +
                        (inp.getAttribute('aria-label') || ''));
      if (['existing audience', 'audiencia existente', 'saved audience'].some(b => blob.includes(b))) return false;
      return ['add interests', 'interests, behaviors', 'interests or behaviours',
              'interests or behaviors', 'añadir intereses', 'demographics', 'demografía']
        .some(g => blob.includes(g));
    };
    // 20.09 12:30: пока открыт список подсказок Meta, в нём живёт свой ввод с
    // тем же placeholder. Он и сбивал выбор группы: обёртка «своего» ввода
    // содержала два поля, проверка 45 с возвращала «группа недоступна», а на
    // снимке секундой позже стояли все пять интересов.
    const inPopup = el => !!el.closest('[role=listbox], [role=dialog], [role=menu]');
    const inputs = [...document.querySelectorAll('input, textarea')]
      .filter(inp => visible(inp) && isTargetingInput(inp) && !inPopup(inp));
    const last = inputs[inputs.length - 1];
    if (!last) return {found: false, why: 'targeting input unavailable'};
    // The recorded Meta block gives each group its own presentation wrapper.
    // An ancestor walk based only on input count can reach the entire page.
    const chosen = last.closest('[role="presentation"]');
    const ownInputs = chosen ? [...chosen.querySelectorAll('input, textarea')]
      .filter(inp => isTargetingInput(inp) && !inPopup(inp)) : [];
    if (!chosen || ownInputs.length !== 1)
      return {found: false, why: 'targeting group unavailable', inputs: ownInputs.length};
    const skip = el => !visible(el) || !!el.closest(
      '[role=listbox], [role=option], [role=menu], [role=menuitem]');
    // 20.09 13:45: крестик у интереса Meta рисует только под курсором, и
    // требование «кнопка видима» обнуляло список: замер в ту же секунду
    // видел 19 интересов, проверка — ни одного. Наличия кнопки достаточно,
    // видимость самой строки проверяется отдельно.
    const isClose = b =>
      /^(close|remove|quitar|eliminar)$/i.test(norm(b.getAttribute('aria-label') || b.textContent));
    // Only leaf list items with their own removal control are selected chips.
    // Outer li elements also contain category headings and other chips.
    const chips = [...chosen.querySelectorAll('li')]
      .filter(li => !skip(li) && !li.querySelector('li') &&
        [...li.querySelectorAll('button')].some(isClose))
      .map(li => {
        const clone = li.cloneNode(true);
        clone.querySelectorAll('button').forEach(b => b.remove());
        return norm(clone.textContent);
      }).filter(Boolean);
    // Meta appends a category in parentheses to some selected names.
    // A substring elsewhere (or in a longer interest name) is not a match.
    const has = term => chips.some(c => c === norm(term) || c.startsWith(norm(term) + ' ('));
    return {found: true, hits: terms.filter(has), missing: terms.filter(t => !has(t)),
            selected: chips.slice(0, 40), by: 'chips', text: chips.join(', ').slice(0, 400)};
  }
"""
"""Verify actual selected chips in the last AND group's recorded container."""


# Общий контейнер всех полей интересов — блок AND-групп целиком.
_GROUPS_BLOCK_JS = (
    "(() => { const ins = [...document.querySelectorAll('input, textarea')].filter(i =>"
    " /interests|demograph|intereses/i.test((i.getAttribute('placeholder') || '') + ' ' +"
    " (i.getAttribute('aria-label') || ''))); if (!ins.length) return null;"
    " let el = ins[0]; while (el && !ins.every(i => el.contains(i))) el = el.parentElement;"
    " for (let k = 0; k < 2 && el && el.parentElement; k++) el = el.parentElement; return el; })()"
)


def _group_has_terms(page: Page, terms: list[str]) -> dict:
    try:
        return page.evaluate(_TARGETING_GROUP_JS, terms) or {"found": False}
    except Exception as exc:
        return {"found": False, "why": str(exc)[:80]}


_SCROLL_TARGETING_INPUT_JS = r"""
  () => {
    const inputs = [...document.querySelectorAll('input, textarea')].filter(inp => {
      const blob = ((inp.getAttribute('placeholder') || '') + ' ' + (inp.getAttribute('aria-label') || '')).toLowerCase();
      return /add interests|interests, behaviors|interests or behaviou?rs|demographics|añadir intereses|demografía/.test(blob);
    });
    const last = inputs[inputs.length - 1];
    if (last) last.scrollIntoView({block: 'center'});
    return !!last;
  }
"""


def _await_group_terms(page: Page, terms: list[str], *, timeout_s: float = 45.0) -> dict:
    """Read the group until the pasted interests have been drawn.

    20.09 07:00: 20 с хватало не всегда — на медленной Meta обе группы
    CO_bnc и группа CR_ait читались пустыми (group_empty), хотя интересы
    потом появлялись. Ждём 45 с.

    Meta draws a pasted list in two passes: first the category header
    ("Interests > Additional interests" with its own Close button), then the
    interests under it. Reading once right after the paste saw only the header
    and failed both campaigns of run 199fa985d531 (19.09) as `group_empty`,
    although the snapshot three seconds later held all five chips.
    """
    import time as _time

    # 19.09 13:02 (прогон 52a7a75ca5af): через 8 с плашек ещё не было, через 11 с
    # — все пять. Ждём дольше и держим поле в кадре: список рисуется лениво.
    def read() -> dict:
        try:
            page.evaluate(_SCROLL_TARGETING_INPUT_JS)
        except Exception:
            pass
        return _group_has_terms(page, terms)

    deadline = _time.monotonic() + timeout_s
    own = read()
    while (not own.get("found") or not own.get("hits")) and _time.monotonic() < deadline:
        page.wait_for_timeout(700)
        own = read()
    if own.get("hits") and own.get("missing"):
        # Interests arrive one by one; give the rest one more beat before
        # reporting them as unrecognised.
        page.wait_for_timeout(1000)
        again = _group_has_terms(page, terms)
        if again.get("found"):
            own = again
    return own


_COMBINED_NOTICE_JS = r"""
  () => {
    const norm = (s) => (s || '').replace(/[\s\u200b]+/g, ' ').trim();
    const visible = (el) => !!el && el.getClientRects().length > 0;
    const notice = [...document.querySelectorAll('*')].find(e =>
      e.children.length === 0 && visible(e)
      && /^Some detailed targeting options have been combined$/i.test(norm(e.textContent)));
    if (!notice) return { notice: false };
    const buttons = [...document.querySelectorAll('[role="button"], button')].filter(b =>
      visible(b) && /^Remove all$/i.test(norm(b.textContent)));
    const b = buttons[0];
    if (!b) return { notice: true, remove_all: false };
    b.scrollIntoView({ block: 'center' });
    const r = b.getBoundingClientRect();
    return { notice: true, remove_all: true, x: r.x + r.width / 2, y: r.y + r.height / 2 };
  }
"""


def _remove_combined_targeting(page: Page) -> dict:
    """Убрать объединённые Meta варианты детального таргетинга.

    19.09 CR_ait Mix2 (c63db6f44f10): «Some detailed targeting options have been
    combined — Before you can publish this ad set you need to update…». Без
    этого ad set не опубликовать. Решение пользователя: такие варианты просто
    убирать — кнопкой «Remove all» внизу уведомления.
    """
    try:
        state = page.evaluate(_COMBINED_NOTICE_JS) or {}
    except Exception as exc:
        return {"notice": None, "error": str(exc)[:80]}
    if not state.get("notice"):
        return {"notice": False}
    if not state.get("remove_all"):
        try:
            from src.fb_ads_rpa import page_capture as _pc

            _pc.capture(page, step="targeting_combined_no_remove_all")
        except Exception:
            pass
        _telemetry.record("targeting_combined", False, note="remove_all_not_found")
        return state
    _cdp_mouse_click(page, state["x"], state["y"])
    gone = False
    for _ in range(10):
        _sleep_h(0.5)
        try:
            gone = not (page.evaluate(_COMBINED_NOTICE_JS) or {}).get("notice")
        except Exception:
            gone = False
        if gone:
            break
    _telemetry.record("targeting_combined", gone, note="removed" if gone else "notice_still_shown")
    return {**state, "cleared": gone}


def paste_targeting_groups(page: Page, targeting_label: str) -> bool:
    """Paste each AND group into its own field, pressing "Define further"
    between them. True only when every group went in.

    The recorded flow pasted the whole library string at once. For a
    two-group target (Mix, Mix2, …) that string is "group1 [AND] group2":
    Meta parsed it as one comma list, every interest of both groups landed
    in one "Include people who match", and "Define further" was never
    pressed — "finance AND luxury" became "finance OR luxury" (Doc20,
    KW_trl_All_Video_Mix_35_16.09, 2026-09-16, operator's screenshot).
    """
    groups = targeting_groups(targeting_label)
    if not groups:
        return False
    unconfirmed: list[int] = []
    for index, group in enumerate(groups):
        if index > 0:
            if not click_define_further(page):
                _telemetry.record("targeting", False, note="define_further_did_not_open",
                                  diag={"group": index + 1, "groups": len(groups)})
                return False
            if not _focus_last_targeting_input(page):
                _telemetry.record("targeting", False, note="define_further_field_not_found",
                                  diag={"group": index + 1})
                return False
        if not paste_targeting_interests(page, group):
            _telemetry.record("targeting", False, note="group_not_added",
                              diag={"group": index + 1, "groups": len(groups)})
            return False
        # Проверка на своей группе: paste_targeting_interests ищет термин по
        # всей странице, и интерес первой группы подтверждал вторую (ACL-1219).
        terms = [t.strip() for t in group.split(",") if t.strip()]
        own = _await_group_terms(page, terms)
        if own.get("found") and own.get("hits") and own.get("missing"):
            # Meta может не распознать часть интересов — это не отказ, но
            # должно быть видно, какие именно не встали.
            _telemetry.record("targeting", None, note="group_partial",
                              diag={"group": index + 1, "missing": own["missing"][:10]})
        if not own.get("found") or not own.get("hits"):
            _telemetry.record("targeting", False, note="group_empty",
                              diag={"group": index + 1, "groups": len(groups),
                                    "terms": terms[:5], "box": own.get("text", "")[:200],
                                    "why": own.get("why", ""), "found": own.get("found"),
                                    "selected": (own.get("selected") or [])[:8]})
            try:
                from src.fb_ads_rpa import page_capture as _pc

                _pc.capture(page, step=f"targeting_group{index + 1}_empty",
                            blocks={"groups": _GROUPS_BLOCK_JS})
            except Exception:
                pass
            # Живое дерево, 19.09: проверка группы на живой странице ошибается
            # («пусто» при стоящих интересах — снимки 199fa985d531, 302c6ab20986),
            # и отказ здесь стоил черновику всей следующей AND-группы. Вставка
            # сама сверила, что термины на странице; группы добавляем дальше,
            # а итог шага остаётся честным — неуспех.
            unconfirmed.append(index + 1)
            continue
    if len(groups) > 1:
        # Снимок заполненных AND-групп: по нему пишутся тесты разметки.
        try:
            from src.fb_ads_rpa import page_capture as _pc

            _pc.capture_on_success(page, step="targeting_and_groups",
                        blocks={"groups": _GROUPS_BLOCK_JS})
        except Exception:
            pass
    # Объединённые Meta интересы блокируют публикацию — убираем их.
    combined = _remove_combined_targeting(page)
    if combined.get("notice") and not combined.get("cleared"):
        return False
    return not unconfirmed


def paste_targeting_interests(page: Page, targeting_label: str) -> bool:
    """Paste the whole comma-separated interest list into Detailed
    targeting in one shot, instead of typing/picking each term one at a
    time.

    Per live user request (2026-07-25): confirmed live via a synthetic
    ClipboardEvent('paste') dispatch — Meta's Detailed targeting field
    parses a pasted comma-separated string itself and adds each resolved
    interest as its own chip automatically (screenshot: 4 interests each
    got their own row under "Interests > Additional interests", audience
    size narrowed accordingly, "Verifying your edits..." confirmed the
    change was accepted). Much simpler and more reliable than the previous
    type-one-term/pick-a-suggestion loop, which needed a real autocomplete
    round-trip (network delay) per term and depended on
    pick_first_suggestion() correctly identifying the right non-role-based
    suggestion row — the same class of "looks native, isn't" pitfall
    chased all session for other fields.

    Assumes the target input already has focus (call after
    open_detailed_targeting_field(), which clicks/focuses it).
    """
    text = (targeting_label or "").strip()
    if not text:
        return False
    try:
        page.evaluate(
            """(text) => {
              const el = document.activeElement;
              if (!el) return;
              const dt = new DataTransfer();
              dt.setData('text/plain', text);
              const evt = new ClipboardEvent('paste', { clipboardData: dt, bubbles: true, cancelable: true });
              el.dispatchEvent(evt);
            }""",
            text,
        )
    except Exception:
        return False
    _sleep_h(1.5)
    try:
        page.get_by_text("Verifying your edits", exact=False).first.wait_for(state="hidden", timeout=8000)
    except Exception:
        pass
    # Any term of the pasted list showing up counts, not only the first. Meta
    # resolves each name on its own and silently drops the ones it does not
    # know: the second Mix group starts with "Frequent international
    # travelers", which never appears, while Business class, Business jet,
    # FirstClass, Horse racing and luxury all did (Doc20, 18:51, operator-
    # visible "and must also match" group full) — and the step reported
    # `group_not_added`.
    terms = [t.strip() for t in text.split(",") if t.strip()]
    for term in terms:
        try:
            if page.get_by_text(term, exact=False).first.is_visible():
                return True
        except Exception:
            continue
    return False


# ── Country code → search name mapping ───────────────────────────────────────
# ISO-2 → English name used as FB location search term.
# Covers all regions (LATAM, MENA, EU, APAC, Africa).
_COUNTRY_SEARCH: dict[str, str] = {
    # LATAM
    "MX": "Mexico", "CL": "Chile", "CO": "Colombia", "EC": "Ecuador",
    "PE": "Peru", "AR": "Argentina", "BR": "Brazil", "VE": "Venezuela",
    "BO": "Bolivia", "PY": "Paraguay", "UY": "Uruguay", "CR": "Costa Rica",
    "GT": "Guatemala", "HN": "Honduras", "SV": "El Salvador", "NI": "Nicaragua",
    "PA": "Panama", "DO": "Dominican Republic", "PR": "Puerto Rico",
    "CU": "Cuba", "JM": "Jamaica",
    # North America
    "US": "United States", "CA": "Canada",
    # Europe
    "GB": "United Kingdom", "DE": "Germany", "FR": "France", "ES": "Spain",
    "IT": "Italy", "PT": "Portugal", "NL": "Netherlands", "BE": "Belgium",
    "CH": "Switzerland", "AT": "Austria", "SE": "Sweden", "NO": "Norway",
    "DK": "Denmark", "FI": "Finland", "PL": "Poland", "CZ": "Czechia",
    "SK": "Slovakia", "HU": "Hungary", "RO": "Romania", "BG": "Bulgaria",
    "HR": "Croatia", "RS": "Serbia", "BA": "Bosnia and Herzegovina",
    "GR": "Greece", "TR": "Turkey", "UA": "Ukraine", "RU": "Russia",
    "BY": "Belarus", "LT": "Lithuania", "LV": "Latvia", "LU": "Luxembourg",
    "IE": "Ireland",
    # MENA
    "AE": "United Arab Emirates", "SA": "Saudi Arabia", "KW": "Kuwait",
    "QA": "Qatar", "OM": "Oman", "BH": "Bahrain", "IQ": "Iraq",
    "JO": "Jordan", "LB": "Lebanon", "EG": "Egypt", "MA": "Morocco",
    "TN": "Tunisia", "DZ": "Algeria", "LY": "Libya", "SY": "Syria",
    "YE": "Yemen", "IL": "Israel",
    # Africa
    "NG": "Nigeria", "GH": "Ghana", "KE": "Kenya", "ZA": "South Africa",
    "ET": "Ethiopia", "TZ": "Tanzania", "SN": "Senegal", "CM": "Cameroon",
    "ML": "Mali", "CI": "Ivory Coast", "MZ": "Mozambique", "ZM": "Zambia",
    "ZW": "Zimbabwe", "AO": "Angola", "NA": "Namibia", "BW": "Botswana",
    "SO": "Somalia", "SD": "Sudan",
    # APAC
    "AU": "Australia", "NZ": "New Zealand", "JP": "Japan", "CN": "China",
    "IN": "India", "ID": "Indonesia", "PH": "Philippines", "TH": "Thailand",
    "VN": "Vietnam", "MY": "Malaysia", "SG": "Singapore", "TW": "Taiwan",
    "KR": "South Korea", "BD": "Bangladesh", "PK": "Pakistan",
    "LK": "Sri Lanka", "NP": "Nepal", "KH": "Cambodia", "LA": "Laos",
    "MM": "Myanmar",
    # Central Asia
    "KZ": "Kazakhstan", "UZ": "Uzbekistan", "AZ": "Azerbaijan",
    "AM": "Armenia", "GE": "Georgia", "KG": "Kyrgyzstan", "TJ": "Tajikistan",
    "TM": "Turkmenistan",
    # Other
    "ALL": "Worldwide",
}

# Reverse map: lowercase English name → ISO-2 (auto-generated from _COUNTRY_SEARCH).
# Also includes common aliases.
_COUNTRY_NAME_TO_ISO: dict[str, str] = {
    v.lower(): k for k, v in _COUNTRY_SEARCH.items()
}
_COUNTRY_NAME_TO_ISO.update({
    "usa": "US", "united states of america": "US",
    "uk": "GB", "great britain": "GB",
    "uae": "AE", "emirates": "AE",
    "czech republic": "CZ",
    "south korea": "KR", "republic of korea": "KR",
    "russia": "RU", "russian federation": "RU",
    "viet nam": "VN",
    "bolivia (plurinational state of)": "BO",
    "venezuela (bolivarian republic of)": "VE",
    "iran": "IR", "syria": "SY", "syrian arab republic": "SY",
    "cote d'ivoire": "CI", "ivory coast": "CI",
    "tanzania": "TZ", "united republic of tanzania": "TZ",
})


def _country_search_term(code: str) -> str:
    """Accept an ISO-2 code ('CR') OR a full name ('Costa Rica') and return
    the English name to type into Facebook's location search box."""
    raw = (code or "").strip()
    cu = raw.upper()
    # Already an ISO-2 code
    if len(cu) == 2 and cu.isalpha():
        return _COUNTRY_SEARCH.get(cu, raw)
    # Full country name (e.g. "Costa Rica", "COSTA RICA") → look up via name map
    iso = _COUNTRY_NAME_TO_ISO.get(raw.lower())
    if iso:
        return _COUNTRY_SEARCH.get(iso, raw)
    # Fallback: use the original string (FB search can handle it in many cases)
    return raw


def _wait_location_options(root, timeout_s: float = 6.0) -> bool:
    """Ждёт, пока под полем поиска локаций появятся подсказки."""
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            # Подсказки Meta не всегда размечены role=option (17.09 19:45:
            # на всех кампаниях ждали впустую, а Enter потом добавлял страну);
            # открытый список виден по aria-expanded у поля поиска.
            if root.locator("[role=option]:visible").count() or root.locator(
                "input[placeholder*='location' i][aria-expanded='true'],"
                " input[placeholder*='ubicaci' i][aria-expanded='true']"
            ).count():
                return True
        except Exception:
            pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.3)


def _pick_country_suggestion(root, page: Page, search_term: str) -> bool:
    """Pick the country-level suggestion from an open location autocomplete.

    Facebook's location search often lists several granularities for the
    same typed text (e.g. a "Costa Rica (Country)" entry alongside a city or
    region that happens to share the name) — per live user guidance
    (2026-07-24), prefer the option explicitly tagged "(Country)" over a
    blind first-match, since picking the wrong granularity would silently
    narrow targeting instead of the whole country. Falls back to the first
    visible option, then a bare Enter, if no country-tagged option shows up.
    """
    country_re = re.compile(rf"^{re.escape(search_term)}\s*\(country\)", re.I)
    if not _wait_location_options(root):
        # 17.09 (CO_bnc FX, два прогона подряд): «Colombia» в поле, а список
        # подсказок так и не открылся (aria-expanded=false) — Enter ничего не
        # добавлял, осталась United States. Набираем заново, чтобы поиск
        # запустился ещё раз.
        try:
            page.keyboard.press("ControlOrMeta+A")
            page.keyboard.press("Backspace")
            _sleep_h(0.5)
            page.keyboard.type(search_term, delay=60)
        except Exception:
            pass
        if not _wait_location_options(root):
            _telemetry.record("audience_location", None, note="no_location_suggestions",
                              diag={"typed": search_term})
            return False
    for sel in ("[role=option]:visible", "li[role=option]"):
        try:
            opt = root.locator(sel).filter(has_text=country_re).first
            if opt.count() and opt.is_visible():
                opt.click(timeout=4000)
                _sleep_h(0.6)
                return True
        except Exception:
            continue
    for sel in (
        "[role=option]:visible",
        "[role=listbox] [role=option]:first-child",
        "li[role=option]:first-child",
        "ul[role=listbox] li:first-child",
        "li[role=option]",
    ):
        try:
            sug = root.locator(sel).first
            if sug.count() and sug.is_visible():
                sug.click(timeout=4000)
                _sleep_h(0.6)
                return True
        except Exception:
            continue
    page.keyboard.press("Enter")
    _sleep_h(0.5)
    return True


def _remove_stale_default_locations(page: Page, keep_text: str) -> None:
    """Remove pre-existing Inclusion location chips other than keep_text.

    Per live user request (2026-07-25): Meta ad accounts default the
    Locations field to some country (e.g. "Hong Kong" was the default seen
    live on both test accounts this session) — adding the target country is
    not enough on its own, the stale default stays alongside it and the
    audience keeps targeting both. Best-effort only: if no remove control is
    found, this silently does nothing rather than risk clicking the wrong
    thing (never remove the entry we just added).
    """
    # Ground truth from a live full outerHTML dump (2026-07-25, DQ204
    # Test30, output/debug_locations_section_after_add.txt): each Inclusion
    # location row is an <li> containing the location name text and a real
    # <button type="button"> whose accessible name is "Close" — computed
    # from a visually-hidden <span class="accessible_elem">Close</span>
    # child, not an aria-label attribute (why the earlier [aria-label*=...]
    # guesses found nothing). Both the newly-added country and the stale
    # default have this same structure. get_by_role("button", name="Close")
    # picks up that computed accessible name directly.
    keep_re = re.compile(re.escape(keep_text), re.I)
    for root in _roots(page):
        try:
            close_buttons = root.get_by_role("button", name="Close")
            n = close_buttons.count()
            removed_any = False
            for i in range(n):
                btn = close_buttons.nth(i)
                try:
                    row_text = btn.locator("xpath=ancestor::li[1]").inner_text(timeout=1000)
                except Exception:
                    row_text = ""
                # No <li> ancestor (or empty) means this "Close" button isn't
                # a location chip at all (e.g. an unrelated banner/dialog
                # close control elsewhere on the page) — skip rather than
                # risk clicking something unrelated. Also skip the row that
                # is the country we just added.
                if not row_text or keep_re.search(row_text):
                    continue
                try:
                    btn.click(timeout=3000, force=True)
                    _sleep_h(0.6)
                    removed_any = True
                    break
                except Exception:
                    try:
                        box = btn.bounding_box(timeout=1500)
                        if box and box.get("width", 0) > 0:
                            _cdp_mouse_click(page, box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                            _sleep_h(0.6)
                            removed_any = True
                            break
                    except Exception:
                        continue
            try:
                from src.fb_ads_rpa.screenshots import capture_on_success as _capture

                _capture(page, f"recorded_locations_remove_default_removed{removed_any}")
            except Exception:
                pass
            return
        except Exception:
            continue


def _location_rows(page: Page) -> list[dict]:
    """Included locations as the Locations card lists them now, each with the
    centre of its own remove control.

    The 2026-09 layout groups included locations by country: a heading
    ("United States") with an ✕ beside it, then the location row. The older
    `<li>` + "Close" button shape is still read for accounts that have it.
    Everything is scoped to the card that holds "Search locations", so an ✕
    elsewhere on the page — the WhatsApp Status banner in Placements has one
    — is never a candidate. That banner is very likely what the old code
    clicked on Doc21 (2026-09-16 17:40: `removedTrue`, United States still
    included).
    """
    try:
        return page.evaluate(
            r"""
            () => {
              const norm = (s) => (s || '').replace(/[\s​]+/g, ' ').trim();
              const search = [...document.querySelectorAll('input')].find(i =>
                /search locations?|buscar ubicaci/i.test(i.getAttribute('placeholder') || ''));
              if (!search) return [];
              let card = search;
              for (let i = 0; i < 12 && card; i++, card = card.parentElement) {
                if (/^Locations/i.test(norm(card.textContent)) || /Ubicaciones/i.test(norm(card.textContent).slice(0, 40))) break;
              }
              // 21.09 20:31 (SA_arc GMBL): блока, чей текст начинается словом
              // «Locations», в этой разметке нет вовсе — поиск упирался в
              // двенадцатый уровень, строки стран оставались в стороне, и
              // список выходил пустым. Тогда ищем блок по самим строкам.
              const hasRemove = (el) => !!el && [...el.querySelectorAll('[role=button], button, [aria-label]')].some(b => {
                const label = norm(b.getAttribute('aria-label') || '') + ' ' + norm(b.textContent);
                return /(close|remove|quitar|eliminar)/i.test(label);
              });
              if (!hasRemove(card)) {
                let alt = search;
                for (let i = 0; i < 20 && alt.parentElement; i++) {
                  alt = alt.parentElement;
                  if (hasRemove(alt)) { card = alt; break; }
                }
              }
              if (!card) return [];
              const searchBox = search.closest('[role=combobox]') || search.parentElement;
              const rows = [];
              const controls = [...card.querySelectorAll('[role=button], button, [aria-label]')].filter(b => {
                if (searchBox && searchBox.contains(b)) return false;
                const r = b.getBoundingClientRect();
                if (r.width <= 0 || r.height <= 0 || r.width > 60 || r.height > 60) return false;
                // Named controls only. An unnamed icon button here may be the
                // card's collapse chevron or the search box's expander, and
                // clicking either would be worse than leaving a default.
                const label = norm(b.getAttribute('aria-label') || '') + ' ' + norm(b.textContent);
                return /\b(close|remove|delete|quitar|eliminar)\b/i.test(label);
              });
              for (const b of controls) {
                let row = b.parentElement, name = '';
                for (let i = 0; i < 5 && row && row !== card; i++, row = row.parentElement) {
                  // 21.09 18:44 (CR_ait IPO): имя читалось как «United StatesClose» —
                  // слово прилипает без пробела, и чистка по границе слова его не
                  // убирала, поэтому строка не совпадала ни с одной страной.
                  const t = norm(row.textContent)
                    .replace(/\b(Close|Remove|Quitar|Eliminar)\b/gi, '')
                    .replace(/(Close|Remove|Quitar|Eliminar)\s*$/i, '').trim();
                  if (t && t.length <= 80) { name = t; break; }
                }
                if (!name) continue;
                const r = b.getBoundingClientRect();
                rows.push({ name, x: r.x + r.width / 2, y: r.y + r.height / 2 });
              }
              return rows;
            }
            """
        ) or []
    except Exception:
        return []


_LOCATION_SUMMARY_JS = r"""
  () => {
    const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
    const leaves = [...document.querySelectorAll('*')]
      .filter(e => e.offsetParent !== null && e.children.length === 0)
      .map(e => norm(e.textContent));
    const line = leaves.find(t => /^(Inclusion|Inclusion|Incluir)\s*:/i.test(t));
    if (!line) return null;
    return line.split(':').slice(1).join(':').split(',').map(x => norm(x)).filter(Boolean);
  }
"""
"""Свёрнутая строка «Inclusion: Colombia» — страны, которые Meta показывает списком."""


def _location_summary_countries(page: Page) -> list[str] | None:
    try:
        return page.evaluate(_LOCATION_SUMMARY_JS)
    except Exception:
        return None


def _only_wanted_in_summary(page: Page, keep_text: str) -> bool:
    """В сводке ровно наша страна и никого больше.

    18.09 06:32 (CO_bnc GMBL): строки карточки не прочитались
    (`location_rows_not_read`), и шаг отказал, хотя страна была одна. Когда
    строки не читаются, вопрос «сколько там стран» ещё можно задать сводке.
    """
    got = _location_summary_countries(page)
    if not got:
        return False
    keep = (keep_text or "").strip().lower()
    return len(got) == 1 and keep in got[0].lower()


_LOCATIONS_EDIT_JS = r"""
  () => {
    // 20.09 00:16 SA_arc GMBL: «All languages» стояла под нижней панелью
    // редактора, клик по её координатам попал в Close — редактор закрылся.
    // Сначала прокрутить к цели, потом убедиться, что в точке — она сама.
    const at = (t) => {
      t.scrollIntoView({block: 'center'});
      const r = t.getBoundingClientRect();
      const x = r.x + r.width / 2, y = r.y + r.height / 2;
      const hit = document.elementFromPoint(x, y);
      const nm = (s) => (s || '').replace(/[\s\u200b]+/g, ' ').trim();
      if (!hit || !(t.contains(hit) || (hit.contains(t) && nm(hit.textContent).length < 200))) return null;
      return { x, y };
    };
    const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
    const label = [...document.querySelectorAll('*')].find(e =>
      e.children.length === 0 && /^(Locations|Ubicaciones)$/i.test(norm(e.textContent))
      && e.getBoundingClientRect().width > 0);
    if (!label) return null;
    let row = label.parentElement;
    for (let i = 0; i < 4 && row; i++, row = row.parentElement) {
      const btns = [...row.querySelectorAll('[role=button], button, [aria-label]')].filter(b => {
        const r = b.getBoundingClientRect();
        if (r.width <= 0 || r.height <= 0 || r.width > 80) return false;
        const name = (norm(b.getAttribute('aria-label')) + ' ' + norm(b.textContent)).toLowerCase();
        return /edit|editar|pencil/.test(name) || (!norm(b.textContent) && r.width <= 48);
      });
      if (btns.length) {
        return at(btns[btns.length - 1]);
      }
    }
    return null;
  }
"""
"""Карандаш строки «Locations», когда карточка свёрнута в сводку."""


_SUMMARY_ROW_JS = r"""
  ([label, valueHint]) => {
    // 20.09 00:16 SA_arc GMBL: «All languages» стояла под нижней панелью
    // редактора, клик по её координатам попал в Close — редактор закрылся.
    // Сначала прокрутить к цели, потом убедиться, что в точке — она сама.
    const at = (t) => {
      t.scrollIntoView({block: 'center'});
      const r = t.getBoundingClientRect();
      const x = r.x + r.width / 2, y = r.y + r.height / 2;
      const hit = document.elementFromPoint(x, y);
      const nm = (s) => (s || '').replace(/[\s\u200b]+/g, ' ').trim();
      if (!hit || !(t.contains(hit) || (hit.contains(t) && nm(hit.textContent).length < 200))) return null;
      return { x, y };
    };
    const norm = (s) => (s || '').replace(/[\s\u200b]+/g, ' ').trim();
    const leaves = [...document.querySelectorAll('*')]
      .filter(e => e.children.length === 0 && e.getBoundingClientRect().width > 0);
    const head = leaves.find(e => new RegExp('^(' + label + ')$', 'i').test(norm(e.textContent)));
    if (!head) return null;
    const re = new RegExp(valueHint, 'i');
    // Значение строки: ближайший к подписи текст, похожий на сводку.
    const hit = leaves.find(e => re.test(norm(e.textContent))
      && Math.abs(e.getBoundingClientRect().top - head.getBoundingClientRect().top) < 80);
    const el = hit || head;
    const pt = at(el);
    if (!pt) return null;
    return { x: pt.x, y: pt.y, text: norm(el.textContent).slice(0, 60) };
  }
"""
"""Координаты значения свёрнутой строки — по ним и надо жать."""


def _click_summary_row(page: Page, label: str, value_hint: str) -> bool:
    """Открыть свёрнутую строку кликом по её значению.

    18.09 08:23 (CL_ml Trading, CR_ait Day): у Meta два вида свёрнутого блока
    аудитории. В одном у строки есть карандаш, в другом — ни одной кнопки,
    только текст «Inclusion: Chile, United States» / «All languages», и
    строка раскрывается кликом по самому тексту.
    """
    try:
        pt = page.evaluate(_SUMMARY_ROW_JS, [label, value_hint])
    except Exception:
        return False
    if not pt:
        return False
    _cdp_mouse_click(page, pt["x"], pt["y"])
    _sleep_h(0.8)
    return True


_ROW_EXPANDER_JS = r"""
  (labelRe) => {
    // 20.09 00:16 SA_arc GMBL: «All languages» стояла под нижней панелью
    // редактора, клик по её координатам попал в Close — редактор закрылся.
    // Сначала прокрутить к цели, потом убедиться, что в точке — она сама.
    const at = (t) => {
      t.scrollIntoView({block: 'center'});
      const r = t.getBoundingClientRect();
      const x = r.x + r.width / 2, y = r.y + r.height / 2;
      const hit = document.elementFromPoint(x, y);
      const nm = (s) => (s || '').replace(/[\s\u200b]+/g, ' ').trim();
      if (!hit || !(t.contains(hit) || (hit.contains(t) && nm(hit.textContent).length < 200))) return null;
      return { x, y };
    };
    const norm = (s) => (s || '').replace(/[\s\u200b]+/g, ' ').trim();
    const re = new RegExp('^(' + labelRe + ')$', 'i');
    const label = [...document.querySelectorAll('*')].find(e =>
      e.children.length === 0 && re.test(norm(e.textContent)) && e.getBoundingClientRect().width > 0);
    if (!label) return null;
    // 19.09 18:25 / 20.09 00:16 SA_arc: раскрывается вся строка — role=button
    // aria-expanded=false на 10 уровней выше подписи. Только строка, не
    // целый раздел (короткий текст).
    const own = label.closest('[role="button"][aria-expanded="false"]');
    if (own && norm(own.textContent).length < 200) {
      own.scrollIntoView({block: 'center'});
      return at(label);
    }
    let row = label.parentElement;
    for (let i = 0; i < 6 && row; i++, row = row.parentElement) {
      const exp = [...row.querySelectorAll('[aria-expanded="false"]')].filter(b => {
        const r = b.getBoundingClientRect();
        return r.width > 0 && r.height > 0 && !b.contains(label) &&
          (label.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);
      });
      if (exp.length) {
        return at(exp[0]);
      }
    }
    return null;
  }
"""


def _click_row_expander(page: Page, label_re: str) -> bool:
    """Раскрыть свёрнутую строку аудитории её стрелкой.

    19.09 15:00 (SA_arc GMBL, прогон 15479dc0955b): «Locations — Inclusion:
    Saudi Arabia, United States», карандаша нет, клик по значению строку не
    раскрывает; справа у строки — безымянная кнопка `aria-expanded="false"`.
    """
    try:
        pt = page.evaluate(_ROW_EXPANDER_JS, label_re)
    except Exception:
        return False
    if not pt:
        return False
    _cdp_mouse_click(page, pt["x"], pt["y"])
    _sleep_h(0.8)
    return True


_ROW_CENTRE_JS = r"""
  (labelRe) => {
    const norm = (s) => (s || '').replace(/[\s​]+/g, ' ').trim();
    const re = new RegExp('^(' + labelRe + ')$', 'i');
    const label = [...document.querySelectorAll('*')].find(e =>
      e.children.length === 0 && re.test(norm(e.textContent)) && e.getBoundingClientRect().width > 0);
    if (!label) return null;
    let row = label.closest('[role="button"]');
    if (!row) {
      row = label;
      for (let i = 0; i < 10 && row.parentElement; i++) {
        row = row.parentElement;
        if (row.getBoundingClientRect().width > 300) break;
      }
    }
    row.scrollIntoView({block: 'center'});
    const r = row.getBoundingClientRect();
    return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
  }
"""
"""Середина строки — туда наводят курсор, чтобы проявился её карандаш."""


def _hover_row(page: Page, label_re: str) -> bool:
    """Навести курсор на строку аудитории.

    20.09 01:30 (SA_arc GMBL): карандаш правки у строки Locations Meta
    рисует только под курсором — на скриншоте он есть (мышь стояла на
    строке), а в разметке снимка его нет. Без наведения строку нечем
    раскрыть, и страна остаётся неубранной.
    """
    try:
        pt = page.evaluate(_ROW_CENTRE_JS, label_re)
        if not pt:
            return False
        page.mouse.move(pt["x"], pt["y"])
        _sleep_h(0.5)
        return True
    except Exception:
        return False


_FOCUS_ROW_BUTTON_JS = r"""
  (labelRe) => {
    const norm = (s) => (s || '').replace(/[\s​]+/g, ' ').trim();
    const re = new RegExp('^(' + labelRe + ')$', 'i');
    const label = [...document.querySelectorAll('*')].find(e =>
      e.children.length === 0 && re.test(norm(e.textContent)) && e.getBoundingClientRect().width > 0);
    if (!label) return false;
    const row = label.closest('[role="button"]');
    if (!row) return false;
    row.scrollIntoView({block: 'center'});
    if (!row.hasAttribute('tabindex')) row.setAttribute('tabindex', '-1');
    row.focus();
    return document.activeElement === row;
  }
"""
"""Фокус на строке-кнопке — её раскрывают с клавиатуры."""


def _open_row_with_keyboard(page: Page, label_re: str) -> bool:
    """Раскрыть строку аудитории клавишей.

    20.09 05:39 (SA_arc GMBL, телеметрия locations_open=False/summary):
    карандаш не показывается даже под курсором, клик по тексту строки её не
    раскрывает. Но строка — настоящая кнопка (role=button), а кнопку можно
    нажать с клавиатуры, минуя и наведение, и координаты.
    """
    try:
        if not page.evaluate(_FOCUS_ROW_BUTTON_JS, label_re):
            return False
        page.keyboard.press("Enter")
        _sleep_h(0.8)
        return True
    except Exception:
        return False


def _click_locations_edit(page: Page) -> bool:
    """Раскрыть карточку локаций её собственной кнопкой правки.

    18.09 07:07 (CL_ml Day) и 07:09 (CO_bnc GMBL): в сводке стояло
    «Inclusion: Chile, United States» — нужная страна добавлена, лишняя
    осталась, а снять её нечем: у свёрнутой строки нет крестиков. Разметку
    строк (`_location_rows`) в таком виде тоже не прочитать.
    """
    _hover_row(page, "Locations|Ubicaciones")
    try:
        pt = page.evaluate(_LOCATIONS_EDIT_JS)
    except Exception:
        pt = None
    how = "pencil"
    if pt:
        _cdp_mouse_click(page, pt["x"], pt["y"])
    elif _click_row_expander(page, "Locations|Ubicaciones"):
        how = "expander"
    elif _click_summary_row(page, "Locations|Ubicaciones", "^(Inclusion|Inclusi\u00f3n)\\s*:"):
        how = "summary"
    else:
        _telemetry.record("locations_open", False, note="nothing_to_click")
        return False
    # 20.09 02:35 (SA_arc Trading): строки читали через 0.8 с после клика — при
    # сегодняшней медленной Meta карточка ещё не успевала раскрыться, и шаг
    # падал с location_rows_not_read. Ждём её собственное поле поиска до 8 с.
    opened = False
    for _ in range(16):
        try:
            opened = bool(page.evaluate(
                """() => [...document.querySelectorAll('input')].some(i =>
                  /search locations?|buscar ubicaci/i.test(i.getAttribute('placeholder') || ''))"""))
        except Exception:
            opened = False
        if opened:
            break
        _sleep_h(0.5)
    if not opened and _open_row_with_keyboard(page, "Locations|Ubicaciones"):
        how += "+enter"
        for _ in range(12):
            try:
                opened = bool(page.evaluate(
                    """() => [...document.querySelectorAll('input')].some(i =>
                      /search locations?|buscar ubicaci/i.test(i.getAttribute('placeholder') || ''))"""))
            except Exception:
                opened = False
            if opened:
                break
            _sleep_h(0.5)
    _telemetry.record("locations_open", opened, note=how)
    return True


_LOCATIONS_COLLAPSE_JS = r"""
  () => {
    const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
    const label = [...document.querySelectorAll('*')].find(e =>
      e.children.length === 0 && /^(Locations|Ubicaciones)$/i.test(norm(e.textContent))
      && e.getBoundingClientRect().width > 0);
    if (!label) return null;
    let row = label.parentElement;
    for (let i = 0; i < 5 && row; i++, row = row.parentElement) {
      const open = [...row.querySelectorAll('[aria-expanded="true"]')].filter(b => {
        const r = b.getBoundingClientRect();
        return r.width > 0 && r.height > 0 && r.width <= 80;
      });
      if (open.length) {
        const r = open[open.length - 1].getBoundingClientRect();
        return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
      }
    }
    return null;
  }
"""
"""Кнопка-створка раскрытой карточки локаций."""


def _collapse_locations_card(page: Page) -> bool:
    """Свернуть карточку локаций обратно.

    18.09 07:42 (CL_ml Day, CO_bnc GMBL): после карандаша карточка осталась
    раскрытой — с картой и полем поиска, — страница уехала, и следующие шаги
    (языки, открытие детального таргетинга) своих полей уже не нашли. Страну
    поставили правильно, а соседние шаги сломали.
    """
    try:
        pt = page.evaluate(_LOCATIONS_COLLAPSE_JS)
    except Exception:
        return False
    if not pt:
        return False
    _cdp_mouse_click(page, pt["x"], pt["y"])
    _sleep_h(0.6)
    return True


def _done(page: Page, opened_by_us: bool, result):
    """Вернуть ответ шага, свернув карточку, если раскрывали её мы."""
    if opened_by_us:
        _collapse_locations_card(page)
    return result


_SCROLL_LOCATION_ROW_JS = r"""
  (name) => {
    const norm = (s) => (s || '').replace(/[\s\u200b]+/g, ' ').trim();
    const clean = (s) => norm(s)
      .replace(/\b(Close|Remove|Quitar|Eliminar)\b/gi, '')
      .replace(/(Close|Remove|Quitar|Eliminar)\s*$/i, '').trim();
    const wanted = clean(name).toLowerCase();
    const ctrls = [...document.querySelectorAll('[role=button], button, [aria-label]')].filter(b => {
      const label = norm(b.getAttribute('aria-label') || '') + ' ' + norm(b.textContent);
      return /(close|remove|quitar|eliminar)/i.test(label);
    });
    for (const b of ctrls) {
      let row = b.parentElement;
      for (let i = 0; i < 5 && row; i++, row = row.parentElement) {
        const t = clean(row.textContent);
        if (t && t.length <= 80) {
          if (t.toLowerCase() !== wanted) break;
          b.scrollIntoView({block: 'center'});
          const r = b.getBoundingClientRect();
          if (r.width <= 0 || r.height <= 0) return null;
          return {x: r.x + r.width / 2, y: r.y + r.height / 2};
        }
      }
    }
    return null;
  }
"""
"""Прокрутить строку страны в окно и вернуть свежие координаты её крестика."""


def _scroll_location_row_into_view(page: Page, name: str) -> dict | None:
    """Строка может стоять далеко за окном — клик по старым координатам мимо."""
    try:
        return page.evaluate(_SCROLL_LOCATION_ROW_JS, name)
    except Exception:
        return None


def _clear_other_locations(page: Page, keep_text: str) -> list[str] | None:
    """Remove every included location that is not `keep_text`; return the
    names still left afterwards (empty list = clean, None = unreadable).

    Verified by reading the card again after each click, not by the click
    being sent: `_remove_stale_default_locations` reported `removedTrue` on a
    run whose draft still targeted United States next to Guatemala.
    """
    keep = (keep_text or "").strip().lower()
    opened_by_us = False
    seen = _location_rows(page)
    if not seen and _click_locations_edit(page):
        # Свёрнутая карточка: строк нет, пока её не раскрыли карандашом.
        opened_by_us = True
        seen = _location_rows(page)
    if not seen and _only_wanted_in_summary(page, keep_text):
        # 18.09 06:32 (CO_bnc GMBL): карточку прочитать не удалось, а страна
        # была одна — это видно по свёрнутой строке «Inclusion: …».
        _telemetry.record("audience_location", None, note="rows_unread_summary_clean",
                          diag={"keep": keep_text,
                                "summary": _location_summary_countries(page)})
        return _done(page, opened_by_us, [])
    if not seen:
        # Not even the target's own row was read: the card's shape is not
        # the one this knows. Say so rather than pass silently, and keep the
        # page so the next fix is read off it, not guessed.
        _telemetry.record("audience_location", None, note="location_rows_not_read",
                          diag={"keep": keep_text})
        try:
            from src.fb_ads_rpa import page_capture as _pc

            _pc.capture(page, step="locations_unread")
        except Exception:
            pass
        return _done(page, opened_by_us, None)
    for _ in range(6):
        rows = _location_rows(page)
        if not rows:
            return _done(page, opened_by_us,
                         [] if _only_wanted_in_summary(page, keep_text) else None)
        if not any(keep in r["name"].lower() for r in rows):
            # Do not delete the old country before the target is confirmed.
            return _done(page, opened_by_us, [r["name"] for r in rows])
        others = [r for r in rows if keep not in r["name"].lower()]
        if not others:
            return _done(page, opened_by_us, [])
        target = others[0]
        # 21.09 18:44 (CR_ait IPO): строка стояла на y=3811 — далеко за окном, и
        # клик по её координатам уходил в пустоту. Прокручиваем к ней, берём
        # свежие координаты, затем наводим курсор: крестик, как и карандаш,
        # рисуется только под ним.
        fresh = _scroll_location_row_into_view(page, target["name"])
        if fresh:
            target = {**target, **fresh}
        try:
            page.mouse.move(target["x"], target["y"])
            _sleep_h(0.4)
        except Exception:
            pass
        _cdp_mouse_click(page, target["x"], target["y"])
        _sleep_h(0.9)
    rows = _location_rows(page)
    if not rows:
        return _done(page, opened_by_us,
                     [] if _only_wanted_in_summary(page, keep_text) else None)
    return _done(page, opened_by_us, [r["name"] for r in rows if keep not in r["name"].lower()])


_LOCATION_SEARCH_JS = r"""
  () => !![...document.querySelectorAll('input')].find(i =>
    /search locations?|add locations?|buscar ubicaci|agregar ubicaci/i.test(i.getAttribute('placeholder') || ''))
"""
"""Поле поиска локаций на странице — значит, карточка раскрыта."""


def _pick_country_and_clear_defaults(root, page: Page, search_term: str) -> bool:
    if not _pick_country_suggestion(root, page, search_term):
        # 20.09 15:04 (SA_arc GMBL): страна не добавилась, в карточке осталась
        # одна United States — а её крестик Meta держит отключённым, пока
        # страна единственная, так что убрать её нечем. Карточку к этому
        # моменту уже раскрыли (Enter), поле поиска на месте: вводим страну
        # ещё раз в него.
        opened = _click_locations_edit(page)
        try:
            has_input = bool(page.evaluate(_LOCATION_SEARCH_JS))
        except Exception:
            has_input = False
        if not (opened and has_input and _pick_country_suggestion(root, page, search_term)):
            return False
        _telemetry.record("audience_location", None, note="added_after_reopen",
                          diag={"keep": search_term})
    # The old remover above can report success on the wrong ✕; this is the
    # check that decides. A default country left beside the target means the
    # campaign spends in two markets, so it fails the step instead of passing.
    left = _clear_other_locations(page, search_term)
    if left is None:
        _telemetry.record("audience_location", False, note="location_rows_not_read",
                          diag={"keep": search_term})
        return False
    if left:
        _telemetry.record("audience_location", False, note="default_location_left",
                          diag={"keep": search_term, "left": left[:5]})
        return False
    return True


def fill_audience_location(page: Page, country_code: str) -> bool:
    """Type country into Audience → Locations input and pick first suggestion."""
    search_term = _country_search_term(country_code)
    # Don't call scroll_to_section here at all — confirmed live 2026-07-24
    # (recorded_flow path) that this page was ALREADY correctly positioned
    # with Locations clearly visible before this function ran (screenshot
    # evidence), yet scroll_to_section's own "Locations"/"Audience" text
    # search matched some OTHER instance of that text earlier in the DOM and
    # scrolled all the way back up to it, leaving the real field off-screen
    # for every placeholder/input search below. get_by_placeholder(...) below
    # targets the specific input directly and Playwright's own .click()/
    # .fill() already auto-scroll that exact element into view — no need to
    # pre-scroll via a much fuzzier heading/text match first.
    _sleep_h(0.4)

    # Locations renders as a collapsed read-only summary ("Locations /
    # Inclusion: Hong Kong") with no visible input at all — confirmed live
    # 2026-07-24 via a placeholder dump (only "Ad set name" and "Search
    # existing audiences" were visible, no location-related input). Click
    # the summary/edit control to expand it into edit mode first, the same
    # way Conversion location needed an explicit open-dropdown click before
    # any option became interactable.
    #
    # Ground truth from a live outerHTML dump (2026-07-24,
    # output/debug_locations_row_html.txt): this row has NO real
    # button/link/role=button anywhere — only a role="presentation" (non-
    # interactive per ARIA) info-icon SVG and a bare
    # <span><strong>Inclusion:</strong> value</span>. Several locator-based
    # .click() attempts on different targets in this row all produced the
    # identical result — the info tooltip — which first looked like a
    # hover-intercept (Playwright's click() moves the mouse first, which can
    # trigger the icon's :hover tooltip before the press lands). Switching to
    # a raw CDP click (no separate hover dwell) still hit the same tooltip on
    # a live retest (2026-07-24) — but that time a fresh outerHTML dump
    # proved "Locations" WAS located and scrolled to correctly right before
    # the click, while the failure screenshot showed the page back at the
    # Conversion section with the tooltip floating over it. So the real
    # cause is a page-side scroll/re-render race between locate and click
    # (already suspected in the diary's "unstable scroll after
    # Further-limit-reach" notes), not purely a hover intercept — by the time
    # the click fires, our computed coordinates may no longer point at
    # Locations at all. Mitigation: retry the whole
    # locate -> scroll -> click -> verify-edit-mode cycle a few times instead
    # of trusting one shot, so a later attempt can catch the row after Meta's
    # transient re-render settles.
    location_placeholders = [
        re.compile(r"Add locations?", re.I),
        re.compile(r"Agregar ubicaciones?", re.I),
        re.compile(r"Search locations?", re.I),
        re.compile(r"Buscar ubicaciones?", re.I),
        re.compile(r"Enter a location", re.I),
    ]

    def _find_edit_mode_input():
        for root in _roots(page):
            for ph in location_placeholders:
                try:
                    inp = root.get_by_placeholder(ph).first
                    if inp.count() and inp.is_visible():
                        return root, inp
                except Exception as exc:
                    try:
                        with open("output/debug_locations_find_input_error.txt", "a", encoding="utf-8") as f:
                            f.write(f"ph={ph.pattern!r} exc={exc!r}\n")
                    except Exception:
                        pass
                    continue
        return None, None

    dumped_html = False
    for attempt in range(3):
        for root in _roots(page):
            try:
                heading_locator = root.get_by_text(re.compile(r"^Locations$|^Ubicaciones$", re.I))
                heading = heading_locator.first
                heading.wait_for(state="visible", timeout=3000)
                heading.scroll_into_view_if_needed(timeout=3000)
                row = heading.locator("xpath=ancestor::*[3]")
                if not dumped_html:
                    try:
                        html_dump = row.evaluate("el => el.outerHTML")
                        Path("output/debug_locations_row_html.txt").write_text(
                            html_dump[:6000], encoding="utf-8"
                        )
                        dumped_html = True
                    except Exception:
                        pass
                    # Diagnostic (2026-07-24): every click variant tried so
                    # far lands on the same tooltip/wrong-place result
                    # regardless of exact target, on 2 different accounts —
                    # so capture what's actually on screen right when we
                    # believe we've found+scrolled to "Locations", plus how
                    # many DOM nodes match that text, to check whether this
                    # locator is matching a duplicate summary/overview
                    # instance instead of the real editable field.
                    try:
                        match_count = heading_locator.count()
                        Path("output/debug_locations_match_count.txt").write_text(
                            str(match_count), encoding="utf-8"
                        )
                    except Exception:
                        pass
                    try:
                        from src.fb_ads_rpa.screenshots import capture_on_success as _capture

                        _capture(page, "recorded_locations_heading_found")
                    except Exception:
                        pass
                clicked = False
                # Attempt #9 (2026-07-24): a diagnostic screenshot taken at
                # exactly this point (before any click) confirmed the page IS
                # correctly scrolled to the real, unique (count==1) Locations
                # row — ruling out "wrong element found" and "scroll race" as
                # causes. Every click target tried so far (icon, value text,
                # _7zgk block, header, whole row) produced the identical
                # tooltip result regardless of which one — consistent with
                # the row having a single shared hover/click handler on a
                # wrapping element, where the real "open edit mode" trigger
                # might not even exist in the static outerHTML dumped so far
                # (those dumps were taken via el.outerHTML with no real mouse
                # movement — if Meta conditionally renders an edit affordance
                # only on :hover via React state, a dump without a real hover
                # would never see it). Hover first (real mouse move + dwell,
                # not a click) and re-dump the row afterward to check for a
                # newly-appeared button/pencil-icon before falling back to
                # the same click cascade as before.
                section = heading.locator("xpath=ancestor::*[6]")
                if not dumped_html:
                    try:
                        section_dump = section.evaluate("el => el.outerHTML")
                        Path("output/debug_locations_section_html.txt").write_text(
                            section_dump[:15000], encoding="utf-8"
                        )
                    except Exception:
                        pass
                try:
                    row.hover(timeout=2000)
                    _sleep_h(0.6)
                    hover_dump = row.evaluate("el => el.outerHTML")
                    Path("output/debug_locations_row_html_after_hover.txt").write_text(
                        hover_dump[:6000], encoding="utf-8"
                    )
                    section_hover_dump = section.evaluate("el => el.outerHTML")
                    Path("output/debug_locations_section_html_after_hover.txt").write_text(
                        section_hover_dump[:15000], encoding="utf-8"
                    )
                except Exception:
                    pass
                for hover_sel in ("button", "[role=button]", "[aria-label*=edit i]", "a"):
                    try:
                        hover_btn = row.locator(hover_sel).first
                        if hover_btn.count() and hover_btn.is_visible():
                            box = hover_btn.bounding_box(timeout=1500)
                            if box and box.get("width", 0) > 0:
                                _cdp_mouse_click(page, box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                            else:
                                hover_btn.click(timeout=3000)
                            clicked = True
                            break
                    except Exception:
                        continue
                inclusion_block = row.locator("._7zgk").first
                for candidate in ([] if clicked else [inclusion_block, heading.locator("xpath=ancestor::*[2]"), row]):
                    try:
                        box = candidate.bounding_box(timeout=1500)
                        if box and box.get("width", 0) > 0 and box.get("height", 0) > 0:
                            _cdp_mouse_click(page, box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                            clicked = True
                            break
                    except Exception:
                        continue
                if not clicked:
                    for sel in ("button", "[role=button]", "a"):
                        try:
                            btn = row.locator(sel).first
                            if btn.count() and btn.is_visible():
                                btn.click(timeout=3000)
                                clicked = True
                                break
                        except Exception:
                            continue
                # Check for the edit-mode input right after a successful
                # click — live evidence 2026-07-25 (DQ204 Test22-25) showed
                # the click reliably DOES open the real editor (search input
                # + "Include"/"Browse" visible in a screenshot taken right
                # after), confirmed reproducible on attempt 1 of every run —
                # yet both a zero-delay check (Test23) AND a flat 0.25s-delay
                # check (Test24/25) missed it every single time, despite the
                # screenshot taken immediately afterward proving it WAS open
                # at roughly that same moment. A single point-in-time check
                # isn't reliable here — the open/closed state looks like it's
                # flickering (React re-mount racing our query, not a clean
                # one-way "opens then closes after Xms" timeline), or our
                # query is hitting a transient "execution context destroyed"
                # error during a re-render and silently swallowing it. Poll
                # repeatedly for a short window instead of trusting one shot.
                immediate_root, immediate_inp = (None, None)
                if clicked:
                    for _ in range(10):
                        _sleep_h(0.15)
                        immediate_root, immediate_inp = _find_edit_mode_input()
                        if immediate_inp is not None:
                            break
                try:
                    from src.fb_ads_rpa.screenshots import capture_on_success as _capture

                    _capture(page, f"recorded_locations_click_attempt{attempt + 1}_clicked{clicked}")
                except Exception:
                    pass
                if immediate_inp is not None:
                    try:
                        immediate_inp.scroll_into_view_if_needed(timeout=3000)
                        immediate_inp.click(timeout=3000)
                        _sleep_h(0.3)
                        # Root cause found live 2026-07-25 (DQ204 Test26):
                        # .fill() throws — Playwright's error log shows the
                        # placeholder-matched element resolves to a
                        # <span placeholder="Search locations" ...>, not a
                        # real <input>/<textarea>/[contenteditable]. Meta
                        # built this as a custom fake-input component, so
                        # .fill() is fundamentally incompatible with it.
                        # Type via keyboard instead — it dispatches key
                        # events to whatever has DOM focus regardless of
                        # element type, which a real <input> doesn't need
                        # but this span-based component does.
                        page.keyboard.type(search_term, delay=40)
                        _sleep_h(1.5)
                        return _pick_country_and_clear_defaults(immediate_root, page, search_term)
                    except Exception as fill_exc:
                        try:
                            with open("output/debug_locations_fill_error.txt", "a", encoding="utf-8") as f:
                                f.write(f"attempt={attempt + 1} exc={fill_exc!r}\n")
                        except Exception:
                            pass
                elif clicked:
                    try:
                        with open("output/debug_locations_fill_error.txt", "a", encoding="utf-8") as f:
                            f.write(
                                f"attempt={attempt + 1}: clicked=True but "
                                f"_find_edit_mode_input() found nothing after polling\n"
                            )
                    except Exception:
                        pass
                if clicked:
                    break
            except Exception:
                continue

        # Check for the edit-mode input immediately rather than trusting the
        # click blindly — if it didn't land (stale coordinates, tooltip),
        # retry the whole locate+click cycle instead of falling straight
        # through to a single 5s wait per placeholder.
        _sleep_h(0.5)
        root, inp = _find_edit_mode_input()
        if inp is not None:
            try:
                inp.scroll_into_view_if_needed(timeout=3000)
                inp.click(timeout=3000)
                _sleep_h(0.3)
                # See the note above (attempt loop): this is a <span
                # placeholder="..."> fake-input, not a real editable
                # element — .fill() throws, keyboard typing works.
                page.keyboard.type(search_term, delay=40)
                _sleep_h(1.5)
                return _pick_country_and_clear_defaults(root, page, search_term)
            except Exception:
                pass
        else:
            # No element matched our placeholder list, but the click may
            # still have focused a hidden/proxy input — Meta sometimes keeps
            # the real text field opacity:0 or zero-width behind the
            # read-only summary until typing starts (per live user request
            # 2026-07-24: "just try typing the country" instead of only
            # hunting for a visible input first). Type blind at whatever
            # currently has focus and only declare success if a real
            # suggestion list actually shows up — otherwise this would
            # falsely report success on a stray keystroke landing nowhere.
            try:
                page.keyboard.type(search_term, delay=40)
                _sleep_h(1.5)
                if page.locator("[role=option]:visible, li[role=option]").first.count():
                    return _pick_country_and_clear_defaults(page, page, search_term)
            except Exception:
                pass
        _sleep_h(0.5)

    # Fallback: find any input near "Locations" label
    for root in _roots(page):
        try:
            marker = root.get_by_text(re.compile(r"Locations|Ubicaciones", re.I)).first
            marker.scroll_into_view_if_needed(timeout=5000)
            for depth in range(1, 10):
                row = marker.locator(f"xpath=ancestor::*[{depth}]")
                inp = row.locator("input").first
                if inp.count() and inp.is_visible():
                    inp.click(timeout=5000)
                    _sleep_h(0.3)
                    inp.fill(search_term, timeout=5000)
                    _sleep_h(1.5)
                    return _pick_country_and_clear_defaults(page, page, search_term)
        except Exception:
            pass
    return False


def _advantage_plus_audience_on(page: Page) -> bool | None:
    """Is the ad set's audience in Advantage+ mode right now?

    Two tells, either is enough: the "Advantage+ on" badge on the Audience
    card, or the "Advantage+ leads campaign" switch reading checked. None
    when neither control can be found at all — unknown is not "off".

    Live run `ceab1a4bf031` (2026-09-16): the age step ran with both tells
    on screen, clicked 35, and the dropdowns stayed at 18–65+. In that mode
    the age controls are Advantage+ suggestions, not a hard range.
    """
    js = r"""
    () => {
      const norm = (s) => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
      const leaves = [...document.querySelectorAll('*')]
        .filter(e => e.offsetParent !== null && e.children.length === 0);
      let seen = false;
      for (const e of leaves) {
        const t = norm(e.textContent);
        if (t === 'advantage+ on') return true;
        if (t === 'advantage+ off') seen = true;
      }
      for (const el of document.querySelectorAll('*')) {
        if (!el.offsetParent) continue;
        const t = norm(el.textContent).slice(0, 80);
        if (!t.includes('advantage+ leads')) continue;
        let node = el;
        for (let i = 0; i < 6 && node; i++, node = node.parentElement) {
          const sw = node.querySelector('[role=switch], input[type=checkbox], [aria-checked]');
          if (!sw || !sw.offsetParent) continue;
          seen = true;
          if (sw.getAttribute('aria-checked') === 'true' || sw.checked === true) return true;
          break;
        }
      }
      return seen ? false : null;
    }
    """
    for root in _roots(page):
        try:
            got = root.evaluate(js)
        except Exception:
            continue
        if got is not None:
            return bool(got)
    return None


def _ensure_manual_audience(page: Page) -> bool | None:
    """Leave Advantage+ audience mode before touching the age controls.

    Returns the state AFTER: False = manual, True = still Advantage+, None =
    could not tell. Records its own telemetry row either way, so a run where
    the switch-off did not hold can be told from one where it was never
    attempted — until 2026-09-16 nothing recorded either.

    The conversion step already switches Advantage+ off ("Advantage+ On
    forces Website and instant forms"), and on three of four runs that held.
    On the fourth it was On again by the age step. Checking here, right
    before the control that depends on it, is cheaper than working out why.
    """
    # ACL-1313 (25.09): состояние читается по бейджу карточки Audience, а не
    # по всей странице. `_advantage_plus_audience_on` видел «Advantage+ on» у
    # Placements и кампанийный переключатель, отвечал «on» всегда и кликал
    # впустую (162 записи still_on). Бейдж «off» или бейджа нет — ничего не
    # трогаем: итог после возраста проверяет `verify_audience_advantage_off`.
    before = _audience_badge_on(page)
    badge = {True: "on", False: "off"}.get(before)
    if before is not True:
        _telemetry.record("advantage_plus_off", None,
                          note="already_off" if before is False else "badge_not_found",
                          diag={"badge": badge})
        return before
    flipped = _turn_off_advantage_plus_leads(page)
    picked = False
    if not flipped:
        # On the 2026-09 layout the leads-campaign control is not a switch
        # at all: a `role=button` reading "On" beside the label that opens a
        # menu with "Off" (page dump of run 86b7cd7edda9 — three switches on
        # the page, none of them this one). Open it and pick Off.
        picked = _pick_advantage_plus_leads_off(page)
    if not flipped and not picked:
        # Older Audience card: a link out of Advantage+ audience. Best
        # effort, verified below like everything else here.
        _click_xpath(
            page,
            "//*[self::a or @role='button' or @role='link' or self::button]"
            "[contains(normalize-space(.), 'Switch to original audience')]",
        )
    _sleep_h(0.8)
    after = _audience_badge_on(page)
    if after is True:
        # Разметка до и после клика снималась на каждом прогоне (ACL-1313 —
        # теперь только с FB_RPA_SHOT_ALL=1); отказу снимок остаётся.
        try:
            from src.fb_ads_rpa import page_capture as _pc

            _pc.capture(page, step="advantage_plus_still_on")
        except Exception:
            pass
    _telemetry.record("advantage_plus_off", after is not True,
                      note="switched" if after is not True else "still_on",
                      diag={"flipped_switch": flipped, "picked_off": picked,
                            "badge": badge, "badge_after": {True: "on", False: "off"}.get(after)})
    return after


def _audience_badge_on(page: Page) -> bool | None:
    """Бейдж карточки Audience: True «on», False «off», None — карточки нет.

    ACL-1313, раскладка 09.2026 (снимки b30c3b2b6be9/00, d3e690189a6e/04):
    Advantage+ audience — не переключатель, а надпись в заголовке карточки
    `#advantage-state-audience-section-header`, которую ставит сама Meta.
    Такая же «Advantage+ on» стоит у Placements, поэтому поиск по всей
    странице (`_advantage_plus_audience_on`) читал аудиторию как «on» и
    после выключения (8587f7668f86/02).
    """
    js = r"""
    () => {
      const h = document.getElementById('advantage-state-audience-section-header');
      if (!h) return null;
      const t = (h.textContent || '').replace(/\s+/g, ' ').toLowerCase();
      if (t.includes('advantage+ off')) return false;
      if (t.includes('advantage+ on')) return true;
      return null;
    }
    """
    for root in _roots(page):
        try:
            got = root.evaluate(js)
        except Exception:
            continue
        if got is not None:
            return bool(got)
    return None


def verify_audience_advantage_off(page: Page) -> bool:
    """Итог: карточка Audience читает «Advantage+ off». Звать ПОСЛЕ возраста.

    Meta снимает Advantage+ audience сама, когда в аудитории появляется
    жёсткое условие — возраст со снятой «Use as a suggestion»
    (d3e690189a6e/04). До возраста «on» — норма, поэтому прежняя проверка
    `advantage_plus_off` (до возраста) давала still_on во всех 346 записях.
    Единственный наш рычаг здесь — та галочка: снять ещё раз и перечитать.
    """
    state = _audience_badge_on(page)
    retried = state is True
    if retried:
        _uncheck_age_suggestion(page)
        _sleep_h(0.8)
        state = _audience_badge_on(page)
    ok = state is False
    _telemetry.record(
        "advantage_plus_audience", ok,
        note="off" if ok else ("still_on" if state else "badge_not_found"),
        diag={"retried_suggestion": retried, "age": _age_triggers_read(page)},
    )
    return ok


def _count_xpath(page: Page, xpath: str) -> int:
    """How many nodes an XPath matches on the live page; -1 if it could not ask."""
    try:
        return int(page.evaluate(
            "(xp) => document.evaluate(xp, document, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null).snapshotLength",
            xpath,
        ))
    except Exception:
        return -1


def _pick_advantage_plus_leads_off(page: Page) -> bool:
    """Open the "Advantage+ leads campaign · On ˅" menu and choose Off.

    True when Off was clicked; the caller re-reads the page to see whether
    it took. The trigger is the first `role=button` after the label whose
    own text is "On"; the menu item is a visible leaf reading "Off".
    """
    # Measured on the saved page of run 86b7cd7edda9 (offline Chrome over the
    # dump): the label is a `role=heading` span; "On" next to it is plain
    # text, not a button; the control is the FIRST `role=button` after the
    # heading — an expander whose accessible text is "Open". The earlier
    # guesses ("a button reading On", a switch) matched nothing at all,
    # which is why three parallel runs logged `picked_off: false`.
    # Keep the page BEFORE touching anything: the second parallel batch
    # (15:04) found no expander at all on a resized window, and without a
    # capture that is one more guess. What the control looks like right now
    # is the one fact the next fix needs.
    try:
        from src.fb_ads_rpa import page_capture as _pc

        _pc.capture_on_success(page, step="advantage_plus_before")
    except Exception:
        pass
    # The control is the row itself: a `role=button` (325×36) that CONTAINS
    # the heading, its state text "On" and the chevron. Measured on the page
    # captured right before the click (run b238c187884b, 15:29). Two earlier
    # readings were wrong in instructive ways: a button reading "On" does
    # not exist, and the first button AFTER the heading at that moment is
    # the editor's **Close** at the bottom of the screen — the unscoped
    # `following::` path clicked it. The ancestor is the one thing that is
    # there whenever the heading is.
    #
    # Matched on the element's whole string value `normalize-space(.)`, not
    # on `text()`. React renders "Advantage+" and " leads campaign" as
    # separate text nodes; `text()` then yields only the first one, and the
    # path matched nothing live — five runs in a row — while matching every
    # saved page, because serialising to HTML and parsing back merges
    # adjacent text nodes into one. The row itself does not equal the label
    # (its string value also carries "On"), so only the label elements match
    # and their nearest `role=button` ancestor is the row.
    opened = _click_xpath(
        page,
        "(//*[normalize-space(.)='Advantage+ leads campaign']"
        "/ancestor::*[@role='button'][1])[1]",
    )
    if not opened:
        _telemetry.record("advantage_plus_off", None, note="row_not_found",
                          diag={"labels": _count_xpath(page, "//*[normalize-space(.)='Advantage+ leads campaign']")})
        return False
    _sleep_h(0.8)
    # What opened is not known offline. Keep the page so the next run says.
    try:
        _pc.capture_on_success(page, step="advantage_plus_control")
    except Exception:
        pass
    # Only a real menu item. 16.09, run e79f678f1450 (Doc21 FX): the row
    # opens a checklist (Budget / Audience / Placements), not a menu, and the
    # old path also accepted any div/span starting with "Off" — it hit the
    # label of the **Dynamic creative** switch, Meta raised "How to create ads
    # using dynamic creative", and that dialog sat over the age dropdowns.
    picked = _click_xpath(
        page,
        "(//*[@role='menuitem' or @role='option' or @role='menuitemradio']"
        "[starts-with(normalize-space(.), 'Off') and string-length(normalize-space(.)) < 40])[1]",
    )
    _cancel_dynamic_creative_dialog(page)
    return picked


def _cancel_dynamic_creative_dialog(page: Page) -> bool:
    """Dismiss Meta's "How to create ads using dynamic creative" with Cancel.

    Continue would switch the ad set to dynamic creative and drop its
    customisations — never what the operator built. True when a dialog was
    found and cancelled.
    """
    try:
        dialog = page.locator("[role=dialog]").filter(
            has_text=re.compile(r"dynamic creative", re.I)
        )
        if not dialog.count():
            return False
        dialog.last.get_by_role("button", name=re.compile(r"^Cancel$", re.I)).first.click(timeout=3000)
        _sleep_h(0.5)
        _telemetry.record("dynamic_creative_dialog", None, note="cancelled")
        return True
    except Exception:
        return False


_AGE_VALUE_RE = re.compile(r"^\d{2}\+?$")


def _age_triggers_read(page: Page) -> list[str]:
    """What the two closed age dropdowns show right now, e.g. ['18', '65+'].

    Visible leaves that look like an age value, inside the Age row (the
    nearest ancestor of the "Age" label that holds two of them), and not
    inside an open listbox. Empty when the row cannot be found.
    """
    js = r"""
    () => {
      const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
      const isAge = (t) => /^\d{2}\+?$/.test(t);
      const leaves = [...document.querySelectorAll('*')]
        .filter(e => e.offsetParent !== null && e.children.length === 0);
      const label = leaves.find(e => /^(Age|Edad)$/i.test(norm(e.textContent)));
      if (!label) return [];
      let node = label;
      for (let i = 0; i < 8 && node; i++, node = node.parentElement) {
        const vals = [...node.querySelectorAll('*')]
          .filter(e => e.offsetParent !== null && e.children.length === 0
                    && !e.closest('[role=listbox]') && isAge(norm(e.textContent)))
          .map(e => norm(e.textContent));
        if (vals.length >= 2) return vals.slice(0, 2);
      }
      return [];
    }
    """
    for root in _roots(page):
        try:
            got = root.evaluate(js)
        except Exception:
            continue
        if got:
            return [str(v) for v in got]
    return []


def _uncheck_age_suggestion(page: Page) -> None:
    """Снять «Use as a suggestion» — иначе Meta расширяет возраст сама.

    Галочка живёт прямо под границами и по умолчанию включена, а с ней возраст
    становится пожеланием: итог «Your suggestion 35–65+».
    """
    try:
        page.evaluate(
            """() => {
              const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
              const row = [...document.querySelectorAll('*')].find(e =>
                norm(e.textContent).startsWith('Use as a suggestion') && e.offsetParent !== null);
              if (!row) return;
              let box = row;
              for (let i = 0; i < 6 && box; i++) {
                const cb = box.querySelector('input[type=checkbox]');
                if (cb) {
                  const aria = cb.getAttribute('aria-checked');
                  const on = aria === 'true' ? true : aria === 'false' ? false : cb.checked;
                  if (on) cb.click();
                  return;
                }
                box = box.parentElement;
              }
            }"""
        )
        _sleep_h(0.3)
    except Exception:
        pass


def _age_shows_value(page: Page, value: str) -> bool:
    """Стоит ли граница возраста на `value` — по той же разметке, что и клик.

    18.09 06:15 (CL_ml Trading): на экране «35 – 65+», а `_age_boundary_shows`
    отвечал «нет» на обе границы (подъём на шесть уровней от подписи «Age» не
    доходит до значений), поэтому шаг не снял «Use as a suggestion» и отказал
    при верном возрасте. `_mark_age_trigger` те же значения находит.
    """
    return _mark_age_trigger(page, value) or _age_boundary_shows(page, value)


def _age_boundary_shows(page: Page, value: str) -> bool:
    """В закрытой выпадашке возраста рядом с «Age» видно `value` (не в открытом списке)."""
    try:
        return bool(page.evaluate(
            r"""(value) => {
              const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
              const label = [...document.querySelectorAll('*')].find(e =>
                e.children.length === 0 && /^(Age|Edad)$/i.test(norm(e.textContent))
                && e.getBoundingClientRect().width > 0);
              if (!label) return false;
              let box = label;
              for (let i = 0; i < 6 && box.parentElement; i++) {
                box = box.parentElement;
                const hit = [...box.querySelectorAll('*')].some(e =>
                  e.children.length === 0 && norm(e.textContent) === value
                  && e.getBoundingClientRect().width > 0 && !e.closest('[role=listbox]'));
                if (hit) return true;
              }
              return false;
            }""",
            value,
        ))
    except Exception:
        return False


_MARK_AGE_TRIGGER_JS = r"""(value) => {
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
  document.querySelectorAll('[data-acl-age-trigger]').forEach(e =>
    e.removeAttribute('data-acl-age-trigger'));
  // Все видимые подписи «Age», кроме диалогов: 17.09 19:44 (CR_ait Day)
  // первой находилась подпись из закрывающегося диалога Further limit,
  // и «18» оказывалось выше неё.
  const labels = [...document.querySelectorAll('*')].filter(e =>
    e.children.length === 0 && /^(Age|Edad)$/i.test(norm(e.textContent))
    && e.getBoundingClientRect().width > 0 && !e.closest('[role=dialog]'));
  if (!labels.length) return false;
  const own = (e) => [...e.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent).join('');
  let best = null, bestGap = Infinity;
  for (const e of document.querySelectorAll('*')) {
    if (norm(own(e)) !== value) continue;
    const r = e.getBoundingClientRect();
    if (r.width <= 0 || e.closest('[role=listbox], [role=dialog]')) continue;
    for (const label of labels) {
      const gap = r.top - label.getBoundingClientRect().top;
      if (gap < 0 || gap > 400) continue;
      if (gap < bestGap) { best = e; bestGap = gap; }
    }
  }
  if (!best) return false;
  best.setAttribute('data-acl-age-trigger', '1');
  return true;
}"""


def _mark_age_trigger(root, value: str) -> bool:
    """Помечает выпадашку возраста со значением `value` — ближайшую под подписью «Age».

    Всё, что выше подписи (поле Start time с часом «18») или далеко ниже,
    не подходит.
    """
    deadline = time.monotonic() + 3.0
    while True:
        try:
            if root.evaluate(_MARK_AGE_TRIGGER_JS, value):
                return True
        except Exception:
            pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.3)


def fill_audience_age(page: Page, age_min: int, age_max: int) -> bool:
    """Set the audience age range, and only say so when the page shows it.

    Three parts since 2026-09-16, each answering a question the old single
    function did not:

    * Is the audience in Advantage+ mode? Then the age controls are
      suggestions and a click on 35 does not take (run `ceab1a4bf031`).
      Switch it off first, and record whether that held.
    * Set the two dropdowns — `_fill_audience_age_controls`, the previous
      body unchanged.
    * What do the dropdowns show now? "clicked 35" and "shows 35" are
      different facts, and only the first was ever logged. A mismatch is a
      failure with both numbers in the diagnostic.
    """
    # Switch-off is attempted and recorded, but it no longer GATES the
    # controls. The "still on" signal comes from the campaign-level
    # "Advantage+ leads campaign" switch in the right panel, and that switch
    # stays On even after "Further limit the reach of your ads" has put the
    # audience into manual mode with live age dropdowns (run 2182744d1126,
    # 16:06: screenshot shows 18/65+ dropdowns editable, panel still "On").
    # Gating on it refused a settable age in every run of the evening, where
    # the old code had set it in three runs of four. The read-back below is
    # the real check of whether the age took.
    _ensure_manual_audience(page)
    _cancel_dynamic_creative_dialog(page)
    if not _fill_audience_age_controls(page, age_min, age_max):
        return False
    wanted = [str(age_min), str(age_max) if age_max < 65 else "65+"]
    seen = _age_triggers_read(page)
    if seen != wanted:
        # 18.09 06:32: на CL_ml, CO_bnc и CR_ait `_age_triggers_read` вернул
        # пусто на всех трёх кампаниях, хотя на экране стояло «35 – 65+»: его
        # подъём от подписи «Age» не доходит до значений этой разметки.
        # Спрашиваем тем же поиском, которым по границе кликаем.
        by_label = [v for v in wanted if _age_shows_value(page, v)]
        if len(by_label) == len(wanted):
            _telemetry.record("audience_age", None, note="read_back_by_label",
                              diag={"wanted": wanted, "seen": seen})
            _uncheck_age_suggestion(page)
            return True
        _telemetry.record("audience_age", False,
                          note="read_back_mismatch" if seen else "read_back_unavailable",
                          diag={"wanted": wanted, "seen": seen})
        return False
    _uncheck_age_suggestion(page)
    return True


def _fill_audience_age_controls(page: Page, age_min: int, age_max: int) -> bool:
    """Set audience age range dropdowns / inputs (min and max)."""
    # Same reasoning as fill_audience_location: skip the fuzzy
    # scroll_to_section heading match — the precise marker search a few
    # lines below (exact "^Age$|^Edad$" match) already does its own
    # scroll_into_view_if_needed on the correct element.
    _sleep_h(0.3)
    age_min_str = str(age_min)
    age_max_str = str(age_max) if age_max < 65 else "65+"
    age_label_re = re.compile(r"^Age$|^Edad$", re.I)
    ok = False

    # Diagnostic (2026-07-25, DQ204 Test40): Locations' own mystery was
    # eventually resolved in part by checking how many DOM nodes match its
    # heading text at all (count==1 ruled out a duplicate-instance theory).
    # That exact check was never done for Age — after 7 failed attempts and
    # multiple runs where a screenshot proved the real dropdowns were on
    # screen while every query found zero matches, it's worth confirming
    # get_by_text(age_label_re).first isn't grabbing an unrelated "Age" text
    # node elsewhere in the DOM (nav, filter, breadcrumb) ahead of the real
    # Audience field in document order.
    for root in _roots(page):
        try:
            age_locator = root.get_by_text(age_label_re)
            match_count = age_locator.count()
            with open("output/debug_age_match_count.txt", "a", encoding="utf-8") as f:
                f.write(f"root={root} match_count={match_count}\n")
            # Test42 found match_count=2 for the very first time this was
            # checked — confirming a real duplicate-instance risk (exactly
            # what ruling this out for Locations required back when). Dump
            # each match's own ancestor context individually so we can see
            # which index is the real Audience field and which is a decoy
            # elsewhere in the DOM (nav/breadcrumb/summary card) that
            # `.first` may have been silently grabbing all along.
            for i in range(min(match_count, 5)):
                try:
                    el = age_locator.nth(i)
                    ctx = el.locator("xpath=ancestor::*[4]")
                    outer = ctx.evaluate("el => el.outerHTML")
                    visible = el.is_visible()
                    with open("output/debug_age_match_contexts.txt", "a", encoding="utf-8") as f:
                        f.write(f"--- match[{i}] visible={visible} ---\n{outer[:2000]}\n\n")
                except Exception as e2:
                    with open("output/debug_age_match_contexts.txt", "a", encoding="utf-8") as f:
                        f.write(f"--- match[{i}] ERROR {e2!r} ---\n\n")
        except Exception:
            pass

    # SOLVED (2026-07-25, DQ204 Test49): clicking the "Age" label kept
    # timing out — Playwright's own retry log named the exact blocker:
    # <div id="...">18</div> (a dynamic id) physically intercepts pointer
    # events at the label's location. That "18" div (and presumably a
    # sibling "65+" div) is not a decoy — it IS the real, already-live
    # age-min/age-max trigger, just visually overlapping the "Age" label
    # text. Click those two value elements directly instead of the label,
    # via raw CDP (bypasses Playwright's own actionability/interception
    # check, which is exactly what made every locator-based click here
    # fail). Search broadly (not scoped to a specific ancestor depth of the
    # "Age" marker) since this element may not even be a descendant of the
    # row we were querying — matching the earlier finding that our
    # select/combobox searches inside that row never found anything.
    for root in _roots(page):
        # Bug found live (2026-07-25, Test64): this session's test data
        # always has age_max=65, which trivially equals Meta's own default
        # ("65+") — the old single shared `ok` flag meant that trivial match
        # alone satisfied the whole function, masking a genuinely failed
        # age_min selection (screenshot showed "18" still unchanged while
        # the function had already reported success). Track both boundaries
        # independently and only report success if BOTH are confirmed.
        boundary_results = []
        for default_text, target_val in (("18", age_min_str), ("65+", age_max_str)):
            if target_val == default_text and _age_shows_value(page, default_text):
                # Граница уже стоит как надо — открывать выпадашку незачем.
                # 17.09 13:19 (CL_ml): клик по «65+» упал на прокрутке
                # (TimeoutError), граница записалась как проваленная, и шаг
                # отказал при 35–65+ на экране — а заодно не сняли галочку
                # «Use as a suggestion», которая снимается только при успехе.
                boundary_results.append(True)
                continue
            try:
                # 17.09 15:21 (CR_ait Day): первым «18» на странице оказался
                # час в поле Start time («18:20») — кликнули по часам, возраст
                # остался 18. Берём значение только из поля Age.
                if not _mark_age_trigger(root, default_text):
                    boundary_results.append(False)
                    continue
                el = root.locator("[data-acl-age-trigger]").first
                if not el.count() or not el.is_visible():
                    boundary_results.append(False)
                    continue
                # Test50 bug: no scroll_into_view before computing the box —
                # the resulting screenshot showed a totally unrelated section
                # of the page (Custom audiences), meaning either the click
                # coordinates were stale relative to actual scroll position,
                # or this matched an unrelated "18"/"65+" text node elsewhere
                # entirely. Force it into view first so the box is trustworthy.
                el.scroll_into_view_if_needed(timeout=2000)
                box = el.bounding_box(timeout=1500)
                if not box or box.get("width", 0) <= 0:
                    continue
                trigger_x, trigger_y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
                _cdp_mouse_click(page, trigger_x, trigger_y)
                _sleep_h(0.5)
                try:
                    from src.fb_ads_rpa.screenshots import capture_on_success as _capture

                    _capture(page, f"recorded_age_value_click_{default_text.replace('+', 'p')}")
                except Exception:
                    pass
                if target_val != default_text:
                    # ROOT CAUSE FOUND (2026-07-25): isolation test proved a
                    # single click on the "13" list item ALONE (no scrolling
                    # at all) reproduces the exact same "whole Audience
                    # panel switches to Suggest-an-audience/Advantage+"
                    # corruption. This isn't a stray-click accident — age 13
                    # is Meta's/COPPA's minimum targetable age and a real
                    # boundary value; clicking it very plausibly triggers
                    # Meta's own age-restriction handling (forcing a
                    # simplified/restricted audience mode as a safety
                    # measure), independent of anything our scroll logic
                    # does. The "13" anchor was only ever meant to locate
                    # the scrollable list container, not to actually select
                    # anything — reuse the ALREADY-CLICKED `el` (the current
                    # "18"/"65+" trigger, already open and not a new
                    # selection) for that instead, with no extra click on
                    # any list value.
                    #
                    # FIXED (2026-07-27, confirmed live): walking up
                    # ancestors from `el` (the closed-state trigger text)
                    # never finds the real scrollable list — the open
                    # dropdown is a separate, portal-rendered
                    # `[role=listbox]` element, not an ancestor/descendant
                    # of the trigger at all. Live diagnosis (walking up
                    # from a value INSIDE the open list, e.g. "13") found
                    # the real container this way instead: a global
                    # `[role=listbox]` search, since only one dropdown is
                    # open at a time right after clicking the trigger.
                    # FIXED (2026-07-27, confirmed live): the previous
                    # measure-bounding-box-then-separately-CDP-click pattern
                    # left a real gap between computing (x, y) and clicking
                    # it — confirmed via elementFromPoint(x, y) returning
                    # null right at the computed click point, meaning the
                    # page had already reflowed/scrolled by the time the
                    # click fired.
                    #
                    # Tried making the WHOLE scroll+find+click sequence one
                    # synchronous page.evaluate() call — regressed (both
                    # boundaries went from "found" to "not_found"). Root
                    # cause: this list virtualizes its rows (only renders a
                    # DOM node for whatever's near the current scroll
                    # position); a synchronous JS loop that changes
                    # scrollTop 60 times in a row never yields back to the
                    # browser's render/effect cycle, so the virtualizer
                    # never gets a chance to actually mount the newly
                    # scrolled-to rows — scrollTop ends up at its final
                    # value but the DOM still only reflects whatever was
                    # rendered before the loop started. Keep the scroll
                    # loop as separate Python-driven ticks with real sleeps
                    # (confirmed working: found target 30 at tick 2, 45 at
                    # tick 12) so the browser can render between each step —
                    # only the FINAL find+click is atomic, closing the
                    # actual staleness gap without breaking virtualization.
                    _age_dbg: list[str] = []
                    tagged = page.evaluate(
                        """() => {
                          const el = document.querySelector('[role=listbox]');
                          if (!el) return false;
                          el.setAttribute('data-__age-scroll-target', '1');
                          el.scrollTop = 0;
                          return true;
                        }"""
                    )
                    _age_dbg.append(f"target_val={target_val!r} tagged={tagged}")

                    def _age_target_present() -> bool:
                        return bool(
                            page.evaluate(
                                """(targetVal) => {
                                  const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                                  const listbox = document.querySelector('[data-__age-scroll-target]');
                                  if (!listbox) return false;
                                  return [...listbox.querySelectorAll('*')].some(e =>
                                    e.offsetParent !== null && norm(e.textContent) === targetVal);
                                }""",
                                target_val,
                            )
                        )

                    found_tick = None
                    if tagged:
                        if _age_target_present():
                            found_tick = -1
                        else:
                            for scroll_i in range(40):
                                try:
                                    page.evaluate(
                                        """() => {
                                          const node = document.querySelector('[data-__age-scroll-target]');
                                          if (node) node.scrollTop += 50;
                                        }"""
                                    )
                                except Exception:
                                    pass
                                _sleep_h(0.12)
                                if _age_target_present():
                                    found_tick = scroll_i
                                    break
                    _age_dbg.append(f"found_tick={found_tick}")

                    if found_tick is not None:
                        # Atomic re-find + click — no separate bounding_box
                        # measurement, no gap for the page to reflow before
                        # the click actually fires.
                        click_result = page.evaluate(
                            """(targetVal) => {
                              const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                              const listbox = document.querySelector('[data-__age-scroll-target]');
                              if (!listbox) return 'no_listbox';
                              const matches = [...listbox.querySelectorAll('*')].filter(e =>
                                e.offsetParent !== null && norm(e.textContent) === targetVal);
                              if (!matches.length) return 'no_match';
                              matches.sort((a, b) => a.children.length - b.children.length);
                              const el = matches[0];
                              el.scrollIntoView({ block: 'center', behavior: 'instant' });
                              el.click();
                              return 'clicked';
                            }""",
                            target_val,
                        )
                        _age_dbg.append(f"click_result={click_result}")
                        _sleep_h(0.4)
                    try:
                        with open("output/debug_age_scroll_click.txt", "a", encoding="utf-8") as f:
                            f.write("\n".join(_age_dbg) + "\n---\n")
                    except Exception:
                        pass
                # Test50 bug (partially fixed, then re-found in Test63):
                # checking "is target_val visible anywhere on the page" is
                # NOT sufficient proof of selection — when target_val is
                # already visible as an unselected item inside the still-open
                # dropdown list (e.g. "20" sitting right there in the
                # unscrolled 13-20 view), this reports a false success even
                # though the trigger still shows the old default_text and
                # nothing was actually selected (confirmed live: Test63's
                # "after_audience_age" screenshot still showed "18", not
                # "20", despite this check having set ok=True).
                #
                # Real fix: check what's actually rendered at the trigger's
                # own screen position (trigger_x/trigger_y, captured before
                # any click) via elementFromPoint — this is agnostic to
                # whatever markup/class the trigger uses and immune to the
                # "text visible elsewhere on the page" false positive, since
                # it only cares about that one specific pixel spot where the
                # value box lives.
                if target_val == default_text:
                    boundary_results.append(True)
                else:
                    boundary_ok = False
                    try:
                        _sleep_h(0.3)
                        # FIXED (2026-07-27, confirmed live): trigger_x/
                        # trigger_y are captured before the dropdown even
                        # opens — by the time this runs (after scrolling,
                        # selecting, and the list closing), the whole
                        # section has reflowed and elementFromPoint at that
                        # stale position no longer means anything (proven
                        # live: a run where the value screenshot clearly
                        # showed the correct 30/45 still logged this
                        # boundary as failed). Check the closed-dropdown
                        # trigger's real current text directly instead of a
                        # frozen screen coordinate: a visible leaf reading
                        # exactly target_val that is NOT inside an open
                        # [role=listbox] (a value sitting unselected inside
                        # a still-open list doesn't count, matching the
                        # original Test50/63 concern this code already
                        # guards against).
                        boundary_ok = bool(
                            page.evaluate(
                                """(targetVal) => {
                                  const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                                  return [...document.querySelectorAll('*')].some(e => {
                                    if (e.offsetParent === null || e.children.length !== 0) return false;
                                    if (norm(e.textContent) !== targetVal) return false;
                                    return !e.closest('[role=listbox]');
                                  });
                                }""",
                                target_val,
                            )
                        )
                    except Exception:
                        pass
                    boundary_results.append(boundary_ok)
            except Exception as age_click_exc:
                try:
                    with open("output/debug_age_label_click_error.txt", "a", encoding="utf-8") as f:
                        f.write(f"val={default_text!r} exc={age_click_exc!r}\n")
                except Exception:
                    pass
                boundary_results.append(False)
                continue
        # 17.09 22:12 (CR_ait FX_Y): на экране стояло ровно «35 – 65+», но
        # одна из границ отчиталась провалом, и из-за этого не сняли галочку
        # «Use as a suggestion» — шаг отказал при верном возрасте. Верим
        # итоговому виду поля, а не отчёту по каждой границе.
        read_back = False
        if not (len(boundary_results) == 2 and all(boundary_results)):
            read_back = _age_shows_value(page, age_min_str) and _age_shows_value(page, age_max_str)
            if read_back:
                try:
                    _telemetry.record("audience_age", None, note="read_back_ok",
                                      diag={"boundaries": boundary_results,
                                            "shows": [age_min_str, age_max_str]})
                except Exception:
                    pass
        if (len(boundary_results) == 2 and all(boundary_results)) or read_back:
            # "Use as a suggestion" appears right below the Age boundaries
            # once both are set to non-default values — confirmed live
            # 2026-07-27 it's checked by default ("We'll also reach people
            # beyond your age range..."). The account's real config wants
            # this off (a hard age boundary, not a soft suggestion).
            try:
                page.evaluate(
                    """() => {
                      const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                      const row = [...document.querySelectorAll('*')].find(e =>
                        norm(e.textContent).startsWith('Use as a suggestion') && e.offsetParent !== null);
                      if (!row) return;
                      let container = row;
                      for (let i = 0; i < 6 && container; i++) {
                        const cb = container.querySelector('input[type=checkbox]');
                        if (cb) {
                          if (cb.checked) cb.click();
                          return;
                        }
                        container = container.parentElement;
                      }
                    }"""
                )
                _sleep_h(0.3)
            except Exception:
                pass
            return True

    # Diagnostic (2026-07-25, DQ204 Test37): a fail screenshot showed real
    # "18 [v] 65+ [v]" controls visibly on screen, yet our select/combobox/
    # spinbutton search found zero matches for the entire ~9.6s poll window
    # — strongly suggesting these are neither <select> nor
    # [role=combobox]/[role=spinbutton] at all, most likely a [role=button]
    # custom dropdown trigger like several other fields in this file (e.g.
    # Conversion location, Performance goal). Dump the tag/role of whatever
    # element's own text is exactly "18" or "65+" near the Age marker,
    # whatever the current state happens to be, before any click attempt.
    for root in _roots(page):
        try:
            marker = root.get_by_text(age_label_re).first
            for depth in range(1, 8):
                row = marker.locator(f"xpath=ancestor::*[{depth}]")
                for val_text in ("18", "65+", "65"):
                    try:
                        el = row.get_by_text(re.compile(rf"^{re.escape(val_text)}$"), exact=True).first
                        if el.count() and el.is_visible():
                            info = el.evaluate(
                                "el => ({tag: el.tagName.toLowerCase(), role: el.getAttribute('role'), "
                                "cls: el.className, outer: el.outerHTML.slice(0,600), "
                                "parentOuter: el.parentElement ? el.parentElement.outerHTML.slice(0,800) : ''})"
                            )
                            with open("output/debug_age_value_elements.txt", "a", encoding="utf-8") as f:
                                f.write(f"depth={depth} val={val_text!r} info={info}\n\n")
                    except Exception:
                        continue
            break
        except Exception:
            continue

    # Diagnostic only (2026-07-25, DQ204 Test27/28): Age can render as a
    # collapsed read-only summary ("18 - 65+", no select/combobox nearby)
    # rather than live dropdowns, seen via the fail_audience_age screenshot
    # right after the Locations fix expanded that section (map + chips
    # pushed everything below it down). A click-to-expand attempt modeled on
    # the Locations fix was tried and reverted (Test28) — it landed on the
    # info icon and scrolled the page far past Age entirely (down to
    # Placements), making things worse, not better, unlike Locations where
    # the same click pattern reliably opened the real editor. Age's
    # collapsed/expanded mechanics are evidently different from Locations'
    # and need their own live investigation, not a copy-paste of that fix.
    # Keep only a passive HTML dump of Age's natural (un-clicked) state here
    # for the next session to start from real data instead of guessing.
    for root in _roots(page):
        try:
            heading = root.get_by_text(age_label_re).first
            heading.wait_for(state="visible", timeout=2000)
            heading.scroll_into_view_if_needed(timeout=2000)
            row = heading.locator("xpath=ancestor::*[3]")
            try:
                age_row_dump = row.evaluate("el => el.outerHTML")
                Path("output/debug_age_row_html.txt").write_text(
                    age_row_dump[:6000], encoding="utf-8"
                )
            except Exception:
                pass
            break
        except Exception:
            continue

    # If Age isn't already showing live dropdowns, try the same
    # click-to-expand pattern that fixed Locations — the earlier single
    # naive attempt (Test28) scrolled the page away instead of expanding,
    # but it lacked the poll/retry discipline that made the Locations fix
    # reliable (a single point-in-time check is not enough, per that same
    # day's evidence). Only attempt this if Age genuinely isn't interactive
    # yet — Test32 showed it sometimes already is, in which case don't risk
    # an unnecessary click.
    already_expanded = False
    for root in _roots(page):
        try:
            marker = root.get_by_text(age_label_re).first
            for depth in range(1, 10):
                row = marker.locator(f"xpath=ancestor::*[{depth}]")
                if row.locator("select, [role=combobox], [role=spinbutton]").count() >= 2:
                    already_expanded = True
                    break
            break
        except Exception:
            continue

    if not already_expanded:
        for root in _roots(page):
            try:
                heading = root.get_by_text(age_label_re).first
                heading.wait_for(state="visible", timeout=2000)
                heading.scroll_into_view_if_needed(timeout=2000)
                header_block = heading.locator("xpath=ancestor::*[2]")
                click_row = heading.locator("xpath=ancestor::*[3]")
                wide_row = heading.locator("xpath=ancestor::*[6]")
                # First click wakes the row into a :hover-like state — live
                # evidence 2026-07-25 (Test34 fail screenshot) shows this
                # reveals a pencil/edit icon at the row's right edge, but
                # doesn't open edit mode on its own (unlike Locations, where
                # the same single click opened the editor directly). A
                # second click specifically near where that icon renders is
                # needed.
                for candidate in (header_block, click_row):
                    try:
                        box = candidate.bounding_box(timeout=1500)
                        if box and box.get("width", 0) > 0 and box.get("height", 0) > 0:
                            _cdp_mouse_click(page, box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                            break
                    except Exception:
                        continue
                _sleep_h(0.3)
                try:
                    wbox = wide_row.bounding_box(timeout=1500)
                    if wbox and wbox.get("width", 0) > 0:
                        _cdp_mouse_click(page, wbox["x"] + wbox["width"] - 20, wbox["y"] + 20)
                except Exception:
                    pass
                break
            except Exception:
                continue
        # Test35 fail screenshot showed Age genuinely reaching the live
        # "18 [v] 65+ [v]" dropdown state (row highlighted blue) — but the
        # select-count check below still found nothing at the time it ran,
        # same class of "screenshot proves it but our check ran too early"
        # timing gap the Locations fix hit before its own poll was widened.
        # Test36's 24x200ms (4.8s) still missed it — log every single tick
        # with a timestamp (append, not overwrite) instead of guessing at
        # a longer duration blindly, so the exact moment it appears (if it
        # ever does within this window) is visible after the run.
        poll_start = time.time()
        value_dumped = False
        for tick in range(24):
            _sleep_h(0.2)
            found = False
            match_count = 0
            value_hits = 0
            for root in _roots(page):
                try:
                    marker = root.get_by_text(age_label_re).first
                    for depth in range(1, 10):
                        row = marker.locator(f"xpath=ancestor::*[{depth}]")
                        c = row.locator("select, [role=combobox], [role=spinbutton]").count()
                        if c > match_count:
                            match_count = c
                        if c >= 2:
                            found = True
                        # Broader check (2026-07-25): the select/combobox/
                        # spinbutton search above found nothing for a full
                        # 9.6s window in Test37 despite a screenshot proving
                        # "18"/"65+" controls were visible — they're likely
                        # some other element type entirely (e.g. a plain
                        # [role=button], matching several other custom
                        # dropdown triggers elsewhere in this file). Look for
                        # the exact value text directly and dump its real
                        # tag/role/outerHTML the first time it's seen.
                        if not value_dumped:
                            try:
                                for val_text in ("18", "65+", "65"):
                                    el = row.get_by_text(re.compile(rf"^{re.escape(val_text)}$"), exact=True).first
                                    if el.count() and el.is_visible():
                                        value_hits += 1
                                        info = el.evaluate(
                                            "el => ({tag: el.tagName.toLowerCase(), role: el.getAttribute('role'), "
                                            "cls: el.className, outer: el.outerHTML.slice(0,600), "
                                            "parentOuter: el.parentElement ? el.parentElement.outerHTML.slice(0,800) : ''})"
                                        )
                                        with open("output/debug_age_value_elements.txt", "a", encoding="utf-8") as f:
                                            f.write(
                                                f"tick={tick} depth={depth} val={val_text!r} info={info}\n\n"
                                            )
                                        value_dumped = True
                            except Exception:
                                pass
                        if found:
                            break
                    break
                except Exception:
                    continue
            try:
                with open("output/debug_age_poll_ticks.txt", "a", encoding="utf-8") as f:
                    f.write(
                        f"tick={tick} t={time.time() - poll_start:.2f}s "
                        f"max_match_count={match_count} value_hits={value_hits} found={found}\n"
                    )
            except Exception:
                pass
            if found:
                try:
                    from src.fb_ads_rpa.screenshots import capture_on_success as _capture

                    _capture(page, f"recorded_age_poll_found_tick{tick}")
                except Exception:
                    pass
                break

    for root in _roots(page):
        try:
            marker = root.get_by_text(age_label_re).first
            marker.scroll_into_view_if_needed(timeout=5000)
            for depth in range(1, 10):
                row = marker.locator(f"xpath=ancestor::*[{depth}]")
                selects = row.locator("select, [role=combobox], [role=spinbutton]")
                if selects.count() >= 2:
                    try:
                        dump_lines = []
                        for k in range(min(selects.count(), 4)):
                            e = selects.nth(k)
                            tag_k = e.evaluate("el => el.tagName.toLowerCase()")
                            outer_k = e.evaluate("el => el.outerHTML")
                            dump_lines.append(f"[{k}] tag={tag_k}\n{outer_k[:2500]}")
                        Path("output/debug_age_selects.txt").write_text(
                            "\n\n---\n\n".join(dump_lines), encoding="utf-8"
                        )
                    except Exception:
                        pass
                    for idx, val in [(0, age_min_str), (1, age_max_str)]:
                        try:
                            el = selects.nth(idx)
                            if not el.is_visible():
                                continue
                            tag = el.evaluate("el => el.tagName.toLowerCase()")
                            if tag == "select":
                                el.select_option(
                                    value=str(age_min if idx == 0 else age_max)
                                )
                            else:
                                el.click(timeout=3000)
                                _sleep_h(0.3)
                                try:
                                    from src.fb_ads_rpa.screenshots import capture_on_success as _capture

                                    _capture(page, f"recorded_age_dropdown_open_idx{idx}")
                                except Exception:
                                    pass
                                target_val = age_min if idx == 0 else age_max
                                opt = root.get_by_role(
                                    "option",
                                    name=re.compile(rf"^{re.escape(val)}$", re.I),
                                ).first
                                if opt.count() and opt.is_visible():
                                    opt.click(timeout=3000)
                                else:
                                    el.fill(str(target_val), timeout=3000)
                                    page.keyboard.press("Enter")
                            _sleep_h(0.4)
                            ok = True
                        except Exception as age_exc:
                            try:
                                with open("output/debug_age_fill_error.txt", "a", encoding="utf-8") as f:
                                    f.write(f"idx={idx} exc={age_exc!r}\n")
                            except Exception:
                                pass
                            continue
                    if ok:
                        return True
        except Exception:
            pass

    # JS fallback for <select> elements
    script = """
    ([minVal, maxVal]) => {
      const visible = (el) => !!(el && el.offsetParent !== null);
      let ageMarker = null;
      for (const el of document.querySelectorAll('label, span, div, h3')) {
        if (!visible(el)) continue;
        const t = (el.textContent || '').replace(/\\s+/g, ' ').trim().toLowerCase();
        if (t === 'age' || t === 'edad') { ageMarker = el; break; }
      }
      if (!ageMarker) return false;
      let node = ageMarker.parentElement;
      for (let d = 0; d < 8 && node; d++) {
        const selects = node.querySelectorAll('select');
        if (selects.length >= 2) {
          const setS = (sel, val) => {
            for (const o of sel.options) {
              if (o.value === val || o.text.trim() === val) {
                sel.value = o.value;
                sel.dispatchEvent(new Event('change', { bubbles: true }));
                return true;
              }
            }
            return false;
          };
          return !!(setS(selects[0], minVal) | setS(selects[1], maxVal));
        }
        node = node.parentElement;
      }
      return false;
    }
    """
    for root in _roots(page):
        try:
            if root.evaluate(script, [age_min_str, age_max_str]):
                _sleep_h(0.5)
                return True
        except Exception:
            pass
    return False


_LANG_EDIT_JS = r"""
  () => {
    // 20.09 00:16 SA_arc GMBL: «All languages» стояла под нижней панелью
    // редактора, клик по её координатам попал в Close — редактор закрылся.
    // Сначала прокрутить к цели, потом убедиться, что в точке — она сама.
    const at = (t) => {
      t.scrollIntoView({block: 'center'});
      const r = t.getBoundingClientRect();
      const x = r.x + r.width / 2, y = r.y + r.height / 2;
      const hit = document.elementFromPoint(x, y);
      const nm = (s) => (s || '').replace(/[\s\u200b]+/g, ' ').trim();
      if (!hit || !(t.contains(hit) || (hit.contains(t) && nm(hit.textContent).length < 200))) return null;
      return { x, y };
    };
    const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
    const label = [...document.querySelectorAll('*')].find(e =>
      e.children.length === 0 && /^(Languages|Idiomas)$/i.test(norm(e.textContent))
      && e.getBoundingClientRect().width > 0);
    if (!label) return null;
    let row = label.parentElement;
    for (let i = 0; i < 4 && row; i++, row = row.parentElement) {
      const btns = [...row.querySelectorAll('[role=button], button, [aria-label]')].filter(b => {
        const r = b.getBoundingClientRect();
        if (r.width <= 0 || r.height <= 0 || r.width > 80) return false;
        const name = (norm(b.getAttribute('aria-label')) + ' ' + norm(b.textContent)).toLowerCase();
        return /edit|изменить|editar|pencil/.test(name) || (!norm(b.textContent) && r.width <= 48);
      });
      if (btns.length) {
        return at(btns[btns.length - 1]);
      }
    }
    return null;
  }
"""
"""Карандаш строки «Languages» в свёрнутом виде блока аудитории."""


def _click_languages_edit(page: Page) -> bool:
    """Раскрыть строку языков её собственной кнопкой правки.

    17.09 23:04 (CR_ait Day): блок аудитории показан сводкой — «Languages: All
    languages», поля поиска нет вовсе, а клик по центру строки его не открывал.
    Справа у строки живёт карандаш — жать надо его.
    """
    _hover_row(page, "Languages|Idiomas")
    try:
        pt = page.evaluate(_LANG_EDIT_JS)
    except Exception:
        return False
    if not pt:
        return _click_row_expander(page, "Languages|Idiomas") or _click_summary_row(page, "Languages|Idiomas", "^(All languages|Todos los idiomas)")
    _cdp_mouse_click(page, pt["x"], pt["y"])
    _sleep_h(0.6)
    return True


def fill_audience_languages(page: Page, language_names: list[str]) -> bool:
    """Type language(s) into Audience → Languages and pick suggestions.

    New (2026-07-25), modeled directly on fill_audience_location's proven
    pattern from the same session rather than starting from scratch blind:
    Languages is a sibling field in the same Audience panel with a very
    similar "collapsed summary vs. open search" shape (Locations' fix
    already established that this general family of field can render as a
    span-based fake input needing keyboard.type, not .fill()). Not yet
    live-verified — first real test of this exact function.
    """
    if not language_names:
        return True
    _sleep_h(0.3)
    lang_placeholders = [
        re.compile(r"Add languages?", re.I),
        re.compile(r"Search languages?", re.I),
        re.compile(r"Agregar idiomas?", re.I),
        re.compile(r"Buscar idiomas?", re.I),
    ]
    lang_label_re = re.compile(r"^Languages$|^Idiomas$", re.I)

    def _find_lang_input():
        for root in _roots(page):
            for ph in lang_placeholders:
                try:
                    inp = root.get_by_placeholder(ph).first
                    if inp.count() and inp.is_visible():
                        return root, inp
                except Exception:
                    continue
        return None, None

    root, inp = _find_lang_input()
    if inp is None and _click_languages_edit(page):
        for _ in range(10):
            _sleep_h(0.2)
            root, inp = _find_lang_input()
            if inp is not None:
                break
    if inp is None:
        for root in _roots(page):
            try:
                heading = root.get_by_text(lang_label_re).first
                heading.wait_for(state="visible", timeout=3000)
                heading.scroll_into_view_if_needed(timeout=3000)
                header_block = heading.locator("xpath=ancestor::*[2]")
                row = heading.locator("xpath=ancestor::*[3]")
                for candidate in (header_block, row):
                    try:
                        box = candidate.bounding_box(timeout=1500)
                        if box and box.get("width", 0) > 0 and box.get("height", 0) > 0:
                            _cdp_mouse_click(page, box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                            break
                    except Exception:
                        continue
                break
            except Exception:
                continue
        for _ in range(10):
            _sleep_h(0.15)
            root, inp = _find_lang_input()
            if inp is not None:
                break

    if inp is None:
        return False

    # 25.09 (SA, 20/20 прогонов): Meta сама ставит язык страны — «Arabic» без
    # «(All)», — а мы всё равно печатали его в поиск. Уже выбранный язык в
    # подсказках не показывается, кликать нечего, шаг отказывал при верном
    # поле. Поэтому сначала читаем выбранные чипы: всё уже стоит — успех без
    # единого нажатия.
    wanted = [n.strip() for n in language_names if n.strip()]
    present = _languages_selected(page, wanted)
    if wanted and all(any(_language_chip_matches(p, w) for p in present) for w in wanted):
        _telemetry.record("languages", None, note="already_selected", diag={"selected": present})
        return True

    ok_any = False
    for name in language_names:
        if any(_language_chip_matches(p, name) for p in present):
            continue
        try:
            inp.click(timeout=3000)
            page.keyboard.type(name, delay=40)
            _sleep_h(1.2)
            # Сначала «<язык> (All)» (живые подсказки 25.07: у Spanish есть
            # «Spanish (Spain)», «Spanish (Mexico)» и широкий «Spanish (All)»),
            # затем подсказка, чей текст ровно «<язык>» (у Arabic варианта
            # «(All)» нет). Слепого клика по первой подсказке больше нет: он
            # мог взять региональный вариант, а это другой, узкий таргетинг.
            picked = _click_language_option(page, name)
            if picked:
                ok_any = True
            else:
                # Набранный текст не оставляем в поиске: открытая выпадашка
                # перекрывает соседние поля. Escape нельзя — закрывает редактор.
                try:
                    inp.fill("", timeout=2000)
                except Exception:
                    pass
            root, inp = _find_lang_input()
            if inp is None:
                break
        except Exception:
            continue
    if not ok_any:
        # 17.09 06:56 (S15): язык мог появиться за время ввода (Meta подставляет
        # по стране). Выбранный язык — успех.
        # Без Escape: в редакторе Ads Manager он закрывает сам редактор (17.09
        # 13:19, CR_ait Day — после шага языков на экране список ad set, и все
        # следующие шаги упали). Открытая выпадашка чтению выбранных не мешает.
        present = _languages_selected(page, wanted)
        if wanted and all(any(_language_chip_matches(p, w) for p in present) for w in wanted):
            _telemetry.record("languages", None, note="already_selected", diag={"selected": present})
            return True
    return ok_any


def _language_key(text: str) -> str:
    """Имя языка без регистра, лишних пробелов и хвоста «(All)»/«(…)»."""
    t = re.sub(r"\s+", " ", (text or "")).strip().lower()
    return re.sub(r"\s*\([^)]*\)$", "", t).strip()


def _language_chip_matches(chip: str, wanted: str) -> bool:
    """Чип «Arabic», «arabic», «Spanish (All)», «Spanish (Mexico)» — это
    язык «Arabic»/«Spanish». Сравнение по имени целиком: «Arab» не «Arabic»."""
    w = _language_key(wanted)
    return bool(w) and _language_key(chip) == w


def _click_language_option(page: Page, name: str) -> bool:
    """Кликнуть подсказку «<name> (All)», иначе подсказку ровно «<name>».

    Никаких других вариантов: региональный «Arabic (Egypt)» — чужой таргетинг,
    лучше честный отказ шага, чем тихо суженная аудитория.
    """
    base = re.sub(r"\s+", " ", name).strip()
    patterns = (
        re.compile(rf"^\s*{re.escape(base)}\s*\((all|todos)\)\s*$", re.I),
        re.compile(rf"^\s*{re.escape(base)}\s*$", re.I),
    )
    for pat in patterns:
        for sel in ("[role=option]:visible", "li[role=option]"):
            try:
                opts = page.locator(sel)
                for i in range(min(opts.count(), 30)):
                    opt = opts.nth(i)
                    try:
                        text = opt.inner_text(timeout=1000)
                    except Exception:
                        continue
                    # Подсказка может нести вторую строку («Language») —
                    # сравниваем первую непустую.
                    first = next((ln for ln in text.splitlines() if ln.strip()), "")
                    if pat.match(first) and opt.is_visible():
                        opt.click(timeout=3000)
                        _sleep_h(0.5)
                        return True
            except Exception:
                continue
    return False


def _languages_selected(page: Page, wanted: list[str] | None = None) -> list[str]:
    """Подписи уже выбранных языков в блоке Languages (например «Spanish (All)»).

    Поднимаемся от поля поиска языков, а не от подписи «Languages»: 17.09
    (CL_ml, CR_ait) «Spanish (All)» стоял, а подъём на 6 уровней от подписи до
    него не доставал — шаг отказывал при верном поле.

    25.09 (SA): Meta ставит «Arabic» по стране — без «(All)» и без скобок
    вообще, и форма «X (…)» такой чип не видела. Поэтому считается и чип,
    текст которого — один из ``wanted`` (без регистра и лишних пробелов, с
    необязательным хвостом «(All)»/«(…)»).
    """
    keys = [_language_key(w) for w in (wanted or []) if _language_key(w)]
    try:
        return page.evaluate(
            r"""(keys) => {
              const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
              const key = (t) => norm(t).toLowerCase().replace(/\s*\([^)]*\)$/, '').trim();
              const looks = (t) => /\((All|Todos)\)$/.test(t) || /^[A-Z][a-z]+ \([^)]+\)$/.test(t)
                || (t.length < 60 && keys.includes(key(t)));
              const inputs = [...document.querySelectorAll('input[placeholder]')]
                .filter(i => /language|idioma/i.test(i.getAttribute('placeholder') || ''));
              for (const inp of inputs) {
                let box = inp;
                for (let k = 0; k < 10 && box.parentElement; k++) {
                  box = box.parentElement;
                  const found = [...box.querySelectorAll('*')]
                    .filter(e => e.children.length === 0 && e.getBoundingClientRect().width > 0)
                    // Своё поле ввода и выпадашка подсказок — не чипы.
                    .filter(e => !e.closest('[role=option],[role=listbox]'))
                    .map(e => norm(e.textContent))
                    .filter(looks);
                  if (found.length) return found;
                }
              }
              return [];
            }""",
            keys,
        ) or []
    except Exception:
        return []


def select_manual_placements(page: Page) -> bool:
    """Click Manual placements option in the Placements section."""
    scroll_to_section(
        page, "Placements", "Ubicaciones de anuncio", "Placement",
        "Account controls", "Controles de cuenta",
    )
    _sleep_h(0.4)
    for root in _roots(page):
        for text_re in (
            re.compile(r"Manual placements?", re.I),
            re.compile(r"Ubicaciones manuales?", re.I),
        ):
            for getter in (
                lambda r, t: r.get_by_role("radio", name=t),
                lambda r, t: r.get_by_role("button", name=t),
                lambda r, t: r.get_by_text(t),
            ):
                try:
                    loc = getter(root, text_re).first
                    loc.wait_for(state="visible", timeout=4000)
                    loc.scroll_into_view_if_needed(timeout=4000)
                    loc.click(timeout=5000)
                    _sleep_h(0.8)
                    return True
                except Exception:
                    continue
    return False


def select_placement_device(page: Page, os_val: str) -> bool:
    """Select device type in Placements / Account controls.

    os_val: 'All' | 'Mobile' | 'iOS' | 'Android' | 'Desktop'
    'All' is a no-op (Advantage+ placements cover all devices).
    """
    os_norm = (os_val or "All").strip().title()
    if os_norm == "All":
        return True

    scroll_to_section(
        page,
        "Device type", "Tipo de dispositivo",
        "Devices", "Dispositivos",
        "Account controls", "Controles de cuenta",
    )
    _sleep_h(0.4)

    device_targets: dict[str, list] = {
        "Mobile":  [
            re.compile(r"Mobile devices only", re.I),
            re.compile(r"^Mobile$", re.I),
        ],
        "Ios":     [re.compile(r"iOS", re.I)],
        "Android": [re.compile(r"Android", re.I)],
        "Desktop": [
            re.compile(r"^Desktop$", re.I),
            re.compile(r"^Ordenadores$|^Computadoras$", re.I),
        ],
    }
    patterns = device_targets.get(os_norm, [])
    for root in _roots(page):
        for pattern in patterns:
            for getter in (
                lambda r, p: r.get_by_role("checkbox", name=p),
                lambda r, p: r.get_by_role("radio", name=p),
                lambda r, p: r.get_by_text(p),
            ):
                try:
                    loc = getter(root, pattern).first
                    loc.wait_for(state="visible", timeout=4000)
                    try:
                        if loc.is_checked():
                            return True
                    except Exception:
                        pass
                    loc.scroll_into_view_if_needed(timeout=4000)
                    loc.click(timeout=5000)
                    _sleep_h(0.6)
                    return True
                except Exception:
                    continue
    return False


# ── Teach-mode: capture what the user just clicked ────────────────────────────

def _capture_website_locator_to_recipe(page: Page) -> dict:
    """After user manually clicked Website, snapshot the selected element
    and write its locator into config/rpa_recipes/conversion_location_website.json.

    Returns the captured info dict.
    """
    recipe_path = (
        Path(__file__).resolve().parents[2]
        / "config"
        / "rpa_recipes"
        / "conversion_location_website.json"
    )

    # Try to find the Website element that is now selected / has aria-checked=true
    captured: dict = {}
    find_script = """
    () => {
      const texts = ['website', 'sitio web'];
      // Look for elements whose text matches 'Website' (exact) and are in a list/menu
      for (const el of document.querySelectorAll(
        '[role="radio"], [role="option"], [role="menuitem"], div, span'
      )) {
        if (!el.offsetParent) continue;
        const t = (el.textContent || '').trim().toLowerCase();
        if (!texts.includes(t)) continue;
        // Prefer checked/selected
        const checked = el.getAttribute('aria-checked') === 'true'
          || el.getAttribute('aria-selected') === 'true';
        const rect = el.getBoundingClientRect();
        return {
          own_text: (el.textContent || '').trim(),
          tag: el.tagName,
          role: el.getAttribute('role') || '',
          aria_checked: el.getAttribute('aria-checked') || '',
          x: Math.round(rect.x + rect.width / 2),
          y: Math.round(rect.y + rect.height / 2),
          w: Math.round(rect.width),
          h: Math.round(rect.height),
          preferred: checked,
        };
      }
      return null;
    }
    """
    for root in _roots(page):
        try:
            info = root.evaluate(find_script)
            if info:
                captured = info
                break
        except Exception:
            pass

    if not captured:
        return {}

    # Load existing recipe, update pick_website step
    try:
        existing: dict = json.loads(recipe_path.read_text(encoding="utf-8"))
    except Exception:
        existing = {"name": "conversion_location_website", "steps": [], "verify": {}}

    steps: list = existing.get("steps") or []
    for step in steps:
        if step.get("id") == "pick_website":
            step["recorded"] = {
                "x": captured.get("x"),
                "y": captured.get("y"),
                "w": captured.get("w"),
                "h": captured.get("h"),
                "tag": captured.get("tag", ""),
                "role": captured.get("role", ""),
                "teach_captured": True,
            }
            step["match"] = {
                "own_text": captured.get("own_text", "Website"),
                "exact": True,
                "exclude": ["instant", "calls", "messenger"],
            }
            break
    else:
        # No pick_website step found — append one
        steps.append({
            "id": "pick_website",
            "action": "click",
            "match": {
                "own_text": captured.get("own_text", "Website"),
                "exact": True,
                "exclude": ["instant", "calls", "messenger"],
            },
            "recorded": {
                "x": captured.get("x"),
                "y": captured.get("y"),
                "w": captured.get("w"),
                "h": captured.get("h"),
                "tag": captured.get("tag", ""),
                "role": captured.get("role", ""),
                "teach_captured": True,
            },
        })
        existing["steps"] = steps

    existing["teach_updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    try:
        recipe_path.write_text(
            json.dumps(existing, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass

    return captured
