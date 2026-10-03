# -*- coding: utf-8 -*-
"""Чтение спенда и стран переоткрывает окно, открытое руками, как Publish (ACL-1518).

03.10 07:45 «Read spend» у Facebook_1443 и Facebook_6540 сразу кончался
`profile_open_by_hand (no_automation_port)`, а «open profile» отвечал 409, хотя
Publish на 1443 через две минуты дождался тишины ввода и переоткрыл окно
(ACL-1388/1427). Теперь чтения идут тем же путём: те же условия (возраст окна,
не на переднем плане, тишина ввода), ожидание тишины до 2 минут, только мягкая
остановка через MLX API — никакого kill. Пока ждём — строка на карте; не
дождались — простой текст вместо «failed · retry».

Часы, сон, Win32, процессы и MLX подменены (фикстуры ACL-1427); настоящий
Multilogin не трогается.
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
from src.fb_ads_rpa import browser as br  # noqa: E402
from src.fb_ads_rpa import hand_window as hw  # noqa: E402
from src.fb_ads_rpa import profile_trouble as pt  # noqa: E402

# Машина и мир ACL-1427: окно открыто руками (процесс 4242), порта нет.
from tests.test_a_hand_opened_window_waits_for_idle import (  # noqa: E402,F401
    HAND_PID,
    PROFILE,
    machine,
    world,
)

ACCOUNT = "Facebook_1443"
OTHER = "Facebook_6540"
OTHER_PID = 4343
OTHER_PROFILE = "6ba7b810-9dad-11d1-80b4-00c04fd430c8"
PAGE = ROOT / "src" / "web" / "templates" / "report.html"
ROW = ROOT / "src" / "web" / "templates" / "_account_map_country.html"


class _Direct:
    """Слой «прицепиться к открытому»: порта нет, процесс профиля есть."""

    def __init__(self, world):
        self.world = world

    def attach_existing_only(self, profile_id, user_data_dir, *, new_page=False):
        return None

    def profile_is_running(self, user_data_dir, profile_id=""):
        return bool(self.world.pids)


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

    # Что строка аккаунта видела, пока шло ожидание.
    world.seen_notes = []
    real_sleep = hw._sleep

    def sleep(s):
        world.seen_notes.append(sp.hand_wait_note(ACCOUNT))
        real_sleep(s)

    monkeypatch.setattr(hw, "_sleep", sleep)
    return world


def _attach(account=ACCOUNT):
    with sp.reopening_by_hand():
        return sp._attach_only(sp._entry_for(account), account)


# ── hand-opened + idle → переоткрыто и прочитано ───────────────────────────

def test_hand_opened_and_idle_is_reopened_for_the_read(reads, monkeypatch):
    reads.m.input_until = 30.0
    # Ревью PR #519 (M12): ни остановка, ни подъём не просят права убить.
    kills: list = []
    real_stop, real_start = reads.prov._stop_profile, reads.prov._start_via_mlx_api

    def stop(*a, **kw):
        kills.append(("stop", kw.get("allow_kill", True)))
        return real_stop(*a, **kw)

    def start(*a, **kw):
        kills.append(("start", kw.get("allow_kill", True)))
        return real_start(*a, **kw)

    monkeypatch.setattr(reads.prov, "_stop_profile", stop)
    monkeypatch.setattr(reads.prov, "_start_via_mlx_api", start)
    conn = _attach()
    assert kills == [("stop", False), ("start", False)], kills
    assert conn.page == "reopened-page"
    assert reads.ev == [("stop", PROFILE), "start", "connect"]
    assert "kill" not in reads.ev, "чтение убило окно человека"
    # Пока ждали — строка на карте; после — её нет.
    assert sp.HAND_WAIT_NOTE in reads.seen_notes
    assert sp.hand_wait_note(ACCOUNT) == ""
    step, ok, kw = [r for r in reads.rows if r[0] == "profile_hand_window"][-1]
    assert (ok, kw["note"], kw["diag"]["wait_outcome"]) == (True, "reopened", hw.WAIT_IDLE)


def test_the_spend_read_asks_for_the_publish_reopen(monkeypatch):
    """`read_via_rpa` просит переоткрывать окно, открытое руками, и отдаёт код отказа."""
    seen = []

    def attach(entry, label):
        seen.append(sp._REOPEN_BY_HAND.get())
        raise sp.ProfileNotAttachable(sp.HAND_OPEN_MESSAGE, code=sp.HAND_OPEN_CODE)

    monkeypatch.setattr(sp, "_entry_for", lambda account: SimpleNamespace(ad_account_id="act_1", profile_id="p"))
    monkeypatch.setattr(sp, "_attach_only", attach)
    out = sp.read_via_rpa(ACCOUNT, "2026-10-01", "2026-10-01")
    assert seen == [True]
    assert out.state == "failed" and out.error_code == sp.HAND_OPEN_CODE
    assert out.to_dict()["error_code"] == sp.HAND_OPEN_CODE
    assert sp._REOPEN_BY_HAND.get() is False, "флаг пережил чтение"


def test_the_spend_read_reaches_the_page_after_the_reopen(reads, monkeypatch):
    """Полный путь `read_via_rpa`: после переоткрытия чтение идёт в Ads Manager."""
    reads.m.input_until = 30.0
    visited = []

    class Page:
        def goto(self, url, **kw):
            visited.append(url)
            raise RuntimeError("stop here")  # дальше — обычное чтение, его тут не проверяем

        def close(self):
            pass

    monkeypatch.setattr(reads.prov, "_connect_started",
                        lambda started, pid: reads.ev.append("connect") or SimpleNamespace(
                            page=Page(), close=lambda **kw: None))
    with pytest.raises(RuntimeError, match="stop here"):
        sp.read_via_rpa(ACCOUNT, "2026-10-01", "2026-10-01")
    assert reads.ev == [("stop", PROFILE), "start", "connect"]
    assert visited and "act=123" in visited[0]


# ── ввод продолжается → простой текст ─────────────────────────────────────

def test_input_that_continues_ends_with_the_plain_message(reads):
    reads.m.input_until = 10_000.0
    out = sp.read_via_rpa(ACCOUNT, "2026-10-01", "2026-10-01")
    assert out.state == "failed"
    assert out.error == sp.HAND_OPEN_MESSAGE
    assert out.error_code == sp.HAND_OPEN_CODE
    assert reads.ev == [], "окно тронули, хотя ввод не прекращался"
    assert sp.HAND_WAIT_NOTE in reads.seen_notes
    assert sp.hand_wait_note(ACCOUNT) == ""
    kw = [r for r in reads.rows if r[0] == "profile_hand_window"][-1][2]
    assert kw["diag"]["wait_outcome"] == hw.WAIT_TIMEOUT


def test_the_country_read_gives_the_same_plain_message(reads):
    reads.m.input_until = 10_000.0
    out = cs.read_country_split(ACCOUNT, "2026-10-01")
    assert out.state == "failed"
    assert out.error == sp.HAND_OPEN_MESSAGE and out.error_code == sp.HAND_OPEN_CODE
    assert cs.get_split(ACCOUNT, "2026-10-01", "2026-10-01")["error_code"] == sp.HAND_OPEN_CODE
    assert reads.ev == []


def test_the_country_read_reopens_after_idle(reads, monkeypatch):
    reads.m.input_until = 30.0
    seen = []
    real = cs._attach_only

    def attach(entry, label):
        seen.append(sp._REOPEN_BY_HAND.get())
        return real(entry, label)

    monkeypatch.setattr(cs, "_attach_only", attach)
    cs.read_country_split(ACCOUNT, "2026-10-01")  # дальше страница-заглушка, итог не важен
    assert seen == [True]
    assert reads.ev[:3] == [("stop", PROFILE), "start", "connect"]


# ── окно на переднем плане → сразу простой текст ───────────────────────────

def test_a_window_in_front_gives_the_plain_message_at_once(reads):
    reads.m.input_until = 10_000.0
    reads.m.fg = HAND_PID
    out = sp.read_via_rpa(ACCOUNT, "2026-10-01", "2026-10-01")
    assert out.error == sp.HAND_OPEN_MESSAGE and out.error_code == sp.HAND_OPEN_CODE
    assert reads.ev == [] and reads.m.sleeps == [], "ждали, хотя окно было впереди"
    assert sp.HAND_WAIT_NOTE not in reads.seen_notes


def test_without_the_read_flag_nothing_changes(reads):
    """Прочие пользователи `_attach_only` (выключение, проба кабинета) окно не трогают."""
    reads.m.t = 500.0  # давно тихо — переоткрыть было бы можно
    with pytest.raises(sp.ProfileNotAttachable) as exc:
        sp._attach_only(sp._entry_for(ACCOUNT), ACCOUNT)
    assert exc.value.code == ""
    assert reads.ev == []


def test_the_provider_reopens_for_a_read_only_when_asked(reads):
    """`allow_kill=False` без `reopen_hand_window` — прежняя остановка (ACL-1388)."""
    reads.m.t = 500.0
    with pytest.raises(pt.ProfileStop):
        reads.prov.open(sp._entry_for(ACCOUNT), ACCOUNT, allow_restart=True, allow_kill=False)
    assert reads.ev == []
    conn = reads.prov.open(sp._entry_for(ACCOUNT), ACCOUNT, allow_restart=True, allow_kill=False,
                           reopen_hand_window=True)
    assert conn.page == "reopened-page" and "kill" not in reads.ev


# ── Read all: два окна, открытых руками ────────────────────────────────────

def test_read_all_reopens_two_hand_opened_profiles_one_after_another(reads):
    reads.m.input_until = reads.m.t + 30.0
    assert _attach(ACCOUNT).page == "reopened-page"
    reads.pids = frozenset({OTHER_PID})      # второе окно, тоже руками
    reads.m.input_until = reads.m.t + 20.0    # человек снова шевельнул мышью
    assert _attach(OTHER).page == "reopened-page"
    assert reads.ev == [("stop", PROFILE), "start", "connect",
                        ("stop", OTHER_PROFILE), "start", "connect"]
    assert "kill" not in reads.ev
    notes = [r[2]["note"] for r in reads.rows if r[0] == "profile_hand_window"]
    assert notes == ["reopened", "reopened"]


def _wait_for(predicate, seconds=5.0):
    for _ in range(int(seconds / 0.02)):
        if predicate():
            return True
        threading.Event().wait(0.02)
    return predicate()


def test_a_waiting_read_does_not_block_another_account(monkeypatch, tmp_path):
    """Read all: пока один аккаунт ждёт тишины ввода, чтение другого не стоит.

    Ревью PR #519 (F3): через настоящий `read_via_rpa` и его замок профиля
    (`serialized_browser(key=_profile_key, reading=True)`), а не подменённое
    чтение. Третий ярлык на ТОТ ЖЕ профиль, наоборот, ждёт — замок настоящий.
    """
    monkeypatch.setattr(sp, "_store_path", lambda: tmp_path / "account_spend.json")
    monkeypatch.setattr(sp, "_UNSAVED", {})
    same = "Facebook_1443_alias"
    entries = {ACCOUNT: SimpleNamespace(profile_id="p-1443", ad_account_id="act_1"),
               OTHER: SimpleNamespace(profile_id="p-6540", ad_account_id="act_2"),
               same: SimpleNamespace(profile_id="p-1443", ad_account_id="act_1")}
    monkeypatch.setattr(sp, "_entry_for", entries.get)
    waiting, release = threading.Event(), threading.Event()
    attached: list = []

    def attach(entry, label):
        attached.append(label)
        assert sp._REOPEN_BY_HAND.get() is True
        if label == ACCOUNT:
            # Так выглядит ожидание тишины ввода изнутри: строка на карте и
            # поток, который держит замок своего профиля.
            sp._set_hand_wait(label, True)
            waiting.set()
            release.wait(10)
            sp._set_hand_wait(label, False)
            raise sp.ProfileNotAttachable(sp.HAND_OPEN_MESSAGE, code=sp.HAND_OPEN_CODE)
        raise sp.ProfileNotAttachable(f"{label}: the profile is not open.")

    monkeypatch.setattr(sp, "_attach_only", attach)
    day = "2026-10-01"
    sp.start_read(ACCOUNT, day, day)
    assert waiting.wait(5)
    sp.start_read(OTHER, day, day)
    sp.start_read(same, day, day)
    assert _wait_for(lambda: sp.get_reading(OTHER, day, day).state != "running")
    assert sp.get_reading(OTHER, day, day).error.endswith("the profile is not open.")
    # Тот же профиль стоит за ждущим чтением — до его конца.
    assert sp.get_reading(ACCOUNT, day, day).state == "running"
    assert sp.get_reading(same, day, day).state == "running"
    assert same not in attached
    assert sp.hand_wait_note(ACCOUNT) == sp.HAND_WAIT_NOTE
    release.set()
    assert _wait_for(lambda: sp.get_reading(same, day, day).state != "running")
    first = sp.get_reading(ACCOUNT, day, day)
    assert first.state == "failed" and first.error_code == sp.HAND_OPEN_CODE
    assert attached.index(same) > attached.index(ACCOUNT)
    assert sp.hand_wait_note(ACCOUNT) == ""


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


def test_open_profile_on_a_hand_opened_profile_reopens_instead_of_409(reads):
    reads.m.input_until = 30.0
    r = _post("/account-spend/open-profile", {"account": ACCOUNT})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert reads.ev == [("stop", PROFILE), "start", "connect"]
    assert sp.HAND_WAIT_NOTE in reads.seen_notes


def test_open_profile_that_cannot_reopen_gives_the_plain_message_not_409(reads):
    reads.m.input_until = 10_000.0
    r = _post("/account-spend/open-profile", {"account": ACCOUNT})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False and body["message"] == sp.HAND_OPEN_MESSAGE
    assert body["code"] == sp.HAND_OPEN_CODE
    assert reads.ev == []
    # Ревью PR #519 (F6): маршрут решает по коду, а не по тексту.
    ok, message = sp.open_for_automation(ACCOUNT)
    assert not ok and message.code == sp.HAND_OPEN_CODE and message == sp.HAND_OPEN_MESSAGE


def test_connecting_a_new_profile_still_leaves_a_hand_opened_window_alone(reads):
    """`/mlx-profile-open` (подключение нового профиля) в задачу не входит."""
    reads.m.t = 500.0
    ok, message = sp.open_profile_for_automation(PROFILE, "f-1", "A", "DQ438")
    assert not ok and "opened manually" in message
    assert reads.ev == []


def test_the_status_poll_carries_the_waiting_note(monkeypatch, tmp_path):
    monkeypatch.setattr(sp, "_store_path", lambda: tmp_path / "account_spend.json")
    monkeypatch.setattr(cs, "_store_path", lambda: tmp_path / "country_splits.json")
    sp._set_hand_wait(ACCOUNT, True)
    try:
        r = _get("/account-spend/status", {"account": ACCOUNT, "date_from": "2026-10-01",
                                           "date_to": "2026-10-01"})
        assert r.status_code == 200 and r.json()["waiting"] == sp.HAND_WAIT_NOTE
    finally:
        sp._set_hand_wait(ACCOUNT, False)
    r = _get("/account-spend/status", {"account": ACCOUNT, "date_from": "2026-10-01",
                                       "date_to": "2026-10-01"})
    assert r.json()["waiting"] == ""


# ── строка карты: по-английски и без «failed · retry» ─────────────────────

def test_the_row_texts_are_english():
    for text in (sp.HAND_WAIT_NOTE, sp.HAND_OPEN_MESSAGE):
        assert not any("Ѐ" <= ch <= "ӿ" for ch in text), text
    assert sp.HAND_WAIT_NOTE == "Waiting for the computer to be idle to reopen the profile…"
    assert sp.HAND_OPEN_MESSAGE == ("The profile is open by hand — close its window in Multilogin, "
                                    "then Read again")


def test_the_row_shows_the_wait_and_the_plain_message():
    js = PAGE.read_text(encoding="utf-8")
    # Без общих переменных: стенды вырезают функции по одной (ревью PR #519).
    assert "AMAP_HAND_CODE" not in js
    assert js.count("typeof amapHandWait === 'function'") >= 2
    assert "function amapHandWait(account, note)" in js
    # Ожидание — из опроса состояния, в спенде, в странах и в «open profile».
    assert js.count("j.waiting") >= 4
    # Отказ «открыт руками» — не «failed · retry» и не «countries failed».
    assert "btn.textContent = hand ? 'Read spend' : 'failed · retry';" in js
    assert "mark.textContent = code === 'open_by_hand' ? why : 'countries failed';" in js
    assert "s.error_code" in js
    row = ROW.read_text(encoding="utf-8")
    assert "sr.error_code == 'open_by_hand'" in row
    assert "sp.error_code == 'open_by_hand'" in row


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
// Пока ждём тишины — строка в ячейке.
ctx.amapApplyReading('Acc', {state: 'running'}, null, __WAIT__);
assert.equal(cell.querySelector('.amap-hand-wait').textContent, __WAIT__);
assert.equal(btn.textContent, 'reading…');
// Дождались и читаем дальше — строки больше нет.
ctx.amapApplyReading('Acc', {state: 'running'}, null, '');
assert.equal(cell.querySelector('.amap-hand-wait'), null);
// Не дождались — простой текст целиком, кнопка обычная, не «failed · retry».
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
    source = (_ROW_HARNESS.replace("__WAIT__", json.dumps(sp.HAND_WAIT_NOTE))
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


def test_the_server_row_shows_the_plain_message_for_a_hand_opened_profile(rendered):
    html = rendered(sp.HAND_OPEN_MESSAGE, sp.HAND_OPEN_CODE, split_code=sp.HAND_OPEN_CODE)
    cell = _cell(html, 'class="num amap-live"')
    # Текст целиком, в своём классе, не обрезан до 40 знаков.
    assert (f'<span class="amap-live-hand" title="{sp.HAND_OPEN_MESSAGE}">'
            f'{sp.HAND_OPEN_MESSAGE}</span>') in cell
    assert "amap-live-bad" not in cell
    # Страны — тем же текстом, не «countries failed».
    assert '<small class="amap-split-bad hand"' in cell and "countries failed" not in cell
    actions = _cell(html, '<td class="amap-actions">')
    assert ">Read spend</button>" in actions and "failed · retry" not in actions
    assert "amap-openprof" in actions, "open profile is still offered"


def test_the_server_row_keeps_failed_retry_for_other_failures(rendered):
    html = rendered("Ads Manager did not finish loading in time", "", split_code="")
    cell = _cell(html, 'class="num amap-live"')
    assert "amap-live-hand" not in cell and 'class="amap-live-bad"' in cell
    assert "countries failed" in cell
    assert "failed · retry" in _cell(html, '<td class="amap-actions">')
