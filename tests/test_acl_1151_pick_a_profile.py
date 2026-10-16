"""ACL-1151: карта подключает так же, как вкладка Accounts — выбором профиля.

Что проверяется:
* имя аккаунта и кабинет выводятся из профиля одним правилом
  (`src/web/profile_connection.py`), и его зовут оба пути — профиль,
  найденный поиском (`/mlx-profile-details`), и открытый на машине
  (`/mlx-open-profiles`, обзор аккаунтов);
* в форме нет поля «последние буквы» и кнопки Reload;
* запрос подключения один на все экраны (`aclConnectAccount`);
* пустое поле со живым токеном показывает открытые профили с выведенным,
  без токена — ничего не показывает и не спрашивает.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "src" / "web" / "templates" / "report.html"
FORM = ROOT / "src" / "web" / "templates" / "_account_map_add.html"

from src.web import profile_connection as pc  # noqa: E402


# ── правило вывода имени ───────────────────────────────────────────────────

LABELS = [
    "FacebookMKTOBFLX84 SV Ait",
    "FacebookMKTOBFLX84",          # то же, коротким именем из статистики
    "FacebookDQ501 - Duan Guyen",
    "FacebookElf403",
    "FacebookOther403",            # второй аккаунт с ключом 403 у другой буквы
    "FacebookAB12",
    "FacebookCD12",                # ключ B12/D12 различается — не спор
]


def test_the_profile_tail_names_its_account():
    assert pc.suggest_account_name("KLD_BFL_X84", LABELS) == "FacebookMKTOBFLX84 SV Ait"


def test_a_person_after_the_name_does_not_hide_the_key():
    assert pc.suggest_account_name("KLD_DQ501", LABELS) == "FacebookDQ501 - Duan Guyen"


def test_two_accounts_with_the_same_key_are_not_guessed():
    labels = ["FacebookMKTOBFLX84", "FacebookOtherX84"]
    assert pc.suggest_account_name("KLD_BFL_X84", labels) == ""


def test_a_profile_without_a_key_gets_no_name():
    assert pc.suggest_account_name("my browser", LABELS) == ""
    assert pc.suggest_account_name("", LABELS) == ""


def test_derive_reads_the_profile_from_disk_only(monkeypatch):
    from src.fb_ads_rpa import mlx_profiles

    monkeypatch.setattr(mlx_profiles, "ad_account_id_for", lambda pid: "act_84" if pid == "p84" else "")
    monkeypatch.setattr(mlx_profiles, "folder_id_for", lambda pid: "f84" if pid == "p84" else "")
    got = pc.derive("p84", "KLD_BFL_X84", LABELS)
    assert got == {"ad_account_id": "act_84", "folder_id": "f84", "opened_here": True,
                   "suggested_name": "FacebookMKTOBFLX84 SV Ait"}
    assert pc.derive("new", "KLD_BFL_X84", LABELS)["opened_here"] is False


def test_known_labels_join_the_map_and_what_the_overview_read(monkeypatch, tmp_path):
    path = tmp_path / "ad_account_map.csv"
    path.write_text("account_name,ad_account_id\nFacebookElf403,act_1\n", encoding="utf-8")
    import config.paths

    monkeypatch.setattr(config.paths, "get_ad_account_map_path", lambda: str(path))
    monkeypatch.setattr(pc, "_remembered", set())
    pc.remember_labels(["FacebookMKTOBFLX84 SV Ait", "", "  "])
    assert pc.known_labels(["FacebookDQ501"]) == [
        "FacebookElf403", "FacebookMKTOBFLX84 SV Ait", "FacebookDQ501"]


# ── оба пути зовут одно правило ────────────────────────────────────────────

def _body(resp):
    return json.loads(resp.body)


def test_search_and_open_profiles_derive_the_same_way(monkeypatch, tmp_path):
    from src.fb_ads_rpa import mlx_profiles
    from src.web import app as web

    path = tmp_path / "ad_account_map.csv"
    path.write_text("account_name,ad_account_id,profile_id\nFacebookElf403,act_1,p403\n", encoding="utf-8")
    import config.paths

    monkeypatch.setattr(config.paths, "get_ad_account_map_path", lambda: str(path))
    monkeypatch.setattr(web, "get_ad_account_map_path", lambda: str(path))
    monkeypatch.setattr(pc, "_remembered", set())
    pc.remember_labels(["FacebookMKTOBFLX84 SV Ait"])
    monkeypatch.setattr(mlx_profiles, "ad_account_id_for", lambda pid: {"p84": "act_84"}.get(pid, ""))
    monkeypatch.setattr(mlx_profiles, "folder_id_for", lambda pid: "f-" + pid)
    monkeypatch.setattr(mlx_profiles, "open_profiles_detailed", lambda: [
        {"profile_id": "p84", "name": "KLD_BFL_X84", "folder_id": ""},
        {"profile_id": "p403", "name": "KLD_Elf403", "folder_id": "fold"},
    ])

    found = _body(asyncio.run(web.mlx_profile_details(profile_id="p84", name="KLD_BFL_X84")))
    listed = _body(asyncio.run(web.mlx_open_profiles()))
    assert found["ok"] and listed["ok"]
    by_id = {p["profile_id"]: p for p in listed["profiles"]}
    assert found["suggested_name"] == by_id["p84"]["suggested_name"] == "FacebookMKTOBFLX84 SV Ait"
    assert found["ad_account_id"] == by_id["p84"]["ad_account_id"] == "act_84"
    assert by_id["p84"]["folder_id"] == "f-p84"
    assert by_id["p403"] == {"profile_id": "p403", "name": "KLD_Elf403", "folder_id": "fold",
                             "ad_account_id": "", "suggested_name": "FacebookElf403",
                             "taken_by": "FacebookElf403"}


def test_the_old_server_guess_is_gone():
    source = (ROOT / "src" / "web" / "app.py").read_text(encoding="utf-8")
    assert "_account_name_from_profile" not in source, "вторая формула имени вернулась"
    overview = source[source.index('"open_profiles": [\n'):]
    overview = overview[: overview.index("for p in open_detail")]
    assert "suggest_account_name" in overview, "обзор выводит имя не общим правилом"


# ── форма ──────────────────────────────────────────────────────────────────

def test_the_form_has_no_last_letters_field_and_no_reload():
    form = FORM.read_text(encoding="utf-8")
    markup = "\n".join(l for l in form.splitlines() if not l.strip().startswith(("{#", "«", "Accounts")))
    assert "last letters" not in form
    assert ">Reload<" not in form and "amapFind(" not in form
    assert 'placeholder="pick a profile' in markup
    assert "function amapFind" not in REPORT.read_text(encoding="utf-8")


def test_the_tab_has_no_name_formula_of_its_own():
    page = REPORT.read_text(encoding="utf-8")
    assert "function tailKey" not in page
    fn = page[page.index("function accountForProfile"):]
    fn = fn[: fn.index("\n  }\n")]
    assert "suggested_name" in fn


def test_one_connect_request_for_every_screen():
    page = REPORT.read_text(encoding="utf-8")
    shared = page[page.index("async function aclConnectAccount"):page.index("function amapAddThisCountry(")]
    assert "'/account-map-patch'" in shared
    for name in ("async function amapConnect", "function addManual", "function save(btn)",
                 "function addFromCountry"):
        body = page[page.index(name):]
        body = body[: body.index("\n  }\n") if name != "async function amapConnect" else body.index("\n}\n")]
        assert "/account-map-patch" not in body, f"{name} шлёт свой запрос"
        assert "aclConnectAccount(" in body, f"{name} не зовёт общий запрос"


# ── в браузере ─────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def browser():
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=os.environ.get("ACL_BROWSER_EXECUTABLE") or None)
        yield b
        b.close()


OPEN = [
    {"profile_id": "p84", "name": "KLD_BFL_X84", "folder_id": "f84", "ad_account_id": "act_84",
     "suggested_name": "FacebookMKTOBFLX84 SV Ait", "taken_by": ""},
    {"profile_id": "p9", "name": "KLD_NEW_Q9", "folder_id": "f9", "ad_account_id": "",
     "suggested_name": "", "taken_by": ""},
]


def _run(browser, *, alive: bool):
    page_src = REPORT.read_text(encoding="utf-8")
    script = page_src[page_src.index("var _mlxCache ="):page_src.index("function amapAddThisCountry")]
    html = """<!doctype html><html><body>
