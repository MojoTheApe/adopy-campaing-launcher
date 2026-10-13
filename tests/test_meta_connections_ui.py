"""Chromium executes the real shared Connect partial and its production JS.

HTTP goes through the actual ASGI application/security boundary. The fake is
only the explicitly isolated OS encryption boundary, never Meta/MLX success.
"""
from __future__ import annotations

import importlib
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from jinja2 import Environment, FileSystemLoader
from playwright.sync_api import sync_playwright

from tests.test_meta_connections import setup, call, status, TOKEN_A, TOKEN_B, SECRET
from src.web import meta_connections as connections

ROOT = Path(__file__).resolve().parents[1]


def connect_html(accounts):
    folder = ROOT / "src/web/templates"
    environment = Environment(loader=FileSystemLoader(folder), autoescape=True)
    partial = environment.get_template("_account_map_add.html").render(
        amap={"all_accounts": accounts}, amap_value="", mlx={"A": "Inhouse", "B": "KLD"})
    source = (folder / "report.html").read_text(encoding="utf-8")
    script = source[source.index("function amapMetaClear("):source.index("function amapHint(")]
    return '<!doctype html><html><body><section class="acc-connect"><h3>Connect an account</h3>' + partial + '</section><script>' + script + '</script></body></html>'


def test_actual_connect_controls_save_reopen_restart_replace_disconnect(setup):
    requests = []
    with ThreadPoolExecutor(max_workers=1) as api, sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get("ACL_BROWSER_EXECUTABLE") or None)
        page = browser.new_page()
        def route(request_route):
            request = request_route.request
            if request.url == "http://127.0.0.1/connect-test":
                request_route.fulfill(status=200, content_type="text/html", body=connect_html(["Alpha", "Beta"]))
            elif "/meta-connections/" in request.url:
                from urllib.parse import urlsplit
                path = urlsplit(request.url).path
                requests.append((request.method, path))  # Never record credential bodies.
                response = api.submit(call, request.method, path, body=request.post_data_json,
                                      headers={"Origin": "http://127.0.0.1", "Sec-Fetch-Site": "same-origin"}).result(timeout=10)
                request_route.fulfill(status=response.status_code, content_type="application/json", body=response.text)
            else:
                raise AssertionError("Unexpected browser or provider call")
        page.route("**/*", route)
        page.goto("http://127.0.0.1/connect-test")
        assert page.locator('.acc-connect').count() == 1
        assert page.locator('.amap-ml').count() == 1
        panel = page.locator('.amap-meta')
        panel.locator('summary').click()
        panel.locator('.amap-meta-account').select_option("Alpha")
        page.wait_for_function("document.querySelector('.amap-meta')._metaState !== null")
        first = panel.evaluate("el => el._metaState")
        panel.locator('.amap-meta-app').fill("300000001")
        panel.locator('.amap-meta-token').fill(TOKEN_A)
        panel.locator('.amap-meta-secret').fill(SECRET)
        panel.locator('.amap-meta-save').click()
        page.wait_for_function("document.querySelector('.amap-meta')._metaState.has_credential === true")
        assert "not verified" in panel.locator('.amap-meta-message').inner_text()
        assert panel.locator('.amap-meta-token').input_value() == ''
        assert panel.locator('.amap-meta-secret').input_value() == ''
        assert TOKEN_A not in page.content() and SECRET not in page.content()
        panel.locator('summary').click()
        panel.locator('summary').click()
        page.wait_for_function("document.querySelector('.amap-meta')._metaState !== null")
        assert panel.evaluate("el => el._metaState.connection_id") == first["connection_id"]
        # Reset server module state and recreate the browser document. The next
        # request creates a fresh ASGI client and recovers from persisted files.
        importlib.reload(connections)
        page.reload()
        panel.locator('summary').click()
        panel.locator('.amap-meta-account').select_option("Alpha")
        page.wait_for_function("document.querySelector('.amap-meta')._metaState?.has_credential === true")
        assert panel.evaluate("el => el._metaState.connection_id") == first["connection_id"]
        panel.locator('.amap-meta-token').fill(TOKEN_B)
        panel.locator('.amap-meta-save').click()
        page.wait_for_function("document.querySelector('.amap-meta')._metaState.credential_revision === 2")
        panel.locator('.amap-meta-disconnect').click()
        page.wait_for_function("document.querySelector('.amap-meta')._metaState.phase === 'disconnected'")
        assert "not revoked" in panel.locator('.amap-meta-message').inner_text()
        assert not api.submit(status, first).result(timeout=10)["has_credential"]
        assert TOKEN_A not in page.content() and TOKEN_B not in page.content() and SECRET not in page.content()
        assert len([r for r in requests if r[1].endswith('/credential')]) == 2
        assert len([r for r in requests if r[1].endswith('/disconnect')]) == 1
        browser.close()


def test_hostile_account_label_stays_text_in_rendered_connect():
    html = connect_html(['<img src=x onerror="window.executed=true">'])
    assert '<img src=x' not in html
    assert '&lt;img' in html
