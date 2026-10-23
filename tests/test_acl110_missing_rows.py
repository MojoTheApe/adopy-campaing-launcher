"""Real full-report regressions for one-period-only rows (ACL-110)."""
from __future__ import annotations

from copy import deepcopy
from datetime import date, timedelta
from unittest.mock import MagicMock

import pytest

from src.web import app as web
from src.web import period_compare as pc
from tests.test_web_build_upload import _minimal_report

INFO = {"mode": "prev", "date_from": "2026-10-01", "date_to": "2026-10-07"}
ZERO = {"spend": 0, "leads": 0, "deposits": 0}


def metrics(value=10, **fields):
    return {"spend": value, "leads": 2, "deposits": 1, "aggregate_key": {}, **fields}


def fixtures():
    report = _minimal_report()
    c = report["countries"][0]
    c.update(country="Costa Rica", open_recs={}, close_recs={}, cm_comparison=[metrics(cm="Ann", is_me=True, net=0, roi=0)],
             top_combos=[metrics(20, offer="Current offer", device="mobile", age="25", interest="Yoga", creative="Creative A", category="Strong")])
    prev_country = {"country": "Costa Rica", "cm_comparison": [metrics(cm="Ann"), metrics(cm="Bob")],
                    "close_recs": {"Earlier account": [metrics(adset_name="Earlier ad set")]},
                    "open_recs": {"Earlier account": [{"strategy_type": 1, "targeting": "Earlier targeting"}]},
                    "top_combos": [metrics(10, offer="Current offer", device="mobile", age="25", interest="Yoga", creative="Creative A", category="Strong")]}
    vanished = {"country": "Mexico", "my_totals": metrics(50), "cm_comparison": [metrics(50, cm="Earlier manager")],
                "account_rows": [], "open_recs": {}, "close_recs": {}}
    amap = {"countries": [{"country": "Costa Rica", "key": "cr", "book": metrics(20), "rows": [], "virtual": True}],
            "cm_section": [{"country": "Costa Rica", **metrics(20), "rows": [metrics(20, cm="Ann", is_me=True)]}],
            "totals": metrics(20), "all_accounts": [], "available": [], "live": {}, "fallback": {}, "splits": {},
            "date_from": "2026-10-08", "date_to": "2026-10-14", "dates": [(date(2026, 9, 1) + timedelta(days=n)).isoformat() for n in range(44)], "span": ""}
    prev_map = {"countries": [{"country": "Costa Rica", "book": metrics(), "rows": []},
                              {"country": "Mexico", "book": metrics(50), "rows": []}],
                "totals": metrics(60), "cm_section": [
                    {"country": "Costa Rica", **metrics(), "rows": [metrics(cm="Ann"), metrics(cm="Bob")]},
                    {"country": "Mexico", **metrics(50), "rows": [metrics(50, cm="Earlier manager")]}]}
    return report, amap, pc.PeriodCompare(INFO, {"countries": [prev_country, vanished]}, prev_map)


def render(report, amap, compare):
    return web.templates.env.get_template("report.html").render(
        request=MagicMock(), report=report, mlx_labels={}, warnings=[], built_at="2026-10-23", upload_errors={},
        account_map=amap, account_preflight={}, account_preflight_json={}, ref_options={}, ref_options_json={},
        account_alias_map_json={}, draft_status={}, period={"date_from": "2026-10-08", "date_to": "2026-10-14", "span": ""},
        compare=compare, compare_note="", compare_pick={'mode': compare.mode, 'date_from': compare.date_from, 'date_to': compare.date_to} if compare else {}, home_kpis=web._home_kpis(report))


def test_real_zero_row_is_present_but_explicit_no_data_is_absent():
    prev = {"countries": [{"country": "Zero land", "cm_comparison": [{"cm": "Zero", **ZERO}]}]}
    compare = pc.PeriodCompare(INFO, prev, {"countries": [], "cm_section": []})
    assert compare.cm("Zero land", "Zero") == {"cm": "Zero", **ZERO}
    assert pc.state_for(ZERO, metrics()) == "both"
    assert pc.state_for({**ZERO, "has_data": False}, metrics()) == "gone"


