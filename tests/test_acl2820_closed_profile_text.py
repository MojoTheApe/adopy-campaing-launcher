"""ACL-2820: a profile closed during an action says "closed", not "busy or locked"; real MLX locks keep their text."""
from __future__ import annotations

import pytest

from src.fb_api import plain_errors as pe
from src.fb_api import warm
from src.web import publish_api

CLOSED_LINE = "The profile was closed - press Open (or Retry) to open it again"
LOCK_LINE = "The Multilogin profile is busy or locked"


@pytest.mark.parametrize("exc", [
    warm.WarmError("This profile was closed or restarted while waiting; the next attempt opens it again.",
                   "profile_closed"),
    warm.WarmError("The Doc120 profile closed while the action was starting; the next attempt opens it again.",
                   "profile_closed"),
    publish_api.ApiPublishError("profile closed", code="profile_closed"),
])
def test_closed_profile_gets_its_own_text(exc):
    f = pe.classify(exc)
    assert f.key == "profile_closed"
    assert f.line() == CLOSED_LINE
    assert "busy" not in f.text() and "locked" not in f.text()


def test_real_mlx_lock_keeps_lock_text():
    exc = warm.WarmError("Multilogin still holds the Doc120 profile after it closed; nothing was started. "
                         "Try again in a minute.", "profile_locked")
    f = pe.classify(exc)
    assert f.key == "profile_locked"
    assert f.short == LOCK_LINE
    assert LOCK_LINE in f.line()


def test_lock_code_from_publish_api_keeps_lock_text():
    f = pe.classify(publish_api.ApiPublishError("busy", code="profile_locked"))
    assert f.key == "profile_locked" and f.short == LOCK_LINE


def test_the_two_codes_never_share_a_key():
    assert pe._API_CODE_KEYS["profile_closed"] == "profile_closed"
    assert pe._API_CODE_KEYS["profile_locked"] == "profile_locked"
    assert pe.TABLE["profile_closed"].short != pe.TABLE["profile_locked"].short
