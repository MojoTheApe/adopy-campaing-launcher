from playwright.sync_api import sync_playwright
import json

SCRIPT = """
() => {
  const visible = el => !!(el && el.offsetParent !== null);
  const norm = s => (s||'').replace(/\\s+/g,' ').trim();
  const out = [];

  // 1. Current wizard sidebar
  for (const el of document.querySelectorAll('[role=treeitem]')) {
    if (!visible(el)) continue;
    out.push({t:'TREEITEM', text:norm(el.textContent).slice(0,50), sel:el.getAttribute('aria-selected')});
  }

  // 2. Conversion section elements
  for (const el of document.querySelectorAll('h2,h3,label,div,span')) {
    if (!visible(el) || el.children.length > 2) continue;
    const t = norm(el.textContent);
    if (!t || t.length > 60) continue;
    if (t === 'Conversion' || t === 'Conversion location' || t === 'Conversion event'
        || t === 'Website and instant forms' || t === 'Website'
        || t === 'Single' || t === 'Multiple') {
      out.push({t:'KEY_ELEM', text:t, tag:el.tagName,
        role:el.getAttribute('role'),
        aria_expanded:el.getAttribute('aria-expanded'),
        aria_checked:el.getAttribute('aria-checked'),
        parent_role:el.parentElement?.getAttribute('role'),
        parent_tag:el.parentElement?.tagName});
    }
  }

  // 3. Any open menus
  for (const el of document.querySelectorAll('[role=listbox],[role=menu],[aria-expanded="true"]')) {
    if (!visible(el)) continue;
    out.push({t:'OPEN_MENU', role:el.getAttribute('role'), expanded:el.getAttribute('aria-expanded'),
      text:norm(el.textContent).slice(0,80)});
  }

  // 4. All comboboxes
  for (const el of document.querySelectorAll('[role=combobox],[aria-haspopup]')) {
    if (!visible(el)) continue;
    const t = norm(el.textContent);
    if (!t) continue;
    out.push({t:'COMBOBOX', text:t.slice(0,60), expanded:el.getAttribute('aria-expanded'),
      haspopup:el.getAttribute('aria-haspopup')});
  }

  return out;
}
"""

with sync_playwright() as pw:
    try:
        browser = pw.chromium.connect_over_cdp('http://127.0.0.1:19222')
    except Exception as e:
        print('connect failed:', e)
        exit()

    ctx = browser.contexts[0]
    page = None
    for p in ctx.pages:
        print('PAGE:', p.url[:100])
        if 'facebook' in p.url:
            page = p

    if not page:
        page = ctx.pages[0]

    result = page.evaluate(SCRIPT)
    print('\n--- DOM FINDINGS ---')
    for item in result:
        print(json.dumps(item, ensure_ascii=False))

    browser.disconnect()
