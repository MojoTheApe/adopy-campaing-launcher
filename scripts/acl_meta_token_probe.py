"""Inspect one official Meta token without reading ACM's machine configuration."""
from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from meta_campaign_agent.official_token_verifier import (
    OfficialTokenVerifier, Outcome, ScopeComparison, Secret,
)


class _ArgumentsError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's message can contain rejected secret-valued flags/positionals.
        raise _ArgumentsError


def _parser():
    parser = _Parser(description="Inspect token/application identity only; account access and publication are not checked.",
                     allow_abbrev=False)
    parser.add_argument("--app-id", required=True, help="Expected numeric application ID")
    parser.add_argument("--api-version", required=True, help="Explicit supported Graph version, for example v24.0")
    parser.add_argument("--require-scope", action="append", default=[], help="Optional scope to compare with reported scopes")
    parser.add_argument("--token-env", help="Explicit environment variable NAME holding the subject token; otherwise hidden prompt")
    parser.add_argument("--app-secret-env", help="Explicit environment variable NAME holding the inspector App Secret; otherwise hidden prompt")
    return parser


def _hidden_input(label):
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        return getpass.getpass(label)


def main(argv=None, *, verifier=None, prompt=None, environ=None, stdout=None, stderr=None):
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    try:
        args = _parser().parse_args(argv)
        if (not re.fullmatch(r"[1-9][0-9]{0,39}", args.app_id)
                or not re.fullmatch(r"v[1-9][0-9]{0,2}\.[0-9]{1,2}", args.api_version)
                or len(args.require_scope) > 128
                or any(not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", scope) for scope in args.require_scope)
                or any(name is not None and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", name)
                       for name in (args.token_env, args.app_secret_env))):
            raise _ArgumentsError
    except _ArgumentsError:
        stderr.write("Invalid diagnostic arguments. Use --help; credentials must use hidden input or explicit environment names.\n")
        return 2
    environment = os.environ if environ is None else environ
    reader = _hidden_input if prompt is None else prompt
    try:
        token = environment[args.token_env] if args.token_env else reader("Subject token (hidden): ")
        secret = environment[args.app_secret_env] if args.app_secret_env else reader("App Secret (hidden): ")
    except (Exception, KeyboardInterrupt):
        stderr.write("Protected credential input unavailable. No request made.\n")
        return 2
    try:
        result = (OfficialTokenVerifier() if verifier is None else verifier).verify(
            args.app_id, Secret(token), Secret(secret), api_version=args.api_version,
            required_scopes=args.require_scope)
        stdout.write(json.dumps(result.as_dict(), ensure_ascii=True, sort_keys=True) + "\n")
        if result.outcome == Outcome.INVALID_INPUT:
            return 2
        if result.outcome == Outcome.APP_MATCH:
            return 0 if result.required_scopes in (ScopeComparison.NOT_REQUESTED, ScopeComparison.MET) else 4
        if result.outcome in (Outcome.SUBJECT_INVALID, Outcome.APP_MISMATCH, Outcome.EXPIRED, Outcome.UNSUPPORTED_TYPE):
            return 1
        return 3
    except (Exception, KeyboardInterrupt):
        stderr.write("Inspection failed. Diagnostic details suppressed.\n")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
