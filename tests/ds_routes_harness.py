"""ACL-1797: страница целиком в Chromium без сети — для проверок и снимков дизайн-системы.

Шаблон рендерится так же, как в соседних браузерных тестах (`_render_html`), но
с богатым отчётом: у страны есть активы, лендинги, тренд, комбо, таргетинг и
креативы, поэтому каждая вкладка страны не пустая. Сеть подменена: `/api/*`
отдают образцы, остальное — пустой JSON. Живых Meta и Multilogin тут нет.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_web_drill import _MOCK_REPORT, _render_html  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

# Маршруты, которых касается дизайн-система: боковая колонка и вкладки страны.
GLOBAL_VIEWS = ["home", "targets", "ad-texts", "accounts"]
COUNTRY_VIEWS = ["manage", "assets", "landings", "trend", "combos", "targeting", "creatives", "optimization"]


def _row(i: int, **extra):
    base = {"spend": 120.0 + i * 31, "leads": 9 + i, "deposits": i % 3, "net": 40.0 * (i % 3) - 5,
            "cpl": 13.3, "cpa": 60.0, "roi": 1.1}
    base.update(extra)
    return base


def rich_report() -> dict:
    """Отчёт с заполненными вкладками страны; пары «длинное имя — много колонок» растягивают таблицы."""
    country = dict(_MOCK_REPORT["countries"][0])
    long_interest = "Interests: Forex trading, Online trading, Stock market, Investment, Day trading, Binary option"
    country.update({
        "asset_summary": [_row(i, offer=f"Aramco Form {i}") for i in range(6)],
        "variants": [_row(i, variant=f"lp-v{i}", is_untagged=(i == 0)) for i in range(4)],
        "trend": {
            "has_date": True, "period_label": "Oct 1, 2026 – Oct 9, 2026",
            "trending_up": [_row(i, offer="Aramco Form", device="Android", interest=long_interest[:40],
                                 creative=f"1121 · {i}", spend_recent=50, leads_recent=4, deposits_recent=1,
                                 spend_total=200, leads_total=19, deposits_total=2) for i in range(3)],
            "stable": [], "trending_down": [],
        },
        "top_combos": [_row(i, offer="Aramco Form", device="iOS", age="25-34", interest=long_interest,
                            creative=f"1121 · {i}", category="Strong" if i % 2 else "Interesting") for i in range(5)],
        "targeting": [_row(i, interest=long_interest + f" #{i}") for i in range(5)],
        "creatives": [_row(i, creative_id=f"1121 · {i}", ad_title=f"Title {i}", text_name=f"Text {i}",
                           has_variants=(i == 0),
                           variants=[_row(7, label="Variant A"), _row(8, label="Variant B")]) for i in range(4)],
    })
    return {**_MOCK_REPORT, "countries": [country]}


def render() -> str:
    return _render_html(rich_report())


TARGET_PROFILES = {"ok": True, "profiles": [
    {"name": f"FX interests {i}", "interests": "Forex trading, Online trading, Stock market, Investment",
     "narrow": "Frequent travelers", "added_by": "Evgeni", "added_at": "2026-10-01", "updated_at": "2026-10-05",
     "scope": "team"} for i in range(4)], "deleted": []}

AD_TEXTS = {"ok": True, "texts": [
    {"name": f"Text {i}", "headline": "Learn to trade today", "body": "Open an account in minutes.\nNo experience needed.",
     "description": "Start now", "asset": "Aramco Form", "language": "ES" if i % 2 else ""} for i in range(4)],
    "deleted": [], "duplicates": {}, "library": {}}

ACCOUNTS = {"ok": True, "accounts": [], "stats": {}, "machine": {}}
MACHINE_PATHS = {"ok": True, "stats_workbook": "C:/data/stats.xlsx", "creatives_folder": "C:/data/creatives",
                 "manager_name": "Evgeni Sedin"}


def make_route(html: str):
    def route(r):
        path = urlparse(r.request.url).path
        if r.request.resource_type == "document":
            r.fulfill(content_type="text/html", body=html)
            return
        if path == "/api/targeting-profiles":
            r.fulfill(json=TARGET_PROFILES)
        elif path == "/api/ad-texts":
            r.fulfill(json=AD_TEXTS)
        elif path == "/api/accounts-overview":
            r.fulfill(json=ACCOUNTS)
        elif path == "/api/machine-paths":
            r.fulfill(json=MACHINE_PATHS)
        elif path.endswith((".css", ".svg", ".ico")):
            f = ROOT / "src" / "web" / "static" / Path(path).name
            if path.startswith("/static/") and f.exists():
                r.fulfill(path=str(f))
            else:
                r.fulfill(status=204)
        else:
            r.fulfill(json={})
    return route


def browser_launch(pw):
    return pw.chromium.launch(executable_path=os.environ.get("ACL_BROWSER_EXECUTABLE") or None)


def open_page(browser, html: str, *, theme: str = "light", width: int = 1280, height: int = 900):
    ctx = browser.new_context(viewport={"width": width, "height": height}, color_scheme=theme)
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.add_init_script(f"try {{ localStorage.setItem('acl.theme', '{theme}') }} catch (e) {{}}")
    page.route("**/*", make_route(html))
    page.goto("http://acl.test/")
    return ctx, page, errors


def country_id(page) -> str:
    return page.evaluate("document.querySelector('[id^=\"view-country-\"]').id.replace('view-country-','').replace(/-[a-z]+$/,'')")


def view_ids(page) -> list[str]:
    return page.evaluate("Array.from(document.querySelectorAll('.view-panel')).map(p => p.id.replace('view-',''))")


def show(page, view: str) -> None:
    page.evaluate("v => document.querySelector('[data-view=\"' + v + '\"]').click()", view)
    page.wait_for_timeout(250)


OVERFLOW_JS = """() => {
  const de = document.documentElement;
  const wide = [];
  document.querySelectorAll('.view-panel.active *').forEach(el => {
    const r = el.getBoundingClientRect();
    if (r.width && r.right > innerWidth + 1) wide.push((el.id || el.className || el.tagName).toString().slice(0, 40) + ':' + Math.round(r.right));
  });
  return {scrollWidth: de.scrollWidth, bodyScrollWidth: document.body.scrollWidth, inner: innerWidth, wide: wide.slice(0, 6)};
}"""
