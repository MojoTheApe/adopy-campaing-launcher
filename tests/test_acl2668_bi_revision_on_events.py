"""ACL-2668 #21: BI revision is checked on buyer events, not every 5 minutes."""
import threading

import pytest
from fastapi.testclient import TestClient

from src.analysis_agent import bi_stats
from src.web import app as web
from src.web import cache


@pytest.fixture()
def bi_on(monkeypatch):
    monkeypatch.setenv("BI_MYSQL_HOST", "h")
    monkeypatch.setenv("BI_MYSQL_USER", "u")
    monkeypatch.setenv("BI_MYSQL_PASSWORD", "p")
    monkeypatch.delenv("FB_STATS_SOURCE", raising=False)


@pytest.fixture()
def client(bi_on, monkeypatch):
    calls = []
    monkeypatch.setattr(web, "_LAST_PERIOD", {})
    monkeypatch.setattr(web, "recheck_bi_revision", lambda: calls.append("check"))
    # Страница отрисована «из памяти» — сюда важен только факт проверки BI.
    monkeypatch.setattr(web, "_page_ready", lambda *a, **k: True)
    monkeypatch.setattr(web, "_render_and_remember",
                        lambda request, inputs, **kw: web.HTMLResponse("ok"))
    monkeypatch.setattr("src.web.buyer.get_selected_cm", lambda: "CM")
    c = TestClient(web.app, base_url="http://localhost")
    c.calls = calls
    return c


def test_no_periodic_watcher_thread():
    assert not hasattr(bi_stats, "ensure_revision_watcher")
    assert not any(t.name == "acl-bi-revision" for t in threading.enumerate())


def test_same_period_reload_does_not_ask_bi(client):
    client.get("/?date_from=2026-11-01&date_to=2026-11-10")
    client.get("/?date_from=2026-11-01&date_to=2026-11-10")
    assert client.calls == []


def test_date_change_asks_bi(client):
    client.get("/?date_from=2026-11-01&date_to=2026-11-10")
    client.get("/?date_from=2026-11-05&date_to=2026-11-10")
    assert client.calls == ["check"]


def test_compare_change_asks_bi(client):
    client.get("/?date_from=2026-11-01&date_to=2026-11-10")
    client.get("/?date_from=2026-11-01&date_to=2026-11-10&compare=previous")
    assert client.calls == ["check"]


def test_date_change_without_bi_does_not_ask(client, monkeypatch):
    monkeypatch.setenv("FB_STATS_SOURCE", "excel")
    client.get("/?date_from=2026-11-01")
    client.get("/?date_from=2026-11-02")
    assert client.calls == []


def test_recheck_forces_and_bounds_wait(bi_on, monkeypatch):
    seen, release = [], threading.Event()

    def slow(**kw):
        seen.append(kw)
        release.wait(5)
    monkeypatch.setattr(bi_stats, "refresh_revision", slow)
    cache.recheck_bi_revision(timeout=0.2)  # не ждёт медленный BI дольше лимита
    release.set()
    assert seen == [{"force": True}]


def test_recheck_skipped_without_bi(monkeypatch):
    monkeypatch.setenv("FB_STATS_SOURCE", "excel")
    monkeypatch.setattr(bi_stats, "refresh_revision", lambda **kw: pytest.fail("no BI call"))
    cache.recheck_bi_revision(timeout=0.2)


def test_refresh_rebuild_forces_revision_check(bi_on, monkeypatch, tmp_path):
    calls = []
    monkeypatch.setitem(bi_stats._REV, "value", "r1")
    monkeypatch.setattr(bi_stats, "refresh_revision", lambda **kw: calls.append(kw))
    monkeypatch.setattr(cache, "_resolve_excel_path", lambda _p: str(tmp_path / "none.xlsx"))
    monkeypatch.setattr(cache, "_build_locked", lambda *a, **k: ({}, None, []))
    cache.get_cached_report(force_rebuild=True)
    assert calls == [{"force": True}]
