"""Sign release.json with the ACL publisher key, or check a signature (ACL-1357).

The release workflow runs ``sign`` after ``acl_release_manifest.py``: it reads
the Ed25519 private key from the CI secret ``ACL_RELEASE_SIGNING_KEY`` and
writes ``release.json.sig`` next to ``release.json``. Clients verify it with
the public key pinned in ``src/web/release_signing.py``.

    python scripts/acl_sign_release.py sign --manifest <dir>/release.json [--require]
    python scripts/acl_sign_release.py verify --manifest <dir>/release.json
    python scripts/acl_sign_release.py keygen --out <path OUTSIDE the repository>

``keygen`` is for the owner, once, on a trusted machine: it writes the private
key to a file outside this checkout (to be pasted into the secret and then
removed) and prints the line to pin in the client. The private key is never
printed and never written inside the repository.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts._console import use_utf8  # noqa: E402
from src.web import release_signing as rs  # noqa: E402

use_utf8()


def sign(manifest: Path, *, require: bool, env: dict[str, str] | None = None,
         pinned: dict[str, str] | None = None) -> int:
    env = os.environ if env is None else env
    pinned = rs.PINNED_PUBLIC_KEYS if pinned is None else pinned
    secret = (env.get(rs.SIGNING_KEY_ENV) or "").strip()
    # Ключ закреплён — неподписанный выпуск больше не вариант: пропавший или
    # удалённый секрет не должен молча отменять закрепление.
    require = require or bool(pinned)
    if not secret:
        if require:
            print(f"ERROR: {rs.SIGNING_KEY_ENV} is not set; this release must be signed "
                  "(a publisher key is pinned or --require was given).", file=sys.stderr)
            return 1
        print(f"WARNING: {rs.SIGNING_KEY_ENV} is not set - release.json is published UNSIGNED. "
              "Clients in warn mode accept it and report it.")
        return 0
    if require and not pinned:
        print("ERROR: signatures are required but the client pins no publisher key; "
              "enforcing builds would refuse every update.", file=sys.stderr)
        return 1
    try:
        key = rs.load_private_key(secret)
    except rs.SigningError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    key_id, public = rs.pinned_entry(key)
    if pinned and pinned.get(key_id) != public:
        print(f"ERROR: signing key {key_id} is not pinned in src/web/release_signing.py; "
              "clients would refuse this release.", file=sys.stderr)
        return 1
    data = manifest.read_bytes()
    signature = rs.sign_manifest(data, key)
    verdict = rs.verify_manifest(data, signature, pinned={key_id: public})
    if not verdict.valid:  # pragma: no cover - сам себя проверяет на всякий случай
        print(f"ERROR: fresh signature did not verify ({verdict.status})", file=sys.stderr)
        return 1
    out = manifest.with_name(rs.SIGNATURE_ASSET)
    out.write_bytes(signature)
    print(f"Signed {manifest.name} with key {key_id} -> {out.name}")
    return 0


def verify(manifest: Path, signature: Path | None) -> int:
    signature = signature or manifest.with_name(rs.SIGNATURE_ASSET)
    raw = signature.read_bytes() if signature.is_file() else None
    verdict = rs.verify_manifest(manifest.read_bytes(), raw)
    print(f"{manifest.name}: {verdict.status}" + (f" (key {verdict.key_id})" if verdict.key_id else ""))
    return 0 if verdict.valid else 1


def keygen(out: Path) -> int:
    out = out.resolve()
    if out == _ROOT or _ROOT in out.parents:
        print("ERROR: refusing to write a private key inside the repository.", file=sys.stderr)
        return 1
    if out.exists():
        print(f"ERROR: {out} already exists.", file=sys.stderr)
        return 1
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "xb") as fh:
        fh.write(pem)
    key_id, public = rs.pinned_entry(key)
    print(f"Private key written to {out} - put its content into the secret {rs.SIGNING_KEY_ENV}, then delete the file.")
    print("Pin in src/web/release_signing.py:")
    print(f'    PINNED_PUBLIC_KEYS = {{"{key_id}": "{public}"}}')
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p_sign = sub.add_parser("sign")
    p_sign.add_argument("--manifest", type=Path, required=True)
    p_sign.add_argument("--require", action="store_true", help="fail instead of publishing unsigned")
    p_verify = sub.add_parser("verify")
    p_verify.add_argument("--manifest", type=Path, required=True)
    p_verify.add_argument("--signature", type=Path)
    p_keygen = sub.add_parser("keygen")
    p_keygen.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "sign":
        return sign(args.manifest, require=args.require)
    if args.command == "verify":
        return verify(args.manifest, args.signature)
    return keygen(args.out)


if __name__ == "__main__":
    raise SystemExit(main())
