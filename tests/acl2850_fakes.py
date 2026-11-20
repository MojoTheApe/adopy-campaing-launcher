"""ACL-2850: общие заглушки для тестов списков Pages / Pixels (одна сессия профиля на оба списка)."""
from __future__ import annotations

import contextlib
from types import SimpleNamespace

ACT = 'act_1234567890'


def fake_session(monkeypatch, *, opened=None, client=None, fail=None):
    """`account_spend_api._session` -> заглушка; `opened` копит метки аккаунтов (по одной на вход в профиль)."""
    from src.analysis_agent import account_spend as accounts, account_spend_api as api
    client = client or SimpleNamespace(allow_writes=False, reset_budget=lambda n: None, calls=0,
                                       get_all=lambda *a, **k: [])
    log = opened if opened is not None else []

    @contextlib.contextmanager
    def session(account):
        log.append(account)
        if fail is not None:
            raise fail
        yield SimpleNamespace(client=client, act=ACT, notes={}, account=account)

    monkeypatch.setattr(api, '_session', session)
    monkeypatch.setattr(accounts, 'serialized_browser', lambda **kw: lambda fn: fn)
    monkeypatch.setattr(api, '_failure', lambda account, exc: (str(exc) or 'Session unavailable', 'failed'))
    return log


def reader(fn):
    """Старая подпись `fn(account)` -> `_read_in_session(sess, account)`."""
    return lambda sess, account: fn(account)


def quiet(monkeypatch, kind):
    """Второй список этого теста не интересует: читается пустым (пустое не кэшируется)."""
    from src.web import account_pages, account_pixels
    mod = account_pixels if kind == 'pixels' else account_pages
    monkeypatch.setattr(mod, '_read_in_session', lambda sess, account: {'ok': True, 'state': 'empty', kind: []})
