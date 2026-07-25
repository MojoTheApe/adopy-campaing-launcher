"""DOM helpers for Meta Ads Manager — label-based fills that survive UI refactors."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import Frame, Page
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
        time.sleep(0.3)
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
                time.sleep(0.5)
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
                time.sleep(0.5)
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
                time.sleep(0.6)
                return True
        except Exception:
            pass
    if click_sidebar_tree_index(page, level):
        time.sleep(0.5)
        return True
    if level in (0, 1, 2) and click_wizard_breadcrumb_index(page, level):
        time.sleep(0.5)
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
    time.sleep(0.4)
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
                    time.sleep(0.3)
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
                        time.sleep(0.8)
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
                time.sleep(0.8)
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
                            time.sleep(0.8)
                            # Verify we opened the right menu
                            if _is_conversion_event_menu(page):
                                close_open_dropdown(page)
                                time.sleep(0.3)
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
                    time.sleep(0.55)
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
                time.sleep(0.6)
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
                        time.sleep(0.5)
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
                    time.sleep(0.45)
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
                    time.sleep(0.45)
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
                time.sleep(0.5)
                if is_website_conversion_selected(page):
                    return True
            except Exception:
                pass

            # Strategy 2: Focus + Enter
            try:
                el.focus(timeout=2000)
                el.press("Enter", timeout=2000)
                time.sleep(0.5)
                if is_website_conversion_selected(page):
                    return True
            except Exception:
                pass

            cx = box["x"] + box["width"] / 2
            cy = box["y"] + box["height"] / 2

            # Strategy 3: Mouse click center of SPAN
            _mouse_click_chain(page, cx, cy)
            time.sleep(0.5)
            if is_website_conversion_selected(page):
                return True

            # Strategy 4: Radio circle (left edge)
            _mouse_click_chain(page, box["x"] + min(16, box["width"] * 0.12), cy)
            time.sleep(0.5)
            if is_website_conversion_selected(page):
                return True

            # Strategy 5: Parent container
            try:
                parent = el.locator("xpath=../..")
                parent.click(timeout=3000, force=True)
                time.sleep(0.5)
                if is_website_conversion_selected(page):
                    return True
            except Exception:
                pass

            el.click(timeout=4000, force=True)
            time.sleep(0.45)
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
                time.sleep(0.35)
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
                time.sleep(0.35)
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
            for typ, extra in (
                ("mouseMoved", {"button": "none"}),
                ("mousePressed", {"button": "left", "buttons": 1, "clickCount": 1}),
                ("mouseReleased", {"button": "left", "buttons": 0, "clickCount": 1}),
            ):
                params = {"type": typ, "x": float(x), "y": float(y), **extra}
                session.send("Input.dispatchMouseEvent", params)
                time.sleep(0.05)
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
    time.sleep(0.06)
    page.mouse.down()
    time.sleep(0.07)
    page.mouse.up()


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
                time.sleep(0.35)
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
                time.sleep(0.7)
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
                    time.sleep(0.7)
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
    time.sleep(0.2)
    for key in ("Space", "Enter"):
        try:
            page.keyboard.press(key)
            time.sleep(0.55)
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
            time.sleep(0.6)
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
        time.sleep(0.55)
        if is_website_conversion_selected(page):
            return True
    except Exception:
        pass
    # 2) Row center / label area
    try:
        _mouse_click_chain(page, float(bbox["cx"]), float(bbox["cy"]))
        time.sleep(0.55)
        if is_website_conversion_selected(page):
            return True
    except Exception:
        pass
    # 3) Playwright mouse.click convenience
    try:
        page.mouse.click(float(bbox["radio_x"]), float(bbox["radio_y"]))
        time.sleep(0.55)
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
                    time.sleep(0.45)
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
                        time.sleep(0.45)
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
            time.sleep(0.12)
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
                time.sleep(0.45)
                if is_website_conversion_selected(page):
                    return True
                page.keyboard.press("Space")
                time.sleep(0.45)
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
            time.sleep(0.45)
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
                time.sleep(0.55)
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
        time.sleep(0.5)
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
) -> str:
    """Create -> Website, using the exact locators from a verified human
    Playwright codegen recording (2026-07-21) instead of the label/breadcrumb/
    coordinate cascade the rest of this file falls back to.

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
        # Meta shows a transient full-page "Loading..." overlay, seemingly on
        # a periodic list refresh as well as after transitions (list -> Create
        # modal, Continue -> wizard) — confirmed live (2026-07-21) covering the
        # page while a click/fill silently missed underneath it, and a single
        # wait-then-click was still sometimes racing a fresh overlay. Checked
        # before every action below, with a retry loop around the action itself.
        try:
            page.get_by_text("Loading...", exact=False).first.wait_for(state="hidden", timeout=10000)
        except Exception:
            pass

    def _click(loc, *, attempts: int = 3) -> bool:
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
                time.sleep(0.6)
        return False

    def _fill(loc, value: str, *, attempts: int = 3) -> bool:
        for _ in range(attempts):
            _wait_loading()
            try:
                loc.wait_for(state="visible", timeout=8000)
                loc.click(timeout=3000)
                loc.fill(value, timeout=4000)
                return True
            except Exception:
                time.sleep(0.6)
        return False

    def _shot(label: str) -> None:
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
    for _ in range(3):
        _dismiss_floating_banners(page)
        time.sleep(0.4)
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

    if not _fill(page.get_by_placeholder("Enter your campaign name here"), campaign_name):
        _shot("fail_campaign_name")
        return "campaign_name"
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
    _shot("after_campaign_fields")

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

    time.sleep(0.6)
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
    # (already used by the older recipe_player delegated-section path) rather
    # than writing a new implementation — but don't assume it's correct just
    # because it exists; verify it live the same way Website's check just got
    # caught being wrong.
    if not select_conversion_event_lead(page):
        _shot("fail_conversion_event")
        return "conversion_event"
    _shot("success_conversion_event")

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
                time.sleep(0.6)
                opened = True
                break
            except Exception:
                # Not rendered/visible yet — nudge the page down and retry
                # rather than assuming the text is simply absent.
                try:
                    page.mouse.wheel(0, 400)
                except Exception:
                    pass
                time.sleep(0.6)
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
            time.sleep(0.5)
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
    soft_fails: list[str] = []

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

    # Age — same reused, not-yet-independently-verified function.
    try:
        if not fill_audience_age(page, age_min, age_max):
            _shot("fail_audience_age")
            soft_fails.append("audience_age")
        else:
            _shot("after_audience_age")
    except Exception as exc:
        _shot("fail_audience_age")
        soft_fails.append(f"audience_age:{str(exc)[:60]}")

    # Detailed targeting: paste each comma-separated interest term and pick
    # the first autocomplete suggestion — same pattern the old recipe_player
    # path used for this exact field.
    if targeting_label:
        try:
            if not open_detailed_targeting_field(page):
                _shot("fail_targeting_open")
                soft_fails.append("targeting_open")
            else:
                any_added = False
                for term in [t.strip() for t in targeting_label.split(",") if t.strip()]:
                    if fill_targeting_interest(page, term):
                        pick_first_suggestion(page)
                        any_added = True
                        time.sleep(0.4)
                _shot("after_targeting" if any_added else "fail_targeting_none_added")
                if not any_added:
                    soft_fails.append("targeting_none_added")
        except Exception as exc:
            _shot("fail_targeting")
            soft_fails.append(f"targeting:{str(exc)[:60]}")

    if soft_fails:
        try:
            Path("output/debug_audience_soft_fails.txt").write_text(
                "\n".join(soft_fails), encoding="utf-8"
            )
        except Exception:
            pass

    return ""


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
                time.sleep(0.4)
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
                            time.sleep(0.45)
                            return True
                    except Exception:
                        pass
                # Fallback: any dismiss-looking button in the dialog
                try:
                    btn = dialog.get_by_role("button", name=dismiss_re).first
                    if btn.is_visible(timeout=400):
                        btn.click(timeout=3000, force=True)
                        time.sleep(0.45)
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
                time.sleep(0.45)
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
                time.sleep(0.45)
                return
            except Exception:
                pass


