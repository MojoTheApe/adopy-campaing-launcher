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


_EXCEL_MISSING = FileNotFoundError("Excel file not found: C:/x/cursor example.xlsx. Set FB_STATS_EXCEL or pass excel_path.")


def _assert_friendly_step(html):
    assert "Connect statistics" in html
    assert "Sign in with your Adopy Google account" in html
    assert 'id="nd-signin"' in html and "/api/connections/sign-in" in html
    for banned in ("Excel", "xlsx", "FB_STATS_EXCEL", "No data file", "Check again", "nd-check", "workbook"):
        assert banned not in html, banned


def test_no_bi_shows_one_friendly_step_without_excel_or_env_text(client, monkeypatch):
    _fail_with(monkeypatch, _EXCEL_MISSING)
    _assert_friendly_step(client.get("/").text)


def test_excel_era_install_lands_on_the_same_step(client, monkeypatch):
    monkeypatch.setenv("FB_STATS_EXCEL", "C:/gone/cursor example.xlsx")
    _fail_with(monkeypatch, _EXCEL_MISSING)
    _assert_friendly_step(client.get("/").text)


def test_friendly_step_opens_the_dashboard_on_its_own(client, monkeypatch):
    _fail_with(monkeypatch, _EXCEL_MISSING)
    html = client.get("/").text
    # success of the sign-in request, or the connection appearing by any route, reloads the dashboard
    assert "location.replace('/')" in html
    assert "/api/connections" in html and "setInterval" in html


def test_sign_in_starts_the_report_build(client, monkeypatch):
    from src.analysis_agent import connection_settings as cs

    monkeypatch.setattr(cs, "sign_in", lambda *a, **k: {"updated": 3, "library": False, "bi": True})
    monkeypatch.setattr(cs, "check_all", lambda: [{"name": "BI", "ok": True, "configured": True, "line": "ok"}])
    monkeypatch.setenv("BI_MYSQL_HOST", "h")
    monkeypatch.setenv("BI_MYSQL_USER", "u")
    monkeypatch.setenv("BI_MYSQL_PASSWORD", "p")
    monkeypatch.setattr(bi_stats, "refresh_revision", lambda **k: None)
    warmed = []
    monkeypatch.setattr(web, "_start_warm", lambda *a: warmed.append(a))
    assert client.post("/api/connections/sign-in").json()["ok"] is True
    assert warmed == [("", "", "")]


def test_update_keeps_the_bi_connection(tmp_path):
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packaging" / "windows"))
    import machine_config as mc

    env = "\n".join(["BI_MYSQL_HOST=h", "BI_MYSQL_USER=u", "BI_MYSQL_PASSWORD=p", "FB_STATS_EXCEL=C:/old.xlsx", ""])
    merged = mc.merge_env(env, {"MY_CM_NAME": "Dima", "FB_CREATIVES_ROOT": "C:/c"})
    for line in ("BI_MYSQL_HOST=h", "BI_MYSQL_USER=u", "BI_MYSQL_PASSWORD=p"):
        assert line in merged


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
    monkeypatch.setattr(web, "_start_warm", lambda *a: None)
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
