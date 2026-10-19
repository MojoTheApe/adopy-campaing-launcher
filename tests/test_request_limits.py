"""Request limits and safe /static (ACL-1356, security audit S04).

Real ASGI requests through the production app: the body limit is enforced
before the body is buffered, forms carry explicit field/file/part limits, and
UNC / drive-letter / absolute paths to /static never reach the filesystem
resolver (on Windows resolving ``\\\\host\\share`` would make the machine
authenticate to that host over SMB — CVE-2026-48818).
"""
from __future__ import annotations

import asyncio
from urllib.parse import urlencode

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.staticfiles import StaticFiles

from src.web import request_limits
from src.web.app import app
from src.web.request_limits import (
    BUILDER_MAX_BODY,
    DEFAULT_MAX_BODY,
    RequestBodyLimit,
    SafeStaticFiles,
    is_safe_static_path,
)


async def _request(method: str, path: str, **kwargs) -> httpx.Response:
    transport = httpx.ASGITransport(app=kwargs.pop("asgi_app", app), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        return await client.request(method, path, **kwargs)


def _run(method: str, path: str, **kwargs) -> httpx.Response:
    return asyncio.run(_request(method, path, **kwargs))


# --- /static: UNC, drive letters, absolute paths -----------------------------

_HOSTILE_STATIC_PATHS = [
    "/static/%5C%5Cattacker.example%5Cshare%5Clogo.svg",   # \\host\share\...
    "/static/%5C%5C%3F%5CUNC%5Cattacker.example%5Cshare",   # \\?\UNC\host\share
    "/static//attacker.example/share/logo.svg",              # //host/share
    "/static/%2F%2Fattacker.example%2Fshare%2Flogo.svg",     # encoded //host
    "/static/C:/Windows/win.ini",                            # drive letter
    "/static/C:%5CWindows%5Cwin.ini",                        # C:\Windows\...
    "/static/C:win.ini",                                     # drive-relative
    "/static/%5CWindows%5Cwin.ini",                          # \Windows\...
    "/static/..%5C..%5Cconfig%5C.env",                       # ..\..\config
    "/static/logo.svg:Zone.Identifier",                      # NTFS stream
]


@pytest.fixture
def resolver_must_not_run(monkeypatch):
    """Fail the test if the StaticFiles filesystem resolver is reached at all."""
    touched: list[str] = []

    def _forbidden(self, path):  # noqa: ANN001 - mirrors StaticFiles.lookup_path
        touched.append(path)
        raise AssertionError(f"static resolver reached with {path!r}")

    monkeypatch.setattr(StaticFiles, "lookup_path", _forbidden)
    return touched


@pytest.mark.parametrize("path", _HOSTILE_STATIC_PATHS)
def test_static_refuses_unc_and_drive_paths_without_touching_the_filesystem(path, resolver_must_not_run):
    response = _run("GET", path)

    assert response.status_code == 404
    assert resolver_must_not_run == []


def test_static_still_serves_a_plain_file():
    response = _run("GET", "/static/logo.svg")

    assert response.status_code == 200
    assert "svg" in response.headers["content-type"]


@pytest.mark.parametrize(
    "path, safe",
    [
        ("/logo.svg", True),
        ("css/app.min.css", True),
        ("", False),
        ("/", False),
        ("//host/share", False),
        ("\\\\host\\share", False),  # linux-ok: hostile path as data, never opened
        ("C:/Windows", False),
        ("C:Windows", False),
        ("a/../b", False),
        ("./logo.svg", False),
        (".env", False),
        ("a//b", False),
        ("logo.svg\x00", False),
        ("CON", False),
        ("nul.css", False),
        ("img/com1.png", False),
        ("logo.svg.", False),
        ("console.css", True),
    ],
)
def test_is_safe_static_path(path, safe):
    assert is_safe_static_path(path) is safe


def test_safe_static_lookup_rejects_windows_absolute_paths_directly(tmp_path, resolver_must_not_run):
    static = SafeStaticFiles(directory=str(tmp_path))
    for path in ("\\\\host\\share\\x", "C:\\Windows\\win.ini", "C:x", "/etc/passwd", "\\x"):  # linux-ok: hostile paths as data, never opened
        assert static.lookup_path(path) == ("", None)
    assert resolver_must_not_run == []


def test_safe_static_never_follows_symlinks(tmp_path):
    with pytest.raises(ValueError):
        SafeStaticFiles(directory=str(tmp_path), follow_symlink=True)


# --- body limit, enforced before buffering -----------------------------------


def test_oversized_declared_body_is_refused_before_the_route_runs(monkeypatch):
    called: list[str] = []

    async def _no_parse(*args, **kwargs):
        called.append("form parsed")
        raise AssertionError("the form must not be parsed")

    monkeypatch.setattr("src.web.app.read_form", _no_parse)
    body = b"text=" + b"x" * DEFAULT_MAX_BODY

    response = _run(
        "POST",
        "/report-problem",
        content=body,
        headers={"content-type": "application/x-www-form-urlencoded"},
    )

    assert response.status_code == 413
    assert response.json() == {"error": "Request body is too large."}
    # Отказ проходит через local_dashboard_boundary и несёт её заголовки.
    assert response.headers["x-content-type-options"] == "nosniff"
    assert called == []


def test_streamed_body_without_length_is_cut_at_the_limit():
    chunk = b"x" * (64 * 1024)
    sent: list[int] = []

    async def _chunks():
        yield b'{"action": "link", "value": "'
        for i in range(200):  # 12.5 MiB if it were read to the end
            sent.append(i)
            yield chunk

    async def _go():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            # JSON-маршрут: он читает тело целиком и сам ловит любое
            # исключение разбора — отказ всё равно должен быть 413.
            return await client.post(
                "/account-links",
                content=_chunks(),
                headers={"content-type": "application/json"},
            )

    response = asyncio.run(_go())

    assert response.status_code == 413
    # Чтение оборвано на пределе, а не после буферизации всего тела.
    assert len(sent) * len(chunk) <= DEFAULT_MAX_BODY + 2 * len(chunk)


def test_invalid_content_length_is_refused():
    async def _endpoint(request: Request):
        raise AssertionError("must not run")

    tiny = RequestBodyLimit(Starlette(routes=[Route("/", _endpoint, methods=["POST"])]), default_max_body=10)
    scope_headers = [(b"host", b"127.0.0.1"), (b"content-length", b"-5")]

    messages: list[dict] = []

    async def _receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def _send(message):
        messages.append(message)

    scope = {
        "type": "http", "method": "POST", "path": "/", "raw_path": b"/", "root_path": "",
        "query_string": b"", "headers": scope_headers, "scheme": "http",
        "server": ("127.0.0.1", 80), "client": ("127.0.0.1", 1), "http_version": "1.1",
        "asgi": {"version": "3.0"},
    }
    asyncio.run(tiny(scope, _receive, _send))

    assert messages[0]["status"] == 400


def test_a_route_that_swallows_the_overflow_still_answers_413():
    """Маршрут поймал исключение и ответил 400 — клиент всё равно видит 413."""

    async def _endpoint(request: Request):
        try:
            await request.body()
        except Exception:
            return JSONResponse({"error": "bad body"}, status_code=400)
        return JSONResponse({"ok": True})

    inner = Starlette(routes=[Route("/", _endpoint, methods=["POST"])])
    limited = RequestBodyLimit(inner, default_max_body=100)

    async def _chunks():
        for _ in range(10):
            yield b"y" * 50

    response = _run("POST", "/", content=_chunks(), asgi_app=limited)
    assert response.status_code == 413

    small = _run("POST", "/", content=b"y" * 100, asgi_app=limited)
    assert small.status_code == 200


def test_builder_routes_get_the_higher_limit_other_routes_do_not():
    limiter = RequestBodyLimit(lambda *a: None, route_max_body=request_limits.ROUTE_MAX_BODY)

    for path in ("/build-upload", "/preview-publish", "/publish-jobs", "/publish-jobs/", "/publish-plan", "/api/page-forms"):
        assert limiter.limit_for(path) == BUILDER_MAX_BODY
    for path in ("/report-problem", "/account-links", "/publish-jobs/abc/cancel", "/"):
        assert limiter.limit_for(path) == DEFAULT_MAX_BODY


def test_builder_limit_survives_a_root_path_prefix():
    from src.web.request_limits import _route_path

    scope = {"path": "/acl/build-upload", "root_path": "/acl"}
    limiter = RequestBodyLimit(lambda *a: None, route_max_body=request_limits.ROUTE_MAX_BODY)
    assert limiter.limit_for(_route_path(scope)) == BUILDER_MAX_BODY


# --- form limits --------------------------------------------------------------


def test_too_many_fields_on_a_small_form_is_refused():
    fields = {f"f{i}": "1" for i in range(request_limits.SMALL_FORM.max_fields + 1)}
    fields["text"] = "something broke"

    response = _run("POST", "/report-problem", data=fields)

    assert response.status_code == 400
    assert "Too many fields" in response.text


def test_too_many_fields_as_multipart_is_refused():
    files = [(f"f{i}", (None, "1")) for i in range(request_limits.SMALL_FORM.max_fields + 1)]

    response = _run("POST", "/account-spend/read", files=files)

    assert response.status_code == 400
    assert "Too many fields" in response.text


def test_builder_form_field_limit_is_enforced():
    body = urlencode({f"f{i}": "" for i in range(request_limits.BUILDER_FORM.max_fields + 1)})

    response = _run(
        "POST",
        "/preview-publish",
        content=body.encode(),
        headers={"content-type": "application/x-www-form-urlencoded"},
    )

    assert response.status_code == 400
    assert "Too many fields" in response.text


def test_oversized_single_field_is_refused():
    response = _run(
        "POST",
        "/report-problem",
        data={"text": "x" * (request_limits.SMALL_FORM.max_part_size + 1)},
    )

    assert response.status_code == 400
    assert "exceeded maximum size" in response.text


def test_file_parts_are_refused_the_dashboard_takes_no_uploads():
    response = _run(
        "POST",
        "/report-problem",
        data={"text": "hello"},
        files={"attachment": ("creative.mp4", b"\x00" * 1024, "video/mp4")},
    )

    assert response.status_code == 400
    assert "Too many files" in response.text


def _realistic_builder_form(campaigns: int, ads: int) -> list[tuple[str, str]]:
    copy = ("Bonus up to 500 EUR on your first deposit. Play responsibly. 18+ " * 40)[:2000]
    form: list[tuple[str, str]] = [
        ("country", "Mexico"),
        ("account_label", "MX-01"),
        ("tracking_link", "https://example.test/click?sub1={{campaign.name}}"),
        ("camp_count", str(campaigns)),
    ]
    for c in range(campaigns):
        form += [
            (f"camp_{c}_targeting", "Casino lovers"),
            (f"camp_{c}_os", "Android"),
            (f"camp_{c}_age", "35"),
            (f"camp_{c}_language", "Spanish"),
            (f"camp_{c}_asset", "Big Win Casino"),
            (f"camp_{c}_budget", "50"),
            (f"camp_{c}_name", f"MX_BWC_Android_Video_Casino_35_{c}"),
            (f"camp_{c}_creative_id", str(1000 + c)),
            (f"camp_{c}_ad_count", str(ads)),
        ]
        for a in range(ads):
            form += [
                (f"camp_{c}_ad_{a}_creative_type", "Video"),
                (f"camp_{c}_ad_{a}_creative_file", f"MX/Video/creative_{c}_{a}_1080x1920.mp4"),
                (f"camp_{c}_ad_{a}_copy", copy),
            ]
    return form


@pytest.mark.parametrize("as_multipart", [False, True])
def test_a_realistic_large_builder_submission_still_goes_through(monkeypatch, as_multipart):
    """Большой билд (60 кампаний × 12 объявлений с длинными текстами) проходит.

    Файлы креативов по HTTP не едут — билдер передаёт их имена, а сами файлы
    лежат в папке креативов на диске. Поэтому «реалистичная загрузка креатива»
    для дешборда — это форма билдера с сотнями объявлений.
    """
    captured: dict = {}

    def _assemble(*, country, account_label, slots):
        captured.update(country=country, account=account_label, slots=len(slots))
        return [], b"PK-xlsx", "Upload FB.xlsx"

    monkeypatch.setattr("src.web.app.assemble_from_form_slots", _assemble)
    form = _realistic_builder_form(campaigns=60, ads=12)
    assert len(form) > request_limits.SMALL_FORM.max_fields * 10

    if as_multipart:
        # Браузер шлёт FormData — multipart/form-data.
        response = _run("POST", "/build-upload", files=[(k, (None, v)) for k, v in form])
    else:
        response = _run(
            "POST",
            "/build-upload",
            content=urlencode(form).encode(),
            headers={"content-type": "application/x-www-form-urlencoded"},
        )

    assert response.status_code == 200, response.text[:300]
    assert response.content == b"PK-xlsx"
    assert captured == {"country": "MX", "account": "MX-01", "slots": 60 * 12}
    body_size = len(urlencode(form))
    assert DEFAULT_MAX_BODY < body_size < BUILDER_MAX_BODY
