"""DOM helpers for Meta Ads Manager — label-based fills that survive UI refactors."""
from __future__ import annotations

import re
import time
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
    """Click campaign tree item: 0=campaign, 1=ad set, 2=ad."""
    script = """
    (index) => {
      const visible = (el) => {
        if (!el || !el.offsetParent) return false;
        const st = getComputedStyle(el);
        return st.visibility !== 'hidden' && st.display !== 'none';
      };
      const clickEl = (el) => {
        if (!el) return false;
        el.scrollIntoView({ block: 'nearest', inline: 'nearest' });
        el.click();
        return true;
      };

      const tree = [...document.querySelectorAll('[role="treeitem"]')].filter(visible);
      if (tree.length > index) {
        return clickEl(tree[index]);
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
        return clickEl(rows[index].el);
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
          return clickEl(best);
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
          return clickEl(el);
        }
      }

      return false;
    }
    """
    for root in _roots(page):
        try:
            if root.evaluate(script, index):
                return True
        except Exception:
            pass
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
          for (const inp of node.querySelectorAll('input, textarea, [contenteditable=true]')) {
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
    current = _conversion_location_display_value(page)
    if current:
        return _website_only_label(current)

    script = """
    () => {
      const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
      const exact = (t) => t === 'website' || t === 'sitio web';
      const radios = [...document.querySelectorAll('[role=radio], input[type=radio]')];
      for (const r of radios) {
        const label = norm(
          r.getAttribute('aria-label') ||
          r.parentElement?.textContent ||
          ''
        );
        if (!exact(label)) continue;
        if (r.getAttribute('aria-checked') === 'true' || r.checked) return true;
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
    """Expand the 'Single' group inside Conversion location dropdown.

    Finds the accordion HEADER ROW (the smallest element whose first line is
    "Single" and that also contains the description subtitle).  Sorting by
    bounding-box height ascending lets us avoid clicking a large container whose
    center lands outside the header.
    """
    # JS: collect candidates whose first text-line is "Single/Único/Única" AND
    # that contain the subtitle phrase, then sort by height ↑ to find the
    # tightest element (the header row, not an outer wrapper).
    script = """
    () => {
      const norm  = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
      const fline = (s) => norm((s || '').split('\\n')[0]);
      const viz   = (el) => !!(el && el.offsetParent !== null
                               && el.getBoundingClientRect().width > 0);

      const SINGLE_WORDS  = ['single', 'único', 'única'];
      const SUB_PHRASES   = ['send people to one location', 'una ubicación',
                              'one location where'];

      const candidates = [];
      for (const el of document.querySelectorAll(
        'div, li, section, [role=button], [role=listitem], [role=option]'
      )) {
        if (!viz(el)) continue;
        const t  = norm(el.textContent || '');
        const fl = fline(el.textContent || '');
        if (!SINGLE_WORDS.includes(fl)) continue;
        if (!SUB_PHRASES.some(p => t.includes(p))) continue;
        const rect = el.getBoundingClientRect();
        // Skip elements wider than a typical dropdown (> 700px) or
        // taller than a header row (> 180px) — those are outer wrappers.
        if (rect.width > 700 || rect.height > 180) continue;
        candidates.push({ el, height: rect.height });
      }

      if (!candidates.length) return false;

      // Smallest height = most specific (the header row itself)
      candidates.sort((a, b) => a.height - b.height);
      const best = candidates[0].el;
      best.scrollIntoView({ block: 'nearest' });
      best.click();
      return true;
    }
    """
    for root in _roots(page):
        try:
            if root.evaluate(script):
                time.sleep(1.0)
                return True
        except Exception:
            pass

    # Playwright fallback: filter by has_text substring (not exact) then size-check
    for root in _roots(page):
        for sub_phrase in (
            re.compile(r"Send people to one location", re.I),
            re.compile(r"Enviar personas", re.I),
        ):
            try:
                rows = root.locator(
                    "div, li, [role=button], [role=listitem]"
                ).filter(has_text=sub_phrase).all()
                for row in rows[:20]:
                    try:
                        if not row.is_visible():
                            continue
                        blob = (row.inner_text(timeout=800) or "").lower()
                        fl = blob.split('\n')[0].strip()
                        if fl not in ('single', 'único', 'única'):
                            continue
                        box = row.bounding_box()
                        if box and (box['width'] > 700 or box['height'] > 180):
                            continue
                        row.scroll_into_view_if_needed(timeout=2000)
                        row.click(timeout=5000)
                        time.sleep(1.0)
                        return True
                    except Exception:
                        continue
            except Exception:
                pass

    return False


def _wait_for_single_expanded(page: Page, timeout_s: float = 3.0) -> bool:
    """Poll until the Single section is expanded (Website option visible) or timeout."""
    script = """
    () => {
      const norm  = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
      const fline = (s) => norm((s || '').split('\\n')[0]);
      const viz   = (el) => !!(el && el.offsetParent !== null
                               && el.getBoundingClientRect().width > 0);
      const isWebsiteOnly = (el) => {
        const line = fline(el.textContent || el.getAttribute('aria-label') || '');
        if (line !== 'website' && line !== 'sitio web') return false;
        const full = norm(el.textContent || '');
        return !full.includes('instant') && !full.includes('and calls')
               && !full.includes('formularios') && !full.includes('and messenger');
      };
      for (const el of document.querySelectorAll(
        'div, span, label, li, [role=radio], [role=option]'
      )) {
        if (viz(el) && isWebsiteOnly(el)) return true;
      }
      return false;
    }
    """
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        for root in _roots(page):
            try:
                if root.evaluate(script):
                    return True
            except Exception:
                pass
        time.sleep(0.25)
    return False


def _pick_website_from_open_menu(page: Page) -> bool:
    """Expand Single section, then click the Website radio inside it.

    UI structure after clicking the Conversion location trigger:
      Multiple  (^)
        ○ Website and instant forms
        ○ Website and calls
        ○ Instant forms and Messenger
      Single    (v)   ← click to expand
        ● Website          ← target
        ○ Instant forms
        ○ Messenger
    After expanding Single, Website is usually auto-selected.
    We must NOT click "Website and instant forms" (Multiple section).
    """
    time.sleep(0.4)
    _expand_conversion_location_single(page)
    # Poll for up to 3 s: wait until the Single section is visually expanded
    # (i.e. the Website option becomes visible in the DOM).
    _wait_for_single_expanded(page, timeout_s=3.0)

    # After expanding Single, Website is often auto-selected — check immediately.
    if is_website_conversion_selected(page):
        return True

    # JS: find Website radio inside the Single section container.
    # Excludes "Website and instant forms", "Website and calls" by checking
    # that the text does NOT contain "instant forms" / "and calls" / "and messenger".
    script_single_website = """
    () => {
      const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
      const visible = (el) => !!(el && el.offsetParent !== null);
      const firstLine = (s) => norm((s || '').split('\\n')[0]);
      const isWebsite = (el) => {
        const line = firstLine(el.textContent || el.getAttribute('aria-label') || '');
        if (line !== 'website' && line !== 'sitio web') return false;
        const full = norm(el.textContent || '');
        return !full.includes('instant forms') && !full.includes('and calls')
               && !full.includes('and messenger') && !full.includes('and instant');
      };

      // First try: find Single section header, then search within its expanded container
      for (const heading of document.querySelectorAll('*')) {
        if (!visible(heading)) continue;
        const ht = firstLine(heading.textContent || '');
        if (ht !== 'single' && ht !== 'único' && ht !== 'única') continue;
        // Walk up to find container that also has "instant forms" (i.e. expanded sub-options)
        let container = heading.parentElement;
        for (let i = 0; i < 6 && container; i++) {
          const ct = norm(container.textContent || '');
          if (ct.includes('instant forms') && ct.includes('messenger')) {
            // This container has the expanded sub-options — search inside it
            for (const el of container.querySelectorAll(
              '[role=radio], input[type=radio], [role=option], li, label, div, span'
            )) {
              if (!visible(el) || el.children.length > 3) continue;
              if (!isWebsite(el)) continue;
              el.scrollIntoView({ block: 'nearest' });
              el.click();
              return true;
            }
            break;
          }
          container = container.parentElement;
        }
      }

      // Fallback: search whole page for exact "Website" radio (no "and ..." suffix)
      for (const el of document.querySelectorAll(
        '[role=radio], input[type=radio], [role=option], [role=menuitemradio]'
      )) {
        if (!visible(el)) continue;
        if (!isWebsite(el)) continue;
        el.scrollIntoView({ block: 'nearest' });
        el.click();
        return true;
      }
      return false;
    }
    """
    for root in _roots(page):
        try:
            if root.evaluate(script_single_website):
                time.sleep(0.7)
                return True
        except Exception:
            pass

    # Meta uses plain <div> with no role for conversion location options.
    # ARIA-based searches (role=radio, role=option) all miss these elements.
    # JS strategy: find any visible element whose FIRST LINE of text is exactly
    # "Website" and that does NOT contain "instant forms" / "and calls".
    # Then walk up 1-2 DOM levels to click the actual option row.
    script_plain_div = """
    () => {
      const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
      const firstLine = (s) => norm((s || '').split('\\n')[0]).toLowerCase();
      const visible = (el) => !!(el && el.offsetParent !== null && el.getBoundingClientRect().width > 1);
      const isWebsiteOnly = (el) => {
        const line = firstLine(el.textContent || el.getAttribute('aria-label') || '');
        if (line !== 'website' && line !== 'sitio web') return false;
        const full = norm(el.textContent || '').toLowerCase();
        return !full.includes('instant forms') && !full.includes('and calls')
               && !full.includes('and messenger') && !full.includes('and instant')
               && !full.includes('formularios');
      };

      // Search any visible element — Meta uses plain divs without role
      const candidates = [];
      for (const el of document.querySelectorAll(
        'div, span, label, li, [role=option], [role=radio], [role=menuitemradio]'
      )) {
        if (!visible(el)) continue;
        if (!isWebsiteOnly(el)) continue;
        const rect = el.getBoundingClientRect();
        if (rect.width < 4 || rect.height < 4) continue;
        candidates.push({ el, area: rect.width * rect.height });
      }

      if (!candidates.length) return false;

      // Pick the LARGEST bounding box — it is the outer option row, not an inner text span.
      candidates.sort((a, b) => b.area - a.area);
      const best = candidates[0].el;
      best.scrollIntoView({ block: 'nearest' });
      best.click();
      return true;
    }
    """
    for root in _roots(page):
        try:
            if root.evaluate(script_plain_div):
                time.sleep(0.7)
                return True
        except Exception:
            pass

    if _pick_website_from_aria_controls(page) and is_website_conversion_selected(page):
        return True

    return False


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


def _dismiss_floating_banners(page: Page) -> None:
    """Close any floating info/warning banners that may interfere with clicks.

    Meta sometimes shows 'Value rule creation is changing' or similar notices
    as dismissible overlays that can obscure dropdown options.
    """
    script = """
    () => {
      // Look for close / dismiss buttons on any visible banner/notice
      const closeSels = [
        '[aria-label="Close"]', '[aria-label="Cerrar"]',
        '[aria-label="Dismiss"]', '[aria-label="Descartar"]',
        'button[data-testid="close_button"]',
      ];
      for (const sel of closeSels) {
        for (const el of document.querySelectorAll(sel)) {
          if (el.offsetParent !== null) {
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
            root.evaluate(script)
        except Exception:
            pass


def select_conversion_location_website(page: Page, *, skip_open: bool = False) -> bool:
    """Pick Website under Conversion → Conversion location on Ad Set level."""
    scroll_to_section(page, "Conversion location", "Ubicación de la conversión")
    _dismiss_floating_banners(page)
    if is_website_conversion_selected(page):
        return True

    if skip_open or conversion_menu_is_open(page):
        # Safety check: if the WRONG menu is open (Conversion event), close it first
        if _is_conversion_event_menu(page):
            close_open_dropdown(page)
            time.sleep(0.4)
        else:
            if _pick_website_from_open_menu(page) and is_website_conversion_selected(page):
                return True

    for attempt in range(3):
        close_open_dropdown(page)
        _dismiss_floating_banners(page)
        if not click_conversion_location_dropdown(page):
            break
        wait_conversion_menu_open(page)
        time.sleep(0.5)
        _dismiss_floating_banners(page)
        # Guard: verify the right menu opened before trying to pick
        if _is_conversion_event_menu(page):
            close_open_dropdown(page)
            time.sleep(0.3)
            continue
        if _pick_website_from_open_menu(page) and is_website_conversion_selected(page):
            return True
        close_open_dropdown(page)

    # Legacy radio/card UI fallback (popover may use radios without listbox)
    for root in _roots(page):
        try:
            radios = root.get_by_role("radio", name=re.compile(r"^Website(?:\s|$)", re.I))
            for i in range(min(radios.count(), 10)):
                radio = radios.nth(i)
                try:
                    if not radio.is_visible():
                        continue
                    if _menu_first_line(radio.inner_text(timeout=1500)) not in {"website", "sitio web"}:
                        continue
                    radio.scroll_into_view_if_needed(timeout=3000)
                    radio.click(timeout=5000)
                    time.sleep(0.6)
                    if is_website_conversion_selected(page):
                        return True
                except Exception:
                    continue
        except Exception:
            pass

    if select_option_near_section(
        page,
        ["Conversion location", "Ubicación de la conversión"],
        ["Website", "Sitio web"],
    ):
        time.sleep(0.6)
        return is_website_conversion_selected(page)
    return False


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
    scroll_to_section(page, *section_labels)
    # Use partial-match patterns so "* Conversion event" matches "Conversion event"
    patterns = [
        re.compile(re.escape(label), re.I)
        for label in section_labels
        if label.strip()
    ]
    for root in _roots(page):
        for pattern in patterns:
            try:
                marker = root.get_by_text(pattern).first
                marker.scroll_into_view_if_needed(timeout=5000)
                for depth in range(1, 14):
                    row = marker.locator(f"xpath=ancestor::*[{depth}]")
                    for sel in (
                        "[role=combobox]",
                        "[aria-haspopup='listbox']",
                        "[aria-haspopup='menu']",
                        "[role=button]",
                        "button",
                    ):
                        try:
                            # Use .first (not .last) — the field's own button is always
                            # the first/nearest control relative to its label; using .last
                            # can accidentally pick a button from a LOWER section.
                            ctrl = row.locator(sel).first
                            if ctrl.count() and ctrl.is_visible():
                                ctrl.click(timeout=5000)
                                time.sleep(0.6)
                                return True
                        except Exception:
                            continue
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
    return False


def is_lead_conversion_event_selected(page: Page) -> bool:
    current = _section_field_display_value(
        page,
        ["Conversion event", "Evento de conversión"],
    )
    if current:
        return _norm_label(current) in {"lead", "leads", "cliente potencial", "prospecto"}
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
    scroll_to_section(
        page,
        "Audience",
        "Audiencia",
        "Detailed targeting",
        "Segmentación detallada",
    )
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
_COUNTRY_SEARCH: dict[str, str] = {
    "MX": "Mexico", "CL": "Chile", "CO": "Colombia", "EC": "Ecuador",
    "PE": "Peru", "AR": "Argentina", "BR": "Brazil", "VE": "Venezuela",
    "BO": "Bolivia", "PY": "Paraguay", "UY": "Uruguay", "CR": "Costa Rica",
    "GT": "Guatemala", "HN": "Honduras", "SV": "El Salvador", "NI": "Nicaragua",
    "PA": "Panama", "DO": "Dominican Republic", "PR": "Puerto Rico",
    "US": "United States", "CA": "Canada", "GB": "United Kingdom",
    "DE": "Germany", "FR": "France", "ES": "Spain", "IT": "Italy",
    "PT": "Portugal", "NL": "Netherlands", "AU": "Australia",
    "ALL": "Worldwide",
}


def _country_search_term(code: str) -> str:
    c = (code or "").strip().upper()
    return _COUNTRY_SEARCH.get(c, c)


def fill_audience_location(page: Page, country_code: str) -> bool:
    """Type country into Audience → Locations input and pick first suggestion."""
    search_term = _country_search_term(country_code)
    scroll_to_section(page, "Locations", "Ubicaciones", "Audience", "Audiencia")
    time.sleep(0.4)

    location_placeholders = [
        re.compile(r"Add locations?", re.I),
        re.compile(r"Agregar ubicaciones?", re.I),
        re.compile(r"Search locations?", re.I),
        re.compile(r"Buscar ubicaciones?", re.I),
        re.compile(r"Enter a location", re.I),
    ]
    for root in _roots(page):
        for ph in location_placeholders:
            try:
                inp = root.get_by_placeholder(ph).first
                inp.wait_for(state="visible", timeout=5000)
                inp.scroll_into_view_if_needed(timeout=5000)
                inp.click(timeout=5000)
                time.sleep(0.3)
                inp.fill(search_term, timeout=5000)
                time.sleep(1.5)
                for sug_sel in (
                    "[role=option]:visible",
                    "[role=listbox] [role=option]:first-child",
                    "li[role=option]:first-child",
                    "ul[role=listbox] li:first-child",
                ):
                    try:
                        sug = root.locator(sug_sel).first
                        if sug.count() and sug.is_visible():
                            sug.click(timeout=4000)
                            time.sleep(0.6)
                            return True
                    except Exception:
                        continue
                page.keyboard.press("Enter")
                time.sleep(0.5)
                return True
            except Exception:
                continue

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
                    sug = page.locator("[role=option]:visible, li[role=option]").first
                    if sug.count() and sug.is_visible():
                        sug.click(timeout=4000)
                        time.sleep(0.6)
                    else:
                        page.keyboard.press("Enter")
                        time.sleep(0.5)
                    return True
        except Exception:
            pass
    return False


def fill_audience_age(page: Page, age_min: int, age_max: int) -> bool:
    """Set audience age range dropdowns / inputs (min and max)."""
    scroll_to_section(page, "Age", "Edad")
    time.sleep(0.3)
    age_min_str = str(age_min)
    age_max_str = str(age_max) if age_max < 65 else "65+"
    age_label_re = re.compile(r"^Age$|^Edad$", re.I)
    ok = False

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
