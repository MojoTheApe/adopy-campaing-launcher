"""ACL-1761: зависание вкладки и разлогин — классификация таймаута (F1), телеметрия (F2),
порт профиля (F3), последняя вкладка (F4). Только подделки: ни Meta, ни MLX, ни CDP."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.fb_api import creator as cr  # noqa: E402
from src.fb_api import graph as g  # noqa: E402
from src.fb_api import profile as prof  # noqa: E402
from src.fb_api import telemetry as tm  # noqa: E402
from src.fb_api import token_capture as tc  # noqa: E402
from src.fb_api.cdp import CdpTimeout, PageTab  # noqa: E402

SECRET_QUERY = "next=%2Fadsmanager%2F%3Ftoken%3DSECRETVALUE123&access_token=SECRETVALUE456"
LOGIN = f"https://www.facebook.com/login/?{SECRET_QUERY}"
CHECKPOINT = "https://www.facebook.com/checkpoint/1501092823525282/?" + SECRET_QUERY
ADSMANAGER = "https://adsmanager.facebook.com/adsmanager/manage/campaigns?act=123456789&" + SECRET_QUERY


# ───────────────────────── F1: таймаут вкладки ─────────────────────────

class HangTab:
    """Вкладка, рендерер которой завис; браузер на вопрос об URL отвечает (или молчит)."""

    def __init__(self, url: str | None):
        self.url = url
        self.asked = 0
        self.evals = 0

    def create_isolated_world(self, name):
        return 7

    def evaluate_in(self, ctx, expr, timeout=60.0):
        self.evals += 1
        raise CdpTimeout("Runtime.evaluate: the browser did not answer within 15 s")

    def navigate(self, url, timeout=30.0):
        pass

    def browser_url(self, timeout=5.0):
        self.asked += 1
        return self.url or ""


def _graph(url):
    tab = HangTab(url)
    return tc.InPageGraph(tab, sleep=lambda s: None), tab


def test_cdp_timeout_on_login_url_stops_as_logged_out():
    graph, tab = _graph(LOGIN)
    with pytest.raises(g.LoggedOut) as ei:
        graph.wait_ready(timeout=5)
    assert "SECRETVALUE" not in str(ei.value) and "next=" not in str(ei.value)
    assert tab.asked == 1 and tab.evals == 1          # без повторов


def test_cdp_timeout_on_checkpoint_url_stops_as_checkpoint():
    graph, tab = _graph(CHECKPOINT)
    with pytest.raises(g.Checkpoint) as ei:
        graph.wait_ready(timeout=5)
    assert "SECRETVALUE" not in str(ei.value)
    assert "1501092823525282" not in str(ei.value) and "checkpoint/" not in str(ei.value)


def test_cdp_timeout_on_ads_manager_is_page_unresponsive_without_the_query():
    graph, tab = _graph(ADSMANAGER)
    with pytest.raises(g.PageUnresponsive) as ei:
        graph.wait_ready(timeout=5)
    text = str(ei.value)
    assert "adsmanager.facebook.com/adsmanager/manage/campaigns" in text
    assert "SECRETVALUE" not in text and "act=" not in text and "?" not in text
    assert ei.value.kind == "page_unresponsive"


@pytest.mark.parametrize("url", [None, ""])
def test_target_info_failure_keeps_original_cdp_timeout(url):
    graph, tab = _graph(url)
    with pytest.raises(CdpTimeout):
        graph.wait_ready(timeout=5)
    assert tab.asked == 1 and tab.evals == 1


def test_a_graph_call_timeout_is_classified_and_recorded_on_the_client():
    class OkThenHang(HangTab):
        def evaluate_in(self, ctx, expr, timeout=60.0):
            if expr.lstrip().startswith("(() =>"):
                return None
            if ".state()" in expr:
                return {"kind": "ads_manager", "has_token": True}
            return super().evaluate_in(ctx, expr, timeout)

    tab = OkThenHang(ADSMANAGER)
    client = g.GraphClient(tc.InPageGraph(tab, sleep=lambda s: None), sleep=lambda s: None,
                           min_interval_s=0.0, max_calls=5)
    with pytest.raises(g.PageUnresponsive):
        client.get("/me/adaccounts")
    assert isinstance(client.stopped, g.PageUnresponsive)
    with pytest.raises(g.PageUnresponsive):            # дальше клиент не зовёт ничего
        client.get("/me/adaccounts")
    assert tab.evals == 1


class RawBrowser:
    """Сырой CDP: рендерер молчит, браузерный процесс отвечает."""

    def __init__(self, url="https://www.facebook.com/login/?x=1", info_fails=False):
        self.calls = []
        self.url, self.info_fails = url, info_fails

    def call(self, method, params=None, *, session_id="", timeout=5.0):
        self.calls.append((method, dict(params or {}), session_id, timeout))
        if method == "Target.getTargetInfo":
            if self.info_fails:
                raise TimeoutError("no answer")
            return {"targetInfo": {"targetId": "T", "url": self.url}}
        raise TimeoutError("renderer is hung")

    def close(self):
        pass


def test_browser_url_asks_the_browser_not_the_hung_page_session():
    cdp = RawBrowser()
    tab = PageTab(cdp, "T", "S")
    assert tab.browser_url() == "https://www.facebook.com/login/?x=1"
    method, params, session, timeout = cdp.calls[0]
    assert method == "Target.getTargetInfo" and params == {"targetId": "T"}
    assert session == "" and timeout <= 5.0
    assert PageTab(RawBrowser(info_fails=True), "T", "S").browser_url() == ""


def _publisher_with(monkeypatch, tab, opened_log, tmp_path):
    from src.analysis_agent import account_spend as acs
    from tests.test_acl2262_builder_opens_profile import closed_profile_rig
    from src.analysis_agent import account_spend_api as api
    from src.web import publish_api

    monkeypatch.setattr(api, "_COOLDOWN", {})
    monkeypatch.setattr(acs, "_profile_key", lambda account, *a, **k: account)
    monkeypatch.setattr(tm, "report_stop", lambda *a, **k: "")
    monkeypatch.setattr(publish_api, "_resolve_entry", lambda label: SimpleNamespace(
        ad_account_id="act_123456789", profile_id="550e8400-e29b-41d4-a716-446655440000",
        folder_id="", multilogin_instance="A"))

    # ACL-2262: a closed profile is opened by Publish through the warm path (fake registry, no MLX);
    # the checkpoint / logout / frozen-tab stops below are verified on that path.
    closed_profile_rig(monkeypatch, tmp_path, opened_log, "550e8400-e29b-41d4-a716-446655440000")
    monkeypatch.setattr(publish_api, "_open_profile", lambda *a: pytest.fail("the cold path must not be used"))
    monkeypatch.setattr(publish_api, "_open_tab", lambda url: pytest.fail("no own tab on the warm path"))
    monkeypatch.setattr(publish_api, "_make_graph",
                        lambda t, act, *, writes=True: tc.InPageGraph(tab, sleep=lambda s: None))
    return publish_api


def test_open_stops_as_logged_out_with_cooldown_and_no_graph_calls(monkeypatch, tmp_path):
    tab, opened = HangTab(LOGIN), []
    pa = _publisher_with(monkeypatch, tab, opened, tmp_path)
    pub = pa.ApiPublisher()
    with pytest.raises(pa.ApiPublishError) as ei:
        pub.open("Acc")
    assert ei.value.code == "logged_out" and "SECRETVALUE" not in str(ei.value)
    assert pub._client is None
    left, why = pa._cooldown("Acc")
    assert left > 0 and why == "logged_out"


def test_open_stops_as_checkpoint(monkeypatch, tmp_path):
    pa = _publisher_with(monkeypatch, HangTab(CHECKPOINT), [], tmp_path)
    with pytest.raises(pa.ApiPublishError) as ei:
        pa.ApiPublisher().open("Acc")
    assert ei.value.code == "checkpoint"


def test_page_unresponsive_cools_the_profile_for_ten_minutes(monkeypatch, tmp_path):
    opened: list = []
    pa = _publisher_with(monkeypatch, HangTab(ADSMANAGER), opened, tmp_path)
    with pytest.raises(pa.ApiPublishError) as ei:
        pa.ApiPublisher().open("Acc")
    assert ei.value.code == "page_unresponsive"
    left, why = pa._cooldown("Acc")
    assert why == "page_unresponsive" and 590 < left <= 600
    assert len(opened) == 1
    # ACL-2262: the next action gets exactly ONE tab replacement for this freeze episode (the profile
    # is already open, so it is not started again). The replacement hangs too: the full pause is armed.
    with pytest.raises(pa.ApiPublishError) as ei2:
        pa.ApiPublisher().open("Acc")
    assert ei2.value.code == "page_unresponsive" and len(opened) == 1
    left2, why2 = pa._cooldown("Acc")
    assert why2 == "page_unresponsive" and 590 < left2 <= 600
    with pytest.raises(pa.ApiPublishError) as ei3:     # third action: the profile is not touched at all
        pa.ApiPublisher().open("Acc")
    assert ei3.value.code == "page_unresponsive" and len(opened) == 1
    assert "Nothing was created" in str(ei3.value)


# ───────────────────────── F2: телеметрия на исключении ─────────────────────────

def test_pilot_line_written_when_creator_raises_cdp_timeout(monkeypatch):
    from tests.test_acl1761_api_create import FakeMeta, _draft, _publisher

    lines: list = []
    monkeypatch.setattr(tm, "record", lambda event, path=None: lines.append(event))

    def boom(self, plan):
        raise CdpTimeout("Runtime.evaluate: the browser did not answer within 15 s")

    monkeypatch.setattr(cr.CampaignCreator, "run", boom)
    pub = _publisher(FakeMeta())
    with tempfile.TemporaryDirectory() as d:
        with pytest.raises(CdpTimeout):
            pub.publish_draft(_draft(Path(d), ads=1))
    got = [e for e in lines if e.get("event") == "fb_api_create"]
    assert len(got) == 1 and got[0]["ok"] is False and got[0]["stop"] == "cdp_timeout"


def test_pilot_line_carries_the_stop_kind_of_a_graph_stop(monkeypatch):
    from src.analysis_agent import account_spend_api as api
    from tests.test_acl1761_api_create import FakeMeta, _draft, _publisher

    lines: list = []
    monkeypatch.setattr(tm, "record", lambda event, path=None: lines.append(event))
    monkeypatch.setattr(api, "_COOLDOWN", {})

    def boom(self, plan):
        raise g.PageUnresponsive("the Ads Manager tab stopped answering")

    monkeypatch.setattr(cr.CampaignCreator, "run", boom)
    pub = _publisher(FakeMeta())
    with tempfile.TemporaryDirectory() as d:
        with pytest.raises(g.PageUnresponsive):
            pub.publish_draft(_draft(Path(d), ads=1))
    got = [e for e in lines if e.get("event") == "fb_api_create"]
    assert got and got[0]["ok"] is False and got[0]["stop"] == "page_unresponsive"
    assert isinstance(pub.stop, g.PageUnresponsive)


# ───────────────────────── F3: порт профиля ─────────────────────────

class _Proc:
    def __init__(self, exe, cmdline):
        self.info = {"name": "chrome.exe", "exe": exe, "cmdline": cmdline}


MIMIC = "C:/Users/u/mlx/deps/mimic_154/chrome.exe"
PID = "550e8400-e29b-41d4-a716-446655440000"


def _udd(pid=PID):
    return f"C:/Users/u/mlx/profiles/ws/folder/{pid}"


def test_local_debug_port_matches_short_8_3_user_data_dir(monkeypatch):
    long_dir = r"C:\Users\evgenisedin\mlx\profiles\ws\folder" + "\\" + PID  # linux-ok: fake Windows path passed as data, the code under test only string-compares it
    short = r"C:\Users\EVGENI~1\mlx\profiles\WS~1\FOLDER~1" + "\\" + PID  # linux-ok: fake Windows 8.3 path passed as data, the code under test only string-compares it
    monkeypatch.setattr(prof.os.path, "realpath", lambda p: p)          # realpath не помог
    monkeypatch.setattr(prof, "_long_path", lambda p: long_dir if "~" in p else p)
    procs = [_Proc(MIMIC, ["chrome.exe", f"--user-data-dir={short}", "--remote-debugging-port=63405"])]
    # Совпадение нужно именно по папке, не по последнему компоненту: другой pid в запросе.
    got = prof.local_debug_port("another-profile", process_iter=lambda: procs,
                                accepting=lambda p: True, listens=lambda proc, port: True,
                                profile_dir=long_dir)
    assert got == ("port", 63405)
    assert prof._norm(short) == prof._norm(long_dir)


@pytest.mark.skipif(os.name != "nt", reason="GetLongPathNameW exists only on Windows")  # linux-ok: the test is skipped off Windows, so it never touches the API on Linux
def test_long_path_expands_a_real_8_3_name(tmp_path):
    import ctypes

    folder = tmp_path / "a-rather-long-directory-name"
    folder.mkdir()
    buf = ctypes.create_unicode_buffer(1024)
    if not ctypes.windll.kernel32.GetShortPathNameW(str(folder), buf, len(buf)) or "~" not in buf.value:  # linux-ok: only reached on Windows: the test is skipped by the skipif above
        pytest.skip("8.3 names are disabled on this volume")
    assert prof._long_path(buf.value).lower() == str(folder).lower()


def test_local_debug_port_port_in_cmdline_not_listening_is_closing_not_by_hand():
    procs = [_Proc(MIMIC, ["chrome.exe", f"--user-data-dir={_udd()}", "--remote-debugging-port=63405"])]
    got = prof.local_debug_port(PID, process_iter=lambda: procs, accepting=lambda p: True,
                                listens=lambda proc, port: False)
    assert got == ("closing", 63405)


def test_open_profile_raises_profile_closing_after_a_short_wait():
    slept: list = []
    with pytest.raises(prof.ProfileError) as ei:
        prof.open_profile(PID, "", "A", local_probe=lambda pid: ("closing", 63405),
                          launcher_factory=lambda inst: pytest.fail("MLX must not be asked"),
                          sleep=slept.append)
    assert ei.value.code == "profile_closing" and "Nothing was stopped" in str(ei.value)
    assert slept and sum(slept) <= prof.CLOSING_WAIT_S


def test_open_profile_goes_on_when_the_closing_profile_is_gone(monkeypatch):
    answers = iter([("closing", 63405), ("none", None)])

    class Launcher:
        config = None

        def start_profile(self, folder, pid, headless=False):
            return SimpleNamespace(port=7777, folder_id="f", ws_endpoint=None, newly_started=True)

    monkeypatch.setattr(prof, "running_state", lambda launcher, pid: ("not_running", None))
    got = prof.open_profile(PID, "", "A", local_probe=lambda pid: next(answers),
                            launcher_factory=lambda inst: Launcher(), sleep=lambda s: None)
    assert got.port == "7777" and got.started_now is True


def test_local_debug_port_skips_child_processes():
    listened: list = []
    procs = [
        _Proc(MIMIC, ["chrome.exe", "--type=renderer", f"--user-data-dir={_udd()}"]),
        _Proc(MIMIC, ["chrome.exe", "--type=gpu-process", f"--user-data-dir={_udd()}",
                      "--remote-debugging-port=63405"]),
    ]
    got = prof.local_debug_port(PID, process_iter=lambda: procs, accepting=lambda p: True,
                                listens=lambda proc, port: listened.append(port) or True)
    assert got == ("none", None) and listened == []


def test_a_child_process_does_not_hide_the_browser_process_port():
    procs = [
        _Proc(MIMIC, ["chrome.exe", "--type=renderer", f"--user-data-dir={_udd()}"]),
        _Proc(MIMIC, ["chrome.exe", f"--user-data-dir={_udd()}", "--remote-debugging-port=63405"]),
    ]
    got = prof.local_debug_port(PID, process_iter=lambda: procs, accepting=lambda p: True,
                                listens=lambda proc, port: True)
    assert got == ("port", 63405)


# ───────────────────────── F4: последняя вкладка на пути ошибки ─────────────────────────

class OpenFailCdp:
    def __init__(self, other_tabs):
        self.other_tabs = other_tabs
        self.sent: list = []
        self.closed = False

    def call(self, method, params=None, *, session_id="", timeout=5.0):
        self.sent.append((method, dict(params or {})))
        if method == "Target.createTarget":
            return {"targetId": "T-mine"}
        if method == "Target.attachToTarget":
            return {"sessionId": "S-mine"}
        if method == "Target.getTargets":
            pages = [{"targetId": f"T-{i}", "type": "page"} for i in range(self.other_tabs)]
            return {"targetInfos": pages + [{"targetId": "T-mine", "type": "page"}]}
        if method == "Page.enable":
            raise TimeoutError("hung")
        return {}

    def close(self):
        self.closed = True


def test_tab_open_failure_never_closes_the_last_page():
    cdp = OpenFailCdp(other_tabs=0)
    with pytest.raises(CdpTimeout):
        PageTab.open("http://127.0.0.1:1", connect=lambda u, timeout: cdp)
    assert not any(m == "Target.closeTarget" for m, _ in cdp.sent)
    assert cdp.closed


def test_tab_open_failure_still_closes_its_own_tab_when_others_exist():
    cdp = OpenFailCdp(other_tabs=1)
    with pytest.raises(CdpTimeout):
        PageTab.open("http://127.0.0.1:1", connect=lambda u, timeout: cdp)
    assert ("Target.closeTarget", {"targetId": "T-mine"}) in cdp.sent


# ───────────────────────── ревью, раунд 9: запись и F1 ─────────────────────────

ACT = "act_123456789"


class WriteStubTab:
    """Вкладка для записи: помощник цел, `.call(` виснет, браузер называет URL."""

    def __init__(self, url, *, hang_state=False):
        from tests.test_acl1761_api_create import StubTab

        self.inner = StubTab(call_exc=CdpTimeout("Runtime.evaluate: the browser did not answer"))
        self.url, self.hang_state, self.asked = url, hang_state, 0
        self.posted_reply = None

    def create_isolated_world(self, name):
        return self.inner.create_isolated_world(name)

    def evaluate_in(self, ctx, expr, timeout=60.0):
        if ".state()" in expr and self.hang_state:
            raise CdpTimeout("Runtime.evaluate: the browser did not answer")
        if ".call(" in expr and self.posted_reply is not None:
            return self.posted_reply
        return self.inner.evaluate_in(ctx, expr, timeout)

    def browser_url(self, timeout=5.0):
        self.asked += 1
        return self.url


def _camp():
    return {"name": "c", "objective": "OUTCOME_LEADS", "status": "PAUSED",
            "special_ad_categories": "[]", "buying_type": "AUCTION"}


@pytest.mark.parametrize("url", [ADSMANAGER, LOGIN])
@pytest.mark.parametrize("kind", ["json", "image", "video"])
def test_a_post_or_upload_timeout_stays_call_unknown_then_write_unknown(url, kind):
    tab = WriteStubTab(url)
    transport = tc.InPageGraph(tab, allow_writes=True, write_act=ACT, sleep=lambda s: None)
    client = g.GraphClient(transport, allow_writes=True, sleep=lambda s: None, min_interval_s=0.0)
    if kind == "json":
        path, files, params = f"/{ACT}/campaigns", None, _camp()
    elif kind == "image":
        path, files, params = f"/{ACT}/adimages", {"filename": b"0123456789"}, {}
    else:
        path, files, params = f"/{ACT}/advideos", {"source": b"0123456789"}, {"title": "t"}
    assert transport.write(path, params, files=files, filename="a.bin") == {"error": "call_unknown"}
    assert tab.asked == 0                              # POST не классифицируется как зависание вкладки
    with pytest.raises(g.WriteUnknown):
        client.post(path, params, files=files, filename="a.bin")
    assert client.unreconciled == path and client.stopped is None


def test_non_json_post_then_hung_state_is_write_unknown_not_a_plain_stop():
    tab = WriteStubTab(ADSMANAGER, hang_state=True)
    tab.posted_reply = {"status": 200, "body": {"non_json": "<html>"}}
    # помощник «установлен» заранее, state() виснет только на проверке страницы после записи
    transport = tc.InPageGraph(tab, allow_writes=True, write_act=ACT, sleep=lambda s: None)
    transport._ctx, transport._injected = 7, True
    transport._ready_for_call = lambda timeout=10.0: None
    client = g.GraphClient(transport, allow_writes=True, sleep=lambda s: None, min_interval_s=0.0)
    path = f"/{ACT}/campaigns"
    with pytest.raises(g.WriteUnknown) as ei:
        client.post(path, _camp())
    assert client.unreconciled == path
    assert "nothing" not in str(ei.value).lower() and "Ads Manager" in str(ei.value)
    assert client.stopped is not None and "nothing" not in str(client.stopped).lower()


def test_publisher_is_blocked_after_non_json_post_and_hung_page(monkeypatch, tmp_path):
    from tests.test_acl1761_api_create import FakeMeta, _draft, _publisher

    class Meta(FakeMeta):
        hang = False

        def state(self):
            if self.hang:
                raise g.PageUnresponsive("the Ads Manager tab stopped answering")
            return super().state()

        def write(self, path, params, files=None, filename="", **_k):
            if path.endswith("/campaigns"):
                self.hang = True
                return {"status": 200, "body": {"non_json": "<html>"}}
            return super().write(path, params, files=files, filename=filename)

    from src.analysis_agent import account_spend_api as api

    monkeypatch.setattr(api, "_COOLDOWN", {})
    monkeypatch.setattr(tm, "report_stop", lambda *a, **k: "")
    monkeypatch.setattr(tm, "record", lambda *a, **k: None)
    pub = _publisher(Meta())
    res = pub.publish_draft(_draft(tmp_path, ads=1))
    assert pub.blocked and pub._client.unreconciled
    assert res["success"] is False and "nothing was sent" not in (res["error"] or "").lower()
