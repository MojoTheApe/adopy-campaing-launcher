"""ACL-2782: одна короткая строка статуса вместо красной плашки OUTDATED и рамки страницы."""
import time
from pathlib import Path

import pytest

from src.web import cache, publish_jobs, report_store

PAGE = "<html><body><h1>Report 42</h1></body></html>"
OLD_BOX_MARKERS = ("bottom:1rem", "box-shadow", "inset 0 0 0", "acl-stored-frame", "OUTDATED",
                   "border:2px solid", "#7f1212", "Do not rely")


def _stored(age=720, reasons=()):
    return report_store.StoredPage(PAGE, time.time() - age, list(reasons))


def _no_big_box(out):
    for marker in OLD_BOX_MARKERS:
        assert marker not in out, marker
    assert "#b91c1c" not in out  # ни рамки, ни красной заливки


def test_deferred_line_has_age_counts_and_names():
    facts = {"publishes": ["KLD_BFL_B95", "Doc349"], "publish_count": 2, "windows": 1}
    out = report_store.decorate(_stored(), deferred=True, waiting=facts)
    assert ("Saved 12 min ago · waiting for 2 publishes (KLD_BFL_B95, Doc349) "
            "and 1 open profile window · updates by itself") in out
    _no_big_box(out)


def test_waiting_phrase_forms():
    assert report_store.waiting_phrase({"publishes": ["A"], "publish_count": 1, "windows": 0}) == \
        "waiting for 1 publish (A)"
    assert report_store.waiting_phrase({"publishes": [], "publish_count": 0, "windows": 2}) == \
        "waiting for 2 open profile windows"
    assert report_store.waiting_phrase({}) == "waiting for publishes to finish"
    many = report_store.waiting_phrase({"publishes": list("ABCDE"), "publish_count": 5, "windows": 0})
    assert many == "waiting for 5 publishes (A, B, C +2 more)"


def test_outdated_line_has_age_and_short_reason():
    out = report_store.decorate(_stored(7200, ["source", "settings"]))
    assert "Saved 2 h ago · outdated: newer statistics may exist; settings changed · updates by itself" in out
    assert 'data-state="outdated"' in out
    _no_big_box(out)


def test_cached_line_has_age():
    out = report_store.decorate(_stored(300))
    assert "Saved 5 min ago · refreshing · updates by itself" in out
    assert 'data-state="cached"' in out
    _no_big_box(out)


def test_error_line_is_red_text_without_frame_and_stops_polling():
    out = report_store.decorate(_stored(), error="<b>boom</b>")
    assert "Saved 12 min ago · refresh failed: &lt;b&gt;boom&lt;/b&gt;" in out
    assert 'data-state="error"' in out and 'data-polling="0"' in out
    assert "color:#b42318" in out and "<b>boom</b>" not in out
    _no_big_box(out)


def test_chip_is_small_top_and_single_line():
    out = report_store.decorate(_stored())
    assert "position:fixed;top:6px" in out and "white-space:nowrap" in out and "width:max-content" in out
    assert "bottom:" not in out.split('id="acl-stored-style"')[1].split("</style>")[0]


def test_connections_stays_a_small_link_and_slot_is_used():
    out = report_store.decorate(_stored())
    assert 'id="acl-stored-connections"' in out and "<button" not in out
    assert "acl-status-slot" in out  # скрипт переносит чип в строку статуса новой страницы
    template = (Path(__file__).resolve().parent.parent / "src/web/templates/report.html").read_text(encoding="utf-8")
    assert 'id="acl-status-slot"' in template


def test_busy_facts_collects_names_and_windows(monkeypatch):
    monkeypatch.setattr(publish_jobs, "_jobs", {
        "1": {"status": "running", "account_label": "KLD_BFL_B95"},
        "2": {"status": "queued", "account_label": "Doc349"},
        "3": {"status": "done", "account_label": "Old"},
    })
    monkeypatch.setattr(publish_jobs._slots, "kept_windows", lambda: 1)
    facts = publish_jobs.busy_facts()
    assert facts == {"publishes": ["KLD_BFL_B95", "Doc349"], "publish_count": 2, "windows": 1}
    assert cache.rebuild_deferred_facts() == facts


def test_status_endpoint_carries_waiting_text(monkeypatch):
    import asyncio
    import httpx
    from src.web import app as web_app

    monkeypatch.setattr(web_app, "_page_ready", lambda *a: False)
    monkeypatch.setattr(web_app, "rebuild_deferred", lambda: True)
    monkeypatch.setattr(web_app, "rebuild_deferred_facts",
                        lambda: {"publishes": ["Doc349"], "publish_count": 1, "windows": 1})
    monkeypatch.setattr(web_app, "background_build_state",
                        lambda: {"token": None, "building": False, "error": None, "stage": None})
    monkeypatch.setattr(web_app, "cache_built_at", lambda: "x")
    async def request():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web_app.app),
                                     base_url="http://127.0.0.1") as client:
            return await client.get("/report/status")
    body = asyncio.run(request()).json()
    assert body["deferred"] is True
    assert body["waiting_text"] == "waiting for 1 publish (Doc349) and 1 open profile window"
