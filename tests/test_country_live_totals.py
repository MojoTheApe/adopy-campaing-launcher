"""Country headings keep selected-period account readings and their units."""
from unittest.mock import Mock

import pytest

from src.web import app, account_links as links


@pytest.fixture
def setup_map(monkeypatch, tmp_path):
    monkeypatch.setattr(links, 'get_local_config_dir', lambda: str(tmp_path))
    monkeypatch.setattr(links, 'get_ad_account_map_path', lambda: str(tmp_path/'missing.csv'))
    monkeypatch.setattr(app, 'get_ad_account_map_path', lambda: str(tmp_path/'missing.csv'))
    return lambda name: links.link(name, 'country', 'KW')


def test_sums_use_only_successful_finite_readings_and_do_not_mix_units(monkeypatch, setup_map):
    live = {
        'A': dict(state='ok', spend=10.25, currency='usd'),
        'B': dict(state='ok', spend=0, currency='USD'),
        'C': dict(state='ok', spend=7, currency='KWD'),
        'D': dict(state='failed', spend=99, currency='USD'),
        'E': dict(state='ok', spend=None, currency='USD'),
        'F': dict(state='ok', spend=2, currency=''),
        'G': dict(state='ok', spend=3, currency='?'),
        'H': dict(state='ok', spend='broken', currency='USD'),
        'I': dict(state='ok', spend=float('nan'), currency='USD'),
    }
    for name in live:
        setup_map(name)
    read = Mock(return_value=live)
    monkeypatch.setattr(app, '_live_spend', read)
    view=app._build_account_map_view({'countries':[]}, date_from='2026-09-01', date_to='2026-09-09')
    read.assert_called_once_with('2026-09-01','2026-09-09')
    country=view['countries'][0]
    assert country['live_read']==5
    assert country['live_sums']==[
        {'currency':'KWD','spend':7}, {'currency':'USD','spend':10.25},
        {'currency':'','spend':2,'account':'F'}, {'currency':'','spend':3,'account':'G'},
    ]
    html=app.templates.env.get_template('_account_map.html').render(amap=view,mlx={'A':'A','B':'B'})
    assert '5/9 read' in html
    assert 'KWD 7.00' in html and 'USD 10.25' in html
    assert 'currency unknown 2.00 (F)' in html
    assert 'currency unknown 3.00 (G)' in html
    assert 'not country-attributed spend' in html


def test_maximum_does_not_include_dated_readings(monkeypatch, setup_map):
    setup_map('A')
    read=Mock(side_effect=AssertionError('Maximum must not retrieve a dated reading'))
    monkeypatch.setattr(app,'_live_spend',read)
    view=app._build_account_map_view({'countries':[]},span='all')
    country=view['countries'][0]
    assert country['live_read']==0 and country['live_sums']==[]
    html=app.templates.env.get_template('_account_map.html').render(amap=view,mlx={'A':'A','B':'B'})
    assert '0/1 read' in html
    assert '<b>—</b>' in html
    read.assert_not_called()
