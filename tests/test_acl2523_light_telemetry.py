"""ACL-2523: light work tab telemetry - `work_tab_mb` in `fb_api_tab_context`, `mode` in the create stop row."""
from __future__ import annotations

import json

from src.fb_api import light_tab
from tests.test_acl1761_api_create import FakeMeta, _draft, _publisher
from tests.test_acl2344_warm_light import _light_rows, rig  # noqa: F401 - fixture
from tests.test_acl1812_warm_sessions import PID


class MetricTab:
    def __init__(self, metrics=None, boom=False):
        self.metrics, self.boom, self.calls = metrics, boom, []

    def call(self, method, params=None, *, timeout=5.0, **kw):
        self.calls.append(method)
        if self.boom:
            raise RuntimeError("cdp down")
        return {"metrics": self.metrics} if method == "Performance.getMetrics" else {}


def test_heap_used_mb_reads_js_heap_used_size():
    tab = MetricTab([{"name": "Nodes", "value": 5}, {"name": "JSHeapUsedSize", "value": 52428800}])
    assert light_tab.heap_used_mb(tab) == 50.0
    assert tab.calls == ["Performance.enable", "Performance.getMetrics"]


def test_heap_used_mb_is_none_when_unavailable_and_never_raises():
    assert light_tab.heap_used_mb(MetricTab([{"name": "Nodes", "value": 5}])) is None
    assert light_tab.heap_used_mb(MetricTab(None)) is None
    assert light_tab.heap_used_mb(MetricTab(boom=True)) is None
    assert light_tab.heap_used_mb(MetricTab([{"name": "JSHeapUsedSize", "value": "x"}])) is None
    assert light_tab.heap_used_mb(object()) is None
    assert light_tab.heap_used_mb(None) is None


def test_light_lease_row_carries_work_tab_mb_when_available(rig, monkeypatch):  # noqa: F811
    monkeypatch.setattr(light_tab, "heap_used_mb", lambda tab: 41.5)
    lease = rig.checkout()
    row = _light_rows(rig)[-1]
    assert row["mode"] == "light" and row["work_tab_mb"] == 41.5
    assert "token" not in json.dumps(row).lower()
    lease.release()


def test_light_lease_row_omits_work_tab_mb_when_unavailable(rig, monkeypatch):  # noqa: F811
    monkeypatch.setattr(light_tab, "heap_used_mb", lambda tab: None)
    lease = rig.checkout()
    row = _light_rows(rig)[-1]
    assert row["mode"] == "light" and "work_tab_mb" not in row
    lease.release()


def _create_rows(monkeypatch, tmp_path, meta):
    from src.web import publish_api
    rows = []
    monkeypatch.setattr(publish_api, "_record", lambda *a, **k: rows.append((a, k)))
    _publisher(meta).publish_draft(_draft(tmp_path, ads=1))
    return [k["diag"] for a, k in rows if a and a[0] == "api_create_campaign"]


def test_create_campaign_row_carries_mode_ads_manager_by_default(tmp_path, monkeypatch):
    diags = _create_rows(monkeypatch, tmp_path, FakeMeta())
    assert diags and all(d["mode"] == "ads_manager" for d in diags)


def test_create_campaign_row_carries_mode_light(tmp_path, monkeypatch):
    meta = FakeMeta()
    meta.light = True
    diags = _create_rows(monkeypatch, tmp_path, meta)
    assert diags and all(d["mode"] == "light" for d in diags)


def test_create_campaign_stop_row_after_an_exception_carries_mode(tmp_path, monkeypatch):
    from src.fb_api import graph as g
    meta = FakeMeta()
    meta.light = True
    meta.fail_next["campaigns"] = "unsent"
    diags = _create_rows(monkeypatch, tmp_path, meta)
    assert diags and diags[-1]["mode"] == "light"


def test_recapture_row_carries_work_tab_mb_only_when_available(monkeypatch):
    from tests.test_acl2344_light_rework import _lease
    for value, expect in ((33.0, True), (None, False)):
        monkeypatch.setattr(light_tab, "heap_used_mb", lambda tab, v=value: v)
        _tab, _ig, client, rows, _closed = _lease(monkeypatch, second_190=False)
        client.get("/me/adaccounts", {"fields": "id"})
        row = [r for r in rows if r.get("phase") == "light_recapture"][-1]
        assert ("work_tab_mb" in row) is expect
        assert not expect or row["work_tab_mb"] == 33.0
