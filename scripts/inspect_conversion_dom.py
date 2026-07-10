"""Connect to the running Multilogin browser (port 19222) and dump
the exact DOM structure of the Conversion Location section.

Run while the Ads Manager wizard is open (Ad Set level, Conversion section visible).
"""
import json
from playwright.sync_api import sync_playwright

CDP_URL = "http://127.0.0.1:19222"

INSPECT_SCRIPT = """
() => {
  const visible = (el) => !!(el && el.offsetParent !== null);
  const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();

  // Find all elements that contain Conversion-related text
  const results = [];

  // 1. Find the open dropdown container (any listbox / menu / popover)
  const containers = [...document.querySelectorAll(
    '[role=listbox], [role=menu], [role=dialog], [role=presentation], [data-portal]'
  )].filter(visible);

  for (const c of containers) {
    const text = norm(c.textContent || '').slice(0, 100);
    if (!text.toLowerCase().includes('single') && !text.toLowerCase().includes('website')) continue;
    const info = {
      type: 'CONTAINER',
      tag: c.tagName,
      role: c.getAttribute('role'),
      class: c.className?.slice(0, 80),
      aria_label: c.getAttribute('aria-label'),
      text_preview: text,
      children_count: c.children.length,
    };
    results.push(info);

    // Dump all direct children with their roles
    for (const child of c.querySelectorAll('*')) {
      if (!visible(child)) continue;
      const childText = norm(child.textContent || '');
      if (childText.length > 80 || childText.length < 2) continue;
      const role = child.getAttribute('role');
      const tag = child.tagName;
      const ariaLabel = child.getAttribute('aria-label');
      const ariaChecked = child.getAttribute('aria-checked');
      const ariaSelected = child.getAttribute('aria-selected');
      const ariaExpanded = child.getAttribute('aria-expanded');
      if (role || tag === 'INPUT' || tag === 'LABEL' || tag === 'BUTTON') {
        results.push({
          type: 'CHILD',
          tag,
          role,
          aria_label: ariaLabel,
          aria_checked: ariaChecked,
          aria_selected: ariaSelected,
          aria_expanded: ariaExpanded,
          text: childText.slice(0, 60),
        });
      }
    }
  }

  // 2. Find any visible element with "Single" text and its role
  for (const el of document.querySelectorAll('*')) {
    if (!visible(el)) continue;
    const t = norm(el.textContent || '');
    if (t !== 'Single' && t !== 'Único' && t !== 'Única') continue;
    results.push({
      type: 'SINGLE_HEADER',
      tag: el.tagName,
      role: el.getAttribute('role'),
      class: el.className?.slice(0, 80),
      aria_label: el.getAttribute('aria-label'),
      aria_expanded: el.getAttribute('aria-expanded'),
      parent_role: el.parentElement?.getAttribute('role'),
      parent_tag: el.parentElement?.tagName,
      grandparent_role: el.parentElement?.parentElement?.getAttribute('role'),
    });
    break;
  }

  // 3. Find any element with exactly "Website" text
  for (const el of document.querySelectorAll('[role=radio], [role=option], input[type=radio], label, li, div, span')) {
    if (!visible(el)) continue;
    const t = norm(el.textContent || '');
    if (t.toLowerCase() !== 'website' && t.toLowerCase() !== 'sitio web') continue;
    const label = el.closest('label');
    results.push({
      type: 'WEBSITE_RADIO',
      tag: el.tagName,
      role: el.getAttribute('role'),
      class: el.className?.slice(0, 60),
      aria_label: el.getAttribute('aria-label'),
      aria_checked: el.getAttribute('aria-checked'),
      aria_selected: el.getAttribute('aria-selected'),
      data_testid: el.getAttribute('data-testid'),
      parent_role: el.parentElement?.getAttribute('role'),
      parent_tag: el.parentElement?.tagName,
      grandparent_role: el.parentElement?.parentElement?.getAttribute('role'),
      in_label: !!label,
      label_text: label ? norm(label.textContent || '').slice(0, 60) : null,
    });
  }

  return results;
}
"""

def main():
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.connect_over_cdp(CDP_URL)
        except Exception as e:
            print(f"Could not connect to CDP on {CDP_URL}: {e}")
            print("Make sure the Multilogin browser is still open.")
            return

        contexts = browser.contexts
        if not contexts:
            print("No browser contexts found.")
            browser.disconnect()
            return

        pages = contexts[0].pages
        if not pages:
            print("No pages found in context.")
            browser.disconnect()
            return

        # Find the Ads Manager page
        ads_page = None
        for p in pages:
            url = p.url
            print(f"  Page: {url[:80]}")
            if "adsmanager" in url or "facebook.com" in url:
                ads_page = p

        if ads_page is None:
            print("No Ads Manager page found — using first page")
            ads_page = pages[0]

        print(f"\nInspecting: {ads_page.url[:80]}\n")

        try:
            result = ads_page.evaluate(INSPECT_SCRIPT)
        except Exception as e:
            print(f"Evaluate error: {e}")
            browser.disconnect()
            return

        if not result:
            print("No results found. Is the Conversion Location dropdown open?")
            print("Open the dropdown first, then run this script again.")
        else:
            print(f"Found {len(result)} DOM items:\n")
            for item in result:
                print(json.dumps(item, ensure_ascii=False, indent=2))
                print()

        browser.disconnect()


if __name__ == "__main__":
    main()
