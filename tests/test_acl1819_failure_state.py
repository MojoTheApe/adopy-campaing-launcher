"""The failure message stays lazy, bounded and restricted to synthetic UI state."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_acl1819_preflight_browser import _audit_failure_state


def test_failure_snapshot_is_lazy_bounded_and_allowlisted():
    class Page:
        calls = 0

        def evaluate(self, _script):
            self.calls += 1
            return {"currentCampaignName": "synthetic-name" * 1000,
                    "cachedAuditNames": ["synthetic-cache" * 1000] * 100,
                    "auditPresent": True, "currentNameHit": False, "revisionMatches": None,
                    "metaText": "synthetic-meta" * 1000,
                    "token": "DO-NOT-EXPORT-CREDENTIAL", "revision": "DO-NOT-EXPORT-REVISION"}

    page = Page()
    assert "Check again" == "Check again", _audit_failure_state(page, "Check again")
    assert page.calls == 0
    with pytest.raises(AssertionError) as failure:
        assert "unexpected" == "Check again", _audit_failure_state(page, "synthetic-observed" * 1000)
    assert page.calls == 1
    message = str(failure.value)
    assert len(message) < 2500 and "DO-NOT-EXPORT" not in message
    state = _audit_failure_state(page, "observed")
    assert len(json.dumps(state)) < 2500 and len(state["cachedAuditNames"]) == 6
    assert len(state["currentCampaignName"]) == 160 and len(state["metaText"]) == 240
    assert state["auditPresent"] is True and state["currentNameHit"] is False
    assert state["revisionMatches"] is None

    class BrokenPage:
        def evaluate(self, _script):
            raise RuntimeError("DO-NOT-EXPORT-CREDENTIAL")

    assert _audit_failure_state(BrokenPage(), "unused") == {"diagnosticError": "RuntimeError"}
