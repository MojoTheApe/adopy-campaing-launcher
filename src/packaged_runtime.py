"""Read the source identity embedded in a frozen Windows build."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path


def build_identity() -> dict[str, str]:
    if not getattr(sys, "frozen", False):
        return {}
    try:
        manifest = json.loads((Path(sys._MEIPASS) / "acl-build-manifest.json").read_text(encoding="utf-8"))
        sha = manifest["runtime_source_sha"]
        version = manifest["version"]
        if not re.fullmatch(r"[0-9a-f]{40}", sha) or not re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z._-]{0,63}", version):
            return {}
        return {"sha": sha, "short": sha[:12], "tag": version}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {}
