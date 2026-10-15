"""ACL-1330: релей Sentry -> облачная routine (deploy/sentry-relay/worker.js).

Сам релей — Cloudflare Worker на JavaScript, его тесты — `worker.test.js`
на встроенном `node:test` (без пакетов). Этот файл только запускает их из
pytest, чтобы полный прогон в CI их не пропускал. Нет Node 20+ — пропуск с
причиной, а не молчаливый «зелёный».
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

RELAY = Path(__file__).resolve().parents[1] / "deploy" / "sentry-relay"


def _node() -> str | None:
    node = shutil.which("node")
    if not node:
        return None
    try:
        out = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.match(r"v(\d+)", out.strip())
    return node if m and int(m.group(1)) >= 20 else None


def _summary(stdout: str) -> dict[str, int]:
    """Итоги node --test: строки `# pass 16` (TAP) или `ℹ pass 16` (spec).

    Репортер по умолчанию зависит от версии Node и от того, терминал ли это:
    на раннерах CI это TAP, локально в Node 24 — spec. Поэтому формат задаём
    явно (TAP), а разбираем оба.
    """
    out: dict[str, int] = {}
    for m in re.finditer(r"^(?:#|ℹ) (tests|pass|fail|cancelled) (\d+)\s*$", stdout, re.M):
        out[m.group(1)] = int(m.group(2))
    return out


def test_summary_reads_tap_and_spec_reporters():
    tap = "TAP version 13\nok 1 - a\n1..16\n# tests 16\n# pass 16\n# fail 0\n# cancelled 0\n"
    spec = "✔ a (1ms)\nℹ tests 16\nℹ pass 15\nℹ fail 1\nℹ cancelled 0\n"
    assert _summary(tap) == {"tests": 16, "pass": 16, "fail": 0, "cancelled": 0}
    assert _summary(spec) == {"tests": 16, "pass": 15, "fail": 1, "cancelled": 0}
    assert _summary("") == {}


def test_sentry_relay_node_suite():
    node = _node()
    if not node:
        pytest.skip("Node.js 20+ is not installed; run: node --test deploy/sentry-relay/worker.test.js")
    result = subprocess.run(
        [node, "--test", "--test-reporter=tap", "worker.test.js"],
        cwd=str(RELAY), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
    )
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-2000:]
    summary = _summary(result.stdout)
    assert summary.get("fail") == 0 and summary.get("cancelled", 0) == 0, result.stdout[-2000:]
    # Ноль упавших при нуле запущенных — не «зелёный».
    assert summary.get("pass", 0) >= 20 and summary.get("pass") == summary.get("tests"), result.stdout[-2000:]


def test_worker_has_only_the_default_export():
    # Cloudflare Worker: вставляется одним файлом в редактор; лишние
    # именованные экспорты рабочий рантайм мог бы принять за точки входа.
    text = (RELAY / "worker.js").read_text(encoding="utf-8")
    assert re.findall(r"^export\s+\S+", text, re.M) == ["export default"]
    assert "import " not in text.split("export default")[0]


def test_selftest_event_uses_the_robot_path_with_a_fresh_reason(monkeypatch):
    from datetime import datetime, timezone

    from scripts import sentry_selftest_event as selftest
    from src import acl_sentry

    seen = []
    monkeypatch.setattr(acl_sentry, "report_step_failure",
                        lambda step, why="", **kw: seen.append((step, why, kw)) or "")
    event_id, reason = selftest.send(datetime(2026, 9, 30, 12, 0, 1, tzinfo=timezone.utc))
    assert event_id == ""
    assert reason == "selftest-20260930T120001"
    step, _why, kw = seen[0]
    assert step == "relay_selftest"
    assert kw == {"reason": reason, "kind": "blocking"}


def test_selftest_step_is_a_known_kind_in_the_triage_map():
    import json

    rows = json.loads((RELAY.parents[1] / "config" / "sentry_triage_map.json").read_text(encoding="utf-8"))["known"]
    assert any(r["step"] == "relay_selftest" and r["task"] == "ACL-1330" for r in rows)


def test_routine_prompt_posts_files_with_gh_api_capital_f():
    # `gh api -f body=@file` отправляет буквальную строку «@file»; файл читает
    # только `-F` (ACL-1330, ревью #410 F3).
    text = (RELAY.parents[1] / "docs" / "system" / "ROUTINE_SENTRY_PROMPT.md").read_text(encoding="utf-8")
    assert not re.search(r"(?<![\w-])-f\s+\w+=@", text), re.findall(r".*-f\s+\w+=@.*", text)
    assert "comments -F body=@/tmp/comment.md" in text
    assert "-F body=@/tmp/pr.md" in text
