"""Security boundary for ACL's privileged localhost dashboard."""
from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from src.web.app import app, templates


async def _request(method: str, path: str, **kwargs) -> httpx.Response:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1",
    ) as client:
        return await client.request(method, path, **kwargs)


def test_non_loopback_host_is_rejected() -> None:
    response = asyncio.run(_request("GET", "/favicon.ico", headers={"Host": "attacker.example"}))

    assert response.status_code == 400
    assert response.json()["error"] == "Local dashboard host required"


def test_cross_site_browser_post_is_rejected_before_route() -> None:
    response = asyncio.run(
        _request(
            "POST",
            "/publish-jobs",
            headers={"Origin": "https://attacker.example", "Sec-Fetch-Site": "cross-site"},
            data={"activate": "1"},
        )
    )

    assert response.status_code == 403
    assert response.json()["error"] == "Cross-site request blocked"


def test_malformed_origin_is_rejected_without_server_error() -> None:
    response = asyncio.run(
        _request("POST", "/publish-jobs", headers={"Origin": "http://[broken"}, data={})
    )

    assert response.status_code == 403
    assert response.json()["error"] == "Cross-site request blocked"


def test_same_origin_post_reaches_application_safety_gate() -> None:
    response = asyncio.run(
        _request(
            "POST",
            "/publish-jobs",
            headers={"Origin": "http://127.0.0.1:9090", "Sec-Fetch-Site": "same-origin"},
            data={"activate": "1"},
        )
    )

    assert response.status_code == 403
    assert "Live activation is disabled" in response.json()["error"]


def test_security_headers_are_on_local_responses() -> None:
    response = asyncio.run(_request("GET", "/favicon.ico"))

    assert response.status_code == 200
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


def test_jinja_json_cannot_break_out_of_script_tag() -> None:
    payload = {"name": "</script><script>alert(1)</script>"}

    rendered = templates.env.from_string(
        '<script type="application/json">{{ payload | tojson }}</script>'
    ).render(payload=payload)

    assert "</script><script>alert(1)" not in rendered
    assert "\\u003c/script\\u003e" in rendered


def test_dynamic_publish_and_profile_values_are_html_escaped() -> None:
    template = Path("src/web/templates/report.html").read_text(encoding="utf-8")

    assert "aclEscHtml(r.campaign_name || '?')" in template
    assert "r.blocking_failures.map(aclEscHtml)" in template
    assert "mlProfileRowsHtml(profiles, folderMap, instance)" in template
    assert "JSON.stringify(pName) + ',this)'" not in template
