"""Explicit one-account official Meta read pilot; never load machine .env files."""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import warnings
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.meta_campaign_agent.official_access_probe import (  # noqa: E402
    AccessToken, FailureKind, OfficialAccessProbe, ProbeRequest,
)

TOKEN_ENV = "ACL_META_OFFICIAL_ACCESS_TOKEN"


class _Arguments(argparse.ArgumentParser):
    def error(self, message):
        # argparse normally echoes bad arguments, including a mistakenly pasted
        # --token SECRET. No user-supplied argv is ever printed on this path.
        self.print_usage(sys.stderr)
        self.exit(2, "Invalid arguments. Use --help; tokens are accepted only by hidden prompt or --token-env.\n")


def _failure(kind: FailureKind, message: str) -> int:
    print(json.dumps({"status": "failed", "failure": {"kind": kind.value, "message": message},
                      "publishing_access": "not_checked"}))
    return 2


def main(argv: list[str] | None = None) -> int:
    parser = _Arguments(allow_abbrev=False, description="Read one explicitly selected Meta account and spend period. No writes or browser fallback.")
    parser.add_argument("--account", required=True, help="Exact ad account ID (digits or act_<digits>).")
    parser.add_argument("--since", required=True, help="First account-local date, YYYY-MM-DD.")
    parser.add_argument("--until", required=True, help="Last account-local date, inclusive; at most 31 days.")
    parser.add_argument("--token-env", choices=[TOKEN_ENV], help="Explicit process environment source; otherwise use a hidden prompt.")
    args = parser.parse_args(argv)
    try:
        query = ProbeRequest(args.account, args.since, args.until)
    except ValueError as error:
        return _failure(FailureKind.INVALID_INPUT, str(error))
    try:
        if args.token_env:
            value = os.environ.get(TOKEN_ENV, "")
        else:
            with warnings.catch_warnings():
                # Refuse getpass's fallback to echoed stdin when no hidden input
                # is possible. Never read a token from command-line arguments.
                warnings.simplefilter("error", getpass.GetPassWarning)
                value = getpass.getpass("Official Meta access token (hidden): ")
        credential = AccessToken(value)
    except (ValueError, EOFError, KeyboardInterrupt, getpass.GetPassWarning):
        return _failure(FailureKind.INVALID_CREDENTIAL, "No valid credential was supplied through the selected secret input.")
    try:
        result = OfficialAccessProbe(credential).read(query)
    except Exception:
        # A library/platform failure must not dump a credential-bearing traceback
        # into the operator's terminal. No telemetry or raw payload is emitted.
        return _failure(FailureKind.UNEXPECTED_RESPONSE, "The diagnostic could not complete; no raw error details were emitted.")
    print(json.dumps(result.to_dict(), ensure_ascii=True))
    return 0 if result.failure is None else 1


if __name__ == "__main__":
    raise SystemExit(main())
