"""Real ASGI requests for the Campaign Builder form endpoints.

These deliberately do not mock ``Request.form``.  The production routes use
Starlette's form parser, which requires ``python-multipart`` even for ordinary
``application/x-www-form-urlencoded`` bodies.  Import-only smoke tests and the
older direct route tests could therefore pass on a clean install while the
first button click in the dashboard failed at runtime.
"""
from __future__ import annotations

import asyncio

import httpx
from starlette.responses import JSONResponse

from src.web.app import app


async def _post(path: str, data: dict[str, str]) -> httpx.Response:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1",
    ) as client:
        return await client.post(path, data=data)


def test_preview_publish_parses_a_real_urlencoded_form() -> None:
    response = asyncio.run(_post("/preview-publish", {"sentinel": "parsed"}))

    assert response.status_code == 400
    assert response.json()["error"].startswith("Country not set")


def test_publish_jobs_parses_form_before_confirmation_gate() -> None:
    response = asyncio.run(_post("/publish-jobs", {"sentinel": "parsed"}))

    assert response.status_code == 403
    assert "confirm in the dashboard" in response.json()["error"]


def test_build_upload_parses_form_before_rendering(monkeypatch) -> None:
    # Rendering the full report is unrelated to the form-parser contract and
    # can read large local workbooks.  Keep the ASGI request and real parser,
    # but replace the post-parse response with a small deterministic one.
    monkeypatch.setattr(
        "src.web.app._render_report",
        lambda *args, **kwargs: JSONResponse({"rendered": True}, status_code=422),
    )

    response = asyncio.run(_post("/build-upload", {"sentinel": "parsed"}))

    assert response.status_code == 422
    assert response.json() == {"rendered": True}
