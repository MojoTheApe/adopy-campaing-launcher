"""ACL-1532: замер экранных значений по умолчанию только читает.

Живой замер 03.10 показал, что снимки (a) `page.screenshot` и (b)
`screenshots.capture` сбрасывают вьюпорт профиля; ревью ACL-1529 — что (c),
CDP-снимок с clip из новой сессии, сбрасывает чужую подмену размеров. Здесь —
без Multilogin и сети, на поддельных странице и контексте — проверяется, что
без флагов ни один снимок не вызывается, что каждый флаг включает свои снимки
(и `main` передаёт флаги в `probe`), что от вкладок печатается только хост и
что зависший CDP-снимок обрывается по таймауту и отключает свою сессию.
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "probe_screen_values.py"
_spec = importlib.util.spec_from_file_location("probe_screen_values", _PATH)
probe_mod = importlib.util.module_from_spec(_spec)
sys.modules["probe_screen_values"] = probe_mod
_spec.loader.exec_module(probe_mod)

SNAP = {"inner_width": 2558, "inner_height": 1438}


class FakeSession:
    def send(self, method, params=None):
        if method == "Browser.getWindowForTarget":
            return {"bounds": {"width": 1294, "height": 814, "windowState": "normal"}}
        if method == "Target.getTargetInfo":
            return {"targetInfo": {"targetId": "T1"}}
        raise AssertionError(f"unexpected CDP call {method}")

    def detach(self):
        pass


class FakePage:
    def __init__(self, url):
        self.url = url
        self.screenshots = 0

    def evaluate(self, js):
        if js == probe_mod.FRAME_JS:
            return {"x": 0, "y": 0, "width": 100, "height": 50}
        return dict(SNAP)

    def screenshot(self, **kwargs):
        self.screenshots += 1


class FakeContext:
    def __init__(self, pages=()):
        self.pages = list(pages)

    def new_cdp_session(self, page):
        return FakeSession()


PAGES_URLS = [
    "https://adsmanager.facebook.com/adsmanager/manage/campaigns?act=123456789",
    "https://business.facebook.com/settings/ad-accounts/987654321",
    "https://www.facebook.com/some.user.name/",
    "about:blank",
]


def _run(monkeypatch, tmp_path, *, allow=False, cdp=False):
    from src.fb_ads_rpa import screenshots

    captured = []
    monkeypatch.setattr(screenshots, "capture",
                        lambda page, label, **kw: captured.append(label) or "")
    shots = []
    pages = [FakePage(u) for u in PAGES_URLS]
    result = probe_mod.probe(
        FakeContext(), pages, tmp_path, allow_viewport_reset=allow, allow_cdp_capture=cdp,
        shoot_cdp=lambda target_id, path, clip: shots.append((target_id, clip)) or "ok",
        settle_s=0, final_wait_s=0,
    )
    return result, pages[0], captured, shots


def test_default_run_is_reads_only(monkeypatch, tmp_path):
    result, page, captured, shots = _run(monkeypatch, tmp_path)
    assert page.screenshots == 0
    assert captured == []
    assert shots == []
    assert result["steps"] == []
    assert "before" in result and "final_after_wait" in result


def test_cdp_flag_runs_c_and_d_only(monkeypatch, tmp_path):
    result, page, captured, shots = _run(monkeypatch, tmp_path, cdp=True)
    assert page.screenshots == 0
    assert captured == []
    assert [s["step"] for s in result["steps"]] == ["c_cdp_clip_scale2", "d_cdp_noclip"]
    assert [t for t, _ in shots] == ["T1", "T1"]


def test_viewport_reset_flag_runs_a_and_b_only(monkeypatch, tmp_path):
    result, page, captured, shots = _run(monkeypatch, tmp_path, allow=True)
    assert page.screenshots == 1
    assert captured == ["acl1532_failure_shot"]
    assert shots == []
    assert [s["step"] for s in result["steps"]] == ["a_page_screenshot", "b_failure_capture"]


def test_tab_list_prints_hosts_only(monkeypatch, tmp_path):
    result, *_ = _run(monkeypatch, tmp_path)
    assert result["pages"] == ["adsmanager.facebook.com", "business.facebook.com",
                               "www.facebook.com", "about:"]
    printed = str(result)
    for leak in ("123456789", "987654321", "some.user.name", "/settings", "/adsmanager"):
        assert leak not in printed


def test_no_open_tabs_stops_cleanly(tmp_path):
    result = probe_mod.probe(FakeContext(), [], tmp_path, allow_viewport_reset=False,
                             allow_cdp_capture=False, shoot_cdp=lambda *a: "ok")
    assert result == {"stop": "profile has no open tabs"}


def test_hung_cdp_capture_times_out_and_detaches():
    class SlowSession:
        detached = False

        async def send(self, method, params=None):
            # Без asyncio.wait_for вызов вернул бы этот ответ через 3 с.
            await asyncio.sleep(3)
            return {"data": "iVBORw0KGgo="}

        async def detach(self):
            SlowSession.detached = True

    started = time.monotonic()
    got = asyncio.run(probe_mod._bounded_capture(SlowSession(), {}, 0.1))
    elapsed = time.monotonic() - started
    assert got is None
    assert elapsed < 1.5
    assert SlowSession.detached


@pytest.mark.parametrize("argv_flags,expected", [
    ([], {"allow_viewport_reset": False, "allow_cdp_capture": False}),
    (["--allow-cdp-capture"], {"allow_viewport_reset": False, "allow_cdp_capture": True}),
    (["--allow-viewport-reset"], {"allow_viewport_reset": True, "allow_cdp_capture": False}),
])
def test_main_passes_flags_to_probe(monkeypatch, tmp_path, argv_flags, expected):
    calls = []
    monkeypatch.setattr(probe_mod, "_find_owned_port", lambda account: (9999, None, 0))

    @contextmanager
    def fake_connect(cdp_url):
        assert cdp_url == "http://127.0.0.1:9999"

        class B:
            contexts = [FakeContext([FakePage("https://adsmanager.facebook.com/x")])]
        yield B()

    monkeypatch.setattr(probe_mod, "_connect", fake_connect)
    monkeypatch.setattr(probe_mod.time, "sleep", lambda s: None)

    def fake_probe(context, pages, out, **kw):
        calls.append(kw)
        return {"steps": []}

    monkeypatch.setattr(probe_mod, "probe", fake_probe)
    rc = probe_mod.main(["--account", "9943", "--out", str(tmp_path / "shots"), *argv_flags])
    assert rc == 0
    assert len(calls) == 1
    assert {k: calls[0][k] for k in expected} == expected


def test_out_is_created_only_after_account_lookup(monkeypatch, tmp_path):
    monkeypatch.setattr(probe_mod, "_find_owned_port",
                        lambda account: (None, {"stop": "expected one map entry, got 0"}, 2))
    out = tmp_path / "shots"
    assert probe_mod.main(["--account", "nomatch", "--out", str(out)]) == 2
    assert not out.exists()


@pytest.mark.parametrize("out,refused", [
    ("out", True),
    ("scripts/shots", True),
    ("output/acl1532_probe", False),
])
def test_out_inside_repo_only_under_output(out, refused):
    assert bool(probe_mod._out_refused(probe_mod.REPO_ROOT / out)) is refused
