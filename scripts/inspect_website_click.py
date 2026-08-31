"""
Run this script while the Conversion Location menu is OPEN in the browser.
It connects via CDP, dumps the DOM of the Website option, and tries to click it.

Usage:
    python scripts/inspect_website_click.py
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from config.paths import load_env
load_env()

from playwright.sync_api import sync_playwright

# ── Find the running Multilogin browser debug port ──────────────────────────
def find_cdp_url() -> str:
    import requests
    for port in range(9222, 9260):
        try:
            r = requests.get(f"http://127.0.0.1:{port}/json/version", timeout=1)
            if r.status_code == 200:
                print(f"[inspect] Found browser on port {port}")
                return f"http://127.0.0.1:{port}"
        except Exception:
            pass
    # Try common Multilogin ports
    for port in [58888, 45000, 45001, 35000]:
        try:
            r = requests.get(f"http://127.0.0.1:{port}/json/version", timeout=1)
            if r.status_code == 200:
                print(f"[inspect] Found browser on port {port}")
                return f"http://127.0.0.1:{port}"
        except Exception:
            pass
    raise RuntimeError("No running browser found. Make sure Multilogin profile is open.")


def main():
    cdp_url = find_cdp_url()

    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(cdp_url)
        ctx = browser.contexts[0]
        page = ctx.pages[0]

        print(f"\n[inspect] URL: {page.url}\n")

        # ── Script 1: Find all 'Website' text nodes and their DOM info ──────
        result = page.evaluate("""() => {
            const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
            const viz = (el) => !!(el && el.offsetParent !== null);
            const info = [];

            // Find ALL elements containing 'website' in text
            const all = document.querySelectorAll('*');
            for (const el of all) {
                if (!viz(el)) continue;
                const own = norm(
                    Array.from(el.childNodes)
                        .filter(n => n.nodeType === 3)
                        .map(n => n.textContent).join('')
                    || el.getAttribute('aria-label') || ''
                );
                if (own.toLowerCase() === 'website' || own.toLowerCase() === 'sitio web') {
                    const r = el.getBoundingClientRect();
                    info.push({
                        tag: el.tagName,
                        role: el.getAttribute('role'),
                        aria_checked: el.getAttribute('aria-checked'),
                        aria_selected: el.getAttribute('aria-selected'),
                        aria_label: el.getAttribute('aria-label'),
                        data_testid: el.getAttribute('data-testid'),
                        class: (el.className || '').substring(0, 60),
                        own_text: own,
                        full_text: norm(el.textContent || '').substring(0, 80),
                        x: Math.round(r.x),
                        y: Math.round(r.y),
                        w: Math.round(r.width),
                        h: Math.round(r.height),
                        parent_tag: el.parentElement ? el.parentElement.tagName : '',
                        parent_role: el.parentElement ? (el.parentElement.getAttribute('role') || '') : '',
                        parent_text: el.parentElement ? norm(el.parentElement.textContent || '').substring(0, 40) : '',
                        visible: viz(el),
                    });
                }
            }
            return info;
        }""")

        print("=== Elements with own_text='Website' ===")
        for i, item in enumerate(result):
            print(f"\n[{i}] {item['tag']} role={item['role']} aria_checked={item['aria_checked']}")
            print(f"     pos=({item['x']},{item['y']}) size={item['w']}x{item['h']} visible={item['visible']}")
            print(f"     class={item['class']}")
            print(f"     own_text={item['own_text']!r}  full_text={item['full_text']!r}")
            print(f"     parent: {item['parent_tag']} role={item['parent_role']} text={item['parent_text']!r}")

        if not result:
            print("NO elements found with own_text='Website'!")
        else:
            # ── Try clicking the largest visible one ────────────────────────
            visible = [r for r in result if r['visible'] and r['w'] > 4 and r['h'] > 4]
            if visible:
                best = max(visible, key=lambda r: r['w'] * r['h'])
                print(f"\n=== Attempting click on element [{result.index(best)}] at ({best['x']},{best['y']}) ===")

                clicked = page.evaluate("""([targetX, targetY, targetTag, targetClass]) => {
                    const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                    const viz = (el) => !!(el && el.offsetParent !== null);
                    const all = document.querySelectorAll('*');
                    for (const el of all) {
                        if (!viz(el)) continue;
                        const r = el.getBoundingClientRect();
                        if (Math.abs(r.x - targetX) > 5 || Math.abs(r.y - targetY) > 5) continue;
                        const own = norm(
                            Array.from(el.childNodes)
                                .filter(n => n.nodeType === 3)
                                .map(n => n.textContent).join('')
                            || el.getAttribute('aria-label') || ''
                        );
                        if (own.toLowerCase() !== 'website') continue;
                        el.scrollIntoView({block: 'nearest'});
                        el.click();
                        // Also try clicking parent (sometimes the row is the clickable unit)
                        const parent = el.parentElement;
                        if (parent) parent.click();
                        return {clicked: true, tag: el.tagName, text: own};
                    }
                    return {clicked: false};
                }""", [best['x'], best['y'], best['tag'], best['class']])

                print(f"Click result: {clicked}")
                time.sleep(1.5)

                # Check if Website is now selected
                selected = page.evaluate("""() => {
                    const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                    // Find Conversion location dropdown value
                    for (const lbl of document.querySelectorAll('label, span, div')) {
                        if (norm(lbl.textContent) !== 'conversion location') continue;
                        if (!lbl.offsetParent) continue;
                        let node = lbl.parentElement;
                        for (let i = 0; i < 6 && node; i++, node = node.parentElement) {
                            for (const btn of node.querySelectorAll('[role=button],[role=combobox],button')) {
                                if (!btn.offsetParent) continue;
                                const ownText = norm(
                                    Array.from(btn.childNodes)
                                        .filter(n => n.nodeType === 3)
                                        .map(n => n.textContent).join('')
                                    || btn.getAttribute('aria-label') || btn.textContent || ''
                                );
                                if (ownText.includes('website') || ownText.includes('instant')) {
                                    return {value: ownText, website_only: ownText === 'website' || ownText === 'sitio web'};
                                }
                            }
                        }
                    }
                    return {value: 'not found', website_only: false};
                }""")
                print(f"\nConversion location value after click: {selected}")

        # ── Screenshot ──────────────────────────────────────────────────────
        shot_path = "output/rpa_screenshots/inspect_website_click.png"
        page.screenshot(path=shot_path)
        print(f"\nScreenshot saved: {shot_path}")


if __name__ == "__main__":
    main()