def wait_editor_saved(page: Page, *, timeout_s: float = 15) -> None:
    """Wait until Meta finishes 'Verifying your edits...'."""
    import time as _time

    deadline = _time.time() + timeout_s
    while _time.time() < deadline:
        busy = False
        for root in _roots(page):
            try:
                loc = root.get_by_text(re.compile(r"Verifying your edits", re.I)).first
                if loc.is_visible():
                    busy = True
                    break
            except Exception:
                pass
        if not busy:
            return
        _time.sleep(0.5)
    _time.sleep(0.4)


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
                                time.sleep(0.6)
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
    time.sleep(0.4)
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
                            time.sleep(0.6)
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
                                time.sleep(0.6)
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
                            time.sleep(0.6)
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


def select_conversion_event_lead(page: Page) -> bool:
    """Open Conversion event dropdown and choose Lead."""
    scroll_to_section(page, "Conversion event", "Evento de conversión")
    if is_lead_conversion_event_selected(page):
        return True
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
    for root in _roots(page):
        for ph in placeholders:
            try:
                inp = root.get_by_placeholder(ph).first
                inp.wait_for(state="visible", timeout=5000)
                inp.scroll_into_view_if_needed(timeout=5000)
                inp.click(timeout=5000)
                time.sleep(0.3)
                return True
            except Exception:
                pass
        for label_re in (
            re.compile(r"Detailed targeting", re.I),
            re.compile(r"Segmentación detallada", re.I),
        ):
            try:
                inp = root.get_by_label(label_re).first
                inp.wait_for(state="visible", timeout=5000)
                inp.scroll_into_view_if_needed(timeout=5000)
                inp.click(timeout=5000)
                time.sleep(0.3)
                return True
            except Exception:
                pass
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
                time.sleep(0.6)
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
                time.sleep(0.6)
                return True
        except Exception:
            continue
    page.keyboard.press("Enter")
    time.sleep(0.5)
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
    time.sleep(0.4)

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
                    time.sleep(0.6)
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
                        time.sleep(0.15)
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
                        time.sleep(0.3)
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
                        time.sleep(1.5)
                        return _pick_country_suggestion(immediate_root, page, search_term)
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
        time.sleep(0.5)
        root, inp = _find_edit_mode_input()
        if inp is not None:
            try:
                inp.scroll_into_view_if_needed(timeout=3000)
                inp.click(timeout=3000)
                time.sleep(0.3)
                # See the note above (attempt loop): this is a <span
                # placeholder="..."> fake-input, not a real editable
                # element — .fill() throws, keyboard typing works.
                page.keyboard.type(search_term, delay=40)
                time.sleep(1.5)
                return _pick_country_suggestion(root, page, search_term)
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
                time.sleep(1.5)
                if page.locator("[role=option]:visible, li[role=option]").first.count():
                    return _pick_country_suggestion(page, page, search_term)
            except Exception:
                pass
        time.sleep(0.5)

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
                    time.sleep(0.3)
                    inp.fill(search_term, timeout=5000)
                    time.sleep(1.5)
                    return _pick_country_suggestion(page, page, search_term)
        except Exception:
            pass
    return False


