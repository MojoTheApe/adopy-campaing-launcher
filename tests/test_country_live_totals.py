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
        'J': dict(state='ok', spend=1044.82, currency='$'),
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
    assert country['live_read']==6
    assert country['live_sums']==[
        {'currency':'KWD','spend':7}, {'currency':'USD','spend':10.25},
        {'currency':'','spend':2,'account':'F'}, {'currency':'?','spend':3,'account':'G'},
        {'currency':'$','spend':1044.82,'account':'J'},
    ]
    html=app.templates.env.get_template('_account_map.html').render(amap=view,mlx={'A':'A','B':'B'})
    assert '6/10 spend read' in html
    assert 'KWD 7.00' in html and 'USD 10.25' in html
    # ACL-1258: в сводке страны без «currency unknown» и без имени кабинета;
    # суммы без кода стоят отдельно, знак Meta ($) сохраняется.
    summary = html.split('amap-live-summary-values', 1)[1].split('</span>', 1)[0]
    assert '<b>2.00</b>' in summary and '<b>3.00</b>' in summary
    assert '<b>$1,044.82</b>' in summary
    assert 'currency unknown' not in summary
    assert '(F)' not in summary and '(G)' not in summary and '(J)' not in summary
    assert 'not country-attributed spend' in html


def test_maximum_does_not_include_dated_readings(monkeypatch, setup_map):
    setup_map('A')
    read=Mock(side_effect=AssertionError('Maximum must not retrieve a dated reading'))
    monkeypatch.setattr(app,'_live_spend',read)
    view=app._build_account_map_view({'countries':[]},span='all')
    country=view['countries'][0]
    assert country['live_read']==0 and country['live_sums']==[]
    html=app.templates.env.get_template('_account_map.html').render(amap=view,mlx={'A':'A','B':'B'})
    assert '0/1 spend read' in html
    assert '<b>—</b>' in html
    read.assert_not_called()


def test_the_summary_twin_in_the_page_matches_the_server(tmp_path):
    """После Read сводку перерисовывает JS — он обязан писать то же, что сервер."""
    import shutil
    import subprocess
    from pathlib import Path

    from src.analysis_agent.money_format import fmt_summary_amount

    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    page = (Path(__file__).resolve().parents[1] / "src/web/templates/report.html").read_text(encoding="utf-8")
    script = page[page.index("function amapMoney("):page.index("function amapUpdateCountrySummaries(")]
    cases = [(2, ""), (3, "?"), (1044.82, "$"), (7, "KWD"), (10.25, "usd")]
    src = tmp_path / "twin.cjs"
    src.write_text(script + "\nconsole.log(JSON.stringify(" + str([list(c) for c in cases])
                   + ".map(c => amapSummaryMoney(c[0], c[1]))));\n", encoding="utf-8")
    out = subprocess.run([node, str(src)], capture_output=True, text=True, encoding="utf-8", check=True)
    import json
    assert json.loads(out.stdout) == [fmt_summary_amount(v, c) for v, c in cases]
    assert json.loads(out.stdout) == ["2.00", "3.00", "$1,044.82", "KWD 7.00", "USD 10.25"]


def test_the_read_labels_say_spend_everywhere():
    """ACL-1266 (решение владельца): кнопки читают спенд и называют это словом —
    «Read spend», не «Connect». Счётчик на сервере и на странице — одними словами."""
    from pathlib import Path

    tpl = Path(__file__).resolve().parents[1] / "src/web/templates"
    amap = (tpl / "_account_map.html").read_text(encoding="utf-8")
    country = (tpl / "_account_map_country.html").read_text(encoding="utf-8")
    page = (tpl / "report.html").read_text(encoding="utf-8")
    assert ">Read all spend</button>" in amap and "}} spend read</i>" in amap
    assert ">Read all spend in {{ mc.country }}</button>" in country
    assert ">Read spend</button>" in country
    assert ">reading…</button>" in country, "сервер при перезагрузке во время чтения"
    assert "by the Read spend button" in country
    assert "' spend read'" in page and "'Read spend'" in page
    # Занятость — короткое «reading…» и на сервере, и на странице (кнопка при
    # нажатии, при опросе; третье — загрузка журнала): длинная подпись не
    # влезала в колонку кнопок на 1280–1366px.
    assert page.count("'reading…'") == 3 and "'reading spend…'" not in page
    for stale in (">Read</button>", ">Read all</button>", "' read';", "'Read';", "'Reading ' +", "by the Read button"):
        assert stale not in amap + country + page, stale
