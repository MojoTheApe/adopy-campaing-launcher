"""ACL-2785: Graph API — бэкенд по умолчанию; RPA только при явном ACL_FB_BACKEND=rpa."""
from __future__ import annotations

import pytest

from src.analysis_agent import account_spend, account_spend_api, campaign_inventory
from src.fb_api import backend as shared
from src.web import app, campaign_commands, publish_api, publish_jobs, warm_api

CASES = [(None, "api"), ("", "api"), ("  ", "api"), ("rpa", "rpa"), ("RPA", "rpa"),
         ("api", "api"), ("API", "api"), ("bogus", "api")]


def _set(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("ACL_FB_BACKEND", raising=False)
    else:
        monkeypatch.setenv("ACL_FB_BACKEND", value)


@pytest.mark.parametrize("value,expected", CASES)
def test_every_reader_agrees(monkeypatch, value, expected):
    _set(monkeypatch, value)
    is_api = expected == "api"
    assert shared.backend() == expected
    assert publish_api.backend() == expected and publish_api.api_enabled() is is_api
    assert account_spend_api.backend() == expected and account_spend_api.api_enabled() is is_api
    assert campaign_commands.api_enabled() is is_api
    assert warm_api.api_enabled() is is_api
    assert campaign_inventory.backend_ready() is is_api
    assert publish_jobs._fb_backend() == expected
    assert app._fb_backend() == expected
    assert (account_spend.hand_message() == account_spend.HAND_OPEN_MESSAGE_API) is is_api


def test_template_default_is_api():
    text = open("src/web/templates/report.html", encoding="utf-8").read()
    assert "window.ACL_FB_BACKEND = {{ (fb_backend or 'api') | tojson }}" in text
