"""ACL-2557: Home «Open profile» — проводка без браузера. Meta и Multilogin не трогаются."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.web import account_links as links  # noqa: E402
from tests import _home_overview_site as site  # noqa: E402

HOME = (ROOT / "src" / "web" / "templates" / "_home_accounts.html").read_text(encoding="utf-8")
REPORT = (ROOT / "src" / "web" / "templates" / "report.html").read_text(encoding="utf-8")
WARM_JS = (ROOT / "src" / "web" / "static" / "warm_sessions.js").read_text(encoding="utf-8")
APP = (ROOT / "src" / "web" / "app.py").read_text(encoding="utf-8")

MAP_CSV = ("account_name,ad_account_id,page_id,multilogin_instance,folder_id,profile_id,proxy_type,proxy_host,proxy_port,proxy_user,proxy_pass\n"
           "Facebook_DQ-214,act_1,1,A,f,prof-214,,,,,\n"
           "Facebook_DQ-217,act_2,2,A,f,,,,,,\n")


def build(monkeypatch, tmp_path):
    from src.web import app as web

    csv = tmp_path / "map.csv"
    csv.write_text(MAP_CSV, encoding="utf-8")
    monkeypatch.setattr(links, "get_local_config_dir", lambda: str(tmp_path))
    monkeypatch.setattr(links, "get_ad_account_map_path", lambda: str(csv))
    monkeypatch.setattr("src.web.app.get_ad_account_map_path", lambda: str(csv))
    spec = {"Colombia": [("Facebook_DQ-214", 117.0, None, "", 0), ("Facebook_DQ-217", 5.0, None, "", 0)]}
    return site.build_amap(web, links, monkeypatch, spec), web


def test_rows_know_whether_the_account_has_a_profile(monkeypatch, tmp_path):
    amap, _web = build(monkeypatch, tmp_path)
    rows = {r["account"]: r for mc in amap["countries"] for r in mc["rows"]}
    assert rows["Facebook_DQ-214"]["has_profile"] is True
    assert rows["Facebook_DQ-217"]["has_profile"] is False


def test_button_is_rendered_only_for_accounts_with_a_profile(monkeypatch, tmp_path):
    amap, _web = build(monkeypatch, tmp_path)
    html = site.page_html(amap)
    assert html.count('data-aov-open="Facebook_DQ-214"') == 1
    assert 'data-aov-open="Facebook_DQ-217"' not in html
    assert html.count('data-profile="1"') == 1             # плитка Compact: только у аккаунта с профилем
    assert "hidden>Open profile" in html                   # кнопка скрыта, пока страница не узнала про API


def test_home_reuses_the_manage_open_path_without_a_new_route():
    # тот же POST /warm/open с confirm_reopen, никаких новых маршрутов под Home
    assert "post('/warm/open', { accounts: [account], confirm_reopen: confirmed === true })" in WARM_JS
    assert "'/warm/job/'" in WARM_JS and "open_by_hand" in WARM_JS
    assert "/home/open" not in APP and "/home/open" not in WARM_JS
    # поповер плитки получает кнопку только для плитки с профилем
    assert "if (d.profile)" in REPORT and "data-aov-open" in REPORT
    assert "data-aov-open" in HOME and "{% if r.has_profile %}" in HOME


def test_home_open_has_no_timers_on_live_accounts():
    # опрашивается только локальное: задание открытия и очередь чтения; открытия сами по себе нет
    block = WARM_JS[WARM_JS.index("ACL-2557"): WARM_JS.index("addStyle();")]
    assert "setInterval" not in block
    assert block.count("fetch(") == 4                       # /warm/state, /warm/job, /account-refresh/state, /account-map
    for url in ("/warm/state", "/warm/job/", "/account-refresh/state", "/account-map?"):
        assert url in block
