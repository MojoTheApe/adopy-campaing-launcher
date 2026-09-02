"""Owner-side utility for creating a validated ``.aclsetup`` archive."""
from __future__ import annotations

import argparse
import json
import zipfile
from pathlib import Path

from setup_pack import OAUTH_DOCUMENT, SETUP_DOCUMENT, validate_setup_document


def main() -> int:
    parser = argparse.ArgumentParser(description="Create one campaign manager ACL setup pack")
    parser.add_argument("--setup-json", type=Path, required=True)
    parser.add_argument("--oauth-client", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    if args.output.suffix.casefold() != ".aclsetup":
        parser.error("--output must end in .aclsetup")
    document = json.loads(args.setup_json.read_text(encoding="utf-8"))
    oauth = args.oauth_client.read_bytes() if args.oauth_client else None
    validate_setup_document(document, oauth)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(args.output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(SETUP_DOCUMENT, json.dumps(document, indent=2, ensure_ascii=False) + "\n")
        if oauth is not None:
            archive.writestr(OAUTH_DOCUMENT, oauth)
    print(f"Created validated setup pack: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
