# -*- coding: utf-8 -*-
"""Чтение спенда и стран переоткрывает окно, открытое руками, как Publish (ACL-1518).

03.10 07:45 «Read spend» у Facebook_1443 и Facebook_6540 сразу кончался
`profile_open_by_hand (no_automation_port)`, а «open profile» отвечал 409, хотя
Publish на 1443 переоткрыл окно (ACL-1388/1427). Теперь чтения идут тем же
путём: только мягкая остановка через MLX API — никакого kill.

ACL-1562 (решение владельца 04.10): без условий и без ожидания тишины ввода —
сразу. Пока переоткрываем — строка на карте; мягкая остановка не прошла —
простой текст вместо «failed · retry».

Процессы и MLX подменены (фикстуры ACL-1562); настоящий Multilogin не
трогается.
"""
from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.analysis_agent import account_spend as sp  # noqa: E402
from src.analysis_agent import country_split as cs  # noqa: E402
from tests.test_acl2063_dashboard_browser import dashboard  # noqa: E402,F401 -- shared production fixture
from src.fb_ads_rpa import browser as br  # noqa: E402
from src.fb_ads_rpa import profile_trouble as pt  # noqa: E402

# Машина и мир ACL-1562: два окна открыты руками, порта автоматизации нет.
from tests.test_a_hand_opened_window_is_reopened_at_once import (  # noqa: E402,F401
    ACCOUNT,
    OTHER,
    OTHER_PROFILE,
    PROFILE,
    machine,
    world,
)

PAGE = ROOT / "src" / "web" / "templates" / "report.html"
ROW = ROOT / "src" / "web" / "templates" / "_account_map_country.html"


class _Direct:
    """Слой «прицепиться к открытому»: порта нет, процесс профиля есть."""

    def __init__(self, world):
        self.world = world

    def attach_existing_only(self, profile_id, user_data_dir, *, new_page=False):
        return None

    def profile_is_running(self, user_data_dir, profile_id=""):
        return bool(self.world.running.get(profile_id))


@pytest.fixture
def reads(world, monkeypatch, tmp_path):
    """Чтения видят мир ACL-1427 через настоящий провайдер Publish."""
    monkeypatch.setattr(sp, "_mlx_profile_dir", lambda *a, **kw: tmp_path)
    monkeypatch.setattr(sp, "_dir_spellings", lambda p: [str(p)])
    monkeypatch.setattr(br, "MultiloginDirectCDPProvider", lambda: _Direct(world))
    monkeypatch.setattr(br, "get_browser_provider", lambda name=None: world.prov)
    entries = {
        name: SimpleNamespace(profile_id=pid, folder_id="f-1", multilogin_instance="A",
                              ad_account_id="act_123")
        for name, pid in ((ACCOUNT, PROFILE), (OTHER, OTHER_PROFILE))
    }
    monkeypatch.setattr(sp, "_entry_for", entries.get)
    monkeypatch.setattr(cs, "_entry_for", entries.get)
    monkeypatch.setattr(cs, "_store_path", lambda: tmp_path / "country_splits.json")
    monkeypatch.setattr(sp, "_store_path", lambda: tmp_path / "account_spend.json")
    return world


# ── hand-opened → переоткрыто сразу и прочитано ───────────────────────────

def test_the_spend_read_asks_for_the_publish_reopen(monkeypatch):
    """`read_via_rpa` просит переоткрывать окно, открытое руками, и отдаёт код отказа."""
    def attach(entry, label):
        raise sp.ProfileNotAttachable(sp.HAND_OPEN_MESSAGE, code=sp.HAND_OPEN_CODE)

    monkeypatch.setattr(sp, "_entry_for", lambda account: SimpleNamespace(ad_account_id="act_1", profile_id=PROFILE))
    monkeypatch.setattr(sp, "_attach_only", attach)
    out = sp.read_via_rpa(ACCOUNT, "2026-10-01", "2026-10-01")
    assert out.state == "failed" and out.error_code == sp.HAND_OPEN_CODE
    assert out.to_dict()["error_code"] == sp.HAND_OPEN_CODE


def test_the_country_read_gives_the_same_plain_message(reads):
    out = cs.read_country_split(ACCOUNT, "2026-10-01")
    assert out.state == "failed"
    assert out.error == sp.HAND_OPEN_MESSAGE and out.error_code == sp.HAND_OPEN_CODE
    assert cs.get_split(ACCOUNT, "2026-10-01", "2026-10-01")["error_code"] == sp.HAND_OPEN_CODE
    assert reads.ev == [], "a hand-opened window is never stopped (ACL-1812, ACL-1811)"


