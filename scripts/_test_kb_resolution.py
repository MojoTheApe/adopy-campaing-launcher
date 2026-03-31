"""Test 5: KB alias resolution for targets and offers."""
import sys
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.knowledge_agent.knowledge_base import (
    resolve_target_code,
    get_target_context,
    get_offer_context,
    get_all_targets,
    get_all_offers,
)

passed = []
failed = []

def check(label, condition, detail=""):
    if condition:
        passed.append(label)
        print(f"  [OK]   {label}")
    else:
        failed.append(label)
        print(f"  [FAIL] {label}" + (f": {detail}" if detail else ""))

print("=== Target alias resolution ===")
check("resolve 'Day'",         resolve_target_code("Day") is not None,         f"got {resolve_target_code('Day')}")
check("resolve 'Mix'",          resolve_target_code("Mix") is not None,          f"got {resolve_target_code('Mix')}")
# mix_lux is an unknown campaign target — should resolve to None (logged to unknown_targets.log)
check("resolve 'mix_lux' is None (unknown → logged)", resolve_target_code("mix_lux") is None)
check("resolve 'HV'",          resolve_target_code("HV") is not None,          f"got {resolve_target_code('HV')}")
check("resolve 'highvalue'",   resolve_target_code("highvalue") is not None,   f"alias test, got {resolve_target_code('highvalue')}")
check("resolve 'GMBL'",        resolve_target_code("GMBL") is not None,        f"got {resolve_target_code('GMBL')}")
check("resolve unknown returns None", resolve_target_code("NOTEXIST_XYZ") is None)

ctx = get_target_context("Day")
check("get_target_context 'Day' returns dict",  isinstance(ctx, dict))
check("context has fb_interest",               bool(ctx.get("fb_interest")) if ctx else False)

print(f"\n=== Offer context ===")
offers = get_all_offers()
check(f"get_all_offers returns dict", isinstance(offers, dict))
check(f"offers has AI Trader", "AI Trader" in offers, f"keys sample: {list(offers.keys())[:5]}")
check(f"offers has Trade GPT", "Trade GPT" in offers)
check(f"AI Trader has aliases",  bool(offers.get("AI Trader", {}).get("aliases")))
check(f"AI Trader has lp_urls",  bool(offers.get("AI Trader", {}).get("lp_urls")))

ai_trader_ctx = get_offer_context("AI Trader")
check("get_offer_context 'AI Trader' works", ai_trader_ctx is not None, f"got {ai_trader_ctx}")

print(f"\n=== Targets count ===")
targets = get_all_targets()
check(f"get_all_targets returns ≥40 entries", len(targets) >= 40, f"got {len(targets)}")

print(f"\n=== Summary ===")
print(f"  Passed: {len(passed)}")
print(f"  Failed: {len(failed)}")
if failed:
    print(f"  Failed tests: {', '.join(failed)}")
    sys.exit(1)
else:
    print("  All tests passed!")
