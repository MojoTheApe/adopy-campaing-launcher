# -*- coding: utf-8 -*-
"""Исключение, которым кончилось задание, уезжает в Sentry с тегами
(финальное ревью PR #314 (d), находки S2/S8).

* теги события `job_exception` — `failure_kind`, короткий ярлык аккаунта
  (без имени персоны), номер задания в контексте без дефисов, фаза;
* исключение сборки черновиков (`publisher.build_drafts`) в `_run_rpa`
  прежде писалось только в задание — теперь едет в Sentry, как исключение
  публикации.
"""
from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import acl_sentry  # noqa: E402


@pytest.fixture
def sent(monkeypatch):
    import sentry_sdk

    events: list[dict] = []

    class Scope:
        def __init__(self):
            self.tags, self.contexts = {}, {}

        def set_tag(self, key, value):
            self.tags[key] = value

        def set_context(self, key, value):
            self.contexts[key] = value

    scopes: list[Scope] = []

    @contextmanager
    def new_scope():
        scope = Scope()
        scopes.append(scope)
        yield scope

    def capture_exception(exc):
        events.append({"exc": exc, "tags": dict(scopes[-1].tags), "contexts": dict(scopes[-1].contexts)})
        return "event-1"

    monkeypatch.setattr(acl_sentry, "init", lambda: True)
    monkeypatch.setattr(sentry_sdk, "new_scope", new_scope)
    monkeypatch.setattr(sentry_sdk, "capture_exception", capture_exception)
    return events


def test_the_job_exception_carries_its_tags(sent):
    exc = RuntimeError("reconnect failed")
    assert acl_sentry.report_job_exception(exc, job="ab-12-cd", account="FacebookDoc19 - Eros",
                                           phase="publish") == "event-1"
    event = sent[-1]
    assert event["exc"] is exc
    assert event["tags"] == {"failure_kind": "job_exception", "account": "FacebookDoc19", "job_phase": "publish"}
    assert event["contexts"] == {"run": {"job": "ab12cd"}}


def test_no_account_and_no_job_mean_no_tags_for_them(sent):
    acl_sentry.report_job_exception(ValueError("x"))
    assert sent[-1]["tags"] == {"failure_kind": "job_exception"}
    assert sent[-1]["contexts"] == {}


def _job(monkeypatch, publisher_cls):
    from src.web import publish_jobs as pj

    monkeypatch.setattr(pj, "RpaPublisher", publisher_cls)
    monkeypatch.setattr(pj, "_write_draft_status", lambda *a, **k: None)
    job_id = "job-1"
    monkeypatch.setitem(pj._jobs, job_id, {"job_id": job_id, "errors": [], "results": [], "status": "running",
                                           "account_label": "KLD_BFL_S15 Person", "total": 0})
    return pj, job_id


def test_a_build_drafts_exception_goes_to_sentry(monkeypatch):
    reported: list = []
    monkeypatch.setattr(acl_sentry, "report_job_exception",
                        lambda exc, **kw: reported.append((exc, kw)) or "e")

    boom = RuntimeError("sheet unreadable")

    class _Publisher:
        def __init__(self, **kw):
            pass

        def build_drafts(self, *a, **kw):
            raise boom

    pj, job_id = _job(monkeypatch, _Publisher)
    pj._run_rpa(job_id, [], account_label="KLD_BFL_S15 Person", country="MX", activate=False)
    assert reported == [(boom, {"job": job_id, "account": "KLD_BFL_S15 Person", "phase": "build_drafts"})]
    assert any("Failed to build campaign drafts" in e for e in pj._jobs[job_id]["errors"])


def test_a_publish_exception_is_tagged_with_its_phase(monkeypatch):
    reported: list = []
    monkeypatch.setattr(acl_sentry, "report_job_exception",
                        lambda exc, **kw: reported.append((exc, kw)) or "e")
    boom = RuntimeError("browser gone")

    class _Draft:
        name = "C1"
        errors: list = []

    class _Publisher:
        def __init__(self, **kw):
            pass

        def build_drafts(self, *a, **kw):
            return [_Draft()]

        def publish_draft(self, draft):
            raise boom

        def close(self, **kw):
            pass

    pj, job_id = _job(monkeypatch, _Publisher)
    monkeypatch.setattr(pj, "_finish", lambda *a, **k: None)
    pj._run_rpa(job_id, [], account_label="KLD_BFL_S15 Person", country="MX", activate=False)
    assert reported and reported[0][0] is boom and reported[0][1]["phase"] == "publish"
