# -*- coding: utf-8 -*-
"""Второе и третье объявления в новом редакторе (ACL-1353, шаг 2).

Страница собрана из живой вырезки
`tests/fixtures/ads_manager/simple_create_more_ads_2026_09_30.html`
(f9b9fc8d7bb3, Facebook_1443, 30.09): дерево до нажатия, открытое «Action
menu» строки группы, дерево после «Create ad» (вторая строка «New Leads
Ad»), раздел имени объявления 1 и нового объявления, Media нового.

Синтетическое здесь только поведение Meta между снимками: нажатие «Action
menu» показывает снятое меню, нажатие «Create ad» — окно прогресса Meta
«Creating...» на ~1 с (`/am/lib:ads_progress_dialog_modal_8e9c`, живьём 30.09,
d66a22d0fa30), затем дерево и раздел имени сменяются снятыми «после».
Молчание вкладки, пока Meta занята, — подменой `_read_ads` (fix round 2).
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
# ACL-1412: окно выбора медиа после «Create ad» (d1755a59e139, ad3).
_PICKER = _fragments((ROOT / "tests" / "fixtures" / "ads_manager"
                      / "simple_create_new_ad_media_2026_09_30.html").read_text(encoding="utf-8"))["media_picker"]

SIMPLE_URL = ("https://adsmanager.facebook.com/adsmanager/manage/simple_create"
              "?act=1&selected_campaign_ids=2&selected_adset_ids=3&selected_ad_ids=4")

CREATE_SURFACE = "/am/editor_drawer/lib:campaign-structure-item-action-menu-create"
ADSET_MENU = "editor_tree:adset/lib:ads-campaign-structure-action-menu-item"

# Поведение Meta между снимками. window.clicks — «surface|надпись» каждого
# нажатия; window.mode — сценарий «Create ad».
_SCRIPT = """
<style>body { margin: 0; } #tree { width: 300px; } #menu-host [role=menu] { position: fixed;
  left: 320px; top: 120px; width: 260px; background: #fff; z-index: 5; }
  #picker-host > [role=dialog] { position: absolute; left: 40px; top: 40px; width: 900px;
  background: #fff; z-index: 6; }</style>
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
      // Как живьём 30.09 (d66a22d0fa30): окно прогресса Meta «Creating...».
      const dlg = document.createElement('span');
      dlg.setAttribute('data-surface', '/am/lib:ads_progress_dialog_modal_8e9c');
      dlg.innerHTML = '<div role="dialog">Creating...</div>';
      if (window.mode === 'other_dialog') {
        // ACL-1412: окно — не прогресс Meta.
        dlg.removeAttribute('data-surface');
        dlg.innerHTML = '<div role="dialog">Something went wrong</div>';
      }
      document.body.appendChild(dlg);
      window.creates = (window.creates || 0) + 1;
      setTimeout(() => {
        if (window.mode === 'late_close') setTimeout(() => dlg.remove(), 2000);
        else if (window.mode === 'picker' || window.mode === 'picker_discard') {
          // ACL-1412 (d1755a59e139, ad3): вместо «Creating...» — окно выбора медиа.
          dlg.remove();
          swap('picker-host', FR.media_picker);
        }
        else if (window.mode !== 'stuck_dialog' && window.mode !== 'other_dialog') dlg.remove();
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
  // Cancel окна выбора медиа закрывает его (или, в 'picker_discard', спрашивает).
  document.addEventListener('click', (e) => {
    const b = e.target.closest('#picker-host [role=button]');
    if (!b || norm(b.textContent) !== 'Cancel') return;
    swap('picker-host', window.mode === 'picker_discard'
         ? '<div role="dialog">Discard changes? <div role="button">Discard</div></div>' : '');
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

    script = (_SCRIPT.replace("%FRAGS%", json.dumps({**FRAG, "media_picker": _PICKER})).replace("%CREATE%", CREATE_SURFACE)
              .replace("%ADSET%", ADSET_MENU))
    body = ('<div id="tree">' + FRAG["tree_before"] + '</div>'
            '<span data-surface="/am/editor:ad"><div id="name-host">' + FRAG["ad1_name"] + '</div>'
            '<div id="media-host"></div></span><div id="menu-host"></div><div id="picker-host"></div>')
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
        yield {"page": page, "rows": rows, "dir": tmp_path, "mode": mode, "monkeypatch": monkeypatch}
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
    assert got["dialog"]["text"] == "Creating..." and got["dialog"]["still_open"] is False
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
    # Пункт гасится в том же обработчике нажатия, что показал меню (этот
    # слушатель добавлен позже слушателя страницы и срабатывает сразу после
    # него), — не таймером: `setTimeout(0)` мог сработать уже после чтения
    # меню, и «Create ad» нажимался (CI main, run 36733347515, ads_after 2).
    page.evaluate("""() => {
      document.addEventListener('click', () => {
        const it = document.querySelector('[data-surface="' + window.FRX + '"]');
        if (it) it.setAttribute('aria-disabled', 'true');
      }, true);
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
    # ACL-1412: окно прогресса ждём `META_BUSY_WAIT_S` (здесь — 4 с).
    env["monkeypatch"].setattr(sc, "META_BUSY_WAIT_S", 4.0)
    t0 = time.time()
    got = sc.create_next_ad(env["page"], tag="ad2", known_names=["Sample Ad 01"], wait_s=2)
    assert not got["ok"] and got["why"] == "dialog_open", got
    assert time.time() - t0 >= 4.0, "окно прогресса не ждали до предела"
    assert got["ads_after"] == 2
    assert got["dialog"]["text"] == "Creating..." and got["dialog"]["still_open"] is True
    assert got["dialog_kind"] == "progress" and got["progress_s"] >= 3.0
    assert got["text"].startswith("Meta's progress dialog stayed open for 4 s")
    assert got["text"].endswith('(dialog: "Creating...")')
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


def test_the_wait_lasts_until_the_dialog_closes(env):
    """F1: строка и имя появились раньше, чем ушло окно, — ждём окно, а не
    берём объявление под ним."""
    env["mode"]("late_close")
    got = sc.create_next_ad(env["page"], tag="ad2", known_names=["Sample Ad 01"], wait_s=10)
    assert got["ok"], got
    assert got["dialog"]["still_open"] is False and got["dialog"]["until_s"] >= 1.5


# ── Fix round 2 (живьём 30.09): Meta занята «Creating...», вкладка молчит ─

def _silence_after_dialog(monkeypatch, silent_s: float) -> dict:
    """После первого чтения с окном прогресса `_read_ads` не отвечает
    `silent_s` секунд (как `_eval_bounded` на занятой вкладке)."""
    real = sc._read_ads
    state: dict = {"since": None, "errors": 0}

    def read(page):
        if state["since"] is not None and time.time() - state["since"] < silent_s:
            state["errors"] += 1
            time.sleep(0.2)
            return {"error": "TimeoutError"}
        got = real(page)
        if state["since"] is None and sc.is_progress_dialog(got.get("dialog")):
            state["since"] = time.time()
        return got

    monkeypatch.setattr(sc, "_read_ads", read)
    return state


def test_the_live_progress_dialog_is_recognised():
    assert sc.is_progress_dialog({"text": "Creating...", "surface": "/am/lib:ads_progress_dialog_modal_8e9c"})
    assert sc.is_progress_dialog({"text": "Creating...", "surface": ""})
    assert sc.is_progress_dialog({"text": "", "surface": "/am/lib:ads_progress_dialog_modal_#"})
    assert not sc.is_progress_dialog({"text": "Turn off ad blocker", "surface": "/am/lib:x"})
    assert not sc.is_progress_dialog({"text": "Create ad", "surface": ""})
    assert not sc.is_progress_dialog(None)


def test_a_busy_creating_dialog_is_waited_out(env, monkeypatch):
    """Страница не отвечала 2 с при «Creating...» — это «Meta занята»: ждём,
    объявление создано и открыто, время занятости — в итоге шага."""
    monkeypatch.setattr(sc, "META_BUSY_WAIT_S", 8.0)
    state = _silence_after_dialog(monkeypatch, 2.0)
    got = sc.create_next_ad(env["page"], tag="ad2", known_names=["Sample Ad 01"], wait_s=1.0)
    assert got["ok"], got
    assert state["errors"] > 0, "страница не замолкала — тест ничего не проверил"
    assert got["busy_s"] is not None and 1.5 <= got["busy_s"] < 8
    assert len(_clicks(env["page"])) == 2, "во время ожидания что-то нажимали"
    row = [r for r in env["rows"] if r[0] == "simple_create_more_ads"][0]
    assert row[1] is True and row[2] == "created" and row[3]["busy_s"] == got["busy_s"]


def test_meta_busy_longer_than_the_limit_is_tab_frozen(env, monkeypatch):
    monkeypatch.setattr(sc, "META_BUSY_WAIT_S", 2.0)
    _silence_after_dialog(monkeypatch, 60.0)
    t0 = time.time()
    got = sc.create_next_ad(env["page"], tag="ad2", known_names=["Sample Ad 01"], wait_s=1.0)
    assert not got["ok"] and got["why"] == "tab_frozen"
    assert time.time() - t0 < 20, "ждали дольше предела"
    assert got["busy_s"] >= 2.0
    assert "progress dialog" in got["text"] and "did not answer" in got["text"]
    assert len(_clicks(env["page"])) == 2


def _fake_clock(monkeypatch, script: list) -> dict:
    clock = {"t": 1000.0}
    reads = iter(script)
    monkeypatch.setattr(sc, "_read_ads", lambda p: dict(next(reads)))
    monkeypatch.setattr(md, "_sleep_h", lambda *a, **k: None)
    monkeypatch.setattr(sc.time, "time", lambda: clock["t"])
    monkeypatch.setattr(sc.time, "sleep", lambda s: clock.__setitem__("t", clock["t"] + s))
    return clock


CREATING = {"text": "Creating...", "surface": "/am/lib:ads_progress_dialog_modal_8e9c"}


def test_silence_without_the_progress_dialog_is_still_tab_frozen(monkeypatch):
    """Вкладка молчит, а окна прогресса не было — ждать нечего (как прежде)."""
    clock = _fake_clock(monkeypatch, [{"ads": 1, "name_value": "Sample Ad 01", "dialog": None}]
                        + [{"error": "TimeoutError"}] * 200)
    got = sc._wait_new_ad(None, 1, ["Sample Ad 01"], wait_s=30, busy_wait_s=120)
    assert got.get("error") and clock["t"] - 1000.0 < 2.0


def test_a_closed_tab_is_not_waited_for(monkeypatch):
    clock = _fake_clock(monkeypatch, [{"ads": 1, "name_value": "Sample Ad 01", "dialog": CREATING}]
                        + [{"error": "TargetClosedError"}] * 200)
    got = sc._wait_new_ad(None, 1, ["Sample Ad 01"], wait_s=30, busy_wait_s=120)
    assert got["error"] == "TargetClosedError" and clock["t"] - 1000.0 < 2.0


def test_busy_just_under_and_over_the_limit(monkeypatch):
    """Предел — от первого неответа: 110 с молчания — ждём и продолжаем,
    130 с — сдаёмся на 120 с."""
    ok_script = ([{"ads": 1, "name_value": "Sample Ad 01", "dialog": CREATING}]
                 + [{"error": "TimeoutError"}] * 110
                 + [{"ads": 2, "name_value": "New Leads Ad", "dialog": None}] * 3)
    clock = _fake_clock(monkeypatch, ok_script)
    got = sc._wait_new_ad(None, 1, ["Sample Ad 01"], wait_s=30, busy_wait_s=120)
    assert got["name_stable"] and got["ads"] == 2 and got["busy_s"] == 110.0
    bad_script = ([{"ads": 1, "name_value": "Sample Ad 01", "dialog": CREATING}]
                  + [{"error": "TimeoutError"}] * 130
                  + [{"ads": 2, "name_value": "New Leads Ad", "dialog": None}] * 3)
    clock = _fake_clock(monkeypatch, bad_script)
    got = sc._wait_new_ad(None, 1, ["Sample Ad 01"], wait_s=30, busy_wait_s=120)
    assert got.get("error") and got["busy_s"] == 120.0
    assert clock["t"] - 1000.0 <= 121.0


def test_the_busy_wait_needs_the_dialog_gone_too(monkeypatch):
    """Страница снова ответила, но «Creating...» ещё на экране — ждём дальше
    обычного срока, пока окно не уйдёт (в пределах предела занятости)."""
    script = ([{"ads": 1, "name_value": "Sample Ad 01", "dialog": CREATING}]
              + [{"error": "TimeoutError"}] * 5
              + [{"ads": 2, "name_value": "New Leads Ad", "dialog": CREATING}] * 40
              + [{"ads": 2, "name_value": "New Leads Ad", "dialog": None}] * 3)
    _fake_clock(monkeypatch, script)
    got = sc._wait_new_ad(None, 1, ["Sample Ad 01"], wait_s=10, busy_wait_s=120)
    assert got["name_stable"] and not got.get("dialog") and got["ads"] == 2
    assert got["busy_s"] == 5.0
    assert got["waited_s"] > 10, "обычный срок не продлён"


# ── ACL-1412: окно после «Create ad» (живьём 30.09, d1755a59e139) ─────────

def test_a_progress_dialog_open_60_s_while_the_page_answers_is_waited_out(monkeypatch):
    """d1755a59e139, ad3: «Creating...» висело дольше 30 с при живой странице.
    Теперь ждём до `META_BUSY_WAIT_S` и от окна прогресса, а не только от
    неответа; окно ушло — объявление готово."""
    script = ([{"ads": 2, "name_value": "Sample Ad 02", "dialog": CREATING}] * 120      # 60 с
              + [{"ads": 3, "name_value": "New Leads Ad", "dialog": None}] * 3)
    clock = _fake_clock(monkeypatch, script)
    got = sc._wait_new_ad(None, 2, ["Sample Ad 01", "Sample Ad 02"], wait_s=30, busy_wait_s=120)
    assert got["name_stable"] and got["ads"] == 3 and not got.get("dialog")
    assert got["busy_s"] is None, "страница отвечала — это не неответ"
    assert 59.0 <= got["progress_s"] <= 60.5
    assert 60.0 <= clock["t"] - 1000.0 < 62.0


def test_a_progress_dialog_longer_than_the_limit_is_dialog_open(monkeypatch):
    clock = _fake_clock(monkeypatch, [{"ads": 3, "name_value": "New Leads Ad", "dialog": CREATING}] * 400)
    got = sc._wait_new_ad(None, 2, ["Sample Ad 01", "Sample Ad 02"], wait_s=30, busy_wait_s=120)
    assert got["dialog"] == CREATING
    assert 119.5 <= clock["t"] - 1000.0 <= 121.0, "ждали не до предела занятости"


def test_the_new_ad_is_taken_right_after_a_late_progress_dialog(monkeypatch):
    """Окно ушло после обычного срока — ещё `PROGRESS_SETTLE_S` на строку и имя."""
    script = ([{"ads": 2, "name_value": "Sample Ad 02", "dialog": CREATING}] * 80       # 40 с
              + [{"ads": 2, "name_value": "Sample Ad 02", "dialog": None}] * 4
              + [{"ads": 3, "name_value": "New Leads Ad", "dialog": None}] * 3)
    _fake_clock(monkeypatch, script)
    got = sc._wait_new_ad(None, 2, ["Sample Ad 01", "Sample Ad 02"], wait_s=30, busy_wait_s=120)
    assert got["name_stable"] and got["ads"] == 3


def test_a_dialog_that_is_not_meta_progress_is_dialog_open_at_once(env):
    """Не прогресс Meta и не выбор медиа — ждать нечего: `dialog_open` сразу,
    не через 30 и не через 120 с; после окна ничего не нажимали."""
    env["mode"]("other_dialog")
    t0 = time.time()
    got = sc.create_next_ad(env["page"], tag="ad2", known_names=["Sample Ad 01"], wait_s=30)
    assert not got["ok"] and got["why"] == "dialog_open", got
    assert time.time() - t0 < 10, "чужое окно ждали"
    assert got["dialog_kind"] == "other"
    assert got["text"].startswith("a Meta dialog that is not its progress dialog")
    assert got["text"].endswith('(dialog: "Something went wrong")')
    assert len(_clicks(env["page"])) == 2
    row = [r for r in env["rows"] if r[0] == "simple_create_more_ads"][0]
    assert row[1] is False and row[3]["dialog_kind"] == "other"


def test_the_media_picker_left_over_the_new_ad_is_closed_with_its_cancel(env):
    """d1755a59e139, ad3: после «Create ad» поверх нового объявления — окно
    выбора медиа («1 selected»). Его Cancel (охраняемое нажатие) — и новое
    объявление готово к заполнению; «Next» не жмём (чужой выбор)."""
    env["mode"]("picker")
    t0 = time.time()
    got = sc.create_next_ad(env["page"], tag="ad2", known_names=["Sample Ad 01"])
    clicks = _clicks(env["page"])
    assert got["ok"], got
    assert time.time() - t0 < 15, "окно выбора медиа ждали, как чужое окно"
    assert got["media_picker"] == {"closed": True, "why": "", "selected": "1 selected"}
    assert len(clicks) == 3 and clicks[2].endswith("|Cancel"), clicks
    assert not [c for c in clicks if c.endswith("|Next")]
    assert _forbidden(clicks) == []
    assert env["page"].evaluate("document.querySelector('#picker-host [role=dialog]')") is None
    row = [r for r in env["rows"] if r[0] == "simple_create_more_ads"][0]
    assert row[1] is True and row[3]["media_picker"]["closed"] is True
    assert row[3]["dialog"]["open_s"] is not None


def test_a_picker_that_answers_cancel_with_another_dialog_is_named(env):
    env["mode"]("picker_discard")
    got = sc.create_next_ad(env["page"], tag="ad2", known_names=["Sample Ad 01"])
    clicks = _clicks(env["page"])
    assert not got["ok"] and got["why"] == "dialog_open" and got["dialog_kind"] == "other", got
    assert got["text"].endswith('(dialog: "Discard changes? Discard")')
    assert not [c for c in clicks if c.endswith("|Discard")], "в чужом окне нажимали"
