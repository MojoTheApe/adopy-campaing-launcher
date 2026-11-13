"""ACL-2607: first run with no stats data - the error page offers the Connections sign-in."""
import pytest
from fastapi.testclient import TestClient

from src.analysis_agent import bi_stats, morning_service
from src.web import app as web


@pytest.fixture()
def client(monkeypatch):
    for name in ("BI_MYSQL_HOST", "BI_MYSQL_USER", "BI_MYSQL_PASSWORD", "FB_STATS_SOURCE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(web, "_page_ready", lambda *a, **k: True)
    return TestClient(web.app, base_url="http://localhost")


def _fail_with(monkeypatch, exc):
    def boom(*a, **k):
        raise exc
    monkeypatch.setattr(web, "get_cached_report", boom)


def test_bi_not_configured_page_offers_sign_in_and_check(client, monkeypatch):
    _fail_with(monkeypatch, FileNotFoundError("Excel file not found: /x/stats.xlsx. Set FB_STATS_EXCEL or pass excel_path."))
    html = client.get("/").text
    assert "BI is not connected on this computer" in html
    assert 'id="nd-signin"' in html and 'id="nd-check"' in html
    assert "/api/connections/sign-in" in html and "/api/connections/check" in html


def test_bi_connection_error_shows_class_and_code_never_credentials(client, monkeypatch):
    monkeypatch.setenv("BI_MYSQL_HOST", "secret-host.example")
    monkeypatch.setenv("BI_MYSQL_USER", "secret_user")
    monkeypatch.setenv("BI_MYSQL_PASSWORD", "secret_pass")

    class OperationalError(Exception):
        pass

    def fetch(cfg=None):
        raise OperationalError(2003, "Can't connect to MySQL server on 'secret-host.example'")
    monkeypatch.setattr(bi_stats, "fetch_raw", fetch)
    monkeypatch.setattr(bi_stats, "refresh_revision", lambda **k: None)
    try:
        morning_service._load_enriched_df("/nonexistent/stats.xlsx")
    except FileNotFoundError as exc:
        err = exc
    else:
        pytest.fail("expected BIUnavailable")
    _fail_with(monkeypatch, err)
    html = client.get("/").text
    assert "OperationalError 2003" in html
    assert 'id="nd-signin"' in html and 'id="nd-check"' in html
    for secret in ("secret-host", "secret_user", "secret_pass"):
        assert secret not in html


def test_sign_in_and_check_drop_remembered_failure_and_refresh_revision(client, monkeypatch):
    from src.analysis_agent import connection_settings as cs
    from src.web import cache

    calls = []
    monkeypatch.setattr(cs, "sign_in", lambda *a, **k: {"updated": 3, "library": False, "bi": True})
    monkeypatch.setattr(cs, "check_all", lambda: [{"name": "BI", "ok": True, "configured": True, "line": "BI: connected"}])
    monkeypatch.setenv("BI_MYSQL_HOST", "h")
    monkeypatch.setenv("BI_MYSQL_USER", "u")
    monkeypatch.setenv("BI_MYSQL_PASSWORD", "p")
    monkeypatch.setattr(bi_stats, "refresh_revision", lambda **k: calls.append(k))
    for url in ("/api/connections/sign-in", "/api/connections/check"):
        cache._WARM["error"] = "BI unavailable (old failure)"
        assert client.post(url).json()["ok"] is True
        assert cache._WARM["error"] is None
    assert calls == [{"force": True}, {"force": True}]
