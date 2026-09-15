"""DOM helpers for Meta Ads Manager — label-based fills that survive UI refactors."""
from __future__ import annotations

import json
import os
import re
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
_PW_FILE_LIMIT_MB = float(os.environ.get("FB_RPA_PW_FILE_LIMIT_MB") or 3)

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


def scroll_to_section(page: Page, *labels: str) -> bool:
    """Scroll main panel so a section heading/label is visible."""
    for label in labels:
        if not label.strip():
            continue
        pattern = re.compile(re.escape(label), re.I)
        for root in _roots(page):
            for getter in (
                lambda: root.get_by_role("heading", name=pattern).first,
                lambda: root.get_by_text(pattern).first,
                lambda: root.get_by_label(pattern).first,
            ):
                try:
                    loc = getter()
                    loc.wait_for(state="visible", timeout=5000)
                    loc.scroll_into_view_if_needed(timeout=5000)
                    _sleep_h(0.3)
                    return True
                except Exception:
                    pass
    return False


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


def _cdp_mouse_click(page: Page, x: float, y: float) -> None:
    """Trusted click via Chrome DevTools Protocol Input domain.

    Playwright's connect_over_cdp path can drop/reorder mouse events
    (microsoft/playwright#41286): ClickAsync succeeds but the page never
    gets a click. Raw CDP mouseMoved → mousePressed → mouseReleased is the
    community workaround used by Multilogin/CDP harnesses.
    """
    try:
        session = page.context.new_cdp_session(page)
        try:
            # Approach along a curved path instead of teleporting to the
            # target. Audit 2026-07-31: our pointer produced 2-3 points
            # from anywhere to anywhere, with the press ~8ms after the last
            # move — a shape no hand makes. Walk a few interpolated points
            # with a slight arc and human-ish gaps, then press.
            for px, py in _mouse_path(page, x, y):
                session.send(
                    "Input.dispatchMouseEvent",
                    {"type": "mouseMoved", "x": px, "y": py, "button": "none"},
                )
                time.sleep(_human_jitter(0.02, 0.5))
            _sleep_h(0.08)
            for typ, extra in (
                ("mousePressed", {"button": "left", "buttons": 1, "clickCount": 1}),
                ("mouseReleased", {"button": "left", "buttons": 0, "clickCount": 1}),
            ):
                params = {"type": typ, "x": float(x), "y": float(y), **extra}
                session.send("Input.dispatchMouseEvent", params)
                time.sleep(_human_jitter(0.06, 0.4))
        finally:
            try:
                session.detach()
            except Exception:
                pass
        return
    except Exception:
        pass
    # Fallback: Playwright mouse API
    page.mouse.move(x, y)
    _sleep_h(0.06)
    page.mouse.down()
    _sleep_h(0.07)
    page.mouse.up()


