"""ACL-2560: Home «Take out of <country>» и выровненные кнопки — проводка без браузера."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

HOME = (ROOT / "src" / "web" / "templates" / "_home_accounts.html").read_text(encoding="utf-8")
REPORT = (ROOT / "src" / "web" / "templates" / "report.html").read_text(encoding="utf-8")
WARM_JS = (ROOT / "src" / "web" / "static" / "warm_sessions.js").read_text(encoding="utf-8")
APP = (ROOT / "src" / "web" / "app.py").read_text(encoding="utf-8")


def test_every_row_and_tile_carries_the_takeout_button():
    # строка Expanded: кнопка не зависит от профиля (стоит вне {% if r.has_profile %})
    row = HOME[HOME.index('<span class="aov-accrow">'): HOME.index("</td>", HOME.index('<span class="aov-accrow">'))]
    assert row.count("data-aov-takeout") == 1 and row.count("{% if r.has_profile %}") == 2
    assert row.index("data-aov-open=") < row.index("data-aov-takeout") < row.index("aov-open-msg")
    assert 'data-aov-ckey="{{ (mc.key or mc.country) | e }}"' in row
    # плитка Compact отдаёт ключ страны поповеру, поповер строит кнопку всем
    assert 'data-ckey="{{ (mc.key or mc.country) | e }}"' in HOME
    assert "data-aov-takeout" in REPORT and "'Take out of ' + d.country" in REPORT


def test_takeout_reuses_amap_unlink_and_the_account_links_route():
    assert "window.amapUnlink(" in WARM_JS and "'country'" in WARM_JS
    assert "amapRemoveFromAcl" not in WARM_JS and "amapRemoveFromAcl" not in HOME
    # Home сам не шлёт запрос: подтверждение и POST /account-links остаются в amapUnlink/amapSend
    block = WARM_JS[WARM_JS.index("ACL-2560"): WARM_JS.index("addStyle();")]
    assert "/account-links" not in block and "unlink" not in block.replace("amapUnlink", "")
    assert "account-links" in REPORT[REPORT.index("async function amapSend"): REPORT.index("async function amapUnlink")]
    # новых маршрутов нет
    assert "takeout" not in APP.lower() and "take-out" not in APP.lower()


def test_amap_send_reloads_unless_the_caller_repaints():
    send = REPORT[REPORT.index("async function amapSend"): REPORT.index("/* ACL-1620 notes")]
    assert "async function amapSend(body, msgEl, errorAnchor, onOk)" in send
    assert "if (typeof onOk === 'function') { await onOk(); return true; }" in send
    assert "location.reload(); return true;" in send               # Manage остаётся как был
    assert "return amapSend(body, msgEl, errorAnchor, onOk);" in send   # подтверждение «тратил сегодня» не теряет onOk
    unlink = REPORT[REPORT.index("async function amapUnlink"): REPORT.index("async function amapRemoveFromAcl")]
    assert "Take ' + account + ' out of ' + value + '?'" in unlink
    assert "anchor, onOk)" in unlink


def test_actions_are_one_stack_with_a_fixed_name_column():
    assert ".aov-table td.acc .aov-acc { display: flex; flex-direction: column; flex: 0 0 11rem" in REPORT
    assert ".aov-acts{display:flex;flex-direction:column;align-items:flex-start" in WARM_JS
    assert "danger aov-takeout" in HOME
