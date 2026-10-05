# -*- coding: utf-8 -*-
"""Таблица здоровья робота (ACL-1434).

Фикстура `tests/fixtures/robot_health/` — урезанная копия настоящей
телеметрии 30.09–01.10 с машины владельца: 45 последних прогонов, аккаунты,
задания и прогоны переименованы (AcctA_1…, job-0001…, r000…), id и URL
вычищены, кампании и diag (кроме аккаунта) убраны, из информационных строк
оставлена каждая 15-я. Лог дешборда сведён к строкам «RPA flow version» и
первому упоминанию каждого задания.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

# ACL-1582: файл идёт только в Windows-задание CI; причина — в маркере.
pytestmark = pytest.mark.windows_only(reason="launcher logs of the installed Windows build and drive-letter paths")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.web import robot_health as rh  # noqa: E402

FIX = ROOT / "tests" / "fixtures" / "robot_health"
TEL = FIX / "rpa_telemetry.jsonl"
LOG = FIX / "serve-live.log"


def _row(run, step, ok, ts, job="", account=None, note="", build=None):
    r = {"run_id": run, "ts": ts, "step": step, "ok": ok}
    if job:
        r["job"] = job
    if account:
        r["diag"] = {"account": account}
    if note:
        r["note"] = note
    if build:
        r["build"] = build
    return r


def _series(account, outcomes, step="placements", start=0, build="v1"):
    """Прогоны одного аккаунта: по одному исходу шага `step` на прогон."""
    rows = []
    for i, ok in enumerate(outcomes):
        n = start + i
        job = f"job-{account}-{n}"
        ts = f"2026-09-30T{10 + n // 60:02d}:{n % 60:02d}:00+00:00"
        rows.append(_row("", "job_phase", True, ts, job=job, account=account, note="slot_queue"))
        rows.append(_row(f"{account}-{n}", "campaign_name", True, ts, job=job, build=build))
        rows.append(_row(f"{account}-{n}", step, ok, ts, job=job, build=build))
    return rows


# ── что считается шагом ─────────────────────────────────────────────────────

@pytest.mark.parametrize("step,ok,counted", [
    ("placements", True, True),
    ("ad2_destination", False, True),
    ("placements", None, False),              # информационная строка
    ("job_phase", True, False),
    ("click_target_covered", False, False),   # диагностика, всегда false
    ("advantage_plus_off", False, False),
    ("ad_copy_blocked", False, False),
    ("audience_location_attempt", False, False),
    ("window_bounds", True, False),
    ("profile_unresponsive", False, False),
    ("campaign_budget_exceeded", False, False),
])
def test_only_wizard_outcomes_count(step, ok, counted):
    assert rh.is_outcome_step(step, ok) is counted


def test_retried_step_counts_by_its_last_record():
    rows = [_row("r1", "audience_location", False, "2026-09-30T10:00:00+00:00"),
            _row("r1", "audience_location", True, "2026-09-30T10:00:05+00:00")]
    (run,) = rh.build_runs(rows)
    assert run.outcomes["audience_location"][0] is True


def test_step_not_reached_is_not_a_failure():
    rows = _series("A", [True, True]) + [
        _row("A-9", "campaign_name", False, "2026-09-30T11:00:00+00:00", job="job-A-9")]
    report = rh.build_report(rh.build_runs(rows), window=10)
    assert report["cells"]["placements"][rh.ALL]["total"] == 2
    assert report["cells"]["campaign_name"][rh.ALL]["total"] == 3


# ── окна, тренд, подсветка ──────────────────────────────────────────────────

def test_drop_and_streak_are_highlighted():
    rows = _series("Doc19", [True] * 4 + [False] * 4)
    report = rh.build_report(rh.build_runs(rows), window=4)
    cell = report["cells"]["placements"]["Doc19"]
    assert (cell["ok"], cell["total"], cell["prev_ok"], cell["prev_total"]) == (0, 4, 4, 4)
    assert cell["trend"] == -100.0
    assert set(cell["flags"]) == {"drop", "streak"}
    assert len(cell["streak"]) == 4
    assert any(h["step"] == "placements" and h["column"] == "Doc19" for h in report["highlights"])
    assert report["problem_steps"][0] == "placements"


def test_drop_of_exactly_30_points_counts_and_20_does_not():
    # 10/10 → 7/10 = -30: тревога; 10/10 → 8/10 и т.п. — нет.
    rows = _series("A", [True] * 10 + [True] * 7 + [False, True, False] + [True] * 0)
    report = rh.build_report(rh.build_runs(rows), window=10)
    cell = report["cells"]["placements"]["A"]
    assert cell["trend"] == -20.0 and "drop" not in cell["flags"]
    rows = _series("B", [True] * 10 + [False, True, False, True, True, True, True, False, True, True])
    cell = rh.build_report(rh.build_runs(rows), window=10)["cells"]["placements"]["B"]
    assert cell["trend"] == -30.0 and cell["flags"] == ["drop"]


def test_one_attempt_is_not_a_trend():
    rows = _series("A", [True, False])
    cell = rh.build_report(rh.build_runs(rows), window=1)["cells"]["placements"]["A"]
    assert cell["trend"] == -100.0
    assert "drop" not in cell["flags"]


def test_a_streak_broken_by_a_success_is_not_flagged():
    rows = _series("A", [False, False, True])
    cell = rh.build_report(rh.build_runs(rows), window=10)["cells"]["placements"]["A"]
    assert cell["streak"] == [] and cell["flags"] == []


def test_a_streak_older_than_the_window_is_not_flagged():
    # Шаг падал дважды, потом прогоны до него не доходили.
    rows = _series("A", [False, False]) + _series("A", [True] * 3, step="campaign_name", start=2)
    cell = rh.build_report(rh.build_runs(rows), window=3)["cells"]["placements"]["A"]
    assert cell["total"] == 0 and cell["flags"] == []


def test_columns_are_accounts_plus_all():
    rows = _series("A", [True] * 3) + _series("B", [False] * 2, start=10)
    report = rh.build_report(rh.build_runs(rows), window=10)
    assert report["columns"] == [rh.ALL, "A", "B"]
    assert report["cells"]["placements"][rh.ALL]["ok"] == 3
    assert report["cells"]["placements"][rh.ALL]["total"] == 5


def test_a_drop_between_builds_is_highlighted():
    rows = (_series("A", [True] * 4, build="v96+10@aaa")
            + _series("A", [False, False, True], start=4, build="v96+13@bbb"))
    report = rh.build_report(rh.build_runs(rows), window=10)
    assert report["builds"] == ["v96+10@aaa", "v96+13@bbb"]
    cell = report["by_build"]["placements"]["v96+13@bbb"]
    assert (cell["ok"], cell["total"], cell["flags"]) == (1, 3, ["drop"])
    assert any(h["column"] == "build v96+13@bbb" for h in report["highlights"])


def test_fail_only_steps_are_marked():
    rows = _series("A", [False, False], step="objective_leads") + _series("A", [True], start=5)
    report = rh.build_report(rh.build_runs(rows), window=10)
    assert "objective_leads" in report["fail_only"]
    assert "placements" not in report["fail_only"]


# ── сводка прогона ──────────────────────────────────────────────────────────

def test_run_summary_tells_new_from_known():
    rows = (_series("A", [False], step="placements")
            + _series("B", [False], step="languages", start=1)
            + [_row("A-2", "placements", False, "2026-09-30T10:02:00+00:00", job="job-A-2"),
               _row("A-2", "languages", False, "2026-09-30T10:02:01+00:00"),
               _row("A-2", "ad_copy", False, "2026-09-30T10:02:02+00:00"),
               _row("", "job_phase", True, "2026-09-30T10:02:00+00:00", job="job-A-2", account="A")])
    runs = rh.build_runs(rows)
    status = {f["step"]: f["status"] for f in rh.run_summary(runs, "A-2")["failures"]}
    assert status == {"placements": "known", "languages": "known elsewhere", "ad_copy": "new"}


def test_abort_reason_is_the_trailing_non_step_failure():
    rows = [_row("r1", "campaign_name", True, "2026-09-30T10:00:00+00:00"),
            _row("r1", "click_target_covered", False, "2026-09-30T10:00:01+00:00"),
            _row("r1", "adset_name", True, "2026-09-30T10:00:02+00:00"),
            _row("r1", "campaign_budget_exceeded", False, "2026-09-30T10:00:03+00:00", note="ad_name")]
    (run,) = rh.build_runs(rows)
    assert run.abort == "campaign_budget_exceeded (ad_name)"


# ── сборка из лога ──────────────────────────────────────────────────────────

def test_build_is_the_version_under_which_the_log_first_named_the_job(tmp_path):
    j1, j2 = "job-0001", "job-0002"
    log = tmp_path / "serve-live.log"
    log.write_text("\n".join([
        "[serve] RPA flow version: v96+10@aaa",
        f'INFO: "POST /publish-jobs/{j1} HTTP/1.1" 200 OK',
        "[serve] RPA flow version: v96+13@bbb",
        f'INFO: "GET /publish-jobs/{j1}/progress HTTP/1.1" 200 OK',
        f'INFO: "GET /publish-jobs/{j2} HTTP/1.1" 200 OK',
    ]), encoding="utf-8")
    assert rh.job_builds_from_logs([log]) == {j1: "v96+10@aaa", j2: "v96+13@bbb"}


def test_split_stdout_and_stderr_logs_are_paired_by_launch(tmp_path):
    """serveN.log — версии (stdout), serveN.err.log — id заданий (stderr)."""
    j1, j2, j3 = "job-0101", "job-0102", "job-0103"
    (tmp_path / "serve7.log").write_text("\n".join([
        "[serve] RPA flow version: v96+10@aaa",
        "[serve] RPA engine: recipe_player (default)",
        "[serve] RPA flow version: v96+13@bbb",
        "[serve] RPA engine: recipe_player (default)",
    ]), encoding="utf-8")
    (tmp_path / "serve7.err.log").write_text("\n".join([
        "[serve] Dashboard URL: http://127.0.0.1:1/",
        f'INFO: "POST /publish-jobs/{j1} HTTP/1.1" 200 OK',
        "[serve] Dashboard URL: http://127.0.0.1:1/",
        f'INFO: "GET /publish-jobs/{j1}/progress HTTP/1.1" 200 OK',
        f'INFO: "POST /publish-jobs/{j2} HTTP/1.1" 200 OK',
    ]), encoding="utf-8")
    # Чужой stderr без пары не получает сборку соседа.
    (tmp_path / "other.err.log").write_text(
        "[serve] Dashboard URL: x\n" f'INFO: "POST /publish-jobs/{j3} HTTP/1.1" 200 OK',
        encoding="utf-8")
    logs = sorted(tmp_path.glob("*.log"))
    assert rh.job_builds_from_logs(logs) == {j1: "v96+10@aaa", j2: "v96+13@bbb"}


def test_launch_that_died_before_uvicorn_shifts_only_older_launches(tmp_path):
    j_old, j_new = "job-0201", "job-0202"
    (tmp_path / "server.stdout.log").write_text("\n".join([
        "[serve] RPA flow version: v96+1@aaa",
        "[serve] RPA flow version: v96+2@bbb",   # упал до uvicorn
        "[serve] RPA flow version: v96+3@ccc",
    ]), encoding="utf-8")
    (tmp_path / "server.stderr.log").write_text("\n".join([
        "INFO:     Started server process [11]",
        f'INFO: "POST /publish-jobs/{j_old} HTTP/1.1" 200 OK',
        "INFO:     Started server process [12]",
        f'INFO: "POST /publish-jobs/{j_new} HTTP/1.1" 200 OK',
    ]), encoding="utf-8")
    builds = rh.job_builds_from_logs([tmp_path / "server.stderr.log", tmp_path / "server.stdout.log"])
    assert builds[j_new] == "v96+3@ccc"


def test_installed_build_reads_the_launcher_logs(tmp_path):
    """Установленная сборка: stdout/stderr сервера — в %LOCALAPPDATA%/ACL/logs."""
    out = tmp_path / "workspace" / "output"
    out.mkdir(parents=True)
    logs = tmp_path / "logs"
    logs.mkdir()
    job = "job-0301"
    (out / "rpa_telemetry.jsonl").write_text(json.dumps(
        _row("r1", "placements", True, "2026-09-30T09:00:00+00:00", job=job)) + "\n",
        encoding="utf-8")
    (logs / "server.stdout.log").write_text("[serve] RPA flow version: v96+20@ddd\n", encoding="utf-8")
    (logs / "server.stderr.log").write_text(
        "[serve] Dashboard URL: x\n" f'INFO: "POST /publish-jobs/{job} HTTP/1.1" 200 OK\n',
        encoding="utf-8")
    tel, found = rh.default_sources(out, logs)
    assert [p.name for p in found] == ["server.stderr.log", "server.stdout.log"]
    (run,) = rh.build_runs(rh.load_rows(tel), rh.job_builds_from_logs(found))
    assert run.build == "v96+20@ddd"
    ctx = rh.page_context(out, 10, None, logs)
    assert "v96+20@ddd" in ctx["report"]["builds"]
    # Без папки лаунчера сборка неизвестна — ровно то, что чинили.
    assert rh.page_context(out, 10, None)["report"]["builds"] == [rh.UNKNOWN_BUILD]


def test_report_has_no_raw_account_names():
    """Сырое имя аккаунта несёт id профиля или имя человека — в отчёт и --json
    идёт только метка."""
    raw = "Facebook_1443_61570000000000 - Some Person"
    runs = rh.build_runs([_row("r1", "placements", False, "2026-09-30T09:00:00+00:00", account=raw)])
    dumped = json.dumps(rh.build_report(runs, 10), ensure_ascii=False)
    assert "61570000000000" not in dumped and "Some Person" not in dumped


def test_runs_after_this_process_started_take_its_build():
    rows = [_row("r1", "placements", True, "2026-09-30T09:00:00+00:00"),
            _row("r2", "placements", True, "2026-09-30T11:00:00+00:00")]
    runs = rh.build_runs(rows, {}, ("v96+40@ccc", "2026-09-30T10:00:00+00:00"))
    assert [r.build for r in runs] == [rh.UNKNOWN_BUILD, "v96+40@ccc"]


def test_account_label_drops_profile_ids():
    assert rh.account_label("Facebook_1443_61579302961692") == "Facebook_1443"
    assert rh.account_label("FacebookDoc19 - Some Name") == "FacebookDoc19"
    assert rh.account_label("") == rh.UNKNOWN_ACCOUNT


# ── настоящая телеметрия (урезанная копия) ──────────────────────────────────

@pytest.fixture(scope="module")
def real():
    return rh.report_from_files([TEL], [LOG], window=5)


def test_real_telemetry_every_run_has_an_account_and_a_build(real):
    report, runs = real
    counted = [r for r in runs if r.outcomes]
    assert len(counted) == 45
    assert all(r.account != rh.UNKNOWN_ACCOUNT for r in counted)
    assert all(r.build != rh.UNKNOWN_BUILD for r in counted)
    assert report["columns"][0] == rh.ALL and len(report["columns"]) == 6
    assert report["builds"][-1] == "v96+13@410bd23"


def test_real_telemetry_skips_diagnostics(real):
    report, _ = real
    for step in ("job_phase", "click_target_covered", "advantage_plus_off",
                 "window_bounds", "tab_woken", "ad_copy_blocked"):
        assert step not in report["steps"]
    for step in ("campaign_name", "placements", "ad_creative_upload", "ad_level_summary"):
        assert step in report["steps"]


def test_real_telemetry_highlights_the_creative_upload_breakdown(real):
    # 01.10: на одном аккаунте загрузка креатива падала прогон за прогоном.
    report, _ = real
    hl = [h for h in report["highlights"]
          if h["step"] == "ad_creative_upload" and h["column"] == "AcctD_4"]
    assert hl and "streak" in hl[0]["flags"] and "drop" in hl[0]["flags"]
    assert len(hl[0]["streak"]) >= 2


def test_real_telemetry_markdown_has_the_table(real):
    report, _ = real
    md = rh.render_markdown(report, top=5)
    assert "| step | all | " in md
    assert md.count("\n| ") >= 5
    assert "Highlights:" in md
    assert "Facebook_" not in md and "http" not in md


def test_script_prints_the_table(capsys):
    sys.path.insert(0, str(ROOT / "scripts"))
    import robot_health as script  # noqa: E402

    assert script.main(["--telemetry", str(TEL), "--log", str(LOG), "--n", "5",
                        "--top", "5", "--output-dir", str(FIX)]) == 0
    out = capsys.readouterr().out
    assert "| step | all |" in out and "Recent runs" in out


# ── страница ────────────────────────────────────────────────────────────────

@pytest.fixture()
def web_out(tmp_path, monkeypatch):
    from src.web import app as web

    out = tmp_path / "output"
    out.mkdir()
    shutil.copy(TEL, out / "rpa_telemetry.jsonl")
    shutil.copy(LOG, out / "serve-live.log")
    shots = out / "rpa_screenshots" / "r00000000045"
    shots.mkdir(parents=True)
    (shots / "20261001_082900_recorded_fail_ad_creative_upload.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    monkeypatch.setattr(web, "_robot_health_output_dir", lambda: out)
    monkeypatch.setattr(web, "_robot_health_current_build", lambda: None)
    monkeypatch.setattr(web, "_robot_health_logs_dir", lambda: tmp_path / "no-launcher-logs")
    return web, out


def test_page_renders_the_table(web_out):
    web, _ = web_out
    resp = web.robot_health_page(n=5)
    html = resp.body.decode("utf-8")
    assert resp.status_code == 200
    assert "<title>Robot health</title>" in html
    assert "Needs attention" in html and "Steps by account" in html and "Steps by build" in html
    assert "AcctD_4" in html and "v96+13@410bd23" in html
    assert 'href="/robot-health/run/r00000000045?n=5"' in html
    assert "class=\"r-bad flag\"" in html


def test_run_page_shows_failures_and_captures(web_out):
    web, out = web_out
    resp = web.robot_health_run("r00000000045", n=5)
    html = resp.body.decode("utf-8")
    assert resp.status_code == 200
    assert "ad_creative_upload" in html and "known" in html
    assert "/robot-health/capture/r00000000045/20261001_082900_recorded_fail_ad_creative_upload.png" in html
    assert str(out / "rpa_screenshots" / "r00000000045") in html


def test_unknown_run_is_404(web_out):
    web, _ = web_out
    assert web.robot_health_run("nope", n=5).status_code == 404
    assert web.robot_health_run("../etc", n=5).status_code == 404


def test_capture_serves_only_images_of_that_run(web_out):
    web, out = web_out
    (out / "secret.png").write_bytes(b"x")
    ok = web.robot_health_capture(
        "r00000000045", "20261001_082900_recorded_fail_ad_creative_upload.png")
    assert ok.status_code == 200
    for run_id, name in (("r00000000045", "..\\..\\secret.png"), ("r00000000045", "../../secret.png"),
                         ("..", "secret.png"), ("r00000000045", "missing.png"),
                         # Имя с диском без разделителя (Windows): без проверки
                         # родителя `папка / "C:x.png"` уходит из папки прогона.
                         ("r00000000045", "C:x.png"), ("r00000000045", "C:secret.png")):
        assert web.robot_health_capture(run_id, name).status_code == 404


@pytest.mark.skipif(sys.platform != "win32", reason="drive letters exist only on Windows")
def test_capture_rejects_a_name_on_another_drive(tmp_path, monkeypatch):
    """`папка / "Z:x.png"` на Windows — это `Z:x.png` (текущая папка диска Z),
    то есть вне папки прогона. Держит только проверка родителя: делаем вид,
    что такой файл есть, чтобы её не подменила проверка существования."""
    (tmp_path / "rpa_screenshots" / "r1").mkdir(parents=True)
    monkeypatch.setattr(rh.Path, "is_file", lambda self: True)
    assert rh.capture_file(tmp_path, "r1", "shot.png") is not None
    for name in ("Z:x.png", "Q:shot.png"):
        assert rh.capture_file(tmp_path, "r1", name) is None


def test_empty_machine_gets_a_message(tmp_path, monkeypatch):
    from src.web import app as web

    monkeypatch.setattr(web, "_robot_health_output_dir", lambda: tmp_path)
    monkeypatch.setattr(web, "_robot_health_current_build", lambda: None)
    monkeypatch.setattr(web, "_robot_health_logs_dir", lambda: tmp_path / "no-launcher-logs")
    html = web.robot_health_page(n=10).body.decode("utf-8")
    assert "No robot runs on this machine yet" in html


def test_dashboard_header_links_to_the_page():
    tpl = (ROOT / "src" / "web" / "templates" / "report.html").read_text(encoding="utf-8")
    assert 'href="/robot-health"' in tpl
