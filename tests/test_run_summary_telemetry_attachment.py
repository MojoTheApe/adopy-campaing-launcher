# -*- coding: utf-8 -*-
"""Итог кампании везёт строки телеметрии своего прогона вложением (ACL-1554).

Как и `test_run_summary_reaches_sentry.py`: настоящий `sentry_sdk` и
настоящий `acl_sentry.init`/`before_send`, подменён только транспорт —
двойник складывает события и вложения в списки. Адрес DSN —
`example.invalid`, в сеть ничего не уходит. Журнал телеметрии — свой файл
теста (`tmp_path`), строки в нём синтетические: выдуманные номера кабинета,
адрес Ads Manager, почта, токен и путь с именем пользователя.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import sentry_sdk  # noqa: E402
from sentry_sdk.transport import Transport  # noqa: E402

from src import acl_sentry  # noqa: E402
from src.fb_ads_rpa import run_summary  # noqa: E402
from src.fb_ads_rpa import run_telemetry as rt  # noqa: E402

RUN = "a1b2c3d4e5f6"
OTHER = "0f9e8d7c6b5a"
CAMPAIGN = "GT_ait_All_Video_FX_35_04.10"
ACCOUNT = "FacebookDoc19 Ivan Petrov"

# Синтетические «опасные» значения. Номера собраны из частей, чтобы их не
# ловил сканер секретов репозитория.
ACT_ID = "".join(("1234", "5678", "9012", "3456"))
BIZ_ID = "".join(("9876", "5432", "1012", "345"))
URL = (f"https://adsmanager.facebook.com/adsmanager/manage/campaigns?act={ACT_ID}"
       f"&business_id={BIZ_ID}&selected_campaign_ids=555")
EMAIL = "ivan.petrov@example.com"
TOKEN = "EAAB" + "x7Yq" * 12
USER_PATH = r"C:\Users\ivan.petrov\AppData\Local\ACL\workspace\output\shot.png"  # linux-ok: a Windows path as opaque data, never touched on disk
SHORT_ACT = "act_" + "98765"
PROFILE = "-".join(("3f2b8c1e", "9a7d", "4e21", "b5c4", "0d1e2f3a4b5c"))
# Номер задания дешборда — наш uuid4; как и в `acl_sentry`, едет без дефисов.
JOB = "-".join(("7c1d2e3f", "4a5b", "4c6d", "8e7f", "a0b1c2d3e4f5"))

SECRETS = (ACT_ID, BIZ_ID, "adsmanager.facebook.com", EMAIL, TOKEN, "ivan.petrov",
           r"Users\ivan", SHORT_ACT, PROFILE)  # linux-ok: a relative Windows path as opaque data, never touched on disk


class CapturingTransport(Transport):
    """Транспорт-двойник: события и вложения — в списки."""

    def __init__(self, options=None):
        super().__init__(options)
        self.events: list[dict] = []
        self.attachments: list[dict] = []

    def capture_envelope(self, envelope):
        event = envelope.get_event()
        if event is not None:
            self.events.append(event)
        for item in envelope.items:
            if item.headers.get("type") == "attachment":
                self.attachments.append({
                    "filename": item.headers.get("filename"),
                    "content_type": item.headers.get("content_type"),
                    "bytes": item.payload.get_bytes(),
                })

    def flush(self, timeout, callback=None):
        return None

    def kill(self):
        return None


@pytest.fixture
def sentry(monkeypatch):
    transport = CapturingTransport()
    real_init = sentry_sdk.init

    def init_with_fake_transport(*args, **kwargs):
        kwargs["transport"] = transport
        return real_init(*args, **kwargs)

    monkeypatch.setenv("ACL_SENTRY_DSN", "https://public@o1.ingest.example.invalid/1")
    monkeypatch.setenv("ACL_SENTRY_RUN_SUMMARY", "1")
    monkeypatch.setenv("ACL_MACHINE_LABEL", "Dan")
    monkeypatch.setattr(acl_sentry, "_started", False)
    monkeypatch.setattr(acl_sentry, "_machine_tags", {})
    monkeypatch.setattr(sentry_sdk, "init", init_with_fake_transport)
    from src.fb_ads_rpa import telemetry_sync

    monkeypatch.setattr(telemetry_sync, "flow_version", lambda: "v99+0@abc1234")
    rt.clear_current_run()
    rt._STEPS.clear()
    yield transport
    rt.clear_current_run()
    try:
        sentry_sdk.get_client().close()
    except Exception:
        pass
    real_init(dsn="")
    for key in ("machine", "machine_id"):
        sentry_sdk.get_global_scope().remove_tag(key)


@pytest.fixture
def journal(tmp_path, monkeypatch):
    path = tmp_path / "output" / "rpa_telemetry.jsonl"
    monkeypatch.setattr(rt, "TELEMETRY_PATH", path)
    return path


def _write(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _row(run_id: str, step: str, ok=True, **extra) -> dict:
    return {"run_id": run_id, "ts": "2026-10-04T10:00:00+00:00", "step": step, "ok": ok,
            "campaign": CAMPAIGN, **extra}


def _send(steps=None, run_id: str = RUN) -> None:
    run_summary.report_finished(campaign=CAMPAIGN, account=ACCOUNT, failures=[], ads=2,
                                run_id=run_id,
                                steps=steps if steps is not None else [
                                    {"step": "campaign_name", "ok": True}],
                                background=False)


def _summary_event(transport) -> dict:
    found = [e for e in transport.events if (e.get("tags") or {}).get("kind") == "run_summary"]
    assert len(found) == 1, transport.events
    return found[0]


def _lines(attachment: dict) -> list[dict]:
    text = attachment["bytes"].decode("utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


# ── только строки своего прогона ───────────────────────────────────────────

def test_the_summary_carries_only_its_own_run_rows(sentry, journal):
    _write(journal, [
        _row(OTHER, "campaign_name"),
        _row(RUN, "campaign_name", duration_s=2.0),
        _row(OTHER, "ad_copy", False, note="other run"),
        _row(RUN, "audience_location", False, note="still_loading", diag={"why": "table-loading"}),
        _row(RUN, "ad_copy", True, duration_s=12.3),
    ])
    _send()
    event = _summary_event(sentry)
    assert event["contexts"]["run_summary"]["telemetry_attachment"] == "ok"
    (att,) = sentry.attachments
    assert att["filename"] == f"run_telemetry_{RUN}.jsonl"
    rows = _lines(att)
    assert [r["step"] for r in rows] == ["campaign_name", "audience_location", "ad_copy"]
    assert {r["run_id"] for r in rows} == {RUN}
    assert rows[1]["note"] == "still_loading" and rows[1]["diag"]["why"] == "table-loading"
    assert rows[2]["duration_s"] == 12.3


# ── вычистка ───────────────────────────────────────────────────────────────

def test_ids_urls_emails_tokens_and_user_paths_do_not_survive(sentry, journal):
    _write(journal, [
        _row(RUN, "campaign_open", True, note=f"opened {URL}",
             diag={"url": URL, "title": f"Ads Manager - {ACT_ID}",
                   "access_token": TOKEN, "user": EMAIL,
                   "auth": f"Authorization: Bearer {TOKEN}",
                   "screenshot": USER_PATH, "profile": PROFILE,
                   "account": SHORT_ACT}),
        _row(RUN, "ad_copy", False, note=f"token={TOKEN} for {EMAIL} at {USER_PATH}",
             job=JOB),
        _row(RUN, "publish_check", None,
             diag={"nested": [{"href": URL, "path": USER_PATH.replace("\\", "/")}]}),  # linux-ok: a Windows path as opaque data, never touched on disk
    ])
    _send()
    event = _summary_event(sentry)
    assert event["contexts"]["run_summary"]["telemetry_attachment"] == "ok"
    (att,) = sentry.attachments
    text = att["bytes"].decode("utf-8")
    raw = json.dumps(_lines(att), ensure_ascii=False)
    for secret in SECRETS:
        assert secret not in text and secret not in raw, secret
    rows = _lines(att)
    assert len(rows) == 3 and rows[0]["step"] == "campaign_open"
    # Полезное остаётся: шаг, исход, вид пути без имени.
    assert rows[1]["ok"] is False
    assert rows[1]["job"] == JOB.replace("-", "")
    assert "AppData" in rows[0]["diag"]["screenshot"]


def test_a_screenshot_is_never_attached(sentry, journal):
    png = journal.parent / "shot.png"
    png.parent.mkdir(parents=True, exist_ok=True)
    png.write_bytes(b"\x89PNG fake")
    _write(journal, [_row(RUN, "ad_copy", False, diag={"screenshot": str(png)})])
    _send()
    (att,) = sentry.attachments
    assert att["filename"].endswith(".jsonl") and b"PNG fake" not in att["bytes"]
    assert att["content_type"] == "application/x-ndjson"


# ── потолок размера ───────────────────────────────────────────────────────

def test_oversized_telemetry_drops_the_oldest_rows_and_says_so(sentry, journal, monkeypatch):
    monkeypatch.setattr(run_summary, "TELEMETRY_MAX_BYTES", 4096)
    _write(journal, [_row(RUN, f"step_{i:03d}", True, note="x" * 80) for i in range(200)])
    _send()
    event = _summary_event(sentry)
    assert event["contexts"]["run_summary"]["telemetry_attachment"] == "truncated"
    (att,) = sentry.attachments
    assert len(att["bytes"]) <= 4096
    rows = _lines(att)
    marker = rows[-1]
    kept = rows[:-1]
    assert marker["step"] == "telemetry_truncated"
    assert marker["dropped_rows"] == 200 - len(kept) > 0
    # Выброшены самые старые: последняя строка прогона на месте.
    assert kept[-1]["step"] == "step_199"
    assert kept[0]["step"] == f"step_{200 - len(kept):03d}"


def test_the_default_cap_is_256_kb():
    assert run_summary.TELEMETRY_MAX_BYTES == 256 * 1024


# ── отказы: итог уходит всё равно ─────────────────────────────────────────

def test_unreadable_telemetry_still_sends_the_summary_without_attachment(sentry, journal):
    journal.mkdir(parents=True)  # каталог вместо файла: открыть не выйдет
    _send()
    event = _summary_event(sentry)
    assert event["contexts"]["run_summary"]["telemetry_attachment"] == "error"
    assert sentry.attachments == []


def test_a_scrub_failure_is_an_error_not_a_leak(sentry, journal, monkeypatch):
    _write(journal, [_row(RUN, "ad_copy", False, note=f"at {URL}")])

    def broken(_row, _run_id):
        raise RuntimeError("scrubber broke")

    # Ломается вычистка строки вложения, а не `before_send`: событие без
    # вычистки не уходит вовсе, это другое правило.
    monkeypatch.setattr(run_summary, "_attachment_row", broken)
    _send()
    event = _summary_event(sentry)
    assert event["contexts"]["run_summary"]["telemetry_attachment"] == "error"
    assert sentry.attachments == []


def test_a_failing_add_attachment_still_sends_the_summary(sentry, journal, monkeypatch):
    _write(journal, [_row(RUN, "ad_copy", True)])

    def boom(self, *a, **k):
        raise RuntimeError("attachments broke")

    monkeypatch.setattr(sentry_sdk.Scope, "add_attachment", boom)
    _send()
    event = _summary_event(sentry)
    assert event["contexts"]["run_summary"]["telemetry_attachment"] == "error"
    assert sentry.attachments == []


def test_no_rows_for_this_run_is_missing(sentry, journal):
    _write(journal, [_row(OTHER, "campaign_name")])
    _send()
    event = _summary_event(sentry)
    assert event["contexts"]["run_summary"]["telemetry_attachment"] == "missing"
    assert sentry.attachments == []


def test_no_journal_at_all_is_missing(sentry, journal):
    _send()
    assert _summary_event(sentry)["contexts"]["run_summary"]["telemetry_attachment"] == "missing"
    assert sentry.attachments == []


def test_no_run_id_is_missing_and_reads_nothing(sentry, journal):
    _write(journal, [_row("", "campaign_name"), _row(OTHER, "ad_copy")])
    _send(steps=[], run_id="")
    assert _summary_event(sentry)["contexts"]["run_summary"]["telemetry_attachment"] == "missing"
    assert sentry.attachments == []


def test_the_background_send_attaches_too(sentry, journal):
    _write(journal, [_row(RUN, "ad_copy", True)])
    worker = run_summary.report_finished(campaign=CAMPAIGN, account=ACCOUNT, run_id=RUN,
                                         steps=[{"step": "ad_copy", "ok": True}])
    worker.join(10)
    assert _summary_event(sentry)["contexts"]["run_summary"]["telemetry_attachment"] == "ok"
    assert len(sentry.attachments) == 1


# ── ревью PR #556 ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("path, name", [
    (r"C:\Users\Ivan Petrov\AppData\Local\ACL\shot.png", "Petrov"),        # F1: пробел в имени  # linux-ok: a Windows path as opaque data, never touched on disk
    ("C:/Users/Ivan Petrov/AppData/Local/ACL/shot.png", "Petrov"),
    (r"\srv\Users\ivan.petrov\share\run.log", "ivan.petrov"),             # UNC  # linux-ok: a relative Windows path as opaque data, never touched on disk
    ("/Users/ivan.petrov/Library/ACL/run.log", "ivan.petrov"),             # M3b: macOS
    ("/home/ivan.petrov/acl/run.log", "ivan.petrov"),                      # M3b: Linux
])
def test_user_names_in_every_path_form_do_not_survive(sentry, journal, path, name):
    _write(journal, [_row(RUN, "ad_copy", False, note=f"saved to {path} ok",
                          diag={"file": path, "files": [path]})])
    _send()
    (att,) = sentry.attachments
    text = att["bytes"].decode("utf-8")
    assert name not in text and "Ivan" not in text, text
    (row,) = _lines(att)
    assert "<user>" in row["diag"]["file"]
    assert row["diag"]["file"].endswith(path[-8:])  # хвост пути остаётся
    assert row["note"].endswith(" ok")


def test_ids_and_emails_in_dict_keys_do_not_survive(sentry, journal):
    _write(journal, [_row(RUN, "account_status", True, diag={
        "accounts": {ACT_ID: "active", SHORT_ACT: "disabled", EMAIL: "admin",
                     BIZ_ID: "biz", "plain": "kept"},
    })])
    _send()
    (att,) = sentry.attachments
    text = att["bytes"].decode("utf-8")
    for secret in (ACT_ID, SHORT_ACT, EMAIL, BIZ_ID):
        assert secret not in text, secret
    (row,) = _lines(att)
    accounts = row["diag"]["accounts"]
    # Два номера стали `<digits>` — оба значения на месте, ни одно не затёрто.
    assert sorted(accounts.values()) == sorted(["active", "disabled", "admin", "biz", "kept"])
    assert accounts["plain"] == "kept"


def test_another_runs_row_that_mentions_this_run_is_not_attached(sentry, journal):
    _write(journal, [
        _row(OTHER, "retry_of", True, note=f"retrying \"{RUN}\"", diag={"previous": RUN}),
        _row(RUN, "ad_copy", True),
    ])
    _send()
    (att,) = sentry.attachments
    rows = _lines(att)
    assert [r["step"] for r in rows] == ["ad_copy"]


def test_sentry_off_does_not_read_the_journal(journal, monkeypatch):
    monkeypatch.setenv("ACL_SENTRY_DSN", "")
    monkeypatch.setenv("ACL_SENTRY_RUN_SUMMARY", "1")
    monkeypatch.setattr(acl_sentry, "_started", False)
    _write(journal, [_row(RUN, "ad_copy", True)])
    reads = []
    monkeypatch.setattr(run_summary, "telemetry_attachment",
                        lambda *a, **k: reads.append(a) or (None, "ok"))
    monkeypatch.setattr(run_summary, "_run_rows", lambda *a, **k: reads.append(a) or [])
    assert _send() is None
    assert run_summary._deliver({"run_id": RUN, "result": "clean"}) == ""
    assert reads == []


class _Scope:
    def __init__(self):
        self.attachments: list[dict] = []

    def add_attachment(self, **kw):
        self.attachments.append(kw)


def test_acl_sentry_refuses_an_attachment_over_the_cap(monkeypatch):
    monkeypatch.setattr(run_summary, "TELEMETRY_MAX_BYTES", 100)
    scope = _Scope()
    big = {"bytes": b"x" * 101, "filename": f"run_telemetry_{RUN}.jsonl"}
    assert acl_sentry._attach_telemetry(scope, big) is False
    fits = {"bytes": b"x" * 100, "filename": f"run_telemetry_{RUN}.jsonl"}
    assert acl_sentry._attach_telemetry(scope, fits) is True
    assert [a["bytes"] for a in scope.attachments] == [b"x" * 100]


def test_an_oversized_attachment_reaching_sentry_is_dropped_as_error(sentry):
    big = {"bytes": b"{}\n" * (run_summary.TELEMETRY_MAX_BYTES // 3 + 1),
           "filename": f"run_telemetry_{RUN}.jsonl"}
    acl_sentry.report_run_summary({"result": "clean", "run_id": RUN,
                                   "telemetry_attachment": "ok"}, attachment=big)
    assert _summary_event(sentry)["contexts"]["run_summary"]["telemetry_attachment"] == "error"
    assert sentry.attachments == []


@pytest.mark.parametrize("filename", ["shot.png", "run_telemetry.jsonl.png", "page.html", ""])
def test_acl_sentry_attaches_only_jsonl(filename):
    scope = _Scope()
    att = {"bytes": b"{}\n", "filename": filename, "content_type": "application/x-ndjson"}
    assert acl_sentry._attach_telemetry(scope, att) is False
    assert scope.attachments == []
