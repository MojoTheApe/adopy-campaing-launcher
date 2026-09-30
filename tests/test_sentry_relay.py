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


def test_sentry_relay_node_suite():
    node = _node()
    if not node:
        pytest.skip("Node.js 20+ is not installed; run: node --test deploy/sentry-relay/worker.test.js")
    result = subprocess.run(
        [node, "--test", "worker.test.js"],
        cwd=str(RELAY), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
    )
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-2000:]
    assert re.search(r"^ℹ fail 0$", result.stdout, re.M), result.stdout[-2000:]


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
