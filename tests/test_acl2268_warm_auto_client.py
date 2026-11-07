"""ACL-2268: клиент автопрогрева — узел-харнес и проводка в шаблоне."""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = (ROOT / "src" / "web" / "templates" / "report.html").read_text(encoding="utf-8")


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_client_harness():
    done = subprocess.run(["node", str(ROOT / "tests" / "warm_auto_harness.cjs"),
                           str(ROOT / "src" / "web" / "static" / "warm_auto.js")],
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr + done.stdout


def test_drawer_add_buttons_use_the_trusted_event_and_script_is_loaded():
    assert "addCampaign(hit.dataset.country, hit.dataset.account, e.isTrusted)" in TEMPLATE
    assert "addCampaign(countrySel.value, accountSel.value, e.isTrusted)" in TEMPLATE
    assert "trusted === true && window.aclWarmAutoUser" in TEMPLATE
    assert '<script src="/static/warm_auto.js"></script>' in TEMPLATE
