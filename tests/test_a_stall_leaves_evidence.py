# -*- coding: utf-8 -*-
"""Зависание оставляет доказательства — и само не зависает (ACL-1320).

Разбор cf563b4ba594 (26.09) пришлось собирать по косвенным строкам: ни
снимка, ни размера кучи вкладки, которая 14 минут не отвечала. Теперь на
`stall_unstuck` и на ожидании окна цели без радио — `_stall_evidence`: PNG
только ограниченным путём Playwright, разметка с таймаутом, метрики вкладки
(JSHeapUsedSize, Nodes) через `_eval_bounded`; вкладка не отвечает — её не
трогают; не больше `STALL_EVIDENCE_MAX` на прогон.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import browser as br  # noqa: E402
from src.fb_ads_rpa import meta_dom as md  # noqa: E402
from src.fb_ads_rpa import page_capture, screenshots  # noqa: E402


@pytest.fixture
def env(monkeypatch):
    rows: list = []
    shots: list = []
    monkeypatch.setattr(md, "_STALL_EVIDENCE_TAKEN", {})
    monkeypatch.setattr(md, "_UNSTUCK_RELOADED", set())
    monkeypatch.setattr(md, "_sleep_h", lambda *a, **k: None)
    monkeypatch.setattr(md._telemetry, "record", lambda step, ok=None, **kw: rows.append((step, ok, kw)))
    monkeypatch.setattr(md._telemetry, "current_run_id", lambda: "run1")
    monkeypatch.setattr(screenshots, "capture",
                        lambda p, label, **kw: shots.append(("png", label, kw)) or "shot.png")
    monkeypatch.setattr(page_capture, "capture",
                        lambda p, **kw: shots.append(("html", kw["step"])) or "folder")
    monkeypatch.setattr(br, "wake_page", lambda p: {})
    return SimpleNamespace(rows=rows, shots=shots)


def _evidence(rows):
    return [r[2]["diag"] for r in rows if r[0] == "stall_evidence"]


class _Live:
    url = "https://adsmanager.facebook.com/adsmanager/manage/campaigns?act=1"

    def wait_for_function(self, js, arg=None, timeout=None):
        if "JSHeapUsedSize" in js:
            return SimpleNamespace(json_value=lambda: {"v": {"JSHeapUsedSize": 900_000_000, "Nodes": 41_000}},
                                   dispose=lambda: None)
        return True


class _Frozen:
    url = _Live.url

    def wait_for_function(self, *a, **k):
        raise TimeoutError("Timeout 3000ms exceeded.")


def test_a_stall_on_a_live_tab_is_photographed_measured_and_captured(env):
    md._unstick(_Live(), "create", editor=False)
    ev = _evidence(env.rows)
    assert len(ev) == 1
    assert ev[0]["metrics"] == {"JSHeapUsedSize": 900_000_000, "Nodes": 41_000}
    assert ev[0]["png"] == "shot.png" and ev[0]["page"] == "folder"
    png = [s for s in env.shots if s[0] == "png"][0]
    assert png[2].get("cdp_fallback") is False, "неограниченный CDP-снимок"


def test_a_frozen_tab_is_not_touched_for_evidence(env):
    md._unstick(_Frozen(), "create", editor=False)
    ev = _evidence(env.rows)
    assert ev and ev[0]["responsive"] is False
    assert env.shots == []
    assert "metrics" not in ev[0]


def test_evidence_is_bounded_per_run(env):
    for _ in range(md.STALL_EVIDENCE_MAX + 3):
        md._stall_evidence(_Live(), "create")
    assert len(_evidence(env.rows)) == md.STALL_EVIDENCE_MAX


def test_an_objective_wait_without_the_radio_leaves_evidence(env, monkeypatch):
    monkeypatch.setattr(md, "_loading_visible", lambda p: False)

    class _Radio:
        def wait_for(self, state="visible", timeout=0):
            raise TimeoutError("Timeout")

    page = _Live()
    page.get_by_role = lambda *a, **k: SimpleNamespace(first=_Radio())
    assert md._wait_objective_dialog(page, first_s=0.01, idle_s=0.01, poll_s=0.001) is False
    ev = _evidence(env.rows)
    assert ev and ev[0]["where"] == "objective_wait" and ev[0]["outcome"] in ("first_s", "idle")


def test_an_objective_wait_that_found_the_radio_leaves_none(env):
    class _Radio:
        def wait_for(self, state="visible", timeout=0):
            return None

    page = _Live()
    page.get_by_role = lambda *a, **k: SimpleNamespace(first=_Radio())
    assert md._wait_objective_dialog(page) is True
    assert _evidence(env.rows) == []


def test_the_screenshot_without_cdp_fallback_never_takes_the_cdp_path(monkeypatch, tmp_path):
    shots = screenshots
    monkeypatch.setattr(shots, "_page_hidden", lambda p: False)
    monkeypatch.setattr(shots, "_capture_via_cdp",
                        lambda p, path: (_ for _ in ()).throw(AssertionError("CDP-снимок без таймаута")))

    class _P:
        def screenshot(self, **kw):
            raise TimeoutError("Timeout 5000ms exceeded")

    assert shots.capture(_P(), "stall_x", base_dir=tmp_path, cdp_fallback=False) == ""


def test_the_metrics_script_reads_a_real_page():
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        try:
            p = b.new_page()
            p.set_content("<div><p>a</p><p>b</p></div>")
            got = md._eval_bounded(p, md._TAB_METRICS_JS)
        finally:
            b.close()
    assert got["Nodes"] >= 5 and "JSHeapUsedSize" in got
