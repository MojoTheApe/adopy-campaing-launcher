"""Synthetic DOM checks, no account/browser-profile connections.

Run with ACL_BROWSER_EXECUTABLE=/path/to/chrome python tests/browser_country_split.py.
Network is blocked. This verifies our selectors, not Meta's current markup.
"""
import os
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.analysis_agent import country_split as cs

TABLE = '''<table><tr><th>Country</th><th>Amount spent</th></tr>
<tr><td>Peru</td><td>USD 5.00</td></tr>
<tr><td>Total results 1/1 rows displayed</td><td>USD 5.00</td></tr></table>'''
HIDDEN = '''<div role="grid" style="display:none"><div role="row"><span role="columnheader">Campaign name</span><span role="columnheader">Amount spent</span></div><div role="row"><span role="gridcell">KW_demo</span><span role="gridcell">$900.00</span></div></div>'''

with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True, executable_path=os.environ.get("ACL_BROWSER_EXECUTABLE") or None)
    context = browser.new_context()
    context.route("**/*", lambda route: route.abort())
    page = context.new_page()
    count = 0

    page.set_content(HIDDEN + TABLE)
    snapshot = page.evaluate(cs._GRAB_CELLS_JS)
    rows, how = cs._verified_rows(snapshot["matrix"])
    assert not snapshot["error"] and rows[0]["country"] == "Peru" and rows[0]["spend"] == 5
    count += 1

    page.set_content(TABLE + TABLE)
    assert page.evaluate(cs._GRAB_CELLS_JS)["error"]
    count += 1

    page.set_content(TABLE.replace('<table>', '<table aria-busy="true">'))
    assert page.evaluate(cs._GRAB_CELLS_JS)["error"] == "report is still loading"
    count += 1

    aria = '''<div role="grid"><div role="row"><span role="columnheader">Country</span><span role="columnheader">Amount spent</span></div><div role="row"><span role="gridcell">Chile</span><span role="gridcell">$2.00</span></div><div role="row"><span role="rowheader">Total results 1/1 rows displayed</span><span role="gridcell">$2.00</span></div></div>'''
    page.set_content(aria)
    assert cs._verified_rows(page.evaluate(cs._GRAB_CELLS_JS)["matrix"])[0][0]["country"] == "Chile"
    count += 1

    page.set_content('''<label style="display:none"><input type="checkbox">Country</label>
<label><input id="country" type="checkbox" checked>Country</label>''')
    assert page.evaluate(cs._CHECKED_JS, "Country") is True
    page.evaluate(cs._CHECKBOX_JS, {"label": "Country", "wanted": False})
    assert page.locator('#country').is_checked() is False
    count += 1

    page.set_content('''<div role="checkbox" aria-checked="mixed" aria-label="Country">Country</div>''')
    assert page.evaluate(cs._CHECKED_JS, "Country") is None
    count += 1

    page.set_content('''<label><input type="checkbox">Country</label><label><input type="checkbox">Country</label>''')
    assert page.evaluate(cs._CHECKED_JS, "Country") is None
    count += 1

    page.set_content('''<label><input type="checkbox">Country code</label><input type="checkbox" id="c" aria-label="Country">''')
    assert cs._set_checkbox(page, "Country", True)
    assert page.locator('#c').is_checked() and not page.locator('label input').is_checked()
    count += 1

    page.set_content('''<input type="checkbox" aria-labelledby="label"><span id="label">Country</span>''')
    assert page.evaluate(cs._CHECKED_JS, "Country") is False
    count += 1

    page.set_content('''<button style="display:none">Sep 9, 2026</button><button>Sep 10, 2026</button>''')
    assert cs._shown_period_text(page) == "Sep 10, 2026"
    page.set_content('''<button>Sep 9, 2026</button><button>Sep 10, 2026</button>''')
    assert cs._shown_period_text(page) == ""
    count += 1

    page.set_content('''<button hidden>Create report</button><button id="new" onclick="this.dataset.clicked='yes'">Create report</button>''')
    assert page.evaluate(cs._CLICK_LABEL_JS, "Create report")
    assert page.locator('#new').get_attribute('data-clicked') == 'yes'
    page.set_content('<button>Create report</button><button>Create report</button>')
    assert not page.evaluate(cs._CLICK_LABEL_JS, "Create report")
    count += 1


    # ── одинаковые подписи: «Create report» на пустом списке рисуется дважды
    # (живой прогон 15.09: вверху панели и в середине пустого состояния).
    page.set_content(
        '<div><button>Create report</button></div>'
        '<div><span><button>Create report</button></span></div>'
        '<button>Customize</button>'
    )
    page.evaluate("() => { window.__hits = 0; "
                  "document.querySelectorAll('button').forEach(b => "
                  "b.addEventListener('click', () => window.__hits++)) }")
    assert cs._click_label(page, "Create report", tries=1) is False, \
        "две одинаковые подписи нажались без явного разрешения"
    assert page.evaluate("() => window.__hits") == 0
    assert cs._click_label(page, "Create report", tries=1, allow_many=True) is True, \
        "с явным разрешением нажать не удалось"
    assert page.evaluate("() => window.__hits") == 1
    # Строгое правило остаётся строгим там, где его не снимали.
    assert cs._click_label(page, "Customize", tries=1) is True
    count += 3

    browser.close()
    print(f"synthetic checks passed: {count}")
