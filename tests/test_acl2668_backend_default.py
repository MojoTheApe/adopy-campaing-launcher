"""ACL-2668, решение 38: бэкенд по умолчанию — `api`, `rpa` только явно, решается в одном месте."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.fb_api import backend as b

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("value,expected", [
    (None, "api"), ("", "api"), ("  ", "api"), ("api", "api"), (" API ", "api"),
    ("graph", "api"), ("1", "api"), ("rpa", "rpa"), (" RPA ", "rpa"), ("Rpa", "rpa"),
])
def test_default_is_api_and_rpa_only_when_set_explicitly(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv(b.BACKEND_ENV, raising=False)
    else:
        monkeypatch.setenv(b.BACKEND_ENV, value)
    assert b.backend() == expected
    assert b.api_enabled() is (expected == "api")


@pytest.mark.parametrize("value", [None, "rpa", "bogus"])
def test_every_module_follows_the_one_helper(monkeypatch, value):
    from src.analysis_agent import account_spend_api, account_spend
    from src.web import campaign_commands, publish_api, publish_jobs

    if value is None:
        monkeypatch.delenv(b.BACKEND_ENV, raising=False)
    else:
        monkeypatch.setenv(b.BACKEND_ENV, value)
    want = b.backend()
    assert account_spend_api.backend() == want == publish_api.backend() == publish_jobs._fb_backend()
    assert campaign_commands.api_enabled() is account_spend_api.api_enabled() is (want == "api")
    assert account_spend.hand_message() == (account_spend.HAND_OPEN_MESSAGE_API if want == "api"
                                            else account_spend.HAND_OPEN_MESSAGE)


def test_no_other_python_module_reads_the_backend_env_itself():
    """Значение по умолчанию живёт только в `src/fb_api/backend.py` — чтобы не разошлось снова."""
    pattern = re.compile(r"""environ(?:\.get)?[(\[]\s*(?:["']ACL_FB_BACKEND["']|BACKEND_ENV)""")
    offenders = [str(p.relative_to(ROOT)) for p in (ROOT / "src").rglob("*.py")
                 if p.name != "backend.py" and pattern.search(p.read_text(encoding="utf-8"))]
    assert offenders == []


def test_page_falls_back_to_api_when_the_flag_is_missing():
    text = (ROOT / "src" / "web" / "templates" / "report.html").read_text(encoding="utf-8")
    assert "window.ACL_FB_BACKEND = {{ (fb_backend or 'api') | tojson }}" in text