def test_home_renders_earlier_only_countries_and_managers_without_mutation():
    report, amap, compare = fixtures()
    before = deepcopy(amap)
    html = web.templates.env.get_template("_account_map.html").render(amap=amap, compare=compare, mlx={})
    assert "Mexico" in html
    assert "Bob" in html
    assert "Earlier manager" in html
    assert 'cmp-flag gone' in html
    assert amap == before


def test_full_report_keeps_whole_earlier_only_recommendation_account_group():
    report, amap, compare = fixtures()
    html = render(report, amap, compare)
    assert "Earlier account" in html
    assert "Earlier ad set" in html
    assert "Earlier targeting" in html


def test_current_breakdown_numbers_also_get_the_earlier_counterpart():
    report, amap, compare = fixtures()
    html = render(report, amap, compare)
    combo = html.split('id="view-country-costa-rica-combos"', 1)[1].split('</table>', 1)[0]
    assert 'cmp-prev' in combo and '+100%' in combo


def workbench_fixture():
    from src.analysis_agent import country_workbench as cw
    from tests.test_acl1799_country_workbench import fixture_df

    report, amap, _ = fixtures()
    report['my_cm'] = 'Eva'
    prior_frame = fixture_df().assign(**{'Country Final': 'Costa Rica'})
    current_frame = prior_frame[prior_frame['Offer'] == 'Alpha'].copy()
    for key in ('$ Spend', '# Leads', '# FTDs'):
        current_frame[key] *= 2
    current = cw.build_country_workbench(current_frame, 'Costa Rica', 'Eva')
    before = cw.build_country_workbench(prior_frame, 'Costa Rica', 'Eva')
    report['countries'][0]['workbench'] = current
    prior_country = {**deepcopy(report['countries'][0]), 'workbench': before}
    return report, amap, pc.PeriodCompare(INFO, {'countries': [prior_country]}, {}), before


def test_home_and_workbench_use_the_same_period_formulas_and_do_not_mutate_cached_data():
    report, amap, compare, before = workbench_fixture()
    original = deepcopy(report)
    now = report['countries'][0]['workbench']
    model = compare.workbench('costa rica', now)
    for side in ('you', 'others'):
        assert model['slices']['Alpha']['_period_compare'][side][0]['delta'] == '+100%'
        assert compare.home_tiles(report)['spend'][side]['prev'].startswith('$')
    for tab in ('campaigns', 'setups', 'landings', 'targets', 'creatives', 'buyers'):
        assert model['slices']['Alpha'][tab][0]['_period'][0]['delta'] == '+100%'
        assert len(model['slices']['Alpha'][tab]) == len(now['slices']['Alpha'][tab])
    assert model['_period_assets_gone'][0]['name'] == 'Beta'
    assert 'Beta' in model['_period_earlier_slices']
    assert report == original
    assert before['slices']['Alpha']['campaigns'][0].get('_period') is None


def test_full_page_renders_actual_home_comparison_and_safe_workbench_payload():
    import json
    import re

    report, amap, compare, _ = workbench_fixture()
    html = render(report, amap, compare)
    home = html.split('id="home-kpis"', 1)[1].split('</section>', 1)[0]
    assert 'cmp-prev' in home and '$650' in home
    payload = json.loads(re.search(r'id="cw-data-costa-rica">(.*?)</script>', html, re.S)[1])
    assert payload['slices']['Alpha']['campaigns'][0]['_period'][0]['delta'] == '+100%'
    assert payload['_period_assets_gone'][0]['name'] == 'Beta'


