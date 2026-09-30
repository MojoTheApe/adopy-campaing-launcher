# -*- coding: utf-8 -*-
"""Второе и третье объявления в новом редакторе (ACL-1353, шаг 2).

Страница собрана из живой вырезки
`tests/fixtures/ads_manager/simple_create_more_ads_2026_09_30.html`
(f9b9fc8d7bb3, Facebook_1443, 30.09): дерево до нажатия, открытое «Action
menu» строки группы, дерево после «Create ad» (вторая строка «New Leads
Ad»), раздел имени объявления 1 и нового объявления, Media нового.

Синтетическое здесь только поведение Meta между снимками: нажатие «Action
menu» показывает снятое меню, нажатие «Create ad» — окно на ~1 с (живьём оно
было на экране через ~7 с и исчезло к ~13 с, текста в снимках нет), затем
дерево и раздел имени сменяются снятыми «после».
"""
from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import meta_dom as md  # noqa: E402
from src.fb_ads_rpa import page_capture as pc  # noqa: E402
from src.fb_ads_rpa import simple_create as sc  # noqa: E402

_RAW = (ROOT / "tests" / "fixtures" / "ads_manager" / "simple_create_more_ads_2026_09_30.html").read_text(
    encoding="utf-8")


def _fragments(raw: str) -> dict[str, str]:
    parts = re.split(r"<!-- fragment: (\w+) \|[^>]*-->\n", raw)
    return {parts[i]: parts[i + 1].strip() for i in range(1, len(parts), 2)}


FRAG = _fragments(_RAW)

SIMPLE_URL = ("https://adsmanager.facebook.com/adsmanager/manage/simple_create"
              "?act=1&selected_campaign_ids=2&selected_adset_ids=3&selected_ad_ids=4")

CREATE_SURFACE = "/am/editor_drawer/lib:campaign-structure-item-action-menu-create"
ADSET_MENU = "editor_tree:adset/lib:ads-campaign-structure-action-menu-item"

# Поведение Meta между снимками. window.clicks — «surface|надпись» каждого
# нажатия; window.mode — сценарий «Create ad».
_SCRIPT = """
<style>body { margin: 0; } #tree { width: 300px; } #menu-host [role=menu] { position: fixed;
  left: 320px; top: 120px; width: 260px; background: #fff; z-index: 5; }</style>
<script>
  window.clicks = [];
  window.mode = 'ok';
  const FR = %FRAGS%;
  const norm = (s) => (s || '').replace(/[\\s\\u200b]+/g, ' ').trim();
  document.addEventListener('click', (e) => {
    const t = e.target.closest('[role=button], [role=menuitem], button, label, input, [role=radio]');
    if (!t) return;
    window.clicks.push((t.getAttribute('data-surface') || '') + '|'
                       + (norm(t.getAttribute('aria-label')) || norm(t.textContent)).slice(0, 40));
  }, true);
  const swap = (id, html) => { document.getElementById(id).innerHTML = html; };
  document.addEventListener('click', (e) => {
    if (e.target.closest('[data-surface*="%ADSET%"]') && !document.querySelector('[role=menu]')) {
      swap('menu-host', FR.adset_menu);
      if (window.mode === 'covered') {
        const pub = document.createElement('div');
        pub.setAttribute('role', 'button');
        pub.setAttribute('data-surface', '/am/lib:syd_simple_create_publish');
        pub.style.cssText = 'position:fixed; left:0; right:0; top:100px; height:400px; background:#eee; z-index:9';
        pub.textContent = 'Publish';
        document.body.appendChild(pub);
      }
      return;
    }
    if (e.target.closest('[data-surface="%CREATE%"]')) {
      swap('menu-host', '');
      const dlg = document.createElement('div');
      dlg.setAttribute('role', 'dialog');
      dlg.textContent = 'Loading creation';
      document.body.appendChild(dlg);
      window.creates = (window.creates || 0) + 1;
      setTimeout(() => {
        if (window.mode === 'late_close') setTimeout(() => dlg.remove(), 2000);
        else if (window.mode !== 'stuck_dialog') dlg.remove();
        if (window.mode === 'no_row') return;
        const tree = document.getElementById('tree');
        const rows = [...tree.querySelectorAll('[role=row]')];
        // Новая строка — снятая «New Leads Ad» (последняя строка дерева «после»).
        const after = document.createElement('div');
        after.innerHTML = FR.tree_after_create;
        const fresh = [...after.querySelectorAll('[role=row]')].pop();
        const copies = window.mode === 'two_rows' ? 2 : 1;
        for (let i = 0; i < copies; i++) rows[rows.length - 1].parentElement.appendChild(fresh.cloneNode(true));
        if (window.mode !== 'not_open') {
          swap('name-host', window.mode === 'empty_name'
               ? FR.new_ad_name.replace('value="New Leads Ad"', 'value=""') : FR.new_ad_name);
          swap('media-host', FR.new_ad_media);
        }
      }, 1000);
    }
  }, true);
  // Имя, набранное в поле открытого объявления, Meta переносит в его строку дерева.
  document.addEventListener('input', (e) => {
    if (!e.target.matches('input[placeholder="Enter your ad name here..."]')) return;
    const rows = document.querySelectorAll('#tree [role=rowheader][aria-label="Ad"]');
    const row = rows[rows.length - 1];
    row.querySelector('[id^="default-button-for-action-menu_"] span').textContent = e.target.value;
  }, true);
</script>
"""


