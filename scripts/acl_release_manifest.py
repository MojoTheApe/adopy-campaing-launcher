"""Describe a promoted release for managers' machines: release.json, checksum, notes.

Called by the release workflow after the installer is built from an
`acl-release-*` tag (ACL-1239 / ACL-1240). Produces next to the installer:

* ``<installer>.sha256`` — ``<hex>  <file name>``, the format `certutil` and
  `Get-FileHash` users compare against;
* ``release.json`` — what the dashboard's update check reads: version, tag,
  commit, installer URL, size and SHA-256, and the tracker issues fixed since
  the previous release with the pull requests' titles as short notes;
* ``release-notes.md`` — the same list for the GitHub Release page.

Fixed issues come from merged pull requests between the previous release tag
and this one: their mandatory ``Tracker issues:`` line (see
``scripts/tracker_pr_check.py``). Pull request bodies are read through ``gh``.

    python scripts/acl_release_manifest.py --tag acl-release-2026-09-20 \\
        --installer packaging/windows/.artifacts/installer/Adopy-Campaign-Launcher-Setup-2026-09-20.exe \\
        --repo MojoTheApe/adopy-campaing-launcher
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

TAG_PREFIX = "acl-release-"
_MERGE = re.compile(r"Merge pull request #(\d+)")


def version_of(tag: str) -> str:
    if not tag.startswith(TAG_PREFIX) or len(tag) == len(TAG_PREFIX):
        raise ValueError(f"{tag!r} is not an {TAG_PREFIX}* tag")
    return tag[len(TAG_PREFIX):]


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tracker_config() -> dict:
    return json.loads((Path(__file__).resolve().parents[1] / "tracker" / "config.json").read_text(encoding="utf-8"))


def task_id(number: int, config: dict) -> str:
    project = config["project"]
    legacy = project.get("legacy_task_ids", {})
    return legacy.get(str(number), f"{project.get('id_prefix', 'ACL')}-{number + project.get('id_offset', 0)}")


def tracker_issues(body: str, config: dict | None = None) -> list[str]:
    """The PR's ``Tracker issues:`` line as task ids, parsed exactly as the PR check does."""
    config = config or _tracker_config()
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from tracker_pr_check import linked_numbers
    try:
        numbers = linked_numbers(body, config)
    except ValueError:
        return []  # старые PR без строки — в список исправленного не попадают
    return [task_id(n, config) for n in numbers]


def _git(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True, encoding="utf-8").strip()


def previous_tag(tag: str) -> str:
    tags = [t for t in _git("tag", "--list", f"{TAG_PREFIX}*", "--sort=-creatordate").splitlines() if t and t != tag]
    for candidate in tags:
        if subprocess.run(["git", "merge-base", "--is-ancestor", candidate, tag],
                          capture_output=True).returncode == 0:
            return candidate
    return ""


def merged_pull_requests(since: str, tag: str) -> list[int]:
    span = f"{since}..{tag}" if since else tag
    subjects = _git("log", "--merges", "--format=%s", span).splitlines()
    return sorted({int(m.group(1)) for s in subjects if (m := _MERGE.search(s))})


def _pull_request(repo: str, number: int) -> dict:
    raw = subprocess.check_output(["gh", "pr", "view", str(number), "-R", repo, "--json", "number,title,body"],
                                  text=True, encoding="utf-8")
    return json.loads(raw)


def fixed_issues(pulls: list[dict], config: dict | None = None) -> list[dict]:
    fixed: dict[str, dict] = {}
    for pr in pulls:
        for key in tracker_issues(pr.get("body", ""), config):
            entry = fixed.setdefault(key, {"issue": key, "pull_requests": [], "notes": []})
            entry["pull_requests"].append(pr["number"])
            if pr.get("title") and pr["title"] not in entry["notes"]:
                entry["notes"].append(pr["title"])
    return sorted(fixed.values(), key=lambda e: e["issue"])


def build(*, tag: str, installer: Path, repo: str, commit: str, previous: str, pulls: list[dict],
          config: dict | None = None) -> dict:
    return {
        "schema": 1,
        "version": version_of(tag),
        "tag": tag,
        "commit": commit,
        "previous_tag": previous,
        "published_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "installer": {
            "name": installer.name,
            "size": installer.stat().st_size,
            "sha256": sha256_of(installer),
            "url": f"https://github.com/{repo}/releases/download/{tag}/{installer.name}",
        },
        "fixed": fixed_issues(pulls, config),
    }


def notes(release: dict) -> str:
    lines = [f"Adopy Campaign Manager {release['version']}", "",
             f"Installer: `{release['installer']['name']}`  ",
             f"SHA-256: `{release['installer']['sha256']}`", ""]
    if release["fixed"]:
        lines.append("Fixed in this release:")
        lines += [f"- {e['issue']}: {'; '.join(e['notes'])}" for e in release["fixed"]]
    else:
        lines.append("No tracker issues are linked to the pull requests in this release.")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tag", required=True)
    ap.add_argument("--installer", required=True, type=Path)
    ap.add_argument("--repo", required=True, help="owner/name where the release is published")
    ap.add_argument("--source-repo", default="", help="owner/name of the pull requests (default: --repo)")
    ap.add_argument("--out", type=Path, default=None, help="output directory (default: next to the installer)")
    args = ap.parse_args(argv)

    installer = args.installer.resolve()
    if not installer.is_file():
        print(f"Installer not found: {installer}", file=sys.stderr)
        return 2
    out = (args.out or installer.parent).resolve()
    out.mkdir(parents=True, exist_ok=True)
    previous = previous_tag(args.tag)
    source = args.source_repo or args.repo
    pulls = [_pull_request(source, n) for n in merged_pull_requests(previous, args.tag)]
    release = build(tag=args.tag, installer=installer, repo=args.repo,
                    commit=_git("rev-list", "-n", "1", args.tag), previous=previous, pulls=pulls)
    (out / f"{installer.name}.sha256").write_text(f"{release['installer']['sha256']}  {installer.name}\n", encoding="utf-8")
    (out / "release.json").write_text(json.dumps(release, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (out / "release-notes.md").write_text(notes(release), encoding="utf-8")
    print(f"{release['version']}: {release['installer']['sha256']} · fixed {len(release['fixed'])} issue(s) "
          f"since {previous or 'the beginning'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
