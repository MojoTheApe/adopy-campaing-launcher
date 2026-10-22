"""ACL-1710: scripts/ci_retry.sh ограничивает зависшую команду и повторяет её."""
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "ci_retry.sh"
BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(not BASH or not shutil.which("timeout"), reason="нужны bash и timeout")


def _run(*args, env=None):
    return subprocess.run([BASH, str(SCRIPT), *args], capture_output=True, text=True, env=env, timeout=60)


def test_hung_command_is_cut_and_retried_then_fails_clearly():
    start = time.time()
    r = _run("2", "1", "0", "--", "sleep", "30", env={"PATH": __import__("os").environ["PATH"], "CI_RETRY_CLEANUP": "echo cleaned"})
    assert r.returncode == 1
    assert time.time() - start < 20
    assert r.stdout.count("hit the 1s limit") + r.stdout.count("attempt") >= 2
    assert "cleaned" in r.stdout
    assert "all 2 attempts failed" in r.stdout


def test_second_attempt_can_succeed(tmp_path):
    marker = tmp_path / "m"
    cmd = f"[ -f {marker.as_posix()} ] || {{ touch {marker.as_posix()}; exit 3; }}"
    r = _run("3", "5", "0", "--", "bash", "-c", cmd)
    assert r.returncode == 0
    assert "ok on attempt 2" in r.stdout