def _html() -> str:
    import json

    script = (_SCRIPT.replace("%FRAGS%", json.dumps(FRAG)).replace("%CREATE%", CREATE_SURFACE)
              .replace("%ADSET%", ADSET_MENU))
    body = ('<div id="tree">' + FRAG["tree_before"] + '</div>'
            '<span data-surface="/am/editor:ad"><div id="name-host">' + FRAG["ad1_name"] + '</div>'
            '<div id="media-host"></div></span><div id="menu-host"></div>')
    return "<!doctype html><html><head>" + script + "</head><body>" + body + "</body></html>"


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=os.environ.get("ACL_BROWSER_EXECUTABLE") or None)
        try:
            yield b
        finally:
            b.close()


@pytest.fixture
def env(browser, monkeypatch, tmp_path):
    rows: list[tuple] = []
    monkeypatch.setattr(md, "_sleep_h", lambda s, *a, **k: time.sleep(min(float(s), 0.1)))
    monkeypatch.setattr(md, "_human_jitter", lambda base, spread: 0.0)
    monkeypatch.setattr(md, "wait_editor_saved", lambda p, **k: None)
    monkeypatch.setattr(md._telemetry, "record",
                        lambda step, ok=None, **kw: rows.append((step, ok, kw.get("note", ""), kw.get("diag"))))
    monkeypatch.setattr(pc, "_root", lambda: tmp_path)
    monkeypatch.setattr(pc, "_taken", {})
    pc.forget_sections()
    ctx = browser.new_context(viewport={"width": 1280, "height": 800})
    page = ctx.new_page()
    html = _html()
    page.route("**/*", lambda route: route.fulfill(status=200, content_type="text/html", body=html)
               if route.request.url.startswith("https://adsmanager.facebook.com/") else route.abort())
    page.goto(SIMPLE_URL)

    def mode(m: str) -> None:
        page.evaluate("(m) => { window.mode = m; }", m)

    try:
        yield {"page": page, "rows": rows, "dir": tmp_path, "mode": mode}
    finally:
        pc.forget_sections()
        ctx.close()


def _clicks(page) -> list[str]:
    return page.evaluate("window.clicks")


def _forbidden(clicks: list[str]) -> list[str]:
    bad = ("publish", "duplicate", "delete", "discard", "default", "close", "copy", "paste",
           "multiple", "rule")
    return [c for c in clicks if any(w in c.lower().split("|", 1)[1] for w in bad)
            or "syd_simple_create_publish" in c]


def test_the_fixture_is_the_live_capture():
    """Вырезка — то, что снял шаг 1: меню группы с «Create ad», дерево с
    одним и с двумя объявлениями, новое объявление открыто под «New Leads Ad»."""
    assert set(FRAG) == {"tree_before", "adset_menu", "tree_after_create", "ad1_name",
                         "new_ad_name", "new_ad_media"}
    assert FRAG["tree_before"].count('aria-label="Ad"') == 1
    assert FRAG["tree_after_create"].count('aria-label="Ad"') == 2
    assert "New Leads Ad" in FRAG["tree_after_create"] and "New Leads Ad" not in FRAG["tree_before"]
    assert f'data-surface="{CREATE_SURFACE}"' in FRAG["adset_menu"]
    assert "Actions for this ad set" in FRAG["adset_menu"]
    assert 'value="New Leads Ad"' in FRAG["new_ad_name"] and "Set up creative" in FRAG["new_ad_media"]
    # Вычищено: ни номеров объектов Meta, ни act=, ни адресов.
    assert not re.search(r"\b1[0-9]{17}\b|act=|https?://|js_\d", _RAW)


