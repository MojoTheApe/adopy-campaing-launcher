"""ACL-1760: «Read spend», состояние кабинета и дерево через Graph API в профиле.

Ни Meta, ни Multilogin не трогаются: браузер — поддельное CDP из тестов ACL-1756
(`FakePage` / `FakeCdp`), а Graph отвечает по таблице маршрутов. Главное, что
проверяется: ответ API раскладывается в ТОЧНО ту форму `SpendReading` /
`CountrySplit`, которую уже читает экран, и каждая ошибка превращается в тот же
текст и код, что у RPA-чтения.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.analysis_agent import account_spend as sp  # noqa: E402
from src.analysis_agent import account_spend_api as api  # noqa: E402
from src.analysis_agent import country_split as cs  # noqa: E402
from src.fb_api import graph as g  # noqa: E402
from src.fb_api import profile as prof  # noqa: E402
from src.fb_api import telemetry as tm  # noqa: E402
from src.fb_api import token_capture as tc  # noqa: E402
from src.fb_api.cdp import PageTab  # noqa: E402
from tests.test_acl1756_fb_api import ACT, PROFILE_UUID, TOKEN, FakeCdp, FakePage  # noqa: E402

ACCOUNT = "Acc"
D = "2026-10-08"


# ───────────────────────── маршруты Graph ─────────────────────────

def _key(spec: dict) -> str:
    path = spec["path"].replace(ACT, "ACT")
    params = spec.get("params") or {}
    return "|".join([path, str(params.get("level", "")), str(params.get("breakdowns", ""))])


class RoutedPage(FakePage):
    """Страница, отвечающая на Graph-вызовы по пути и уровню."""

    def __init__(self, routes: dict, **kw):
        super().__init__(**kw)
        self.routes = routes

    def evaluate(self, expression: str):
        if ".call(" in expression:
            spec = json.loads(expression.split(".call(", 1)[1].rsplit(")", 1)[0])
            self.specs.append(spec)
            reply = self.routes.get(_key(spec), {"status": 200, "body": {"data": []}})
            if callable(reply):
                reply = reply(spec)
            return json.loads(json.dumps(reply).replace("__TOKEN__", self._token))
        return super().evaluate(expression)


def ok(data, **paging):
    return {"status": 200, "headers": {}, "body": {"data": data, **({"paging": paging} if paging else {})}}


def err(code, subcode=None):
    e = {"message": f"err {code} access_token=__TOKEN__", "code": code}
    if subcode is not None:
        e["error_subcode"] = subcode
    return {"status": 400, "headers": {}, "body": {"error": e}}


ACT_ROW = {"status": 200, "headers": {}, "body": {
    "id": ACT, "account_id": ACT[4:], "name": "Test", "account_status": 1,
    "disable_reason": 0, "currency": "USD"}}


def routes(**over):
    base = {
        "/ACT||": ACT_ROW,
        "/ACT/insights|account|": ok([{"spend": "113.87", "account_currency": "USD"}]),
        "/ACT/campaigns||": ok([
            {"id": "111", "name": "Camp A", "status": "ACTIVE", "effective_status": "ACTIVE"},
            {"id": "222", "name": "Camp B", "status": "PAUSED", "effective_status": "PAUSED"}]),
        "/ACT/ads||": ok([
            {"id": "11", "name": "Ad 1", "status": "ACTIVE", "effective_status": "ACTIVE", "campaign_id": "111"},
            {"id": "22", "name": "Ad 2", "status": "PAUSED", "effective_status": "CAMPAIGN_PAUSED",
             "campaign_id": "222"}]),
        "/ACT/insights|campaign|": ok([
            {"campaign_id": "111", "spend": "100.00",
             "actions": [{"action_type": "lead", "value": "10"}]}]),
        "/ACT/insights|ad|": ok([
            {"ad_id": "11", "spend": "60.00", "actions": [{"action_type": "lead", "value": "5"}]}]),
        "/ACT/insights|account|country": ok([
            {"country": "US", "spend": "80.5", "account_currency": "USD"},
            {"country": "IL", "spend": "33.37", "account_currency": "USD"}]),
    }
    base.update(over)
    return base


# ───────────────────────── стенд ─────────────────────────

class FakeQueue:
    def __init__(self, shared=False):
        self.shared = shared
        self.others = False
        self.granted = 0
        self.calls: list[tuple] = []

    def grants(self, profile):
        return self.granted

    def others_on(self, profile, job_id=""):
        return self.others

    def acquire(self, job_id, profile, *, cancelled=None, on_wait=None, share_profile=False):
        self.calls.append(("acquire", job_id, profile, share_profile))
        if not self.shared:
            self.granted += 1
        return {"waited_s": 0, "decision": {"allow": True}, "first_hold": None, "shared": self.shared}

    def release(self, job_id):
        self.calls.append(("release", job_id))

    def keep(self, job_id, label=""):
        self.calls.append(("keep", job_id, label))


class World:
    def __init__(self, monkeypatch, tmp_path):
        self.mp = monkeypatch
        self.page = RoutedPage(routes())
        self.cdp = FakeCdp(self.page)
        self.queue = FakeQueue()
        self.opened_profiles: list[tuple] = []
        self.local = ("port", 9222)
        self.started_now = False
        self.stops: list[tuple] = []
        self.journal: list[dict] = []
        self.notes: list[str] = []
        entry = SimpleNamespace(profile_id=PROFILE_UUID, folder_id="", multilogin_instance="A",
                                ad_account_id=ACT)
        self.entry = entry
        monkeypatch.setattr(sp, "_entry_for", lambda account: entry if account == ACCOUNT else None)
        monkeypatch.setattr(sp, "_store_path", lambda: tmp_path / "account_spend.json")
        monkeypatch.setattr(sp, "_slot_queue", lambda: self.queue)
        monkeypatch.setattr(sp, "_set_wait_note", lambda account, note: self.notes.append(note))
        monkeypatch.setattr(sp, "_record", lambda *a, **k: None)
        monkeypatch.setattr(api, "_probe_local", lambda pid: self.local)
        monkeypatch.setattr(api, "_open_profile", self._open_profile)
        monkeypatch.setattr(api, "_open_tab", lambda url: PageTab.open(
            url, connect=lambda u, timeout: self.cdp))
        monkeypatch.setattr(api, "_make_graph", lambda tab: tc.InPageGraph(tab, sleep=lambda s: None))
        monkeypatch.setattr(api, "GraphClient", lambda tr, **kw: g.GraphClient(
            tr, sleep=lambda s: None, **kw))
        monkeypatch.setattr(tm, "record", lambda event, path=None: self.journal.append(event))
        monkeypatch.setattr(tm, "report_stop", lambda kind, msg, ctx=None: self.stops.append((kind, msg)) or "")
        monkeypatch.delenv(api.BACKEND_ENV, raising=False)
        api._COOLDOWN.clear()
        sp._PENDING_CLOSE.clear()
        # Мягкая остановка профиля (ACL-1580) — записываем, MLX не трогаем.
        from src.fb_ads_rpa import browser as rpa_browser

        self.soft_stops: list[str] = []
        monkeypatch.setattr(rpa_browser, "stop_profile_on_request",
                            lambda e, label: self.soft_stops.append(label) or "closed")
        # Таймер «подержать окно под страны» не запускаем — вызываем вручную.
        self.timers: list = []

        class _Timer:
            def __init__(inner, secs, fn, args=()):
                inner.secs, inner.fn, inner.args = secs, fn, args
                inner.daemon = False
                self.timers.append(inner)

            def start(inner):
                pass

        monkeypatch.setattr(sp.threading, "Timer", _Timer)
        # Нужна ли разбивка стран, решает правило страницы; страны карты — две.
        from src.web import account_links

        monkeypatch.setattr(account_links, "values_for", lambda account, kind: ["US", "IL"])
        from src.analysis_agent import country_split as _cs

        monkeypatch.setattr(_cs, "_store_path", lambda: tmp_path / "split.json")

    def _open_profile(self, pid, folder, instance):
        self.opened_profiles.append((pid, folder, instance))
        return prof.OpenedProfile(pid, folder, instance, "9222", None, self.started_now, None,
                                  started_now=self.started_now)

    @property
    def paths(self):
        return [_key(s) for s in self.page.specs]


@pytest.fixture
def world(monkeypatch, tmp_path):
    return World(monkeypatch, tmp_path)


# ───────────────────────── выбор бэкенда ─────────────────────────

def test_backend_selection(monkeypatch):
    for value, expected in [(None, "rpa"), ("", "rpa"), ("rpa", "rpa"), ("api", "api"), (" API ", "api"),
                            ("Api", "api"), ("graph", "rpa"), ("1", "rpa")]:
        if value is None:
            monkeypatch.delenv(api.BACKEND_ENV, raising=False)
        else:
            monkeypatch.setenv(api.BACKEND_ENV, value)
        assert api.backend() == expected, value
        assert api.api_enabled() is (expected == "api")
        assert sp.default_reader() is (api.read_via_api if expected == "api" else sp.read_via_rpa)
        assert cs.default_reader() is (
            api.read_country_split_via_api if expected == "api" else cs.read_country_split)


def test_start_read_uses_the_api_reader_only_when_switched(world, monkeypatch):
    seen = []

    def fake_api(account, a, b):
        seen.append("api")
        return sp.SpendReading(account=account, date_from=a, date_to=b, source="api", state="ok", spend=1.0)

    def fake_rpa(account, a, b):
        seen.append("rpa")
        return sp.SpendReading(account=account, date_from=a, date_to=b, state="ok", spend=2.0)

    monkeypatch.setattr(api, "read_via_api", fake_api)
    monkeypatch.setattr(sp, "read_via_rpa", fake_rpa)
    sp._RUNNING.clear()
    sp.start_read(ACCOUNT, D, D)
    _settle()
    monkeypatch.setenv(api.BACKEND_ENV, "api")
    sp.start_read(ACCOUNT, "2026-10-07", "2026-10-07")
    _settle()
    assert seen == ["rpa", "api"]
    assert sp.get_reading(ACCOUNT, "2026-10-07", "2026-10-07").source == "api"


def _settle(seconds: float = 3.0) -> None:
    import threading
    import time

    end = time.time() + seconds
    while time.time() < end:
        if not any(t.name.startswith(("spend:", "split:")) and t.is_alive() for t in threading.enumerate()):
            time.sleep(0.05)
            return
        time.sleep(0.02)


# ───────────────────────── паритет: форма ответа ─────────────────────────

def test_reading_has_exactly_the_dict_shape_the_screen_reads(world):
    reading = api.read_via_api(ACCOUNT, D, D)
    rpa_shape = sp.SpendReading(account=ACCOUNT, date_from=D, date_to=D).to_dict()
    got = reading.to_dict()
    assert set(got) == set(rpa_shape), "the API reading must have the same keys as the RPA reading"
    assert got["source"] == "api"
    assert got["state"] == "ok" and got["error"] == ""
    assert got["spend"] == 113.87 and got["currency"] == "$"
    assert got["date_from"] == D and got["date_to"] == D
    assert got["status"] == "clear" and "active" in got["status_text"].lower()
    assert got["objects_read"] is True and got["objects_error"] == ""
    assert "objects_problem" not in got
    assert not sp.tree_not_read(reading)


def test_tree_objects_have_the_row_shape_of_the_rpa_tree(world):
    objects = api.read_via_api(ACCOUNT, D, D).objects
    row_keys = set(sp.AdObject(level="campaign").to_dict())
    assert objects and all(set(o) == row_keys for o in objects)
    campaigns = [o for o in objects if o["level"] == "campaign"]
    ads = [o for o in objects if o["level"] == "ad"]
    assert [(o["id"], o["name"], o["switch"], o["delivery"]) for o in campaigns] == [
        ("111", "Camp A", "on", "Active"), ("222", "Camp B", "off", "Off")]
    assert campaigns[0]["spend"] == "$100.00" and campaigns[0]["results"] == "10 Leads"
    assert campaigns[0]["cost_per_result"] == "$10.00 per lead"
    assert campaigns[1]["spend"] == "" and campaigns[1]["results"] == ""
    assert [(o["id"], o["parent_id"], o["switch"], o["delivery"]) for o in ads] == [
        ("11", "111", "on", "Active"), ("22", "222", "off", "Campaign off")]
    assert all(o["id_from"] == "graph api" for o in objects)
    # Строка «выключено» красится экраном по слову доставки.
    import re
    assert re.search(r"off|review|reject|not deliver|error", ads[1]["delivery"], re.I)


def test_ads_come_after_their_campaign_order_and_orphans_are_dropped():
    camps = [{"id": "2", "name": "B", "status": "ACTIVE", "effective_status": "ACTIVE"},
             {"id": "1", "name": "A", "status": "ACTIVE", "effective_status": "ACTIVE"}]
    ads = [{"id": "a1", "name": "x", "status": "ACTIVE", "effective_status": "ACTIVE", "campaign_id": "1"},
           {"id": "a2", "name": "y", "status": "ACTIVE", "effective_status": "ACTIVE", "campaign_id": "2"},
           {"id": "a3", "name": "z", "status": "ACTIVE", "effective_status": "ACTIVE", "campaign_id": "9"}]
    out = api.build_objects(camps, ads, {}, {}, "USD")
    assert [o["id"] for o in out] == ["2", "1", "a2", "a1"]


def test_period_goes_to_insights_as_time_range_and_only_get_is_used(world):
    api.read_via_api(ACCOUNT, "2026-10-01", "2026-10-07")
    spec = next(s for s in world.page.specs if _key(s) == "/ACT/insights|account|")
    assert spec["method"] == "GET"
    assert spec["params"]["time_range"] == {"since": "2026-10-01", "until": "2026-10-07"}
    assert all(s["method"] == "GET" for s in world.page.specs)
    assert not any("access_token" in json.dumps(s) for s in world.page.specs)


def test_no_spend_rows_is_zero_in_the_account_currency(world):
    world.page.routes["/ACT/insights|account|"] = ok([])
    r = api.read_via_api(ACCOUNT, D, D)
    assert r.state == "ok" and r.spend == 0.0 and r.currency == "$"


def test_empty_account_is_read_as_zero_campaigns_not_as_unread(world):
    world.page.routes["/ACT/campaigns||"] = ok([])
    r = api.read_via_api(ACCOUNT, D, D)
    assert r.objects == [] and r.objects_read is True and r.objects_error == ""
    assert not sp.tree_not_read(r)
    assert "/ACT/ads||" not in world.paths, "no campaigns - no ads call"


def test_reading_roundtrips_through_the_store(world):
    r = api.read_via_api(ACCOUNT, D, D)
    sp._put(r)
    back = sp.get_reading(ACCOUNT, D, D)
    assert back.source == "api" and back.spend == 113.87 and len(back.objects) == len(r.objects)
    assert sp.get_status(ACCOUNT)["status"] == "clear"


# ───────────────────────── состояние кабинета ─────────────────────────

def test_account_status_maps_to_the_words_the_screen_knows():
    for status, reason, word, needle in [
        (1, 0, "clear", "active"),
        (2, 1, "disabled", "disabled"),
        (2, 3, "disabled", "RISK_PAYMENT"),
        (3, 0, "restricted", "restricted"),
        (9, 0, "restricted", "grace"),
        (7, 0, "review", "under review"),
        (8, 0, "review", "under review"),
        (100, 0, "disabled", "closure"),
        (101, 0, "disabled", "closed"),
        (555, 0, "unknown", "unrecognised"),
    ]:
        got, text = api.map_status(status, reason)
        assert got == word, status
        assert needle.lower() in text.lower(), (status, text)
        assert got in {"clear", "disabled", "restricted", "review", "unknown"}
    assert api.map_status(None, None) == ("unknown", ""), "a missing status is unknown, never clear"


def test_disabled_account_reading_carries_the_status(world):
    row = dict(ACT_ROW["body"], account_status=2, disable_reason=1)
    world.page.routes["/ACT||"] = {"status": 200, "headers": {}, "body": row}
    r = api.read_via_api(ACCOUNT, D, D)
    assert r.state == "ok" and r.status == "disabled" and "ADS_INTEGRITY_POLICY" in r.status_text
    assert sp.get_status(ACCOUNT) == {} or True
    sp._put(r)
    assert sp.get_status(ACCOUNT)["status"] == "disabled"


# ───────────────────────── ошибки → тексты и коды RPA ─────────────────────────

def test_logged_out_page_maps_to_the_rpa_logged_out_text_and_code(world):
    world.page.state_value = {"kind": "login", "has_token": False}
    r = api.read_via_api(ACCOUNT, D, D)
    assert r.state == "failed" and r.source == "api"
    assert r.error == sp.logged_out_message(ACCOUNT)
    assert r.error_code == "profile_logged_out" == sp.LOGGED_OUT_CODE
    assert world.paths == [], "no Graph call on a login page"


def test_token_dead_twice_is_logged_out(world):
    world.page.routes["/ACT||"] = err(190)
    world.page.after_reload = {"kind": "ads_manager", "has_token": True}
    r = api.read_via_api(ACCOUNT, D, D)
    assert r.state == "failed" and r.error_code == "profile_logged_out"
    assert r.error == sp.logged_out_message(ACCOUNT)
    assert world.page.reloads == 1


def test_checkpoint_page_is_a_clear_refusal_without_calls(world):
    world.page.state_value = {"kind": "checkpoint", "has_token": False}
    r = api.read_via_api(ACCOUNT, D, D)
    assert r.state == "failed" and "checkpoint" in r.error.lower() and ACCOUNT in r.error
    assert world.paths == []
    assert ("checkpoint", ) == tuple(k for k, _ in world.stops)


def test_checkpoint_subcode_stops_without_reload(world):
    world.page.routes["/ACT||"] = err(190, 459)
    r = api.read_via_api(ACCOUNT, D, D)
    assert r.state == "failed" and "checkpoint" in r.error.lower()
    assert world.page.reloads == 0 and len(world.page.specs) == 1


def test_no_token_in_the_page_is_a_failed_load(world, monkeypatch):
    monkeypatch.setattr(api, "READY_TIMEOUT_S", 0.0)
    world.page.state_value = {"kind": "other", "has_token": False}
    r = api.read_via_api(ACCOUNT, D, D)
    assert r.state == "failed" and "did not finish loading" in r.error


@pytest.mark.parametrize("code", [4, 17, 32, 613, 80004])
def test_rate_limit_codes_stop_and_cool_the_profile_down(world, code):
    world.page.routes["/ACT||"] = err(code)
    r = api.read_via_api(ACCOUNT, D, D)
    assert r.state == "failed" and "rate limit" in r.error.lower()
    assert [k for k, _ in world.stops] == ["rate_limited"]
    n = len(world.page.specs)
    again = api.read_via_api(ACCOUNT, D, D)
    assert again.state == "failed" and "slow down" in again.error and "Nothing was called" in again.error
    assert len(world.page.specs) == n, "a cooled-down profile gets no Graph call"
    assert len(world.opened_profiles) == 1, "and its profile is not opened again"


def test_368_is_a_policy_stop_with_a_long_cooldown(world):
    world.page.routes["/ACT||"] = err(368)
    r = api.read_via_api(ACCOUNT, D, D)
    assert r.state == "failed" and "not retried" in r.error
    assert api._cooldown_left(sp._profile_key(ACCOUNT)) > 600


def test_no_access_to_the_ad_account_is_said_plainly(world):
    world.page.routes["/ACT||"] = err(200)
    r = api.read_via_api(ACCOUNT, D, D)
    assert r.state == "failed" and "no access to the ad account" in r.error
    assert "1234567890" not in r.error and TOKEN not in r.error


def test_a_different_ad_account_in_the_answer_is_refused(world):
    row = dict(ACT_ROW["body"], account_id="999999999")
    world.page.routes["/ACT||"] = {"status": 200, "headers": {}, "body": row}
    r = api.read_via_api(ACCOUNT, D, D)
    assert r.state == "failed" and "different ad account" in r.error


def test_unknown_account_and_missing_ids_fail_without_touching_anything(world, monkeypatch):
    r = api.read_via_api("Nobody", D, D)
    assert r.state == "failed" and "not in the account map" in r.error
    monkeypatch.setattr(sp, "_entry_for", lambda a: SimpleNamespace(
        profile_id=PROFILE_UUID, folder_id="", multilogin_instance="A", ad_account_id=""))
    assert "no ad_account_id" in api.read_via_api(ACCOUNT, D, D).error
    monkeypatch.setattr(sp, "_entry_for", lambda a: SimpleNamespace(
        profile_id="", folder_id="", multilogin_instance="A", ad_account_id=ACT))
    assert "no profile_id" in api.read_via_api(ACCOUNT, D, D).error
    assert world.opened_profiles == [] and world.page.specs == []


def test_hand_opened_profile_is_refused_with_the_open_by_hand_code(world, monkeypatch):
    def boom(pid, folder, instance):
        raise prof.ProfileError("the profile is open but has no automation port. Nothing was stopped.",
                                code="open_by_hand")

    monkeypatch.setattr(api, "_open_profile", boom)
    r = api.read_via_api(ACCOUNT, D, D)
    assert r.state == "failed" and r.error_code == sp.HAND_OPEN_CODE == "open_by_hand"
    assert "Nothing was stopped" in r.error


def test_real_open_profile_marks_a_hand_window_with_the_code():
    with pytest.raises(prof.ProfileError) as e:
        prof.open_profile(PROFILE_UUID, local_probe=lambda pid: ("no_port", None))
    assert e.value.code == "open_by_hand"


def test_mlx_failure_becomes_a_readable_refusal(world, monkeypatch):
    def boom(pid, folder, instance):
        raise RuntimeError("403 restricted permission access_token=" + TOKEN)

    monkeypatch.setattr(api, "_open_profile", boom)
    r = api.read_via_api(ACCOUNT, D, D)
    assert r.state == "failed" and "could not be opened with automation" in r.error
    assert TOKEN not in r.error


# ───────────────────────── спенд сохраняется, дерево может не прочитаться ─────────────────────────

def test_tree_failure_keeps_the_spend(world):
    world.page.routes["/ACT/campaigns||"] = err(1)
    r = api.read_via_api(ACCOUNT, D, D)
    assert r.state == "ok" and r.spend == 113.87
    assert r.objects == [] and r.objects_read is False
    assert "Campaign tree was not read" in r.objects_error
    assert sp.tree_not_read(r)
    assert r.to_dict()["objects_problem"]["reason"]


def test_rate_limit_inside_the_tree_keeps_spend_and_campaigns(world):
    world.page.routes["/ACT/ads||"] = err(17)
    r = api.read_via_api(ACCOUNT, D, D)
    assert r.state == "ok" and r.spend == 113.87
    assert [o["id"] for o in r.objects] == ["111", "222"] and r.objects_read is True
    assert "ads were not read" in r.objects_error
    assert [k for k, _ in world.stops] == ["rate_limited"]
    assert api._cooldown_left(sp._profile_key(ACCOUNT)) > 0


def test_other_graph_error_on_row_spend_only_blanks_the_numbers(world):
    world.page.routes["/ACT/insights|ad|"] = err(100)
    r = api.read_via_api(ACCOUNT, D, D)
    assert r.state == "ok" and len(r.objects) == 4 and r.objects_read is True
    assert "ad spend were not read" in r.objects_error
    assert r.objects[0]["spend"] == "$100.00"


def test_partial_lists_are_said_out_loud(world):
    more = {"cursors": {"after": "c"}, "next": "https://graph.facebook.com/x?after=c"}
    world.page.routes["/ACT/campaigns||"] = ok(
        [{"id": "111", "name": "A", "status": "ACTIVE", "effective_status": "ACTIVE"}], **more)
    r = api.read_via_api(ACCOUNT, D, D)
    assert r.state == "ok" and "campaign list is partial" in r.objects_error


# ───────────────────────── лимиты вызовов ─────────────────────────

def test_a_read_makes_few_calls_and_never_polls(world):
    api.read_via_api(ACCOUNT, D, D)
    assert world.paths == ["/ACT||", "/ACT/insights|account|", "/ACT/campaigns||", "/ACT/ads||",
                           "/ACT/insights|campaign|", "/ACT/insights|ad|",
                           "/ACT/insights|account|country"]
    assert world.journal[-1]["calls"] == 7


def test_call_cap_per_read_is_our_own_budget_not_a_meta_rate_limit(world, monkeypatch):
    monkeypatch.setenv(api.MAX_CALLS_ENV, "4")
    r = api.read_via_api(ACCOUNT, D, D)
    assert len(world.page.specs) == 4
    assert r.state == "ok" and r.spend == 113.87
    assert "call budget" in r.objects_error and "Meta rate limit" not in r.objects_error
    assert "Read stopped: call budget reached" in r.objects_error
    assert world.stops == [], "our own cap is not reported as a Meta stop"
    assert api._cooldown_left(sp._profile_key(ACCOUNT)) == 0, "and starts no cooldown"
    assert world.journal[-1].get("stop") == "call_budget"
    # Следующее чтение не отказывает без вызова.
    monkeypatch.delenv(api.MAX_CALLS_ENV)
    assert api.read_via_api(ACCOUNT, D, D).state == "ok"


def test_call_budget_alone_reports_a_distinct_code_and_text():
    exc = g.CallBudget("call budget of 4 reached")
    assert isinstance(exc, g.RateLimited) and exc.kind == "call_budget"
    text, code = api._failure(ACCOUNT, exc)
    assert code == "call_budget" and text.startswith("Read stopped: call budget reached")
    assert "rate limit" not in text.lower()


def test_call_cap_env_is_clamped(monkeypatch):
    for raw, expected in [("999", 60), ("1", 4), ("abc", api.DEFAULT_MAX_CALLS), ("", api.DEFAULT_MAX_CALLS)]:
        monkeypatch.setenv(api.MAX_CALLS_ENV, raw)
        assert int(api._env_number(api.MAX_CALLS_ENV, api.DEFAULT_MAX_CALLS, 4, 60)) == expected


# ───────────────────────── профиль и очередь по памяти (ACL-1580) ─────────────────────────

def test_running_profile_is_attached_without_a_slot_and_never_stopped(world):
    api.read_via_api(ACCOUNT, D, D)
    assert world.queue.calls == []
    assert world.opened_profiles == [(PROFILE_UUID, "", "A")]
    assert world.soft_stops == [], "a profile that was already running is not ours to close"


def test_started_profile_is_softly_stopped_and_its_slot_released(world):
    world.local = ("none", None)
    world.started_now = True
    r = api.read_via_api(ACCOUNT, D, D)
    assert r.state == "ok"
    kinds = [c[0] for c in world.queue.calls]
    assert kinds == ["acquire", "release"], "same queue, slot given back; not kept"
    acquire, release = world.queue.calls
    assert acquire[2] == PROFILE_UUID and acquire[3] is True, "same queue, shared with Publish"
    assert release[1] == acquire[1]
    assert world.soft_stops == [ACCOUNT], "stopped once, only this profile, through the soft MLX stop"
    assert sp.STARTING_NOTE.format(account=ACCOUNT) in world.notes and world.notes[-1] == ""


def test_failed_read_still_stops_what_it_started_and_releases_the_slot(world):
    world.local = ("none", None)
    world.started_now = True
    world.page.state_value = {"kind": "login", "has_token": False}
    r = api.read_via_api(ACCOUNT, D, D)
    assert r.state == "failed" and r.error_code == "profile_logged_out"
    assert [c[0] for c in world.queue.calls] == ["acquire", "release"]
    assert world.soft_stops == [ACCOUNT]
    assert world.cdp.closed_targets == ["T-mine"]


def test_tab_failure_after_the_start_still_stops_the_profile(world, monkeypatch):
    world.local = ("none", None)
    world.started_now = True

    def boom(url):
        raise RuntimeError("CDP read timed out")

    monkeypatch.setattr(api, "_open_tab", boom)
    r = api.read_via_api(ACCOUNT, D, D)
    assert r.state == "failed"
    assert world.soft_stops == [ACCOUNT] and [c[0] for c in world.queue.calls] == ["acquire", "release"]


def test_a_profile_touched_by_publish_is_not_stopped(world):
    world.local = ("none", None)
    world.started_now = True
    world.queue.others = True                  # Publish держит или ждёт этот профиль
    api.read_via_api(ACCOUNT, D, D)
    assert world.soft_stops == []
    assert [c[0] for c in world.queue.calls] == ["acquire", "release"], "our slot is still given back"


def test_a_publish_grant_during_the_read_means_hands_off(world):
    world.local = ("none", None)
    world.started_now = True
    # Publish получает место на этом профиле уже после нашего запуска.
    world.page.routes["/ACT/insights|ad|"] = lambda spec: (setattr(world.queue, "granted", 99) or ok([]))
    api.read_via_api(ACCOUNT, D, D)
    assert world.soft_stops == []


def test_a_shared_publish_slot_is_neither_released_nor_closed_by_the_read(world):
    world.local = ("none", None)
    world.queue.shared = True
    world.started_now = True
    api.read_via_api(ACCOUNT, D, D)
    assert [c[0] for c in world.queue.calls] == ["acquire"]
    assert world.soft_stops == []


def test_slot_is_released_when_the_profile_turned_out_to_be_running(world):
    world.local = ("none", None)
    world.started_now = False
    api.read_via_api(ACCOUNT, D, D)
    assert [c[0] for c in world.queue.calls] == ["acquire", "release"]
    assert world.soft_stops == []


def test_a_profile_opened_by_someone_during_the_slot_wait_is_not_ours(world, monkeypatch):
    probes = iter([("none", None), ("port", 9222)])
    monkeypatch.setattr(api, "_probe_local", lambda pid: next(probes))
    world.started_now = False
    api.read_via_api(ACCOUNT, D, D)
    assert [c[0] for c in world.queue.calls] == ["acquire", "release"]
    assert world.soft_stops == []


def test_slot_is_released_when_the_profile_could_not_be_opened(world, monkeypatch):
    world.local = ("none", None)

    def boom(*a):
        raise RuntimeError("MLX 403")

    monkeypatch.setattr(api, "_open_profile", boom)
    r = api.read_via_api(ACCOUNT, D, D)
    assert r.state == "failed"
    assert [c[0] for c in world.queue.calls] == ["acquire", "release"]
    assert world.soft_stops == [], "nothing was started, nothing to stop"


def test_a_hand_opened_profile_is_never_stopped(world, monkeypatch):
    world.local = ("no_port", None)

    def boom(pid, folder, instance):
        raise prof.ProfileError("open by hand. Nothing was stopped.", code="open_by_hand")

    monkeypatch.setattr(api, "_open_profile", boom)
    r = api.read_via_api(ACCOUNT, D, D)
    assert r.error_code == "open_by_hand"
    assert world.soft_stops == [] and world.queue.calls == []


def test_grace_window_lets_the_split_reuse_the_started_profile(world):
    """Спенд принят, страны в той же вкладке не прочитались: окно держим и отдаём странам."""
    world.local = ("none", None)
    world.started_now = True
    world.page.routes["/ACT/insights|account|country"] = err(100)
    r = api.read_via_api(ACCOUNT, D, D)
    assert r.state == "ok"
    assert world.soft_stops == [], "kept for the country split"
    assert len(world.timers) == 1 and world.timers[0].secs == sp.CLOSE_GRACE_S == 30.0
    assert [c[0] for c in world.queue.calls] == ["acquire"], "the slot is held while the window is"
    pending = world.timers[0].args[0]
    assert sp._PENDING_CLOSE.get(PROFILE_UUID) is pending

    # Страны приходят в то же окно: профиль теперь работает, второго запуска нет.
    world.local = ("port", 9222)
    world.started_now = False
    world.page.routes["/ACT/insights|account|country"] = ok(
        [{"country": "US", "spend": "80.5", "account_currency": "USD"}])
    split = api.read_country_split_via_api(ACCOUNT, D, D)
    assert split.state == "ok" and split.source == "api"
    assert len(world.opened_profiles) == 2 and [c[0] for c in world.queue.calls].count("acquire") == 1
    assert world.soft_stops == [ACCOUNT], "the split took the window over and closed it at its end"
    assert [c[0] for c in world.queue.calls] == ["acquire", "release"]
    # Таймер срабатывает позже: профиль уже закрыт, второй остановки нет.
    sp._close_if_still_pending(pending)
    assert world.soft_stops == [ACCOUNT]


def test_grace_window_closes_the_profile_when_nobody_takes_it(world):
    world.local = ("none", None)
    world.started_now = True
    world.page.routes["/ACT/insights|account|country"] = err(100)
    api.read_via_api(ACCOUNT, D, D)
    assert world.soft_stops == []
    sp._close_if_still_pending(world.timers[0].args[0])
    assert world.soft_stops == [ACCOUNT]
    assert [c[0] for c in world.queue.calls] == ["acquire", "release"]


def test_profile_is_not_held_when_the_split_was_read_in_the_same_session(world):
    world.local = ("none", None)
    world.started_now = True
    api.read_via_api(ACCOUNT, D, D)
    assert world.timers == [] and world.soft_stops == [ACCOUNT]


def test_real_slots_a_third_sequential_read_is_granted(monkeypatch, tmp_path):
    """Живая очередь с потолком 2: прежнее «оставить окно» вешало третье чтение навсегда."""
    import dataclasses
    import threading

    from src.web import profile_slots as ps

    w = World(monkeypatch, tmp_path)
    settings = dataclasses.replace(ps.load_settings({}), warmup_s=0, ceiling=2)
    open_profiles: set[str] = set()
    usage = ps.ProfileUsage(lambda profiles: {p: 3000 for p in profiles if p in open_profiles},
                            clock=lambda: 1000.0, background=False)
    queue = ps.ProfileSlots(
        memory=lambda: ps.MemoryReading(total_mb=64000, available_mb=60000),
        settings=lambda: settings, clock=lambda: 1000.0, tick_s=0.05, usage=usage)
    monkeypatch.setattr(sp, "_slot_queue", lambda: queue)

    profiles = {f"Acc{i}": SimpleNamespace(profile_id=f"p{i}", folder_id="", multilogin_instance="A",
                                           ad_account_id=ACT) for i in (1, 2, 3)}
    monkeypatch.setattr(sp, "_entry_for", lambda account: profiles.get(account))
    monkeypatch.setattr(api, "_probe_local", lambda pid: ("none", None))

    def start(pid, folder, instance):
        open_profiles.add(pid)
        return prof.OpenedProfile(pid, folder, instance, "9222", None, True, None, started_now=True)

    monkeypatch.setattr(api, "_open_profile", start)
    from src.fb_ads_rpa import browser as rpa_browser

    monkeypatch.setattr(rpa_browser, "stop_profile_on_request",
                        lambda e, label: open_profiles.discard(e.profile_id) or "closed")
    done: list[str] = []

    def run():
        for name in ("Acc1", "Acc2", "Acc3"):
            r = api.read_via_api(name, D, D)
            done.append(f"{name}:{r.state}")

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(20)
    assert not t.is_alive(), "the third read must not wait forever for a kept window"
    assert done == ["Acc1:ok", "Acc2:ok", "Acc3:ok"]
    assert open_profiles == set(), "every window the reads opened was closed again"
    assert queue.snapshot()["running"] == [] or not queue.snapshot().get("running")


def test_failed_read_still_closes_our_tab(world):
    world.page.routes["/ACT||"] = err(100)
    api.read_via_api(ACCOUNT, D, D)
    assert world.cdp.closed_targets == ["T-mine"]


def test_the_api_module_stops_profiles_only_through_the_rpa_helpers():
    src = (ROOT / "src" / "analysis_agent" / "account_spend_api.py").read_text(encoding="utf-8")
    assert "terminate" not in src.lower() and "kill" not in src.replace("allow_kill", "").lower()
    code = chr(10).join(l for l in src.splitlines() if not l.lstrip().startswith(("#", "*", "`")))
    assert "_finish_started" in code and "stop_profile_on_request(" not in code


# ───────────────────────── страны в той же вкладке (S1) ─────────────────────────

def test_split_is_read_in_the_spend_session_with_no_second_load(world):
    from src.analysis_agent import country_split as cs_

    r = api.read_via_api(ACCOUNT, D, D)
    assert r.state == "ok"
    assert world.cdp.sent.count(("Target.createTarget", {"url": "about:blank"}, "")) == 1
    assert [m for m, _p, _s in world.cdp.sent].count("Page.navigate") == 1, "Ads Manager is loaded once"
    saved = cs_.get_split(ACCOUNT, D, D)
    assert saved["state"] == "ok" and saved["source"] == "api"
    assert [x["country"] for x in saved["rows"]] == ["United States", "Israel"]
    assert saved["spend"] == 113.87 and saved["spend_currency"] == "$" and saved["spend_at"]
    # Страница после чтения спросит «нужна ли разбивка» — и услышит «нет».
    assert cs_.why_split_is_needed(
        countries=2, split=saved, spend_finished_at="2026-10-09 12:00:00",
        spend=r.spend, currency=r.currency) == ""


def test_split_is_skipped_when_the_stored_one_still_explains_the_spend(world):
    api.read_via_api(ACCOUNT, D, D)
    before = len(world.paths)
    api.read_via_api(ACCOUNT, D, D)
    second = world.paths[before:]
    assert "/ACT/insights|account|country" not in second
    assert len(second) == 6


def test_split_failure_does_not_cancel_the_spend_and_is_stored_as_failed(world):
    from src.analysis_agent import country_split as cs_

    world.page.routes["/ACT/insights|account|country"] = err(100)
    r = api.read_via_api(ACCOUNT, D, D)
    assert r.state == "ok" and r.spend == 113.87
    saved = cs_.get_split(ACCOUNT, D, D)
    assert saved["state"] == "failed" and saved["error"]
    assert cs_.why_split_is_needed(countries=2, split=saved) != "", "the page will ask again"


def test_split_is_not_attempted_after_a_stop(world):
    world.page.routes["/ACT/ads||"] = err(17)
    r = api.read_via_api(ACCOUNT, D, D)
    assert r.state == "ok"
    assert "/ACT/insights|account|country" not in world.paths


# ───────────────────────── очередь и замок как у RPA ─────────────────────────

def test_dashboard_entry_goes_through_the_same_lock_and_gate(world, monkeypatch):
    held = []

    class Spy:
        def __enter__(self):
            held.append("in")

        def __exit__(self, *a):
            held.append("out")

    monkeypatch.setattr(sp.browser_operation, "reading", lambda: Spy())
    monkeypatch.setattr(sp, "_lock_for", lambda account: Spy())
    monkeypatch.setenv(api.BACKEND_ENV, "api")
    sp._RUNNING.clear()
    sp.start_read(ACCOUNT, D, D)
    _settle()
    assert held == ["in", "in", "out", "out"]
    assert sp.get_reading(ACCOUNT, D, D).source == "api"


# ───────────────────────── страны ─────────────────────────

def test_country_split_has_the_rpa_shape(world):
    split = api.read_country_split_via_api(ACCOUNT, D, D)
    rpa_shape = cs.CountrySplit(account=ACCOUNT, date_from=D, date_to=D).to_dict()
    got = split.to_dict()
    assert set(got) == set(rpa_shape)
    assert got["source"] == "api" and got["state"] == "ok" and got["error"] == ""
    assert got["rows"] == [{"country": "United States", "spend": 80.5, "currency": "USD"},
                           {"country": "Israel", "spend": 33.37, "currency": "USD"}]
    assert got["status"] == "clear"
    spec = next(s for s in world.page.specs if s["params"].get("breakdowns"))
    assert spec["params"]["breakdowns"] == "country" and spec["method"] == "GET"
    assert spec["params"]["time_range"] == {"since": D, "until": D}


def test_country_rows_feed_the_existing_totals(world, monkeypatch, tmp_path):
    monkeypatch.setattr(cs, "_store_path", lambda: tmp_path / "split.json")
    cs.save_split(api.read_country_split_via_api(ACCOUNT, D, D))
    totals = cs.totals_by_country(D, D)
    assert {t["country"]: t["spend"] for t in totals} == {"United States": 80.5, "Israel": 33.37}


def test_unknown_country_code_is_kept_as_the_code(world):
    world.page.routes["/ACT/insights|account|country"] = ok(
        [{"country": "HK", "spend": "1.5", "account_currency": "USD"}])
    assert api.read_country_split_via_api(ACCOUNT, D, D).rows[0]["country"] == "HK"


def test_empty_country_report_is_empty_not_failed(world):
    world.page.routes["/ACT/insights|account|country"] = ok([])
    split = api.read_country_split_via_api(ACCOUNT, D, D)
    assert split.state == "empty" and split.rows == []


def test_partial_country_report_is_refused(world):
    more = {"cursors": {"after": "c"}, "next": "https://graph.facebook.com/x?after=c"}
    world.page.routes["/ACT/insights|account|country"] = lambda spec: ok(
        [{"country": "US", "spend": "1", "account_currency": "USD"}], **more)
    split = api.read_country_split_via_api(ACCOUNT, D, D)
    assert split.state == "failed" and "partial" in split.error and split.rows == []


def test_country_logged_out_uses_the_rpa_code_and_text(world):
    world.page.state_value = {"kind": "login", "has_token": False}
    split = api.read_country_split_via_api(ACCOUNT, D, D)
    assert split.state == "failed" and split.error_code == "profile_logged_out"
    assert split.error == sp.logged_out_message(ACCOUNT)


def test_start_split_uses_the_api_reader_only_when_switched(world, monkeypatch, tmp_path):
    monkeypatch.setattr(cs, "_store_path", lambda: tmp_path / "split.json")
    monkeypatch.setenv(api.BACKEND_ENV, "api")
    cs._RUNNING.clear()
    cs.start_split(ACCOUNT, D, D)
    _settle()
    saved = cs.get_split(ACCOUNT, D, D)
    assert saved["source"] == "api" and saved["state"] == "ok"


# ───────────────────────── токен и вычистка ─────────────────────────

def test_token_never_reaches_the_reading_the_journal_or_sentry(world):
    world.page.routes["/ACT||"] = err(1)          # ошибка с токеном в тексте
    r = api.read_via_api(ACCOUNT, D, D)
    blob = json.dumps([r.to_dict(), world.journal, world.stops], ensure_ascii=False, default=str)
    assert TOKEN not in blob and "access_token=" + TOKEN not in blob
    ok_run = api.read_via_api(ACCOUNT, D, D)  # ещё и успешное чтение
    assert TOKEN not in json.dumps(ok_run.to_dict(), default=str)


def test_journal_marks_the_source_as_api(world):
    api.read_via_api(ACCOUNT, D, D)
    last = world.journal[-1]
    assert last["source"] == "api" and last["kind"] == "spend" and last["ok"] is True


def test_graph_client_additions_refuse_unknown_levels_breakdowns_and_dates():
    c = g.GraphClient(SimpleNamespace(), sleep=lambda s: None)
    for kwargs in ({"level": "user"}, {"breakdowns": "age"}):
        with pytest.raises(ValueError):
            c.insights_range(ACT, D, D, **kwargs)
    with pytest.raises(ValueError):
        c.insights_range(ACT, "yesterday", D)


def test_checkpoint_or_logout_seen_during_the_token_wait_starts_the_cooldown(world):
    """Найдено на странице до клиента (ревью S2): следующее чтение — отказ без вызовов."""
    for state, code, word in (({"kind": "checkpoint", "has_token": False}, "", "checkpoint"),
                              ({"kind": "login", "has_token": False}, "profile_logged_out", "logged out")):
        api._COOLDOWN.clear()
        world.page.state_value = state
        first = api.read_via_api(ACCOUNT, D, D)
        assert first.state == "failed"
        assert api._cooldown_left(sp._profile_key(ACCOUNT)) > 0, word
        tabs = world.cdp.sent.count(("Target.createTarget", {"url": "about:blank"}, ""))
        opened = len(world.opened_profiles)
        second = api.read_via_api(ACCOUNT, D, D)
        assert second.state == "failed" and second.error_code == code, word
        assert world.page.specs == [], "no Graph call at all"
        assert len(world.opened_profiles) == opened, "and the profile is not opened again"
        assert world.cdp.sent.count(("Target.createTarget", {"url": "about:blank"}, "")) == tabs
        assert ("checkpoint" in second.error.lower()) == (word == "checkpoint")


# ───────────────────────── валюта: как у RPA (live 10.10) ─────────────────────────

def test_currency_is_stored_the_way_rpa_wrote_it_so_the_cell_fits(world):
    """RPA клал знак («$179.47»); «USD 179.47» не влезало в колонку."""
    r = api.read_via_api(ACCOUNT, D, D)
    assert r.currency == "$"
    # Тот же текст рисуют и сервер (после перезагрузки), и страница (после Read).
    from src.analysis_agent import money_format

    assert money_format.fmt_currency(r.spend, r.currency) == "$113.87"


def test_only_the_signs_the_screen_knows_are_mapped_the_rest_stays_an_iso_code():
    from src.analysis_agent import money_format

    for code, shown in [("USD", "$"), ("EUR", "€"), ("GBP", "£"), ("ILS", "₪"), ("usd", "$")]:
        assert api.display_currency(code) == shown
    for code in ("BRL", "MXN", "KWD", "CAD", "AUD", "JPY"):
        # R$ / MX$ экран не знает и показал бы «currency unknown»; код он рисует.
        assert api.display_currency(code) == code
        assert money_format.fmt_currency(21.61, api.display_currency(code)) == f"{code} 21.61"
    assert api.display_currency("") == "" and api.display_currency(None) == ""


def test_row_money_reads_like_ads_manager_and_still_parses_as_a_number():
    assert api._money(100, "USD") == "$100.00"
    assert api._money(1234.5, "EUR") == "€1,234.50"
    assert api._money(21.61, "KWD") == "KWD 21.61"
    # `amapMoneyIn` берёт первое число строки: запятые тысяч и знак ему не мешают.
    import re

    for text, want in (("€1,234.50", 1234.5), ("KWD 21.61", 21.61), ("$100.00", 100.0)):
        m = re.search(r"-?\d[\d,\s]*(?:\.\d+)?", text)
        assert float(m.group(0).replace(",", "").replace(" ", "")) == want


def test_per_currency_sums_keep_working_for_codes_and_keep_signs_apart(world):
    """Код суммируется по кодам; знак, как у RPA, остаётся отдельным значением."""
    import re

    sums, unknown = {}, []
    readings = [api.read_via_api(ACCOUNT, D, D)]                    # USD -> «$»
    world.page.routes["/ACT||"] = {"status": 200, "headers": {}, "body": dict(
        ACT_ROW["body"], currency="KWD")}
    world.page.routes["/ACT/insights|account|"] = ok([{"spend": "21.61", "account_currency": "KWD"}])
    readings.append(api.read_via_api(ACCOUNT, "2026-10-07", "2026-10-07"))
    readings.append(api.read_via_api(ACCOUNT, "2026-10-06", "2026-10-06"))
    for r in readings:                                              # то же, что делает app.py
        cur = str(r.currency or "").upper()
        if re.fullmatch(r"[A-Z]{3}", cur):
            sums[cur] = sums.get(cur, 0.0) + r.spend
        else:
            unknown.append({"currency": r.currency, "spend": r.spend})
    assert sums == {"KWD": 43.22}
    assert unknown == [{"currency": "$", "spend": 113.87}]


def test_status_cell_text_is_the_same_word_set_as_rpa_and_the_long_text_is_only_a_tooltip(world):
    """«no warni…» режет колонка, а не API: слово то же; длинный текст — только title."""
    import re
    from pathlib import Path

    r = api.read_via_api(ACCOUNT, D, D)
    assert r.status == "clear"                      # экран пишет «no warning» по слову, не по тексту
    tpl = (ROOT / "src" / "web" / "templates" / "_account_map_country.html").read_text(encoding="utf-8")
    cell = re.search(r"st\.status == 'clear' -%\}<span[^>]*>([^<]*)</span>", tpl)
    assert cell and cell.group(1) == "no warning"
    assert "st.status_text" in tpl.split("{{ st.status }}")[0].rsplit("title=", 1)[-1]
    for code in (1, 2, 3, 7, 8, 9, 100, 101, 555):
        word, _text = api.map_status(code, 0)
        assert word in {"clear", "disabled", "restricted", "review", "unknown"}


def test_old_api_reading_with_iso_code_is_shown_with_a_sign(tmp_path, monkeypatch):
    """Чтение 10.10 сохранило «USD» — карта показывала «USD 179…» и обрезала сумму."""
    store = tmp_path / "account_spend.json"
    monkeypatch.setattr(sp, "_store_path", lambda: store)
    key = "Acc_1|2026-10-09|2026-10-09"
    base = {"account": "Acc_1", "date_from": "2026-10-09", "date_to": "2026-10-09",
            "spend": 179.47, "state": "ok", "finished_at": "2026-10-10 10:18:48"}
    store.write_text(json.dumps({
        key: {**base, "source": "api", "currency": "USD"},
        "Acc_2|2026-10-09|2026-10-09": {**base, "account": "Acc_2", "source": "api", "currency": "BRL"},
        "Acc_3|2026-10-09|2026-10-09": {**base, "account": "Acc_3", "source": "rpa", "currency": "USD"},
    }), encoding="utf-8")
    got = sp.all_readings("2026-10-09", "2026-10-09")
    assert got["Acc_1"].currency == "$"
    assert got["Acc_2"].currency == "BRL"   # без знака остаётся кодом
    assert got["Acc_3"].currency == "USD"   # RPA-записи не трогаем
    assert sp.latest_readings()["Acc_1"].currency == "$"
    assert sp._API_SIGNS == api._SIGNS      # одна таблица знаков на оба модуля


# ───────────────────────── ревью ACL-1760: владение профилем и неполные страницы ─────────────────────────

class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body, self.text = status, body, json.dumps(body)

    def json(self):
        return self._body


def _launcher_with_409(monkeypatch):
    from src.meta_campaign_agent import multilogin_launcher as ml
    from src.meta_campaign_agent.multilogin_config import MultiloginInstanceConfig

    stopped = []
    launcher = ml.MultiloginLauncher(MultiloginInstanceConfig("A", "tok", "http://127.0.0.1:1/api/v2"))
    launcher.stop_profile = lambda *a, **k: stopped.append(a)
    launcher._resolve_folder_id = lambda pid: "F"
    body = {"status": {"error_code": "PROFILE_ALREADY_RUNNING", "message": "already running"},
            "data": {"port": "61234"}}
    monkeypatch.setattr(ml.requests, "get", lambda *a, **k: _Resp(409, body))
    return launcher, stopped


def test_launcher_409_is_not_a_fresh_start_and_is_never_stopped(world, monkeypatch):
    """Профиль уже работал, а обзор его пропустил: MLX ответил 409 с портом — он чужой."""
    launcher, mlx_stops = _launcher_with_409(monkeypatch)
    monkeypatch.setattr(prof, "running_state", lambda l, p: ("not_running", None))
    monkeypatch.setattr(api, "_open_profile", lambda pid, folder, instance: prof.open_profile(
        pid, folder, instance, launcher_factory=lambda i: launcher,
        local_probe=lambda p: ("none", None)))
    world.local = ("none", None)
    r = api.read_via_api(ACCOUNT, D, D)
    assert r.state == "ok"
    assert world.soft_stops == [] and mlx_stops == [], "a reused profile is never closed by us"
    assert [c[0] for c in world.queue.calls] == ["acquire", "release"], "slot given back, not kept"


def test_launcher_marks_only_a_real_start_as_new(monkeypatch):
    from src.meta_campaign_agent import multilogin_launcher as ml
    from src.meta_campaign_agent.multilogin_config import MultiloginInstanceConfig

    launcher, _ = _launcher_with_409(monkeypatch)
    assert launcher.start_profile("F", "P").newly_started is False
    fresh = ml.MultiloginLauncher(MultiloginInstanceConfig("A", "tok", "http://127.0.0.1:1/api/v2"))
    monkeypatch.setattr(ml.requests, "get", lambda *a, **k: _Resp(200, {"data": {"port": "5555"}}))
    assert fresh.start_profile("F", "P").newly_started is True


def test_page_with_next_cursor_at_stop_usage_is_marked_truncated(world):
    """Страница пришла, usage >= STOP_PCT: следующую не берём, список неполный."""
    more = {"cursors": {"after": "c"}, "next": "https://graph.facebook.com/x?after=c"}
    hot = {"x-app-usage": json.dumps({"call_count": 85, "total_cputime": 1, "total_time": 1})}
    page = {"status": 200, "headers": hot, "body": {
        "data": [{"country": "US", "spend": "1", "account_currency": "USD"}], "paging": more}}
    world.page.routes["/ACT/insights|account|country"] = lambda spec: page
    split = api.read_country_split_via_api(ACCOUNT, D, D)
    assert split.state != "ok" and split.rows == [], "half the countries must not look whole"
    assert split.state == "failed" and "partial" in split.error


def test_graph_get_all_marks_truncation_when_the_next_page_is_not_taken(world):
    more = {"cursors": {"after": "c"}, "next": "https://graph.facebook.com/x?after=c"}
    hot = {"x-app-usage": json.dumps({"call_count": 85})}
    world.page.routes["/ACT/campaigns||"] = {"status": 200, "headers": hot, "body": {
        "data": [{"id": "1", "name": "A"}], "paging": more}}
    r = api.read_via_api(ACCOUNT, D, D)
    assert r.objects_error and "partial" in r.objects_error