def test_without_the_read_flag_nothing_changes(reads):
    """Прочие пользователи `_attach_only` (выключение, проба кабинета) окно не трогают."""
    with pytest.raises(sp.ProfileNotAttachable) as exc:
        _attach_without_reopen()
    assert exc.value.code == sp.HAND_OPEN_CODE
    assert reads.ev == []


def test_the_provider_reopens_for_a_read_only_when_asked(reads):
    """`allow_kill=False` без `reopen_hand_window` — прежняя остановка (ACL-1388)."""
    with pytest.raises(pt.ProfileStop):
        reads.prov.open(sp._entry_for(ACCOUNT), ACCOUNT, allow_restart=True, allow_kill=False)
    assert reads.ev == []
    conn = reads.prov.open(sp._entry_for(ACCOUNT), ACCOUNT, allow_restart=True, allow_kill=False,
                           reopen_hand_window=True)
    assert conn.page == "reopened-page" and "kill" not in reads.ev


def _wait_for(predicate, seconds=5.0):
    for _ in range(int(seconds / 0.02)):
        if predicate():
            return True
        threading.Event().wait(0.02)
    return predicate()


# ── open profile: больше не 409 ───────────────────────────────────────────

def _post(path, data):
    import httpx

    from src.web.app import app

    async def go():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as c:
            return await c.post(path, data=data)

    return asyncio.run(go())


def _get(path, params):
    import httpx

    from src.web.app import app

    async def go():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as c:
            return await c.get(path, params=params)

    return asyncio.run(go())


def test_connecting_a_new_profile_still_leaves_a_hand_opened_window_alone(reads):
    """`/mlx-profile-open` (подключение нового профиля) в задачу не входит
    (ACL-1518; ACL-1562 оставил так: подключение — не действие над аккаунтом)."""
    ok, message = sp.open_profile_for_automation(PROFILE, "f-1", "A", "DQ438")
    assert not ok and message == sp.HAND_OPEN_MESSAGE
    assert reads.ev == []


# ── строка карты: по-английски и без «failed · retry» ─────────────────────

def test_the_row_texts_are_english():
    for text in (sp.HAND_OPEN_MESSAGE, sp.HAND_OPEN_MESSAGE_API):
        assert not any("Ѐ" <= ch <= "ӿ" for ch in text), text
    assert sp.HAND_OPEN_MESSAGE_API == ("Opened in Multilogin by hand — close it there, wait about 3 minutes, "
                                        "then use Open profiles for today")
    assert "open profile button" in sp.HAND_OPEN_MESSAGE, "the RPA backend has no Open profiles for today button"


def test_the_row_shows_the_wait_and_the_plain_message():
    js = PAGE.read_text(encoding="utf-8")
    # Без общих переменных: стенды вырезают функции по одной (ревью PR #519).
    assert "AMAP_HAND_CODE" not in js
    assert js.count("typeof amapHandWait === 'function'") >= 2
    assert "function amapHandWait(account, note)" in js
    # Строка переоткрытия — из опроса состояния, в спенде, в странах и в «open profile».
    assert js.count("j.waiting") >= 4
    # Отказ «открыт руками» — не «failed · retry» и не «countries failed».
    # ACL-1580: «разлогинен» (`out`) показан так же — простым текстом.
    assert "btn.textContent = hand || out ? 'Read spend' : 'failed · retry';" in js
    assert "var plain = code === 'open_by_hand' || code === 'profile_logged_out';" in js
    assert "mark.textContent = plain ? why : 'countries failed';" in js
    assert "s.error_code" in js
    row = ROW.read_text(encoding="utf-8")
    assert 'class="amap-list"' in row and 'list not read' in row
    assert 'class="amap-live' not in row


# ── строка карты в деле (node) ─────────────────────────────────────────────

