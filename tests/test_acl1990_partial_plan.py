"""ACL-1990: поздний сбой не стирает готовые вердикты и не открывает Publish."""
import json
import subprocess
from contextlib import contextmanager
from pathlib import Path

import pytest

from src.analysis_agent import account_spend_api as session_api
from src.web import publish_api as api
from tests.test_acl1761_api_create import _draft
from tests.test_acl1761_plan_check import _read_session
from tests.test_acl1819_preflight import AuditMeta, _node, _vertical

ROOT = Path(__file__).resolve().parents[1]


def _page_gate(tmp_path, reply, names):
    payload = tmp_path / "reply.json"
    payload.write_text(json.dumps({"reply": reply, "names": names}), encoding="utf-8")
    run = subprocess.run([_node(), str(ROOT / "tests/preflight_gate_harness.cjs"),
                          str(ROOT / "src/web/templates/report.html"), "--reply", str(payload)],
                         capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


@pytest.mark.parametrize("failure", ["second", "teardown"])
@pytest.mark.parametrize("first_verdict", ["plan_error", "audit_error", "ok"])
def test_late_failure_preserves_completed_result_and_blocks_unfinished(tmp_path, monkeypatch, failure, first_verdict):
    first = _draft(tmp_path, ads=1)
    first.name = "first"
    _vertical(first)
    if first_verdict == "plan_error":
        first.link = ""
    elif first_verdict == "audit_error":
        first.page_id = "999"
    drafts = [first]
    if failure == "second":
        other = _draft(tmp_path, ads=1)
        other.name = "second"
        drafts.append(other)
    meta = AuditMeta()
    clients = _read_session(monkeypatch, meta)
    monkeypatch.setattr(api, "build_drafts", lambda *a, **kw: drafts)
    original = api.ApiPublisher.publish_draft
    completed = []

    def publish(self, draft):
        if draft.name == "second":
            raise TimeoutError("Synthetic later campaign read failed")
        result = original(self, draft)
        completed.append(result)
        return result

    monkeypatch.setattr(api.ApiPublisher, "publish_draft", publish)
    if failure == "teardown":
        original_session = session_api._session

        @contextmanager
        def session(account):
            with original_session(account) as current:
                yield current
            raise TimeoutError("Synthetic session teardown failed")

        monkeypatch.setattr(session_api, "_session", session)
    out = api.check_plan([{"name": d.name} for d in drafts], account_label="Test", country="MX")
    assert out["results"] == completed and len(completed) == 1
    assert out["error"] and out["error_kind"] == "plan"
    assert bool(completed[0]["error"]) == (first_verdict != "ok")
    assert meta.posts == [] and all(c.writes == 0 for c in clients)
    shown = _page_gate(tmp_path, out, [d.name for d in drafts])
    expected_block = first_verdict != "ok" or failure == "second"
    assert shown["proceeded"] == (0 if expected_block else 1)
    if failure == "second":
        assert any(row["status"] == "error" for row in shown["lines"][1])


def test_before_any_check_keeps_retry_nonblocking_but_known_invalid_draft_blocks(tmp_path, monkeypatch):
    drafts = [_draft(tmp_path, ads=1), _draft(tmp_path, ads=1)]
    drafts[0].name, drafts[1].name = "valid", "invalid"
    drafts[1].errors = ["Synthetic invalid local draft"]
    monkeypatch.setattr(api, "build_drafts", lambda *a, **kw: drafts)

    @contextmanager
    def session(account):
        raise TimeoutError("Synthetic pre-check session failure")
        yield

    _read_session(monkeypatch, AuditMeta())
    monkeypatch.setattr(session_api, "_session", session)
    out = api.check_plan([{"name": d.name} for d in drafts], account_label="Test", country="MX")
    assert out["error_kind"] == "not_checked"
    assert len(out["results"]) == 1 and out["results"][0]["campaign_name"] == "invalid"
    assert out["results"][0]["error"] == "Synthetic invalid local draft"
    assert _page_gate(tmp_path, out, ["valid"])["proceeded"] == 1
    assert _page_gate(tmp_path, out, ["invalid"])["proceeded"] == 0


@pytest.mark.parametrize("violation", ["capability", "write"])
def test_read_contract_violation_discards_untrusted_results_and_blocks_publish(tmp_path, monkeypatch, violation):
    draft = _draft(tmp_path, ads=1)
    draft.name = "first"
    _vertical(draft)
    monkeypatch.setattr(api, "build_drafts", lambda *a, **kw: [draft])
    meta = AuditMeta()
    _read_session(monkeypatch, meta, writes=violation == "capability")
    original = api.ApiPublisher.publish_draft

    def publish(self, value):
        result = original(self, value)
        self._client.writes += 1
        return result

    if violation == "write":
        monkeypatch.setattr(api.ApiPublisher, "publish_draft", publish)
    out = api.check_plan([{"name": "first"}], account_label="Test", country="MX")
    assert out["results"] == [] and out["error_kind"] == "plan"
    assert "write" in out["error"] and meta.posts == []
    assert _page_gate(tmp_path, out, ["first"])["proceeded"] == 0
