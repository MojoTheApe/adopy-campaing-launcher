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

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Windows consoles are not UTF-8 by default; without this a Russian line
# aborts the script instead of printing. See scripts/_console.py.
from scripts._console import use_utf8  # noqa: E402

use_utf8()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Migrate/provision durable ACL machine config")
    parser.add_argument(
        "--provision",
        action="store_true",
        help="also create safe machine-local examples when no legacy files exist",
    )
    args = parser.parse_args(argv)
    try:
        from config.config_unify import prepare_config_dir

        prepare_config_dir()
        from config.paths import (
            ensure_machine_config,
            get_ad_account_map_path,
            get_env_files,
            migrate_legacy_account_map,
            migrate_legacy_env,
            protect_machine_secrets,
        )

        if args.provision:
            notes = ensure_machine_config()
        else:
            notes = []
            env_note = migrate_legacy_env(create_from_example=False)
            map_note = migrate_legacy_account_map()
            if env_note:
                notes.append(env_note)
            if map_note:
                notes.append(map_note)
            notes.extend(protect_machine_secrets())
        for note in notes:
            print(f"  config: {note}")
        if not notes:
            print(f"  config: account map — {get_ad_account_map_path()}")
            print(f"  config: settings — {(get_env_files() or ['not found'])[0]}")
    except Exception as exc:
        print(f"  config: could not check the account map ({type(exc).__name__}: {exc})")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
