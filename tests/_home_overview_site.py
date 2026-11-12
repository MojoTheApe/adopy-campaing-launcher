"""Стенд для браузерных проверок Home («таблица как в Google» + плитки): настоящие шаблоны и настоящий JS страницы.

Страница собирается из `_account_map.html` (с данными `_build_account_map_view`), CSS `report.html` и фрагмента JS
между `var AMAP_OPEN_KEY` и «Раскрытие билдера» (раскрытие, поиск, блок `aov*`). Сети нет; Meta и Multilogin не трогаются.
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPORT = (ROOT / "src" / "web" / "templates" / "report.html").read_text(encoding="utf-8")
TOKENS = (ROOT / "src" / "web" / "templates" / "adopy-tokens.css").read_text(encoding="utf-8")

#: Всё CSS до первого </style> — те же правила, что у страницы.
_CSS = REPORT[REPORT.index("<style>") + 7: REPORT.index("</style>")]
#: Jinja-вставки внутри CSS стенду не нужны — отрезаем строки с «{%»/«{{».
_CSS = "\n".join(line for line in _CSS.splitlines() if "{%" not in line and "{{" not in line)
#: Монтирование дерева кампаний (без самого компонента `hcMount`: его подменяет заглушка стенда).
_TREE_JS = REPORT[REPORT.index("function amapCampaignTree("): REPORT.index("/* «Read campaigns in")]
_JS = REPORT[REPORT.index("var AMAP_OPEN_KEY = 'acl.amap.open';"): REPORT.index("/* Раскрытие билдера переживает")]


def iso(days_ago: int) -> str:
    return (date.today() - timedelta(days=days_ago)).isoformat() + "T10:00:00"


def build_amap(web, links, monkeypatch, spec: dict[str, list[tuple]], status_extra: dict | None = None):
    """spec: {страна: [(аккаунт, спенд|None, статус, текст статуса, дней назад, in_workbook)]} → amap-представление."""
    statuses, report_countries, not_in_book = {}, [], set()
    for country, accounts in spec.items():
        links.add_value("country", country)
        rows = []
        for acc, spend, status, text, ago, *rest in accounts:
            links.link(acc, "country", country)
            if rest and rest[0] is False:
                not_in_book.add(acc)
            if status:
                statuses[acc] = {"status": status, "status_text": text, "at": iso(ago),
                                 "at_short": "", "stale": "", "tried_at": "", "tried_error": ""}
            if acc in not_in_book:
                continue  # книга аккаунт не знает — в её строках его нет
            rows.append({"account_label": acc, "account_excel": {
                "has_data": spend is not None, "spend": spend, "leads": 12 if spend else 0,
                "deposits": 1 if spend and spend > 100 else 0, "cpl": 4.0, "cpa": 40.0, "cr": 8.0}})
        report_countries.append({"country": country, "account_rows": rows,
                                 "my_spend": sum(a[1] or 0 for a in accounts),
                                 # книжные тоталы страны: из них _amap_totals строит «Total · all countries»
                                 "my_totals": {"spend": sum(a[1] or 0 for a in accounts), "leads": 12 * len(accounts), "deposits": len(accounts)}})
    for acc, extra in (status_extra or {}).items():
        statuses[acc] = {**statuses.get(acc, {}), **extra}
    monkeypatch.setattr(web, "_account_status", lambda a: statuses.get(a, {}))
    monkeypatch.setattr(web, "_workbook_has_account", lambda df, country, label: label not in not_in_book)
    return web._build_account_map_view({"countries": report_countries})


def page_html(amap, theme: str = "light") -> str:
    from src.web.app import templates

    body = templates.env.get_template("_account_map.html").render(amap=amap, compare=None, compare_fragment_note="", mlx={})
    return (f"<!doctype html><html data-theme='{theme}'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<style>{TOKENS}</style><style>{_CSS}</style></head>"
            "<body style='margin:16px;background:var(--adopy-bg);color:var(--adopy-text);font-family:system-ui,sans-serif'>"
            "<div id='amap-slot'>" + body + "</div>"
            "<div class='sidebar-link' data-view='country-chile-manage' style='display:none'></div>"
            "<script>window.__manage = []; document.querySelectorAll('.sidebar-link').forEach(function(b){"
            "b.addEventListener('click', function(){ window.__manage.push(b.dataset.view); }); });</script>"
            # заглушка hcMount запоминает, какие аккаунты и строки получило дерево; сети нет — читать нечего
            "<template id='hc-scoped-tpl'><div class='hc-stub'></div></template>"
            "<script>window.AMAP_CAMPAIGN_TABLE = true; window.__hc = [];"
            "window.hcMount = function (host, opts) { window.__hc.push({country: host.dataset.country,"
            " accounts: opts.seed[0].accounts, rows: Object.keys(opts.rows)});"
            " return {init: function () { return Promise.resolve(); }, syncTop: function () {}}; };</script>"
            f"<script>{_TREE_JS}</script><script>{_JS}</script></body></html>")


#: Набор из мокапа: пять стран, все пять состояний, Chile с 22 аккаунтами (складывание на 15).
def standard_spec() -> dict[str, list[tuple]]:
    chile = [(f"Facebook_CL{i:02d}", 300 - i * 10, None, "", 0) for i in range(8)]
    chile += [(f"Facebook_CLR{i}", 0, "restricted", "profile restricted", 1 + i) for i in range(5)]
    chile += [("Facebook_CLD1", 40, "disabled", "account disabled", 0), ("Facebook_CLD2", 0, "disabled", "account disabled", 2)]
    chile += [(f"Facebook_CLN{i}", None, "", "", 0, False) for i in range(4)]
    chile += [(f"Facebook_CLU{i}", None, "", "", 0) for i in range(3)]
    return {
        "Colombia": [("Facebook_DQ-214", 117.0, None, "", 0), ("Facebook_DQ-217", None, "", "", 0, False)],
        "Argentina": [("Facebook_DQ-213", 68.0, None, "", 0), ("Facebook_DQ-212", 40.0, "disabled", "account disabled", 0),
                      ("Facebook_UL-938", None, "", "", 0, False)],
        "Chile": chile,
        "Brazil": [("Facebook_BR1", 0, "restricted", "Ad account not in BM", 5)],
        "Costa Rica": [],
    }
