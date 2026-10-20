"""ACL-402: повторяющиеся отказы сводятся в сигнатуры, правил не предлагают."""
from datetime import date

from scripts import acl_telemetry_report
from src import failure_signatures as fs

TODAY = date(2026, 10, 20)


def _row(machine, step="create", note="timeout 30s", day="2026-10-19", ok="0", run="r1", **kw):
    return {"ts": f"{day}T10:00:00+00:00", "machine": machine, "step": step, "ok": ok,
            "note": note, "run_id": run, "flow_version_at_upload": "v1@abc", **kw}


def test_three_machines_same_step_is_high_confidence():
    rows = [_row("A", note="timeout 30s"), _row("B", note="Timeout 45s", run="r2"),
            _row("C", note="timeout 12s", run="r3"), _row("D", ok="1", run="r4")]
    res = fs.find_repeated(rows, 7, today=TODAY)
    (sig,) = res["signatures"]
    assert sig["machines"] == ["A", "B", "C"] and sig["confidence"] == "high"
    assert sig["reason"] == "timeout #s" and res["fleet"] == ["A", "B", "C", "D"]


def test_single_machine_is_low_and_other_reasons_stay_apart():
    rows = [_row("A"), _row("A", run="r2"), _row("A", note="button missing", run="r3")]
    res = fs.find_repeated(rows, 7, today=TODAY)
    assert [s["confidence"] for s in res["signatures"]] == ["low"]  # одиночный отказ отсечён
    assert "одной машины" in "\n".join(fs.render_markdown(res))


def test_window_excludes_old_failures():
    rows = [_row("A", day="2026-09-01"), _row("B", day="2026-09-02")]
    assert fs.find_repeated(rows, 7, today=TODAY)["signatures"] == []


def test_reason_falls_back_to_diag_and_sheet_json_string():
    row = _row("A", note="", diag='{"why": "selector gone"}')
    assert fs.raw_reason(row) == "selector gone"
    assert fs.raw_reason(_row("A", note="", diag={"error": "TimeoutError"})) == "TimeoutError"
    assert fs.raw_reason(_row("A", note="", diag='{"why": "cut')) == ""  # обрезанный JSON не роняет
    assert fs.normalise_reason("") == fs.NO_REASON


def test_reason_is_scrubbed_of_secrets_and_ids():
    text = fs.normalise_reason("fail act=123456789012 access_token=SECRETVALUE 0a1b2c3d4e5f6a7b8c9d")
    assert "secretvalue" not in text and "123456789012" not in text
    assert "<id>" in text


def test_markdown_states_it_proposes_no_rule_and_report_includes_it():
    rows = [_row("A"), _row("B", run="r2")]
    a = acl_telemetry_report.analyse(rows, 7)
    text = acl_telemetry_report.render(a, "тест")
    assert "Повторяющиеся отказы" in text and "Автоматически ничего не применяется" in text
    assert "`create`" in text