def test_normalized_breakdown_keys_include_all_identity_dimensions_not_rank_or_date():
    report, amap, _ = fixtures()
    current = report['countries'][0]['top_combos'][0]
    prior = {**current, 'spend': 10, 'category': 'Interesting'}
    different = {**prior, 'age': '65', 'spend': 999}
    compare = pc.PeriodCompare(INFO, {'countries': [{'country': 'Costa Rica', 'top_combos': [different, prior]}]}, {})
    assert compare.breakdown('COSTA  RICA', 'top_combos', current)['spend'] == 10
    assert compare.breakdown_gone('Costa Rica', 'top_combos', [current]) == [different]


def test_whole_earlier_country_contains_readonly_evidence_and_escaped_names():
    import re
    from html import unescape

    report, amap, compare = fixtures()
    compare._countries['mexico']['open_recs'] = {'Retired <account>': [{'strategy_type': 1, 'targeting': '<img src=x onerror=alert(1)>'}]}
    html = web.templates.env.get_template('_account_map.html').render(amap=amap, compare=compare, mlx={})
    ghost = re.search(r'<details class="cmp-only-country">(.*?)</details>', html, re.S)[1]
    assert 'Retired <account>' in unescape(re.sub(r'<[^>]+>', '', ghost))
    assert not re.search(r'<(?:form|button|input|select|img)\b|(?:onclick|hx-get)=', ghost)
    assert '<img src=x' not in ghost


@pytest.mark.parametrize('mode, earlier', [('prev', ('2026-10-01', '2026-10-07')), ('custom', ('2026-09-01', '2026-09-07'))])
def test_account_map_fragment_keeps_both_periods_and_does_not_mutate_cached_report(monkeypatch, mode, earlier):
    from fastapi.testclient import TestClient

    report, amap, compare = fixtures()
    cached = {'countries': [], 'marker': 'unfiltered'}
    original = deepcopy(cached)
    seen = []
    monkeypatch.setattr(web, 'get_cached_report', lambda: (cached, None, []))
    monkeypatch.setattr(web, 'mlx_instance_labels', lambda: {})
    monkeypatch.setattr(web, '_enrich_country_overview', lambda r: r.update(enriched=True))

    def period(base, df, start, end):
        seen.append((start, end))
        return deepcopy(report if start == '2026-10-08' else compare._report)

    def view(r, df, start, end, span=''):
        assert r.get('marker') != 'unfiltered', 'fragment used the unfiltered workbook'
        if start != '2026-10-08':
            return {'countries': [{'country': 'Costa Rica', 'book': metrics(), 'rows': []},
                                  {'country': 'Mexico', 'book': metrics(50), 'rows': []}],
                    'cm_section': [], 'totals': metrics(60)}
        return deepcopy(amap)

    monkeypatch.setattr(web, 'report_for_period', period)
    monkeypatch.setattr(web, '_build_account_map_view', view)
    response = TestClient(web.app, base_url='http://127.0.0.1').get('/account-map', params={
        'date_from': '2026-10-08', 'date_to': '2026-10-14', 'compare': mode, 'cmp_from': earlier[0], 'cmp_to': earlier[1]})
    assert response.status_code == 200
    assert 'cmp-prev' in response.text and 'Mexico' in response.text
    assert ('2026-10-08', '2026-10-14') in seen and earlier in seen
    assert cached == original


def test_missing_metric_is_not_silently_presented_as_recorded_zero():
    cells = pc.metric_compare(metrics(), {'spend': None, 'leads': 2, 'deposits': 1})
    assert cells[0]['prev'] == '—' and cells[0]['delta'] == '—'
    assert cells[3]['prev'] == '—' and cells[4]['prev'] == '—'
    zero = pc.metric_compare(metrics(), {'spend': 0, 'leads': 2, 'deposits': 1})
    assert zero[0]['prev'] == '$0' and zero[0]['delta'] == '—'


def test_earlier_only_creative_parent_retains_all_of_its_variant_rows():
    prior = {'country': 'Costa Rica', 'creatives': [metrics(20, creative_id='42', text_name='text', variants=[metrics(20, variant='3', label='42 · 3')])]}
    compare = pc.PeriodCompare(INFO, {'countries': [prior]}, {})
    tables = compare.earlier_only_tables({'country': 'Costa Rica', 'creatives': []})
    assert any(r['label'] == '42 · 3' for table in tables for r in table['rows'])


