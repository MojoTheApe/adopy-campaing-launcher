"""Preference changes must persist, including competing dashboard processes."""
import asyncio
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from starlette.requests import Request
from src.web import hidden_accounts as ha
from src.web.app import api_accounts_hidden


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv('ACL_CONFIG_DIR', str(tmp_path))


def request(body):
    async def receive():
        return {'type': 'http.request', 'body': json.dumps(body).encode()}
    return Request({'type': 'http', 'method': 'POST', 'path': '/api/accounts-hidden', 'headers': []}, receive)


@pytest.mark.parametrize('body', [[], 'label', 1, None, {'label': ['A']},
                                      {'label': 'A', 'hidden': 'false'}, {'label': 'A', 'hidden': 0}])
def test_invalid_payload_does_not_change_preferences(body):
    response = asyncio.run(api_accounts_hidden(request(body)))
    assert response.status_code == 400
    assert ha.load() == set()


def test_failed_persistence_is_not_reported_as_success(monkeypatch):
    ha.set_hidden('existing', True)
    def fail(*args):
        raise OSError('read-only disk')
    monkeypatch.setattr(ha, '_save', fail)
    response = asyncio.run(api_accounts_hidden(request({'label': 'new', 'hidden': True})))
    assert response.status_code == 500
    assert json.loads(response.body)['ok'] is False
    assert ha.load() == {'existing'}


def test_hide_and_unhide_api_survive_reload():
    for hidden in (True, False):
        response = asyncio.run(api_accounts_hidden(request({'label': 'A', 'hidden': hidden})))
        assert response.status_code == 200
        assert ha.is_hidden('A') is hidden


def test_threads_preserve_each_update():
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda n: ha.set_hidden(str(n), True), range(40)))
    assert ha.load() == {str(n) for n in range(40)}
