"""ACL-2692: Publish on a held profile whose tab is on the login page keeps the hold with its reason; a later open() looks afresh."""
from types import SimpleNamespace

import pytest

from src.fb_api import account_blocks as blocks
from tests.test_acl2847_logged_out_publish import rig  # noqa: F401  (fixture)

def test_publish_with_the_tab_on_the_login_page_is_refused_with_the_reason_and_keeps_the_hold(rig, monkeypatch):
    from tests.test_acl2847_logged_out_publish import ADS_URL, LOGIN_URL, TEXT, _open, pid_of  # noqa: F401
    from src.web import publish_api

    monkeypatch.setattr(publish_api, "_resolve_entry", lambda label: SimpleNamespace(
        profile_id=pid_of(label), folder_id="F", multilogin_instance="A", ad_account_id="act_123456"))
    reached = []
    monkeypatch.setattr(publish_api.ApiPublisher, "_open_warm",
                        lambda self, label, pid: reached.append(label) or True)
    _open(rig, "A", [(LOGIN_URL, "Log in to Facebook")])
    with blocks.problem_action("publish"):
        with pytest.raises(publish_api.ApiPublishError) as err:
            publish_api.ApiPublisher().open("A")
        assert TEXT in str(err.value) and not reached and blocks.is_held("key-A")
        # The person logged in: the next open() in the same action looks afresh and probes instead of refusing.
        _open(rig, "A", [(ADS_URL, "Ads Manager")])
        publish_api.ApiPublisher().open("A")
    assert reached == ["A"] and not blocks.lock_seen_now("key-A")
