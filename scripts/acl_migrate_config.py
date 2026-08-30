"""Move this machine's own configuration out of the checkout, before anything
can overwrite the checkout.

Runs at launch, ahead of the update step, and is a no-op on a machine that has
already been migrated.

Why at launch, and why before the update: a machine installed before
2026-08-25 keeps its account map inside the checkout, at
config/ad_account_map.csv. The file is untracked (it left git in 614e630), so
an update will not delete it — but the checkout is still the folder that a
re-clone replaces and that `git clean` empties, and until 2026-08-30 the only
thing that migrated it was the doctor, which many machines never run.

Running it ahead of the update also keeps the ordering honest for anything we
remove from git later: a merge DOES delete a file the incoming commit removed,
so config that still lives in the checkout has to be moved out before the
update touches it, not after.

Prints one line so the launcher window says what happened. Never raises: a
convenience move must not be able to stop someone from launching campaigns.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def main() -> int:
    try:
        from config.paths import get_ad_account_map_path, migrate_legacy_account_map

        note = migrate_legacy_account_map()
        if note:
            print(f"  config: {note}")
        else:
            where = get_ad_account_map_path()
            print(f"  config: account map already on this machine — {where}")
    except Exception as exc:  # never block the launch
        print(f"  config: could not check the account map ({type(exc).__name__}: {exc})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
