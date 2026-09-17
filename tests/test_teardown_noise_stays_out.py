# -*- coding: utf-8 -*-
"""Exception types alone cannot establish harmless browser teardown.

Keep asynchronous connection failures visible unless cleanup is positively
identified where it happens. Handled logger events can still be real failures.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import acl_sentry  # noqa: E402


def _event(logger: str, *types: str) -> dict:
    return {
        "logger": logger,
        "exception": {"values": [{"type": t, "value": t} for t in types]},
    }


@pytest.mark.parametrize("kind", [
    "CancelledError",
    "ConnectionResetError",
    "TargetClosedError",
    "BrokenPipeError",
])
def test_known_types_without_teardown_evidence_are_preserved(kind):
    assert acl_sentry._before_send(_event("asyncio", kind), {}) is not None


def test_the_one_that_killed_the_dashboard_still_gets_through():
    """`loop is not the running loop` приходит тем же логгером, и это ACL-1163.

    Если однажды его тоже отфильтруют «за компанию», этот тест скажет об этом
    раньше, чем мы перестанем замечать падения дешборда.
    """
    event = _event("asyncio", "RuntimeError")
    assert acl_sentry._before_send(event, {}) is not None


def test_the_same_exception_from_our_own_code_is_kept():
    """Фильтр — про шум закрытия, а не про тип исключения вообще.

    `ConnectionResetError` из нашего кода (упал запрос к Ads Manager) — это
    настоящая поломка, и молчать о ней нельзя.
    """
    event = _event("src.fb_ads_rpa.browser", "ConnectionResetError")
    assert acl_sentry._before_send(event, {}) is not None


def test_a_mixed_chain_is_kept():
    """Цепочка, где рядом с шумом лежит что-то своё, уезжает целиком:
    выбрасывать её значило бы терять настоящую причину."""
    event = _event("asyncio", "CancelledError", "ValueError")
    assert acl_sentry._before_send(event, {}) is not None


def test_step_failures_are_untouched():
    """Отказы шагов приходят без исключения вовсе — их фильтр не касается."""
    event = {"message": "placements: still-loading", "logger": "acl"}
    assert acl_sentry._before_send(event, {}) is not None


def test_the_manual_report_is_untouched():
    event = {"message": "Report: кампания встала", "logger": None}
    assert acl_sentry._before_send(event, {}) is not None


def test_unhandled_connection_loss_during_upload_is_preserved():
    event = _event('asyncio', 'ConnectionResetError')
    event['exception']['values'][0]['mechanism'] = {'handled': False}
    assert acl_sentry._before_send(event, {}) is not None