def test_create_ad_from_the_ad_set_menu_opens_the_new_ad(env):
    page = env["page"]
    got = sc.create_next_ad(page, tag="ad2", known_names=["Sample Ad 01"])
    clicks = _clicks(page)
    assert got["ok"], got
    assert got["why"] == "" and got["text"] == ""
    assert got["pressed"] == ["adset_action_menu", "menu_create_ad"]
    assert got["ads_before"] == 1 and got["ads_after"] == 2 and got["extra_shells"] == 0
    # Ровно два нажатия: «Action menu» строки группы и пункт «Create ad».
    assert len(clicks) == 2, clicks
    assert ADSET_MENU in clicks[0]
    assert clicks[1] == CREATE_SURFACE + "|Create ad"
    assert _forbidden(clicks) == []
    assert page.evaluate("window.creates") == 1
    # Окно после «Create ad» названо, ждали, пока уйдёт.
    assert got["dialog"]["text"] == "Loading creation" and got["dialog"]["still_open"] is False
    # Меню без номера объекта в телеметрии.
    assert "ID: # Copy" in got["menu"]["labels"] or "ID:" in " ".join(got["menu"]["labels"])
    assert not any(re.search(r"\d{5,}", lab) for lab in got["menu"]["labels"])
    assert "Create ad" in got["menu"]["labels"] and "Duplicate" in got["menu"]["labels"]
    row = [r for r in env["rows"] if r[0] == "simple_create_more_ads"]
    assert len(row) == 1 and row[0][1] is True and row[0][2] == "created"
    assert got["capture"] == "", "снимок снимают только при отказе"


def test_the_new_ad_is_found_in_the_list_by_its_name(env):
    page = env["page"]
    assert sc.create_next_ad(page, tag="ad2", known_names=["Sample Ad 01"])["ok"]
    page.get_by_placeholder("Enter your ad name here...").fill("Sample Ad 02")
    assert sc.ad_listed(page, "Sample Ad 02", wait_s=1) == {"listed": True, "ads": 2, "error": None}
    assert sc.ad_listed(page, "Sample Ad 03", wait_s=0.5)["listed"] is False


def test_a_third_ad_is_created_after_the_second(env):
    page = env["page"]
    assert sc.create_next_ad(page, tag="ad2", known_names=["Sample Ad 01"])["ok"]
    page.get_by_placeholder("Enter your ad name here...").fill("Sample Ad 02")
    # Объявление 2 названо — в поле его имя, «New Leads Ad» для 3-го снова новое.
    page.evaluate("""() => { const i = document.querySelector('#name-host input');
                             i.setAttribute('value', 'Sample Ad 02'); }""")
    got = sc.create_next_ad(page, tag="ad3", known_names=["Sample Ad 01", "Sample Ad 02"])
    assert got["ok"], got
    assert got["ads_before"] == 2 and got["ads_after"] == 3
    assert page.evaluate("window.creates") == 2


def test_no_new_row_is_a_named_failure_with_a_capture(env):
    env["mode"]("no_row")
    got = sc.create_next_ad(env["page"], tag="ad2", known_names=["Sample Ad 01"], wait_s=2.5)
    assert not got["ok"] and got["why"] == "no_new_ad_row"
    assert got["text"].startswith("Create ad did not add an ad to the ad set within")
    assert got["capture"] and got["capture"].endswith("_simple_create_more_ads")
    row = [r for r in env["rows"] if r[0] == "simple_create_more_ads"][0]
    assert row[1] is False and row[2] == "no_new_ad_row"


def test_a_new_row_that_is_not_opened_is_named(env):
    """Строка добавилась, а раздел объявления всё ещё показывает объявление 1 —
    заполнять нельзя: имя и креатив легли бы в объявление 1."""
    env["mode"]("not_open")
    got = sc.create_next_ad(env["page"], tag="ad2", known_names=["Sample Ad 01"], wait_s=2.5)
    assert not got["ok"] and got["why"] == "new_ad_not_open"
    assert "fill it in or delete it by hand" in got["text"]
    assert got["ads_after"] == 2


def test_two_rows_from_one_create_are_counted(env):
    env["mode"]("two_rows")
    got = sc.create_next_ad(env["page"], tag="ad2", known_names=["Sample Ad 01"])
    assert got["ok"] and got["extra_shells"] == 1