def _mouse_path(page: Page, x: float, y: float) -> list[tuple[float, float]]:
    """Interpolated points from the current pointer to (x, y), with an arc.

    Starts wherever the tracker last saw the pointer (installed by
    pacing._install_mouse_tracker); falls back to a point offset from the
    target so the first move is never a teleport from (0,0).
    """
    import math
    import random

    try:
        last = page.evaluate("() => window._rpa_last_mouse || null")
    except Exception:
        last = None
    if last and last.get("x", -1) >= 0:
        sx, sy = float(last["x"]), float(last["y"])
    else:
        sx, sy = float(x) - random.uniform(120, 260), float(y) - random.uniform(60, 180)

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
    """
    if is_website_conversion_selected(page):
        return True

    # Dismiss 'Publish draft items?' dialog that may be blocking the dropdown
    dismiss_publish_draft_dialog(page)

    if not _is_website_option_visible(page):
        _expand_conversion_location_single(page)
        _wait_for_single_expanded(page, timeout_s=5.0)

    dismiss_publish_draft_dialog(page)
    _scroll_website_option_into_view(page)

    if is_website_conversion_selected(page):
        return True

    # 1) Keyboard — proven stable when CDP mouse drops events
    if _click_website_via_keyboard_focus(page):
        return True

    # 2) Raw CDP Input.dispatchMouseEvent
    dismiss_publish_draft_dialog(page)
    if _click_website_via_cdp(page):
        return True

    # 3) Human-recorded recipe
    if _pick_website_via_recorded_recipe(page):
        return True

    # 4) Playwright mouse / force / JS / arrow-nav
    if _click_website_via_mouse(page):
        return True
    if _click_website_via_force_locator(page):
        return True
    if _click_website_via_js_row(page):
        return True
    if _click_website_via_keyboard(page):
        return True
    if _pick_website_from_aria_controls(page):
        _sleep_h(0.5)
        return is_website_conversion_selected(page)

    return is_website_conversion_selected(page)


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
            if not overlay.count() or not overlay.is_visible(timeout=200):
                return
            overlay.wait_for(state="hidden", timeout=8000)
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

    def _shot(label: str, diag: dict | None = None) -> None:
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
        )
        _shot_clock["t"] = now
        try:
            from src.fb_ads_rpa.screenshots import capture as _capture

            _capture(page, f"recorded_{label}")
        except Exception:
            pass

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

    for _ in range(3):
        _dismiss_floating_banners(page)
        _sleep_h(0.4)
    if not _click(page.get_by_role("button", name="Create", exact=True)):
        return "create"
    _shot("after_create")

    # New Meta UI sometimes shows a Guided/Manual choice before the objective
    # picker enables Campaign name — click Manual if present, best-effort
    # (the recording's session apparently started already past this choice).
    try:
        page.get_by_text("Manual campaign setup", exact=False).first.click(timeout=3000)
        _shot("after_manual")
    except Exception:
        pass

    try:
        page.get_by_role("heading", name="Leads").first.click(timeout=5000)
    except Exception:
        pass
    if not _click(page.get_by_role("radio", name="Leads")):
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
    modal_btn = _first_visible(
        page,
        (
            page.get_by_role("button", name="Don't show again"),
            page.get_by_role("button", name="OK", exact=True),
        ),
        budget_s=4.0,
    )
    if modal_btn is not None:
        _click(modal_btn, attempts=3)
    _shot("after_onboarding_modal")

    if not _fill(page.get_by_placeholder("Enter your campaign name here"), campaign_name):
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

    if not _fill(page.get_by_placeholder("Enter your ad set name here..."), adset_name):
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
        for _ in range(5):
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
            try:
                toast = page.get_by_text(
                    re.compile(r"You've switched audience setup", re.I)
                ).first
                toast.wait_for(state="visible", timeout=3000)
                toast.wait_for(state="hidden", timeout=8000)
            except Exception:
                pass
            _sleep_h(0.5)
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
                any_added = paste_targeting_interests(page, targeting_label)
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

    # Placements: switch off Advantage+ automatic placements and set the
    # account's real manual configuration (confirmed live 2026-07-26 via
    # user screenshot) — real checkboxes, so this should be more reliable
    # than the Audience fields above, but still soft-fail per the same
    # "don't stop the test" contract.
    try:
        placements_report: dict = {}
        if not fill_placements(page, placements_report):
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
            if not select_devices_ios_only(page):
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
                    _shot("fail_ad_creative_upload", dict(_LAST_UPLOAD_DIAG))
                    ad_level_fails.append("ad_creative_upload")
                else:
                    creative_uploaded = True
                    _shot("after_ad_creative_upload")
            except Exception as exc:
                _shot("fail_ad_creative_upload")
                ad_level_fails.append(f"ad_creative_upload:{str(exc)[:60]}")

        if reached_ad_level and creative_uploaded and (ad_title or ad_body):
            try:
                copy_report: dict = {}
                if not fill_ad_copy(page, ad_title, ad_body, copy_report):
                    _shot("fail_ad_copy", copy_report)
                    ad_level_fails.append("ad_copy")
                else:
                    _shot("after_ad_copy")
            except Exception as exc:
                _shot("fail_ad_copy")
                ad_level_fails.append(f"ad_copy:{str(exc)[:60]}")

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

        # Additional ads (2nd, 3rd, ...) in the same ad set — each one is a
        # fresh "Create ad" (create_additional_ad()) followed by the exact
        # same per-ad fill sequence used for the first ad above. Confirmed
        # live 2026-07-28 that upload_ad_creative/fill_ad_copy/
        # set_ad_destination_website work unmodified here.
        if reached_ad_level and additional_ads:
            for i, extra in enumerate(additional_ads, start=2):
                tag = f"ad{i}"
                try:
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
                except Exception as exc:
                    _shot(f"fail_{tag}_create")
                    ad_level_fails.append(f"{tag}_create:{str(exc)[:60]}")
                    continue

                extra_name = extra.get("ad_name") or f"{campaign_name} Ad {i}"
                if not _fill(page.get_by_placeholder("Enter your ad name here..."), extra_name):
                    _shot(f"fail_{tag}_name")
                    ad_level_fails.append(f"{tag}_name")
                else:
                    _shot(f"after_{tag}_name")

                extra_creative_path = extra.get("creative_path") or ""
                if extra_creative_path:
                    try:
                        if not upload_ad_creative_with_retry(
                            page, extra.get("creative_type") or "", extra_creative_path, edit_url=page.url
                        ):
                            _shot(f"fail_{tag}_creative_upload")
                            ad_level_fails.append(f"{tag}_creative_upload")
                        else:
                            _shot(f"after_{tag}_creative_upload")
                    except Exception as exc:
                        _shot(f"fail_{tag}_creative_upload")
                        ad_level_fails.append(f"{tag}_creative_upload:{str(exc)[:60]}")

                extra_title = extra.get("ad_title") or ""
                extra_body = extra.get("ad_body") or ""
                if extra_title or extra_body:
                    try:
                        copy_report = {}
                        if not fill_ad_copy(page, extra_title, extra_body, copy_report):
                            _shot(f"fail_{tag}_copy", copy_report)
                            ad_level_fails.append(f"{tag}_copy")
                        else:
                            _shot(f"after_{tag}_copy")
                    except Exception as exc:
                        _shot(f"fail_{tag}_copy")
                        ad_level_fails.append(f"{tag}_copy:{str(exc)[:60]}")

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

        # Final destination sweep over every ad of this campaign.
        # Confirmed live 2026-07-29 (Test143): an ad's Browser add-ons
        # radio can revert to "Instant form" AFTER its own in-flow check
        # already passed — the last ad of campaign B read back clean at
        # fill time, yet the post-run live verification found the
        # lead_gen_form_id error banner on it. Re-visit each ad once the
        # whole campaign is filled and re-fix anything that reverted.
        if reached_ad_level and destination_url:
            sweep_names = [ad_name or f"{campaign_name} Ad"]
            for i, extra in enumerate(additional_ads or [], start=2):
                sweep_names.append(extra.get("ad_name") or f"{campaign_name} Ad {i}")
            try:
                sweep_failed = verify_and_fix_ad_destinations(page, sweep_names, destination_url)
                for bad in sweep_failed:
                    ad_level_fails.append(f"destination_sweep:{bad}")
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

PLACEMENTS_SUBITEM_JS = """
  ([label, wantChecked]) => {
    const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
    const root = document.querySelector('[data-rpa-placements-marker]');
    if (!root) return { ok: false, why: 'нет метки блока' };
    const rows = [...root.querySelectorAll('*')].filter(e =>
      e.children.length === 0 && norm(e.textContent) === label && e.offsetParent !== null);
    let frozen = null;
    for (const row of rows) {
      let container = row.parentElement;
      for (let i = 0; i < 4 && container; i++) {
        const cb = container.querySelector('input[type=checkbox]');
        if (cb) {
          if (!cb.disabled) {
            if (cb.checked !== wantChecked) cb.click();
            return { ok: cb.checked === wantChecked, checked: cb.checked };
          }
          if (frozen === null) frozen = cb.checked;
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
          const heading = [...document.querySelectorAll('*')].find(e =>
            e.children.length === 0 && norm(e.textContent) === 'Placements' && e.offsetParent !== null);
          if (!heading) return { tagged: false };
          let node = heading;
          for (let i = 0; i < 10 && node; i++) {
            const t = norm(node.textContent);
            if (t.includes('Account controls')) {
              node.setAttribute('data-rpa-placements-marker', '1');
              const showMoreCount = [...node.querySelectorAll('*')].filter(e =>
                e.offsetParent !== null && norm(e.textContent).toLowerCase().includes('show more settings')).length;
              return { tagged: true, depth: i, textLen: t.length, showMoreCount, rect: node.getBoundingClientRect() };
            }
            node = node.parentElement;
          }
          return { tagged: false };
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
                  const before = hit.cb.checked;
                  if (before !== want) hit.cb.click();
                  return { found: true, checkbox: true, before: before, checked: hit.cb.checked };
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
                  return {
                    found: true,
                    checked: cb.checked,
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
                        checked: h.cb.checked,
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
        _dump_dbg()
        return False

    ok = True

    # ── категории ────────────────────────────────────────────────────────
    report["categories"] = {}
    for name, want in _PLACEMENT_CATEGORIES.items():
        res = _set_category(name, want)
        report["categories"][name] = res
        if not res.get("checkbox") or res.get("checked") is not want:
            ok = False
        _sleep_h(0.3)

    # ── подпункты ────────────────────────────────────────────────────────
    report["expanded"] = {}
    report["subitems_seen"] = []

    def _set(label: str, want_checked: bool) -> bool:
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
        try:
            res = page.evaluate(
                PLACEMENTS_SUBITEM_JS, [label, want_checked]
            ) or {"ok": False, "why": "пустой ответ"}
        except Exception as exc:
            res = {"ok": False, "why": str(exc)[:80]}
        if not res.get("ok"):
            _dbg.append(f"подпункт[{label}]={res}")
            report.setdefault("subitems_failed", {})[label] = res
        return bool(res.get("ok"))

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
            for label in list(remaining_on):
                if label in here and _set(label, True):
                    remaining_on.remove(label)
            for label in list(remaining_off):
                if label in here and _set(label, False):
                    remaining_off.remove(label)
            if not [x for x in remaining_on + remaining_off if x in here]:
                break
            _sleep_h(0.6)

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

    for _ in range(15):
        present = page.evaluate(
            """() => [...document.querySelectorAll('[role=button]')].some(e =>
              e.offsetParent !== null && (e.textContent || '').trim() === 'All mobile devices')"""
        )
        if present:
            break
        _sleep_h(0.2)
    else:
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
    surface_clicked = False
    try:
        surface_clicked = bool(
            page.evaluate(
                """() => {
                  const el = document.querySelector(
                    '[data-surface*="editor_tree:adset"][data-surface*="campaign-structure-action-menu-item"]');
                  if (!el) return false;
                  el.click();
                  return true;
                }"""
            )
        )
    except Exception:
        surface_clicked = False
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
            _diag("no_click_point")
            return False

        # A click with no prior hover dwell can land on the row's own (wider)
        # click target — navigating into the ad set instead of opening the
        # menu — confirmed live 2026-07-28: the icon only becomes the active
        # hit-target after a brief hover.
        page.mouse.move(click_point["x"], click_point["y"])
        _sleep_h(0.4)
        page.mouse.click(click_point["x"], click_point["y"])
        _sleep_h(0.6)

    create_clicked = False
    for _ in range(5):
        create_clicked = page.evaluate(
            """() => {
              const el = document.querySelector(
                '[data-surface="/am/editor_drawer/lib:campaign-structure-item-action-menu-create"]');
              if (!el) return false;
              el.click();
              return true;
            }"""
        )
        if create_clicked:
            break
        _sleep_h(0.3)
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
                  const under = document.elementFromPoint(pt.x, pt.y);
                  return {
                    menuSurfaces: [...new Set(surfaces)].slice(0, 10),
                    anyMenuOpen: !!document.querySelector('[role=menu],[role=menuitem]'),
                    underPoint: under ? (under.tagName + '/' + (under.getAttribute('role') || '') +
                                         '/' + (under.textContent || '').trim().slice(0, 40)) : null,
                  };
                }""",
                {"x": click_point["x"], "y": click_point["y"]},
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
        return False

    # Readiness is the ad-name field specifically — that is what the caller
    # writes to next, so it is the only signal worth gating on.
    try:
        page.get_by_placeholder("Enter your ad name here...").first.wait_for(
            state="visible", timeout=10000
        )
    except Exception:
        return False
    return True


def _creative_modal_open(page: Page) -> bool:
    """Is the "Set up your creative" wizard modal currently covering the page?"""
    try:
        return page.get_by_text("Set up your creative", exact=True).first.is_visible(timeout=1000)
    except Exception:
        return False


def _library_selected_count(page: Page) -> int:
    """Sum of the media library's own "N selected" counters."""
    try:
        return int(
            page.evaluate(
                """
                () => {
                  const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                  let total = 0;
                  for (const el of document.querySelectorAll('*')) {
                    if (!el.offsetParent || el.children.length !== 0) continue;
                    const m = norm(el.textContent).match(/^(\\d+) selected$/);
                    if (m) total += parseInt(m[1], 10);
                  }
                  return total;
                }
                """
            )
        )
    except Exception:
        return 0


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


def _library_undo_selection(page: Page, tiles, clicked: list[int], before: int) -> bool:
    """Снять то, что мы навыбирали, и подтвердить это счётчиком сетки.

    Без этого повторное использование медиатеки опаснее, чем отсутствие
    повторного использования, и 24.08 его именно поэтому и выключили: шаг
    отказывался УЖЕ выбрав плитки, вызывающий честно уходил грузить файл, и
    объявление выходило с 3-6 креативами вместо одного (прогоны 5bff134ea745,
    770bd7e5fb7b: «3 of 10 selected», «6 of 10 selected»). Meta делает из
    лишних отдельные вариации, то есть публикуется не то объявление, которое
    собрал оператор, — и заметить это можно только глазами в кабинете.

    Возвращает, удалось ли вернуть сетку ровно в то состояние, в котором мы её
    застали. Ответ «нет» — не мелочь: он означает, что выбор остался
    применённым, и загружать поверх нельзя.
    """
    for i in reversed(clicked):
        if _library_selected_count(page) <= before:
            break
        try:
            tiles.nth(i).click(timeout=3000)
        except Exception:
            continue
        for _ in range(6):  # ~1.8s: счётчик догоняет клик не мгновенно
            _sleep_h(0.3)
            if _library_selected_count(page) <= before:
                break
    return _library_selected_count(page) <= before


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
    _library_search(page, filename)
    tiles = _library_tiles(page, filename)
    try:
        available = tiles.count()
    except Exception:
        available = 0
    if not available:
        return REUSE_ABSENT

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
    for i in range(min(available, 6)):
        try:
            tile = tiles.nth(i)
            tile.scroll_into_view_if_needed(timeout=3000)
            tile.click(timeout=3000)
        except Exception:
            continue
        clicked.append(i)
        for _ in range(10):  # ~3s
            _sleep_h(0.3)
            if _library_selected_count(page) > before:
                picked = True
                break
        if picked:
            break
    if not picked:
        # Кликали, но счётчик не двинулся. «Не двинулся» — это то, что мы
        # видим, а не то, что произошло: клик мог и зарегистрироваться, просто
        # позже. Поэтому не уходим молча, а снимаем за собой.
        if clicked and not _library_undo_selection(page, tiles, clicked, before):
            return REUSE_DIRTY
        return REUSE_ABSENT

    # One ad, one creative. If a slow render still let two clicks through,
    # toggle the surplus back off — an over-selected ad silently becomes a
    # multi-media ad Meta then spins into extra variations, which is not the
    # campaign the operator built on the dashboard.
    for i in reversed(clicked[:-1]):
        if _library_selected_count(page) <= before + 1:
            break
        try:
            tiles.nth(i).click(timeout=3000)
        except Exception:
            continue
        _sleep_h(0.8)

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
            return _retreat(page, tiles, clicked, before)
        _close_creative_setup_modal(page)
        _sleep_h(0.5)
        if "/ads/edit/" in page.url and _verify_ad_media_present(page):
            return REUSE_ATTACHED
        # Мастер продвинулся, а медиа на объявлении нет. Выбор при этом
        # где-то остался, и снять его отсюда уже нечем: сетки на экране нет.
        return REUSE_DIRTY
    except Exception:
        return _retreat(page, tiles, clicked, before)


def _retreat(page: Page, tiles, clicked: list[int], before: int) -> str:
    """Отступить так, чтобы за нами можно было грузить файл.

    Отдельной функцией, потому что зовётся из трёх мест, и в каждом соблазн
    написать `return False` — ровно та ошибка, которая стоила отключения всей
    возможности 24.08.
    """
    try:
        _close_creative_setup_modal(page)
    except Exception:
        pass
    if not clicked:
        return REUSE_ABSENT
    return REUSE_ABSENT if _library_undo_selection(page, tiles, clicked, before) else REUSE_DIRTY


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
        _telemetry.record("ad_creative_reuse", False, note="library_pick_left_selection")
        return False
    _telemetry.record("ad_creative_reuse", None, note="not_in_library")
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
    # Повторное использование медиатеки проверяем ДО быстрого отказа: копия
    # могла появиться там от соседнего объявления, и тогда файл вообще никуда
    # не поедет. Быстрый отказ — только про повторную ЗАГРУЗКУ.
    if upload_already_failed(creative_path):
        verdict = reuse_before_upload(page, creative_path)
        if verdict is True:
            return True
        _telemetry.record(
            "ad_creative_upload", False,
            note="same_file_already_failed_this_run",
            diag={"file": Path(creative_path).name},
        )
        return False

    for attempt in range(attempts):
        try:
            if upload_ad_creative(page, creative_type, creative_path, edit_url=edit_url):
                return True
        except Exception:
            pass
        _close_creative_setup_modal(page)
        _sleep_h(0.8)
        if _verify_ad_media_present(page):
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
      let best = null, bestArea = Infinity;
      for (let i = 0; i < it.snapshotLength; i++) {
        const el = it.snapshotItem(i);
        if (!el || el.offsetParent === null) continue;
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
            _sleep_h(settle_s)
            if page.evaluate(present_js):
                return True
        except Exception:
            continue
    return False


def _close_creative_setup_modal(page: Page) -> bool:
    """Force-close the "Set up your creative" modal via its own top-right
    icon, regardless of which sub-step it's currently on (Trim/Crop/Text/
    Image generation/Enhancements/Translation/...).

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
    try:
        modal_heading = page.get_by_text("Set up your creative", exact=True).first
        if not modal_heading.count() or not modal_heading.is_visible(timeout=1500):
            return False
        box = page.evaluate(
            """
            () => {
              const all = [...document.querySelectorAll('*')];
              const heading = all.find(e => e.textContent.trim() === 'Set up your creative' && e.offsetParent !== null);
              if (!heading) return null;
              let modal = heading;
              for (let i = 0; i < 10 && modal; i++) {
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
            """
        )
        if box:
            _cdp_mouse_click(page, box["x"], box["y"])
            _sleep_h(0.8)
            return True
    except Exception:
        pass
    return False


def upload_ad_creative(
    page: Page, creative_type: str, creative_path: str, *, edit_url: str = ""
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
    global _LAST_UPLOAD_DIAG
    _LAST_UPLOAD_DIAG = {"file": Path(creative_path).name, "attempts": []}
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

    def _wizard_open() -> bool:
        """Is the creative wizard on screen?

        Meta reshaped this modal on 2026-08-24: the "Set up your creative"
        title is GONE, the panel became a narrow centred modal, and the step
        list moved from a vertical column on the left to a horizontal row
        across the top. Keying on the old title made the code open the wizard
        and then fail to notice it had — every creative upload in the runs of
        24.08 died that way while the screenshot clearly showed it open.

        "Creative setup" alone is NOT enough: it also appears on the plain ad
        page ("Creative setup (0/1)"). Require several sibling step labels
        that exist only inside the wizard, plus its own Next/Cancel pair.
        """
        try:
            return page.evaluate(
                r"""
                () => {
                  const norm = (x) => (x || '').replace(/\s+/g, ' ').trim();
                  const vis = [...document.querySelectorAll('*')]
                    .filter(e => e.offsetParent !== null && e.children.length === 0)
                    .map(e => norm(e.textContent));
                  if (vis.includes('Set up your creative')) return true;  // legacy shape
                  const steps = ['Creative setup', 'Media', 'Text',
                                 'Image generation', 'Enhancements', 'Translation'];
                  const hits = steps.filter(t => vis.includes(t)).length;
                  const hasNav = [...document.querySelectorAll('[role=button],button')]
                    .filter(e => e.offsetParent !== null)
                    .some(e => ['Next', 'Cancel'].includes(norm(e.textContent)));
                  return hits >= 3 && hasNav;
                }
                """
            )
        except Exception:
            return False

    opened = False
    for _attempt in range(4):
        if _wizard_open():
            opened = True
            break
        if not _click_xpath(page, trigger_xpath):
            _sleep_h(0.8)
            continue
        # menu painted?
        item = None
        for _ in range(8):  # ~2.4s
            if page.query_selector("xpath=" + item_xpath) is not None:
                item = True
                break
            _sleep_h(0.3)
        if not item:
            continue  # menu never came up (or toggled shut) — retrigger
        _click_xpath(page, item_xpath)
        for _ in range(10):  # ~3s for the wizard panel to mount
            if _wizard_open():
                opened = True
                break
            _sleep_h(0.3)
        if opened:
            break
    if not opened:
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
            return False
    if upload_btn.count() < 1:
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
                page.keyboard.press("Control+A")
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
    _upload_name = Path(creative_path).name
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
    try:
        _diag["size_mb"] = round(Path(creative_path).stat().st_size / (1024 * 1024), 1)
    except Exception:
        pass

    def _remember(**kw) -> None:
        _diag["attempts"].append(kw)
        global _LAST_UPLOAD_DIAG
        _LAST_UPLOAD_DIAG = _diag
        try:
            Path("output/debug_creative_upload.txt").write_text(
                json.dumps(_diag, ensure_ascii=False, indent=1), encoding="utf-8"
            )
        except Exception:
            pass

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
        try:
            # 1) If a file input is already mounted, feed it — no click, so the
            #    control's shape cannot matter.
            fed = False
            _inputs = page.query_selector_all("input[type=file]")
            _step["file_inputs"] = len(_inputs)
            for h in _inputs:
                try:
                    h.set_input_files(creative_path)
                    fed = True
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
                if _diag["size_mb"] > _PW_FILE_LIMIT_MB:
                    _step["heavy"] = _upload_heavy_via_chooser_event(
                        page, creative_path, _diag, _press_upload
                    )
                    if not _step["heavy"]:
                        # Не вышло — пусть откажет прежний путь, вслух и с
                        # текстом. Молчание здесь стоило полутора дней.
                        with page.expect_file_chooser(timeout=25000) as fc_info:
                            _press_upload()
                        _step["chooser"] = True
                        fc_info.value.set_files(creative_path, timeout=_PW_SET_FILES_MS)
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
        try:
            _step["uploading"] = _upload_in_progress(page)
            _step["progress"] = _upload_progress(page)
        except Exception:
            pass
        _remember(**_step)
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

    # Advance until the wizard closes (or we run out of patience): each
    # round clicks the enabled Next/Done, then checks whether the wizard
    # title is still on screen.
    misses = 0
    for _ in range(16):
        # Ask the SAME detector that opened the wizard. This used to test for
        # the literal title 'Set up your creative', which the 2026-08-24
        # redesign removed: the check reported "already closed" on the very
        # first round, the loop broke before pressing anything, and the ad was
        # left sitting in an open wizard with the tile selected and Next
        # untouched — media never committed, so _verify_ad_media_present then
        # failed the whole step (measured run d5aa7a96bbd1, all 4 ads).
        if not _wizard_open():
            break
        advanced = False
        for label in ("Done", "Save", "Next"):
            if _click_xpath(
                page,
                "//*[@role='button' or self::button]"
                f"[normalize-space(.)='{label}' and not(@aria-disabled='true')]",
            ):
                advanced = True
                break
        if not advanced:
            # A transient render can leave no enabled button for a beat —
            # measured live 2026-08-04 walking this wizard: rounds where the
            # selection counter briefly vanished still had Next a moment
            # later. Breaking on the first miss left the wizard open and the
            # media uncommitted, which is exactly how the step kept failing.
            misses += 1
            if misses >= 4:
                break
            _sleep_h(1.2)
            continue
        misses = 0
        _sleep_h(1.4)

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
                    filled: visible.some(t => t === 'Edit media' || t === 'Remove media'),
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

    if _primary_text_visible():
        return True

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
        return False

    def _click_via_cdp_handle(locator):
        return _focus_via_cdp_click(page, locator)

    def _handle_has_content(handle) -> bool:
        try:
            return bool(handle.evaluate(
                "el => (el.value !== undefined ? el.value : el.textContent || '').trim().length > 0"
            ))
        except Exception:
            return False

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
                    _type_human(page, ad_body)
                    _sleep_h(0.4)
                    if _handle_has_content(handle):
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
                    page.keyboard.press("Control+A")
                    _type_human(page, ad_title)
                    _sleep_h(0.4)
                    if _handle_has_content(handle):
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
          const nameMatches = (raw) => {
            const t = norm(raw).replace(/[…\\u2026]+$/, '');
            if (!t) return false;
            if (t === adName) return true;
            return t.length >= 10 && adName.startsWith(t);
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


_WIZARD_CLOSE_GUARD_JS = """
  // Returns true if an element looks like the wizard's own chrome (toolbar/header Close)
  // and should NOT be clicked by banner-dismiss logic.
  // Wizard Close is typically in a fixed top bar or a role=dialog header with no banner text nearby.
  function _isWizardClose(el) {
    // Walk up to 8 levels looking for known wizard container signals
    let node = el;
    for (let i = 0; i < 8; i++) {
      if (!node || node === document.body) break;
      const role = (node.getAttribute && node.getAttribute('role') || '').toLowerCase();
      const label = (node.getAttribute && node.getAttribute('aria-label') || '').toLowerCase();
      const id = (node.id || '').toLowerCase();
      // Banner / notice / toast containers are NOT the wizard chrome
      if (role === 'alert' || role === 'alertdialog' || role === 'status') return false;
      if (id.indexOf('banner') !== -1 || id.indexOf('toast') !== -1 || id.indexOf('notice') !== -1) return false;
      if (label.indexOf('banner') !== -1 || label.indexOf('notice') !== -1) return false;
      // If we hit a scrollable inner panel (the adset editor body) it's not the wizard chrome
      if (node.scrollHeight > 600 && node.scrollWidth > 400 && node !== document.documentElement) return false;
      node = node.parentElement;
    }
    // If the element's bounding rect puts it in the top ~60px it is likely the wizard toolbar
    const rect = el.getBoundingClientRect();
    if (rect && rect.top < 60 && rect.top >= 0) return true;
    return false;
  }
"""


def _dismiss_advantage_banner(page: Page) -> None:
    """Dismiss the Meta Advantage+ recommendation banner that appears after selecting Website.

    The banner says 'You could get 14-24% lower cost per lead by choosing website
    and instant forms' with an 'Apply now' button. We must NOT click 'Apply now'
    (that switches back to website+forms). We look for a close/X button instead.
    If no close button, we just ignore it — the banner does not block the flow.
    Never clicks the wizard's own toolbar/header Close button.
    """
    script = _WIZARD_CLOSE_GUARD_JS + """
      const closeSels = [
        '[aria-label="Close recommendation"]',
        '[aria-label="Close"]', '[aria-label="Cerrar"]',
        '[aria-label="Dismiss"]', '[aria-label="Descartar"]',
        'button[data-testid="close_button"]',
      ];
      for (const sel of closeSels) {
        for (const el of document.querySelectorAll(sel)) {
          if (el.offsetParent !== null && !_isWizardClose(el)) { el.click(); return true; }
        }
      }
      return false;
    """
    # Wrap in IIFE
    script = f"() => {{{script}}}"
    for root in _roots(page):
        try:
            root.evaluate(script)
        except Exception:
            pass


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


def _dismiss_floating_banners(page: Page) -> None:
    """Close any floating info/warning banners that may interfere with clicks.

    Meta sometimes shows 'Value rule creation is changing' or similar notices
    as dismissible overlays that can obscure dropdown options.
    Never clicks the wizard's own toolbar/header Close button.
    """
    script = _WIZARD_CLOSE_GUARD_JS + """
      const closeSels = [
        '[aria-label="Close"]', '[aria-label="Cerrar"]',
        '[aria-label="Dismiss"]', '[aria-label="Descartar"]',
        'button[data-testid="close_button"]',
      ];
      for (const sel of closeSels) {
        for (const el of document.querySelectorAll(sel)) {
          if (el.offsetParent !== null && !_isWizardClose(el)) {
            el.click();
            return true;
          }
        }
      }
      return false;
    """
    script = f"() => {{{script}}}"
    for root in _roots(page):
        try:
            root.evaluate(script)
        except Exception:
            pass


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
    for root in _roots(page):
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


def fill_targeting_interest(page: Page, text: str) -> bool:
    """Type into Detailed targeting interests field — not 'Search existing audiences'."""
    script = """
    (text) => {
      const visible = (el) => {
        if (!el || el.offsetParent === null) return false;
        const r = el.getBoundingClientRect();
        return r.width > 2 && r.height > 2;
      };
      const bad = ['existing audience', 'audiencia existente', 'saved audience'];
      const good = [
        'add interests', 'interests, behaviors', 'añadir intereses',
        'detailed targeting', 'segmentación detallada', 'include people',
        'incluir personas', 'demographics', 'demografía'
      ];
      const inputs = [...document.querySelectorAll('input, textarea')].filter(visible);
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
            if root.evaluate(script, text):
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
    first_term = text.split(",")[0].strip()
    if not first_term:
        return False
    try:
        return bool(page.get_by_text(first_term, exact=False).first.is_visible())
    except Exception:
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
                from src.fb_ads_rpa.screenshots import capture as _capture

                _capture(page, f"recorded_locations_remove_default_removed{removed_any}")
            except Exception:
                pass
            return
        except Exception:
            continue


def _pick_country_and_clear_defaults(root, page: Page, search_term: str) -> bool:
    if not _pick_country_suggestion(root, page, search_term):
        return False
    try:
        _remove_stale_default_locations(page, search_term)
    except Exception:
        pass
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
                        from src.fb_ads_rpa.screenshots import capture as _capture

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
                    from src.fb_ads_rpa.screenshots import capture as _capture

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


def fill_audience_age(page: Page, age_min: int, age_max: int) -> bool:
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
            try:
                el = root.get_by_text(re.compile(rf"^{re.escape(default_text)}$"), exact=True).first
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
                    from src.fb_ads_rpa.screenshots import capture as _capture

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
        if len(boundary_results) == 2 and all(boundary_results):
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
                    from src.fb_ads_rpa.screenshots import capture as _capture

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
                                    from src.fb_ads_rpa.screenshots import capture as _capture

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

    ok_any = False
    for name in language_names:
        try:
            inp.click(timeout=3000)
            page.keyboard.type(name, delay=40)
            _sleep_h(1.2)
            # Prefer the "(All)" variant over a blind first-match — per live
            # user guidance (2026-07-25) watching the real suggestion list:
            # Meta lists per-country language variants (e.g. "Spanish
            # (Spain)", "Spanish (Mexico)") alongside a broader "Spanish
            # (All)" entry, and the broad one is what's wanted here, same
            # reasoning as _pick_country_suggestion's "(Country)" preference.
            all_re = re.compile(rf"^{re.escape(name)}\s*\(all\)", re.I)
            picked = False
            for sel in ("[role=option]:visible", "li[role=option]"):
                try:
                    opt = page.locator(sel).filter(has_text=all_re).first
                    if opt.count() and opt.is_visible():
                        opt.click(timeout=3000)
                        _sleep_h(0.5)
                        ok_any = True
                        picked = True
                        break
                except Exception:
                    continue
            if not picked:
                opt = page.locator("[role=option]:visible, li[role=option]").first
                if opt.count() and opt.is_visible():
                    opt.click(timeout=3000)
                    _sleep_h(0.5)
                    ok_any = True
            root, inp = _find_lang_input()
            if inp is None:
                break
        except Exception:
            continue
    return ok_any


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