_ROW_HARNESS = r'''
const assert = require('assert/strict'), vm = require('vm');
class El {
  constructor(cls) { this.className = cls || ''; this.children = []; this.textContent = ''; this.title = '';
                     this.parentNode = null; this.dataset = {}; this.disabled = false; }
  set innerHTML(html) {
    this.children = [];
    const m = /class="([^"]+)"/.exec(html || '');
    if (m) this.appendChild(new El(m[1]));
  }
  get firstChild() { return this.children[0]; }
  appendChild(c) { c.parentNode = this; this.children.push(c); return c; }
  insertBefore(c, ref) { c.parentNode = this; this.children.splice(this.children.indexOf(ref) + 1, 0, c); }
  remove() { const p = this.parentNode; if (p) p.children.splice(p.children.indexOf(this), 1); this.parentNode = null; }
  querySelector(sel) {
    const want = sel.replace(/^\./, '');
    for (const c of this.children) {
      if ((' ' + c.className + ' ').includes(' ' + want + ' ')) return c;
      const deep = c.querySelector(sel); if (deep) return deep;
    }
    return null;
  }
  closest() { return this.row; }
}
const actions = new El('amap-actions'), btn = actions.appendChild(new El('amap-btn amap-read'));
const cell = new El('num amap-live'); cell.dataset.account = 'Acc';
const row = {querySelector: s => s === '.amap-read' ? btn : new El()};
cell.row = row;
const ctx = {document: {querySelectorAll: s => s === '.amap-live' ? [cell] : [], createElement: () => new El()},
  amapReadAllBusy: false, amapRereadSync() {}, amapUpdateCountrySummaries() {}, amapStatusTail: () => '',
  amapPaintTree() {}, amapMoney: () => '1', amapReadTitle: () => '', amapOpenProfile() {}, String: String};
vm.createContext(ctx);
vm.runInContext(__SCRIPT__, ctx);
// Пока переоткрываем окно — строка в ячейке.
ctx.amapApplyReading('Acc', {state: 'running'}, null, __WAIT__);
assert.equal(cell.querySelector('.amap-hand-wait').textContent, __WAIT__);
assert.equal(btn.textContent, 'reading…');
// Переоткрыли и читаем дальше — строки больше нет.
ctx.amapApplyReading('Acc', {state: 'running'}, null, '');
assert.equal(cell.querySelector('.amap-hand-wait'), null);
// Не вышло — простой текст целиком, кнопка обычная, не «failed · retry».
ctx.amapApplyReading('Acc', {state: 'running'}, null, __WAIT__);
ctx.amapApplyReading('Acc', {state: 'failed', error: __MSG__, error_code: 'open_by_hand'}, null, '');
assert.equal(cell.querySelector('.amap-hand-wait'), null);
assert.equal(cell.firstChild.className, 'amap-live-hand');
assert.equal(cell.firstChild.textContent, __MSG__);
assert.equal(btn.textContent, 'Read spend');
assert.ok(!btn.className.includes('amap-live-bad'));
assert.ok(actions.querySelector('.amap-openprof'), 'open profile stays offered');
// Обычный сбой — как раньше.
ctx.amapApplyReading('Acc', {state: 'failed', error: 'Ads Manager did not finish loading in time'}, null, '');
assert.equal(btn.textContent, 'failed · retry');
assert.equal(cell.firstChild.className, 'amap-live-bad');
// Страны: простой текст вместо «countries failed».
ctx.amapSplitMark('Acc', __MSG__, 'open_by_hand');
assert.equal(cell.querySelector('.amap-split-bad').textContent, __MSG__);
ctx.amapSplitMark('Acc', 'boom', '');
assert.equal(cell.querySelector('.amap-split-bad').textContent, 'countries failed');
'''


def test_the_row_paints_the_wait_and_the_plain_message_in_node(tmp_path):
    from tests.test_review_ui_regressions import run_node
    import json

    page = PAGE.read_text(encoding="utf-8")

    def cut(start, end):
        at = page.index(start)
        return page[at:page.index(end, at)]

    script = "\n".join([
        cut("var amapHandWaitShown", "/* ── Что в аккаунте на самом деле"),
        cut("function amapSpendFailed(", "function amapOpenProfile("),
        cut("function amapSplitMark(", "function amapSplitBusy("),
    ])
    # Сам скрипт подставляется последним: в нём могут встретиться метки.
    source = (_ROW_HARNESS.replace("__WAIT__", json.dumps("The Acc window was opened by hand — reopening it with automation"))
              .replace("__MSG__", json.dumps(sp.HAND_OPEN_MESSAGE))
              .replace("__SCRIPT__", json.dumps(script)))
    path = tmp_path / "row.cjs"
    path.write_text(source, encoding="utf-8")
    run_node([path])