def test_create_ad_under_publish_is_not_pressed(env):
    env["mode"]("covered")
    got = sc.create_next_ad(env["page"], tag="ad2", known_names=["Sample Ad 01"], wait_s=1)
    clicks = _clicks(env["page"])
    assert got["why"] == "create_ad_refused"
    assert "never presses" in got["text"]
    assert _forbidden(clicks) == [] and len(clicks) == 1, clicks
    assert env["page"].evaluate("window.creates || 0") == 0
    assert ("click_refused", False) in [(r[0], r[1]) for r in env["rows"]]


def test_the_ad_setup_create_ad_radio_is_not_the_control(env):
    """«Create ad» в разделе объявления — радио «Ad setup», не добавление."""
    page = env["page"]
    page.evaluate("""() => {
      const r = document.createElement('label');
      r.innerHTML = '<input type="radio" role="radio" id="setup_create" checked>Create ad';
      document.querySelector('[data-surface="/am/editor:ad"]').appendChild(r);
    }""")
    got = sc.create_next_ad(page, tag="ad2", known_names=["Sample Ad 01"])
    assert got["ok"] and got["pressed"] == ["adset_action_menu", "menu_create_ad"]
    assert not [c for c in _clicks(page) if c.startswith("|Create ad")]


def test_a_disabled_menu_item_is_not_create_ad(env):
    """«Create multiple ads» (disabled) и «Create rule» — не «Create ad»."""
    page = env["page"]
    page.evaluate("""(s) => { window.FRX = s; }""", CREATE_SURFACE)
    page.evaluate("""() => {
      document.addEventListener('click', () => setTimeout(() => {
        const it = document.querySelector('[data-surface="' + window.FRX + '"]');
        if (it) it.setAttribute('aria-disabled', 'true');
      }, 0), true);
    }""")
    got = sc.create_next_ad(page, tag="ad2", known_names=["Sample Ad 01"], wait_s=1)
    assert got["why"] == "no_create_item", got
    assert len(_clicks(page)) == 1


def test_no_ad_list_is_named(env):
    env["page"].evaluate("document.getElementById('tree').remove()")
    got = sc.create_next_ad(env["page"], tag="ad2", known_names=["Sample Ad 01"])
    assert got["why"] == "no_ad_list" and got["pressed"] == []
    assert got["text"] == "the ad list of the single-screen editor was not found"


def test_the_sentry_section_is_the_tree_and_the_open_menu(env):
    """Раздел для события Sentry — дерево и меню, а не карточка с фокусом."""
    page = env["page"]
    page.locator(f'[data-surface*="{ADSET_MENU}"]').first.click()
    parts = pc._snapshot_take(page, pc.section_titles_for("simple_create_more_ads"),
                              tree=pc.section_is_tree("simple_create_more_ads"))
    assert [p["name"] for p in parts][:2] == ["editor tree", "menu"]
    assert "editor_tree:adset" in parts[0]["html"] and "Create ad" in parts[1]["html"]


def test_a_dialog_that_never_closes_is_named_and_the_ad_is_not_filled(env):
    """Ревью PR #365, F1: строка и новое имя есть, но окно после «Create ad»
    не ушло — неизвестно, чего оно ждёт; объявление не считается готовым."""
    env["mode"]("stuck_dialog")
    got = sc.create_next_ad(env["page"], tag="ad2", known_names=["Sample Ad 01"], wait_s=3)
    assert not got["ok"] and got["why"] == "dialog_open", got
    assert got["ads_after"] == 2
    assert got["dialog"]["text"] == "Loading creation" and got["dialog"]["still_open"] is True
    assert got["text"].startswith("a Meta dialog stayed open for")
    assert got["text"].endswith('(dialog: "Loading creation")')
    assert got["capture"], "снимок окна не сохранён"
    assert len(_clicks(env["page"])) == 2, "после окна что-то нажимали"
    row = [r for r in env["rows"] if r[0] == "simple_create_more_ads"][0]
    assert row[1] is False and row[2] == "dialog_open"


def test_an_empty_ad_name_is_not_the_new_ad(env):
    """Ревью PR #365, F3: пустое поле имени — раздел не дорисован, а не новое
    объявление."""
    env["mode"]("empty_name")
    got = sc.create_next_ad(env["page"], tag="ad2", known_names=["Sample Ad 01"], wait_s=2.5)
    assert not got["ok"] and got["why"] == "new_ad_not_open"


