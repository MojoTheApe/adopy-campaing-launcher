"""Настоящий GraphClient + очередь/шаблон/JS, только внешний транспорт синтетический."""
from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.analysis_agent import campaign_inventory as ci, account_spend_api as api
from src.fb_api.graph import GraphClient
from tests.test_acl2063_dashboard_browser import dashboard, goto, wait_ready  # noqa: F401
from tests.test_acl2063_partial_read_failures import Transport


@pytest.mark.parametrize('width', [1280, 390])
@pytest.mark.parametrize('mode,edge', [('network', 'adsets'), ('exception', 'ads')])
def test_native_mid_read_failure_shows_one_retry_and_continues_sweep(dashboard, monkeypatch, width, mode, edge):
    site, page = dashboard
    page.set_viewport_size({'width': width, 'height': 900})
    transports = []

    @contextmanager
    def session(account):
        transport = Transport(mode if account == 'Acc' else 'ok', edge)
        transports.append(transport)
        yield SimpleNamespace(client=GraphClient(transport, min_interval_s=0), act='act_1234567', notes={})

    monkeypatch.setattr(api, '_session', session)
    monkeypatch.setattr(api, '_telemetry', lambda *a, **kw: None)

    def read(account, trigger):
        site.reads.append((account, trigger))
        snap = ci.read_via_api(account, structure_only=True, trigger=trigger)
        snap.finished_at = datetime.fromtimestamp(site.now).strftime('%Y-%m-%d %H:%M:%S')
        snap.finished_at_utc = datetime.fromtimestamp(site.now, timezone.utc).isoformat()
        ci.put_result(snap)
        return snap

    site.q._reader = read
    site.save('Acc', age=7200); site.save('Fresh', age=7200)
    goto(page, 'http://acl.test/'); wait_ready(page)
    page.wait_for_function("document.querySelector('.amap-campaigns')._hc.state.refreshRows.Fresh?.state === 'ok' && document.querySelector('.amap-campaigns')._hc.state.refreshRows.Acc.state === 'failed'")
    assert [account for account, _ in site.reads] == ['Acc', 'Fresh']
    retry = page.locator('tr:has(td[data-account="Acc"]) button.hc-rc')
    assert retry.is_visible() and retry.inner_text() == 'Retry'
    assert page.locator('button.hc-rc:visible').count() == 1
    assert 'failed' in page.locator('tr:has(td[data-account="Acc"]) .hc-snap').inner_text()
    assert ci.get_snapshot('Acc').campaigns and ci.get_snapshot('Acc').state == 'partial'
    assert site.q.state()['halt'] is None and site.errors == []
    assert all(not any('insights' in path for path in t.requests) for t in transports)
