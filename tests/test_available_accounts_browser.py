"""ACL-1319 в браузере: выбрать, убрать, вернуть — на настоящем шаблоне.

Разметка — настоящий фрагмент `/account-map`, отрисованный приложением;
скрипт — вырезан из `report.html`, а не переписан в тест; запросы страницы
уходят в настоящее приложение (эндпоинт, хранилище в tmp, перерисовка
блока маршрутом /account-map/available). Подменена только книга и отчёт — через `get_cached_report`.
"""
from __future__ import annotations

import csv
import os
from pathlib import Path
from urllib.parse import urlparse

import pandas as pd
import pytest

from src.web import account_links as links

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "src" / "web" / "templates" / "report.html"


def _script() -> str:
    src = REPORT.read_text(encoding="utf-8")
    start = src.index("// ── Available accounts: выбрать и убрать (ACL-1319)")
    end = src.index("// Найти профиль в Multilogin", start)
    per = src.index("function amapPeriod() {")
    per_end = src.index("\n}\n", per) + 3
    return src[per:per_end] + "\n" + src[start:end]


@pytest.fixture
def app_client(monkeypatch, tmp_path):
    monkeypatch.setenv("ACL_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(links, "get_local_config_dir", lambda: str(tmp_path))
    map_path = str(tmp_path / "ad_account_map.csv")
    monkeypatch.setattr(links, "get_ad_account_map_path", lambda: map_path)
    monkeypatch.setattr("src.web.app.get_ad_account_map_path", lambda: map_path)
    with open(map_path, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["account_name", "ad_account_id", "profile_id"])
        w.writeheader()
        w.writerow({"account_name": "KLD_BFL_NEW1", "ad_account_id": "act_1", "profile_id": "p1"})

    df = pd.DataFrame([
        {"Account Alias": a, "Date": "2026-09-20", "$ Spend": s, "# Leads": 0, "# FTDs": 0,
         "$ Net": 0.0, "CM": "Evgeni Sedin", "Country Final": "Mexico"}
        for a, s in [("FacebookAA1", 10.0), ("FacebookBB2", 20.0), ("FacebookCC3", 30.0),
                     ("FacebookZZ9", 0.0)]
    ])
    report = {"countries": [{
        "country": "Mexico",
        "account_rows": [{"account_label": l, "account_excel": {}}
                         for l in ("FacebookAA1 MX", "FacebookBB2 MX", "FacebookCC3 MX", "FacebookZZ9 MX")],
    }]}
    monkeypatch.setattr("src.web.app.get_cached_report", lambda: (report, df, []))
    # Отчёт за период на экране — тот же, что строит Home. Здесь он не
    # пересчитывается (это сходило бы в Google Sheets), а только отмечается:
    # блок обязан собираться из отчёта за период, а не из общего.
    periods: list[tuple[str, str]] = []

    def _for_period(rep, _df, date_from, date_to):
        periods.append((date_from, date_to))
        return rep

    monkeypatch.setattr("src.web.app.report_for_period", _for_period)

    from src.web.app import app

    client = _AppClient(app)
    client.periods = periods
    yield client


class _AppClient:
    """Синхронный вызов приложения из обработчика маршрутов Playwright.

    `TestClient` здешней связки starlette/httpx не собирается, а свой цикл
    событий в потоке Playwright запускать нельзя — поэтому каждый запрос
    идёт через httpx.ASGITransport в отдельном потоке."""

    def __init__(self, app):
        from concurrent.futures import ThreadPoolExecutor

        self._app = app
        self._pool = ThreadPoolExecutor(max_workers=1)

    def _call(self, method: str, url: str, **kw):
        import asyncio

        import httpx

        async def _go():
            transport = httpx.ASGITransport(app=self._app, raise_app_exceptions=False)
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as c:
                return await c.request(method, url, **kw)

        return self._pool.submit(asyncio.run, _go()).result()

    def get(self, url: str, **kw):
        return self._call("GET", url, **kw)

    def post(self, url: str, **kw):
        return self._call("POST", url, **kw)


def _page_html(client) -> str:
    r = client.get("/account-map", params={"span": "all"})
    assert r.status_code == 200
    return ('<!doctype html><html><body><div id="amap-slot">' + r.text + "</div>"
            "<script>" + _script() + "</script></body></html>")


def test_select_remove_restore_and_counts(app_client):
    from playwright.sync_api import expect, sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get("ACL_BROWSER_EXECUTABLE") or None)
        page = browser.new_page()
        errors: list[str] = []
        dialogs: list[str] = []
        answer = {"accept": True}
        page.on("pageerror", lambda e: errors.append(str(e)))

        def _dialog(d):
            dialogs.append(d.message)
            d.accept() if answer["accept"] else d.dismiss()

        page.on("dialog", _dialog)

        def route(r):
            url = urlparse(r.request.url)
            if r.request.resource_type == "document":
                r.fulfill(content_type="text/html", body=_page_html(app_client))
            elif url.path == "/api/available-accounts/hidden":
                resp = app_client.post(url.path, content=r.request.post_data,
                                       headers={"Content-Type": "application/json"})
                r.fulfill(status=resp.status_code, content_type="application/json", body=resp.text)
            elif url.path == "/account-map/available":
                resp = app_client.get(url.path + ("?" + url.query if url.query else ""))
                r.fulfill(status=resp.status_code, content_type="text/html", body=resp.text)
            else:
                r.fulfill(status=404, body="")

        page.route("**/*", route)
        page.goto("http://acl.test/")
        page.evaluate("window._amapPick = {from: '2026-09-01', to: '2026-09-20', span: ''}")
        # Метка страницы: перерисовка должна идти фрагментом. Запасной путь
        # скрипта — полная перезагрузка — сотрёт метку, и тест это увидит,
        # а не пройдёт молча через перезагрузку.
        page.evaluate("window.__noReload = 1")

        count = page.locator(".amap-avail-count")
        visible_rows = page.locator(".amap-available tr.amap-avail-row:not([hidden])")
        remove = page.locator(".amap-avail-remove")

        # Активные и только что подключённый — видны; нулевой спенд — нет.
        expect(count).to_have_text("4")
        expect(visible_rows).to_have_count(4)
        expect(page.locator('tr[data-account="FacebookZZ9 MX"]')).to_be_hidden()
        expect(page.locator('tr[data-account="KLD_BFL_NEW1"]')).to_contain_text("new")
        expect(remove).to_be_disabled()

        # «Show inactive» показывает отфильтрованных и прячет обратно.
        page.locator(".amap-avail-show-inactive").check()
        expect(visible_rows).to_have_count(5)
        page.locator(".amap-avail-show-inactive").uncheck()
        expect(visible_rows).to_have_count(4)

        # Несколько: подтверждение спрашивается.
        page.locator('tr[data-account="FacebookAA1 MX"] .amap-avail-pick').check()
        page.locator('tr[data-account="FacebookBB2 MX"] .amap-avail-pick').check()
        expect(remove).to_have_text("Remove selected (2)")
        remove.click()
        expect(count).to_have_text("2")
        assert len(dialogs) == 1 and "Remove 2 accounts" in dialogs[0], dialogs
        expect(page.locator(".amap-avail-hidden-count")).to_have_text("2")
        assert links.hidden_accounts() == ["FacebookAA1 MX", "FacebookBB2 MX"]

        # «Select all» берёт только видимые строки; отказ в подтверждении —
        # ничего не меняет.
        page.locator(".amap-avail-all").check()
        expect(remove).to_have_text("Remove selected (2)")
        answer["accept"] = False
        remove.click()
        expect(count).to_have_text("2")
        assert len(dialogs) == 2 and links.hidden_accounts() == ["FacebookAA1 MX", "FacebookBB2 MX"]
        answer["accept"] = True
        remove.click()
        expect(count).to_have_text("0")
        expect(page.locator(".amap-avail-hidden-count")).to_have_text("4")
        expect(page.locator(".amap-avail-empty")).to_contain_text("Show inactive")
        assert "FacebookZZ9 MX" not in links.hidden_accounts(), "невидимая строка ушла в Remove"

        # Restore возвращает одного.
        page.locator(".amap-avail-hidden > summary").click()
        page.locator('.amap-avail-hidden tr[data-account="FacebookAA1 MX"] .amap-avail-restore').click()
        expect(count).to_have_text("1")
        expect(page.locator(".amap-avail-hidden-count")).to_have_text("3")

        assert page.evaluate("window.__noReload") == 1, "список перерисован перезагрузкой страницы"
        assert app_client.periods and set(app_client.periods) == {("2026-09-01", "2026-09-20")}, app_client.periods

        # Переживает перезагрузку.
        page.reload()
        expect(count).to_have_text("1")
        expect(page.locator(".amap-avail-hidden-count")).to_have_text("3")

        # Один аккаунт — без подтверждения.
        n = len(dialogs)
        page.locator('tr[data-account="FacebookAA1 MX"] .amap-avail-pick').check()
        remove.click()
        expect(count).to_have_text("0")
        assert len(dialogs) == n, dialogs

        # Ничего, кроме списка, не тронуто.
        assert (Path(links.get_local_config_dir()) / "ad_account_map.csv").read_text(encoding="utf-8").count("KLD_BFL_NEW1") == 1
        assert not errors, errors
        browser.close()
