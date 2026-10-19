"""Web drill-down: optimization-only in template and /details endpoint."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd

_ROOT = Path(__file__).resolve().parents[1]

from src.analysis_agent.analyze_fb_stats import aggregate_combos
from src.analysis_agent.morning_report import build_country_section
from src.web.app import mlx_instance_labels, _anchor_slug, _enrich_country_overview, details, templates

def _render_html(report: dict) -> str:
    """Render report.html the way the app does.

    Every test that built this context by hand drifted from the real one and
    then failed on whichever key was added last — `my_totals`,
    `account_preflight` — rather than on anything the test was about. The app
    fills in per-country totals before rendering, so do that here too.
    """
    report = {**report, "countries": [dict(c) for c in report.get("countries", [])]}
    _enrich_country_overview(report)
    html = templates.env.get_template("report.html").render(
        mlx_labels=mlx_instance_labels(),
        request=MagicMock(),
        report=report,
        warnings=[],
        built_at="2026-06-08",
        upload_errors={},
        account_preflight={},
        account_preflight_json={},
        ref_options={},
        ref_options_json={},
        account_alias_map_json={},
        draft_status={},
    )
    # ACL-1819: страница теперь закрывает Publish, пока нет pre-flight audit (ворота наглухо).
    # Эти тесты проверяют очередь и ящик, а не аудит: ворота здесь пропускают. Сам гейт —
    # tests/test_acl1819_preflight*.py (они вырезают эту вставку по метке ACL1819_TEST_GATE).
    head, _, tail = html.rpartition("</body>")
    return head + "<script>/*ACL1819_TEST_GATE*/window.cbPreflightGate = function (items, go) { go(); };</script></body>" + tail


_MOCK_REPORT = {
    "date": "2026-06-08",
    "my_cm": "Evgeni Sedin",
    "source_excel": "test.xlsx",
    "trend_days": 7,
    "total_combos": 1,
    "countries": [
        {
            "country": "Mexico",
            "cm_comparison": [
                {
                    "cm": "Evgeni Sedin",
                    "deposits": 1,
                    "leads": 10,
                    "spend": 50.0,
                    "net": 30.0,
                    "cpa": 50.0,
                    "roi": 1.2,
                    "is_me": True,
                    "aggregate_key": {
                        "section": "cm_comparison",
                        "country": "Mexico",
                        "cm": "Evgeni Sedin",
                    },
                }
            ],
            "trend": {"has_date": False},
            "top_combos": [],
            "targeting": [],
            "creatives": [],
            "close_recs": {
                "MX-acc-1": [
                    {
                        "adset_name": "ME_test_adset",
                        "spend": 100.0,
                        "leads": 5,
                        "deposits": 0,
                        "cpl": 20.0,
                        "cpa": None,
                        "reason": "0 FTD",
                        "aggregate_key": {
                            "section": "optimization",
                            "country": "Mexico",
                            "account": "MX-acc-1",
                            "adset": "ME_test_adset",
                        },
                    }
                ]
            },
            "open_recs": {},
            "account_rows": [],
        }
    ],
}


def _render_report() -> str:
    report = {**_MOCK_REPORT, "countries": [dict(c) for c in _MOCK_REPORT["countries"]]}
    _enrich_country_overview(report)
    return _render_html(report)


def _details_html(query: str) -> str:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/details",
        "query_string": query.encode(),
        "headers": [],
    }

    async def _run() -> str:
        from starlette.requests import Request

        request = Request(scope)
        response = await details(request)
        return response.body.decode()

    return asyncio.run(_run())


def test_no_template_uses_an_htmx_feature_the_dashboard_csp_forbids() -> None:
    """htmx compiles these attributes with new Function; our CSP has no unsafe-eval.

    This is not a style rule. The drill toggle was written as
    `hx-trigger="click[aclDrillToggle(this)]"` and the predicate never ran once:
    the browser refused to compile it, htmx swallowed the EvalError, and the
    function we wrote to stop the duplicate rows was dead code. Closing a drill
    still appeared to work, by accident, deeper inside htmx — while every close
    fired a second request to the server and the outcome depended on which
    response landed first. That is what "sometimes it doesn't open" was.

    A failure of this kind is invisible: nothing throws where the code is, the
    page keeps working, and only the console knows. So it is caught here.
    """
    forbidden = {
        "hx-on": "runs its body through eval",
        "hx-vals": "only the js: form needs eval, but it is the easy one to reach for",
        "hx-headers": "same as hx-vals",
    }
    offenders: list[str] = []
    for path in sorted((_ROOT / "src" / "web" / "templates").glob("*.html")):
        text = path.read_text(encoding="utf-8")
        for line_no, line in enumerate(text.splitlines(), 1):
            if line.lstrip().startswith(("//", "*", "/*", "#")):
                continue  # a comment explaining the rule is not a violation of it
            if re.search(r'hx-trigger="[^"]*\[', line):
                offenders.append(f"{path.name}:{line_no} hx-trigger predicate — compiled with new Function")
            for attr, why in forbidden.items():
                if re.search(rf'\b{attr}(:[\w-]+)?=', line) and "js:" in line:
                    offenders.append(f"{path.name}:{line_no} {attr} — {why}")

    assert not offenders, (
        "Эти атрибуты htmx требуют eval, а CSP дешборда его не даёт — они не "
        "сработают ни разу и провалятся молча:\n  " + "\n  ".join(offenders)
    )


def _drill_click_handler(template: str) -> str:
    """Тело обработчика клика целиком.

    Раньше здесь бралось 1200 символов от начала — и первый же добавленный
    комментарий вытолкнул проверяемую строку за край окна. Тест падал не на
    поведении, а на длине текста над ним.
    """
    start = template.index("document.addEventListener('click', function (ev) {")
    end = template.index("}, true);", start)
    return template[start:end]


def test_drill_toggle_is_a_real_listener_not_an_htmx_predicate() -> None:
    """The close must happen in our own code, before htmx sees the click."""
    template = (_ROOT / "src" / "web" / "templates" / "report.html").read_text(encoding="utf-8")

    body = _drill_click_handler(template)
    assert "tr.drillable" in body
    assert "drill-detail" in body
    # Without this htmx still fires the request on the closing click.
    assert "ev.stopPropagation();" in body


def test_only_one_toggle_owns_a_drill() -> None:
    """Переключатель у раскрытия должен быть один.

    Их было два: этот обработчик снимал панель по клику, а родной отменял
    запрос на htmx:beforeRequest. Они гонялись — на одной строке второй клик
    закрывал, на другой закрывал и тут же открывал заново, и снаружи это
    выглядело как «кнопка не работает».
    """
    template = (_ROOT / "src" / "web" / "templates" / "report.html").read_text(encoding="utf-8")

    body = _drill_click_handler(template)
    assert "data-drill" in body, (
        "обработчик клика обязан пропускать раскрытия с data-drill — у них "
        "есть свой переключатель на htmx:beforeRequest"
    )
    # родной переключатель на месте и отменяет запрос средствами htmx
    assert 'el.getAttribute("data-drill")' in template
    assert "evt.preventDefault();" in template


def test_report_template_marks_drillable_rows() -> None:
    """Drills live in Optimization AND CM comparison.

    This asserted exactly one drillable spot, from when Optimization was the
    only one. The CM-comparison drill was added deliberately (app.py:
    _DRILL_ENABLED), so the old count encoded a rule that no longer holds.
    """
    html = _render_report()

    assert 'data-drill="optimization"' in html
    assert html.count('class="drillable"') >= 1


@patch("src.web.app.get_cached_report")
def test_details_rejects_sections_without_a_drill(mock_cache) -> None:
    """cm_comparison is drillable now, so ask about a section that is not."""
    mock_cache.return_value = (_MOCK_REPORT, pd.DataFrame(), [])
    text = _details_html("section=targeting&country=Mexico")

    assert "not available for this section" in text
    assert "drill-table" not in text


@patch("src.web.app.get_cached_report")
@patch("src.web.app.get_underlying_rows")
def test_details_optimization_returns_fragment(mock_rows, mock_cache) -> None:
    mock_cache.return_value = (_MOCK_REPORT, pd.DataFrame(), [])
    mock_rows.return_value = [
        {
            "ad_title": "Ad A",
            "ad_asset": "trader_pro",
            "spend": 50.0,
            "leads": 10,
            "ftds": 1,
            "cpl": 5.0,
            "cpa": 50.0,
            "date": "2026-06-01",
        }
    ]
    text = _details_html(
        "section=optimization&country=Mexico&account=MX-acc-1&adset=ME_test"
    )

    assert text.count('class="drill-detail"') == 1
    assert "Ad A" in text


def test_optimization_drill_aggregate_keys_match_each_country(
    enriched_df: pd.DataFrame,
    accounts_rows: list,
) -> None:
    """Every country with close_recs gets drill rows scoped to that country."""
    combos = aggregate_combos(enriched_df)
    countries = sorted(enriched_df["Country Final"].dropna().unique().tolist())
    sections = [
        build_country_section(
            enriched_df, combos, country, accounts_rows,
            my_cm="Evgeni Sedin", trend_days=7,
        )
        for country in countries
    ]
    report = {
        "date": "2026-06-08",
        "my_cm": "Evgeni Sedin",
        "source_excel": "fixtures/synthetic.xlsx",
        "trend_days": 7,
        "total_combos": len(combos),
        "countries": sections,
    }
    html = _render_html(report)

    countries_with_drill = 0
    for section in sections:
        country = section["country"]
        cid = _anchor_slug(country)
        close = section.get("close_recs") or {}
        drill_keys = [
            rec["aggregate_key"]
            for recs in close.values()
            for rec in recs
            if rec.get("aggregate_key")
        ]
        if not drill_keys:
            continue
        countries_with_drill += 1
        assert f'id="view-country-{cid}-optimization"' in html
        for key in drill_keys:
            assert key["section"] == "optimization"
            assert key["country"] == country
            assert "data-drill=\"optimization\"" in html
    assert countries_with_drill >= 1


def test_open_section_renders_account_adset_ad_titles() -> None:
    report = {
                "date": "2026-06-08",
                "my_cm": "Evgeni Sedin",
                "source_excel": "test.xlsx",
                "trend_days": 7,
                "total_combos": 1,
                "countries": [
                    {
                        "country": "Mexico",
                        "cm_comparison": [],
                        "trend": {"has_date": False},
                        "top_combos": [],
                        "targeting": [],
                        "creatives": [],
                        "close_recs": {},
                        "open_recs": {
                            "MX-acc-2": [
                                {
                                    "strategy_type": 2,
                                    "campaign_name": "MX_Trl_Ios_Images_trading_35+_08.06",
                                    "creatives": [
                                        {"name": "Creative A"},
                                        {"name": "Creative B"},
                                        {"name": "Creative C"},
                                    ],
                                }
                            ],
                            "MX-acc-1": [
                                {
                                    "strategy_type": 1,
                                    "campaign_name": "MX_Amz_All_Mix_trading_25_08.06",
                                    "creatives": [
                                        {"name": "Ad title one"},
                                        {"name": "Ad title two"},
                                    ],
                                }
                            ],
                        },
                        "account_rows": [],
                    }
                ],
            }
    html = _render_html(report)

    assert 'class="open-tree"' in html
    # .open-account-name is display:none since the account header was
    # rebuilt; the visible name is .cb-acc-title now.
    assert 'class="cb-acc-title">MX-acc-1</span>' in html
    assert 'class="cb-acc-title">MX-acc-2</span>' in html
    # The per-account submit button lost its label suffix when the account
    # header was rebuilt; the account is named in .cb-acc-title above it.
    assert 'action="/build-upload"' in html
    assert html.index("MX-acc-1") < html.index("MX-acc-2")

    # Campaign names and the creative list are no longer rendered server-side.
    # The campaign builder composes the name in the browser from the slot's own
    # fields (offer, device, targeting, age, date), so a name baked into the
    # HTML would go stale the moment someone edits a field. What the server
    # still owes this section is the accounts, in order, each with a form that
    # posts to /build-upload — which is what the assertions above check.
    assert "open_recs" not in html, "внутреннее имя структуры не должно течь в разметку"


def test_web_targeting_table_renders_per_country(
    enriched_df: pd.DataFrame,
) -> None:
    from src.analysis_agent.analyze_fb_stats import aggregate_combos
    from src.analysis_agent.morning_report import build_country_section

    combos = aggregate_combos(enriched_df)
    countries = sorted(enriched_df["Country Final"].dropna().unique().tolist())
    sections = [
        build_country_section(
            enriched_df, combos, country, [],
            my_cm="Evgeni Sedin", trend_days=7,
        )
        for country in countries
    ]
    report = {
                "date": "2026-06-08",
                "my_cm": "Evgeni Sedin",
                "source_excel": "test.xlsx",
                "trend_days": 7,
                "total_combos": len(combos),
                "countries": sections,
            }
    report["countries"] = sections
    html = _render_html(report)

    for country in countries:
        slug = _anchor_slug(country)
        assert f'id="view-country-{slug}-targeting"' in html
        block = html.split(f'id="view-country-{slug}-targeting"', 1)[1].split(
            '<h2 class="country"', 1
        )[0]
        assert 'class="table-targeting"' in block
        section = next(s for s in sections if s["country"] == country)
        for row in section.get("targeting") or []:
            assert row["interest"] in block
