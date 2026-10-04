"""ACL-1532: замер экранных значений по умолчанию не трогает вьюпорт профиля.

Живой замер 03.10 показал, что снимки (a) `page.screenshot` и (b)
`screenshots.capture` сбрасывают вьюпорт профиля. Здесь — без Multilogin и
сети, на поддельных странице и контексте — проверяется, что без флага они не
вызываются, с флагом вызываются, что от вкладок печатается только хост и что
зависший CDP-снимок обрывается и отключает свою сессию.
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
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
    def new_cdp_session(self, page):
        return FakeSession()


PAGES_URLS = [
    "https://adsmanager.facebook.com/adsmanager/manage/campaigns?act=123456789",
    "https://business.facebook.com/settings/ad-accounts/987654321",
    "https://www.facebook.com/some.user.name/",
    "about:blank",
]


def _run(monkeypatch, tmp_path, *, allow):
    from src.fb_ads_rpa import screenshots

    captured = []
    monkeypatch.setattr(screenshots, "capture",
                        lambda page, label, **kw: captured.append(label) or "")
    shots = []
    pages = [FakePage(u) for u in PAGES_URLS]
    result = probe_mod.probe(
        FakeContext(), pages, tmp_path, allow_viewport_reset=allow,
        shoot_cdp=lambda target_id, path, clip: shots.append((target_id, clip)) or "ok",
        settle_s=0, final_wait_s=0,
    )
    return result, pages[0], captured, shots


def test_viewport_reset_captures_off_by_default(monkeypatch, tmp_path):
    result, page, captured, shots = _run(monkeypatch, tmp_path, allow=False)
    assert page.screenshots == 0
    assert captured == []
    assert [s["step"] for s in result["steps"]] == ["c_cdp_clip_scale2", "d_cdp_noclip"]
    assert [t for t, _ in shots] == ["T1", "T1"]


def test_viewport_reset_captures_run_with_flag(monkeypatch, tmp_path):
    result, page, captured, _ = _run(monkeypatch, tmp_path, allow=True)
    assert page.screenshots == 1
    assert captured == ["acl1532_failure_shot"]
    assert [s["step"] for s in result["steps"]][:2] == ["a_page_screenshot", "b_failure_capture"]


def test_tab_list_prints_hosts_only(monkeypatch, tmp_path):
    result, *_ = _run(monkeypatch, tmp_path, allow=False)
    assert result["pages"] == ["adsmanager.facebook.com", "business.facebook.com",
                               "www.facebook.com", "about:"]
    printed = str(result)
    for leak in ("123456789", "987654321", "some.user.name", "/settings", "/adsmanager"):
        assert leak not in printed


def test_no_open_tabs_stops_cleanly(tmp_path):
    result = probe_mod.probe(FakeContext(), [], tmp_path, allow_viewport_reset=False,
                             shoot_cdp=lambda *a: "ok")
    assert result == {"stop": "profile has no open tabs"}


def test_hung_cdp_capture_times_out_and_detaches():
    class HangingSession:
        detached = False

        async def send(self, method, params=None):
            await asyncio.sleep(30)

        async def detach(self):
            HangingSession.detached = True

    got = asyncio.run(probe_mod._bounded_capture(HangingSession(), {}, 0.05))
    assert got is None
    assert HangingSession.detached


@pytest.mark.parametrize("out,refused", [
    ("out", True),
    ("scripts/shots", True),
    ("output/acl1532_probe", False),
])
def test_out_inside_repo_only_under_output(out, refused):
    assert bool(probe_mod._out_refused(probe_mod.REPO_ROOT / out)) is refused
