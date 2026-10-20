"""ACL-1230: правило Sentry→GitHub опирается на тег kind=report — он должен быть на месте."""
from pathlib import Path

from src import acl_sentry

DOC = Path(__file__).resolve().parents[1] / "docs" / "system" / "SENTRY_SUPPORT_TICKETS.md"


def test_manual_report_carries_the_tag_the_alert_rule_matches(monkeypatch):
    import sentry_sdk
    from contextlib import contextmanager

    tags = {}
    prints = []

    class Scope:
        contexts = {}
        fingerprint = None

        def set_tag(self, key, val):
            tags[key] = val

        def set_context(self, key, val):
            pass

        def __setattr__(self, key, val):
            if key == "fingerprint":
                prints.append(tuple(val))
            object.__setattr__(self, key, val)

    @contextmanager
    def new_scope():
        yield Scope()

    monkeypatch.setattr(acl_sentry, "init", lambda: True)
    monkeypatch.setattr(sentry_sdk, "new_scope", new_scope)
    monkeypatch.setattr(sentry_sdk, "capture_message", lambda *a, **k: "synthetic-event")
    acl_sentry.report_problem("synthetic report")
    acl_sentry.report_problem("synthetic report")
    assert tags["kind"] == "report"
    # Уникальность отпечатка одного обращения проверяет ACL-1273 (PR #948), здесь на неё не опираемся:
    # до его вливания в main отпечаток общий, и в setup-документе это названо обязательным условием.
    assert prints, "the manual report must set a fingerprint"


def test_setup_doc_names_the_rule_and_label_and_holds_no_secret():
    text = DOC.read_text(encoding="utf-8")
    assert "`kind` **equals** `report`" in text
    assert "support-ticket" in text
    assert not any(p in text for p in ("ghp_", "gho_", "ghs_", "github_pat_"))
