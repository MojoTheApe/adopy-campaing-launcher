"""ACL-2806: gating и заголовок команды бюджета для lifetime-строк (acm_commands.js, чистые функции под node)."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

JS = Path(__file__).resolve().parents[1] / "src" / "web" / "static" / "acm_commands.js"
pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")

SCRIPT = """
const fs = require('fs'); const vm = require('vm');
const ctx = { console }; ctx.globalThis = ctx; vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), ctx);
const A = ctx.acmCommands, avail = { available: true };
const st = (row) => A.home.controlState(row, 'budget', avail);
const title = (fields) => A.dialogModel({ command: 'set_budget', affected: fields.map((f, i) => ({ id: String(i), level: 'adset', field: f })) }).title;
console.log(JSON.stringify({
  lifetime: st({ level: 'campaign', budget: { kind: 'lifetime', amount: 900 } }),
  lifetime_adset: st({ level: 'adset', budget: { kind: 'lifetime', amount: 600 } }),
  daily: st({ level: 'campaign', budget: { kind: 'daily', amount: 50 } }),
  inherited: st({ level: 'adset', budget: { kind: 'inherited' } }),
  t_life: title(['lifetime_budget']), t_daily: title(['daily_budget']),
  t_daily2: title(['daily_budget', 'daily_budget']), t_mixed: title(['lifetime_budget', 'daily_budget']),
  saved: A.home.applySaved({}, { field: 'lifetime_budget', saved: 90000, unit: 100 }).budget,
}));
"""


@pytest.fixture(scope="module")
def out():
    r = subprocess.run(["node", "-e", SCRIPT, str(JS)], capture_output=True, text=True, check=True)
    return json.loads(r.stdout)


def test_lifetime_rows_that_own_their_budget_are_enabled(out):
    assert out["lifetime"]["enabled"] is True
    assert out["lifetime_adset"]["enabled"] is True
    assert out["daily"]["enabled"] is True


def test_cbo_adset_stays_disabled(out):
    assert out["inherited"] == {"enabled": False, "reason": "Budget is set on the campaign"}


def test_title_names_the_budget_kind(out):
    assert out["t_life"] == "Change lifetime budget of 1 object"
    assert out["t_daily"] == "Change daily budget of 1 object"
    assert out["t_daily2"] == "Change daily budget of 2 objects"
    assert out["t_mixed"] == "Change budget of 2 objects"


def test_saved_lifetime_updates_the_row(out):
    assert out["saved"] == {"kind": "lifetime", "amount": 900}


def test_no_lifetime_not_supported_text_left_in_dashboard():
    root = JS.parents[1]
    for p in list(root.glob("static/*.js")) + list(root.glob("templates/*.html")):
        assert "Lifetime budget is not supported" not in p.read_text(encoding="utf-8", errors="ignore"), p