<div class="amap-add" data-amap-country="">
  <select class="amap-inst"><option value="A" selected>A</option></select>
  <input type="text" class="amap-find" onfocus="amapFilter(this)" oninput="amapFilter(this)">
  <span class="amap-msg"></span><div class="amap-hits" hidden></div>
</div>
<script>
var ACL_MLX_LABELS = {A: 'Inhouse', B: 'KLD'};
function aclEscHtml(s) { return String(s).replace(/[&<>"']/g, function (c) { return '&#' + c.charCodeAt(0) + ';'; }); }
%s
AMAP_ML_STATE.A = {alive: %s};
</script></body></html>""" % (script, "true" if alive else "false")
    ctx = browser.new_context()
    page = ctx.new_page()
    calls, errors = [], []
    page.on("pageerror", lambda e: errors.append(str(e)))

    def route(r):
        from urllib.parse import urlsplit
        path = urlsplit(r.request.url).path
        if path == "/":
            return r.fulfill(body=html, content_type="text/html")
        calls.append(path)
        if path == "/mlx-open-profiles":
            return r.fulfill(body=json.dumps({"ok": True, "profiles": OPEN}), content_type="application/json")
        r.abort()

    ctx.route("**/*", route)
    page.goto("http://acl.test/")
    page.locator(".amap-find").focus()
    return ctx, page, calls, errors


def test_an_empty_field_lists_open_profiles_with_what_was_worked_out(browser):
    ctx, page, calls, errors = _run(browser, alive=True)
    try:
        page.wait_for_function("document.querySelectorAll('.amap-hit').length === 2")
        rows = page.locator(".amap-hit")
        first = rows.nth(0).inner_text()
        assert "KLD_BFL_X84" in first and "FacebookMKTOBFLX84 SV Ait" in first and "act_84" in first
        second = rows.nth(1).inner_text()
        assert "no matching account name" in second and "never opened here" in second
        assert "2 open on this machine" in page.locator(".amap-msg").inner_text()
        assert calls == ["/mlx-open-profiles"], "у выведенного профиля не нужен повторный /mlx-profile-details"
        assert rows.nth(0).get_by_role("button", name="Connect").count() == 1
        assert not errors
    finally:
        ctx.close()


def test_without_a_token_the_profile_list_stays_empty(browser):
    ctx, page, calls, errors = _run(browser, alive=False)
    try:
        page.wait_for_function("document.querySelector('.amap-msg').textContent.includes('Connect Multilogin')")
        assert page.locator(".amap-hit").count() == 0
        assert "/mlx-open-profiles" not in calls
        assert not errors
    finally:
        ctx.close()