def test_earlier_only_workbench_pack_retains_its_exact_versions_without_current_use_rows():
    report, amap, compare, before = workbench_fixture()
    current = deepcopy(report['countries'][0]['workbench'])
    current['slices']['Alpha']['creatives'] = []
    model = compare.workbench('Costa Rica', current)
    gone = model['slices']['Alpha']['_period_gone']['creatives']
    assert any(r.get('key') and r['_period'][0]['prev'].startswith('$') for r in gone)
    assert current['slices']['Alpha']['creatives'] == []


def test_new_exact_version_under_an_existing_pack_is_marked_new_without_mutation():
    report, _, compare, _ = workbench_fixture()
    current = deepcopy(report['countries'][0]['workbench'])
    pack = current['slices']['Alpha']['creatives'][0]
    added = {**pack['versions'][0], 'key': 'new-exact-version', 'version': '99', 'raw': '42_99'}
    pack['versions'].append(added)
    original = deepcopy(current)
    result = compare.workbench('Costa Rica', current)['slices']['Alpha']['creatives'][0]
    assert result['_period_state'] == 'both'
    assert result['versions'][-1].get('_period_state') == 'new'
    assert result['versions'][0].get('_period_state') == 'both'
    assert current == original


def test_static_new_creative_variant_under_retained_parent_has_a_new_badge():
    import re

    report, amap, _ = fixtures()
    old = metrics(10, variant='1', label='Retained version')
    new = metrics(10, variant='2', label='New exact static version')
    parent = metrics(20, creative_id='42', ad_title='42', text_name='Text', has_variants=True, variants=[old, new])
    report['countries'][0]['creatives'] = [parent]
    compare = pc.PeriodCompare(INFO, {'countries': [{'country': 'Costa Rica', 'creatives': [{**parent, 'variants': [old]}]}]})
    html = render(report, amap, compare)
    row = re.search(r'<tr class="creative-variant"[^>]*>\s*<td[^>]*>New exact static version(.*?)</tr>', html, re.S)[1]
    assert 'cmp-flag new' in row


def test_home_new_campaign_manager_country_group_has_a_new_badge():
    import re

    report, amap, compare = fixtures()
    amap['cm_section'].append({'country': 'New country', **metrics(0), 'rows': [metrics(0, cm='Zero manager')]})
    html = web.templates.env.get_template('_account_map.html').render(amap=amap, compare=compare, mlx={})
    group = re.search(r'data-country="__cm__:New country">\s*<summary(.*?)</summary>', html, re.S)[1]
    assert 'cmp-flag new' in group


@pytest.mark.parametrize('all_rows', [False, True], ids=['workbench-disappeared', 'whole-country-disappeared'])
def test_absent_whole_workbench_retains_exact_versions_as_readonly_evidence(all_rows):
    _, _, compare, before = workbench_fixture()
    expected = {v['raw'] for r in before['slices']['Alpha']['creatives'] for v in r['versions']}
    tables = compare.earlier_workbench_tables({'country': 'Costa Rica'}, all_rows=all_rows)
    labels = {r['label'] for table in tables for r in table['rows']}
    assert expected and expected.issubset(labels)


def test_earlier_only_whole_country_retains_campaign_manager_net_and_roi():
    prior = {'country': 'Retired country', 'cm_comparison': [metrics(10, cm='Prior manager', net=234, roi=1.25)]}
    compare = pc.PeriodCompare(INFO, {'countries': [prior]})
    html = web.templates.env.get_template('_period_compare_gone_country.html').render(earlier_country=prior, compare=compare)
    assert '<th>Net</th>' in html and '<th>ROI</th>' in html
    assert '$234' in html and '>1.25</span>' in html
    assert 'button' not in html and 'hx-get' not in html