# ── строка карты с сервера (ревью PR #519, F2) ─────────────────────────────

@pytest.fixture
def rendered(monkeypatch, tmp_path):
    """Карта, собранная `_build_account_map_view` и отрисованная шаблоном."""
    from src.web import account_links as links
    from src.web import app

    monkeypatch.setattr(sp, "_store_path", lambda: tmp_path / "account_spend.json")
    monkeypatch.setattr(sp, "_UNSAVED", {})
    monkeypatch.setattr(cs, "_store_path", lambda: tmp_path / "country_splits.json")
    monkeypatch.setattr(links, "get_local_config_dir", lambda: str(tmp_path))
    monkeypatch.setattr(links, "get_ad_account_map_path", lambda: str(tmp_path / "missing.csv"))
    monkeypatch.setattr(app, "get_ad_account_map_path", lambda: str(tmp_path / "missing.csv"))
    links.link(ACCOUNT, "country", "KW")

    def render(error, code, split_code=None):
        sp._put(sp.SpendReading(account=ACCOUNT, date_from="2026-10-01", date_to="2026-10-01",
                                state="failed", error=error, error_code=code,
                                finished_at="2026-10-03 07:45:00"))
        if split_code is not None:
            cs.save_split(cs.CountrySplit(account=ACCOUNT, date_from="2026-10-01",
                                          date_to="2026-10-01", state="failed",
                                          error=error, error_code=split_code))
        view = app._build_account_map_view({"countries": []}, date_from="2026-10-01",
                                           date_to="2026-10-01")
        return app.templates.env.get_template("_account_map.html").render(amap=view, mlx={})

    return render


def _cell(html, marker):
    part = html[html.index(marker):]
    return part[:part.index("</td>")]


def _current_failure_row(dashboard, message, code):
    """Actual persisted queue result -> production JS, without opening a profile."""
    from datetime import datetime, timezone
    from src.analysis_agent import campaign_inventory as ci
    site, page = dashboard
    stamp = datetime.fromtimestamp(site.now, timezone.utc).isoformat()
    failed = ci.Snapshot(account='Acc', state='failed', error=message,
                         error_code=code, finished_at_utc=stamp)
    ci.put_result(failed)
    site.q.record_result(failed, trigger='action')
    page.goto('http://acl.test/')
    page.wait_for_function("document.querySelector('.amap-campaigns')?._hc?.state.refreshRows?.Acc?.state === 'failed'")
    return site, page, page.locator('tr:has(td[data-account="Acc"])')


def test_the_server_row_shows_the_plain_message_for_a_hand_opened_profile(rendered, dashboard):
    html = rendered(sp.HAND_OPEN_MESSAGE, sp.HAND_OPEN_CODE, split_code=sp.HAND_OPEN_CODE)
    assert 'list not read' in _cell(html, 'class="amap-list"')
    assert 'class="num amap-live"' not in html
    site, page, row = _current_failure_row(dashboard, sp.HAND_OPEN_MESSAGE, sp.HAND_OPEN_CODE)
    assert row.locator('.hc-snap').get_attribute('title') == sp.HAND_OPEN_MESSAGE
    assert row.locator('button.hc-rc:visible').all_inner_texts() == ['Retry']
    page.reload()
    page.wait_for_function("document.querySelector('.amap-campaigns')?._hc?.state.refreshRows?.Acc?.state === 'failed'")
    assert row.locator('.hc-snap').get_attribute('title') == sp.HAND_OPEN_MESSAGE
    assert site.reads == [] and site.errors == []


def test_the_server_row_keeps_failed_retry_for_other_failures(rendered, dashboard):
    html = rendered("Ads Manager did not finish loading in time", "", split_code="")
    assert 'list not read' in _cell(html, 'class="amap-list"')
    assert 'class="num amap-live"' not in html
    message = 'Ads Manager did not finish loading in time'
    site, page, row = _current_failure_row(dashboard, message, '')
    assert row.locator('.hc-snap').get_attribute('title') == message
    assert row.locator('button.hc-rc:visible').all_inner_texts() == ['Retry']
    assert site.reads == [] and site.errors == []


def _attach_without_reopen():
    with sp.profile_lock(PROFILE):
        return sp._attach_only(sp._entry_for(ACCOUNT), ACCOUNT)