def fill_audience_age(page: Page, age_min: int, age_max: int) -> bool:
    """Set audience age range dropdowns / inputs (min and max)."""
    # Same reasoning as fill_audience_location: skip the fuzzy
    # scroll_to_section heading match — the precise marker search a few
    # lines below (exact "^Age$|^Edad$" match) already does its own
    # scroll_into_view_if_needed on the correct element.
    time.sleep(0.3)
    age_min_str = str(age_min)
    age_max_str = str(age_max) if age_max < 65 else "65+"
    age_label_re = re.compile(r"^Age$|^Edad$", re.I)
    ok = False

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

    for root in _roots(page):
        try:
            marker = root.get_by_text(age_label_re).first
            marker.scroll_into_view_if_needed(timeout=5000)
            for depth in range(1, 10):
                row = marker.locator(f"xpath=ancestor::*[{depth}]")
                selects = row.locator("select, [role=combobox], [role=spinbutton]")
                if selects.count() >= 2:
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
                                time.sleep(0.3)
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
                            time.sleep(0.4)
                            ok = True
                        except Exception:
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
                time.sleep(0.5)
                return True
        except Exception:
            pass
    return False


def select_manual_placements(page: Page) -> bool:
    """Click Manual placements option in the Placements section."""
    scroll_to_section(
        page, "Placements", "Ubicaciones de anuncio", "Placement",
        "Account controls", "Controles de cuenta",
    )
    time.sleep(0.4)
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
                    time.sleep(0.8)
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
    time.sleep(0.4)

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
                    time.sleep(0.6)
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