def test_the_new_name_must_hold_for_two_reads(monkeypatch):
    """Ревью PR #365, F3: имя принимается, только когда оно одинаково два
    опроса подряд."""
    reads = iter([
        {"ads": 2, "name_value": "New Leads Ad", "dialog": None},
        {"ads": 2, "name_value": "Sample Ad 01", "dialog": None},
        {"ads": 2, "name_value": "New Leads Ad", "dialog": None},
        {"ads": 2, "name_value": "New Leads Ad", "dialog": None},
    ])
    seen: list = []

    def read(page):
        got = next(reads)
        seen.append(got["name_value"])
        return dict(got)

    monkeypatch.setattr(sc, "_read_ads", read)
    monkeypatch.setattr(md, "_sleep_h", lambda *a, **k: None)
    monkeypatch.setattr(sc.time, "sleep", lambda s: None)
    got = sc._wait_new_ad(None, 1, ["Sample Ad 01"], wait_s=30)
    assert got["name_stable"] is True and len(seen) == 4


def test_one_unanswered_read_after_creating_is_not_a_frozen_tab(monkeypatch):
    """Sentry PYTHON-4D (30.09, 1443, v96+6): «Creating...» ушло за 2,1 с, Meta
    пересобирала редактор, одна проверка (3 с) не дождалась ответа — и шаг
    упал `tab_frozen`, хотя снимок показал вторую строку «New Leads Ad».
    Одиночный сбой чтения — не повод бросать ожидание."""
    reads = iter([
        {"ads": 1, "name_value": "Sample Ad 01", "dialog": {"text": "Creating..."}},
        {"error": "TimeoutError"},
        {"ads": 2, "name_value": "New Leads Ad", "dialog": None},
        {"ads": 2, "name_value": "New Leads Ad", "dialog": None},
    ])
    monkeypatch.setattr(sc, "_read_ads", lambda page: dict(next(reads)))
    monkeypatch.setattr(md, "_sleep_h", lambda *a, **k: None)
    monkeypatch.setattr(sc.time, "sleep", lambda s: None)
    got = sc._wait_new_ad(object(), 1, ["Sample Ad 01"], wait_s=30)
    assert not got.get("error"), got
    assert got["ads"] == 2 and got["name_stable"] is True
    assert got["read_errors"] == 1


def test_a_tab_that_never_answers_is_still_frozen(monkeypatch):
    """Вкладка не отвечает до конца ожидания — это по-прежнему `tab_frozen`."""
    monkeypatch.setattr(sc, "_read_ads", lambda page: {"error": "TimeoutError"})
    monkeypatch.setattr(md, "_sleep_h", lambda *a, **k: None)
    got = sc._wait_new_ad(object(), 1, ["Sample Ad 01"], wait_s=0.3)
    assert got.get("error") == "TimeoutError" and got["read_errors"] >= 1


def test_a_new_ad_then_a_silent_tab_is_frozen(monkeypatch):
    """Ревью: новое объявление мелькнуло, а потом вкладка молчит до конца —
    итог `tab_frozen`, счётчик сбоев — в телеметрию шага."""
    reads = iter([{"ads": 2, "name_value": "New Leads Ad", "dialog": {"text": "Creating..."}}])
    monkeypatch.setattr(sc, "_read_ads",
                        lambda page: dict(next(reads, {"error": "TimeoutError"})))
    monkeypatch.setattr(md, "_sleep_h", lambda *a, **k: None)
    got = sc._wait_new_ad(object(), 1, ["Sample Ad 01"], wait_s=0.3)
    assert got.get("error") == "TimeoutError" and got["read_errors"] >= 1


def test_a_closed_tab_stops_the_wait_at_once(monkeypatch):
    """Ревью: закрытая вкладка — выход сразу, а не через `NEW_AD_WAIT_S`."""
    monkeypatch.setattr(sc, "_read_ads", lambda page: {"error": "TargetClosedError"})
    monkeypatch.setattr(md, "_sleep_h", lambda *a, **k: None)
    monkeypatch.setattr(md, "_tab_gone", lambda page: True)
    got = sc._wait_new_ad(object(), 1, ["Sample Ad 01"], wait_s=30)
    assert got.get("error") and got["read_errors"] == 1 and got["waited_s"] < 5


def test_the_wait_lasts_until_the_dialog_closes(env):
    """F1: строка и имя появились раньше, чем ушло окно, — ждём окно, а не
    берём объявление под ним."""
    env["mode"]("late_close")
    got = sc.create_next_ad(env["page"], tag="ad2", known_names=["Sample Ad 01"], wait_s=10)
    assert got["ok"], got
    assert got["dialog"]["still_open"] is False and got["dialog"]["until_s"] >= 1.5
